"""
Testes da montagem da matriz gravada no Google Sheets.

O foco é `limpar_ausentes`, que decide o destino das marcações em linhas que a
fonte de dados não cobre. É a opção mais perigosa do módulo: ligada na hora
errada apaga presença legítima; desligada na hora errada deixa presença errada
para sempre na planilha compartilhada.

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import google_sheets as gs

DIAS = ["Segunda", "Terça"]


def _grade(linhas_alunos, dias=DIAS, periodo="12/05 a 16/05"):
    """
    Monta o `valores` cru de uma aba no layout horizontal.

    linhas_alunos: [(nome, matricula, presencas, {dia: marca})]
    """
    cabecalho_periodo = ([""] * gs._COL_FIXAS + [gs._PREFIXO_PERIODO + periodo]
                          + [""] * len(dias))
    cabecalho_colunas = list(gs._CABECALHO_FIXO) + ["Presenças"] + list(dias)

    grade = [cabecalho_periodo, cabecalho_colunas]
    for i, (nome, mat, presencas, marcas) in enumerate(linhas_alunos, start=1):
        grade.append([str(i), nome, mat] + [""] * (gs._COL_FIXAS - 3) + [str(presencas)] +
                     [marcas.get(d, "") for d in dias])
    return grade


def _dados(por_linha):
    """por_linha: {linha_1indexed: {dia: marca}}"""
    return {
        linha: {"marcas": dict(marcas),
                "presencas": sum(1 for v in marcas.values() if v)}
        for linha, marcas in por_linha.items()
    }


ROSTER_COM_FANTASMA = [
    ("Ana Souza", "111", 1, {"Segunda": "AJ"}),
    ("Bruno Lima", "222", 0, {}),
    ("Aluno 3", "", 1, {"Segunda": "A"}),      # linha fantasma do lançamento errado
]


class TestLimparAusentes:

    def _montar(self, limpar):
        valores = _grade(ROSTER_COM_FANTASMA)
        grupo = gs._ler_grupos(valores)[0]
        # A releitura correta cobre só as duas pessoas reais (linhas 3 e 4).
        dados = _dados({3: {"Segunda": ""}, 4: {"Segunda": "AJ"}})
        return gs._montar_matriz_grupo(valores, grupo, DIAS, dados, 5, limpar)

    def test_ligado_apaga_a_marca_da_linha_fantasma(self):
        """
        Sem isso, as marcações que o lançamento errado deixou em linhas
        "Aluno N" sobreviveriam à reconciliação: essas linhas não existem no
        roster correto, então nunca são sobrescritas.
        """
        ordem, matriz = self._montar(limpar=True)
        idx_segunda = 1 + ordem.index("Segunda")

        assert matriz[2][idx_segunda] == ""   # linha 5 = "Aluno 3"
        assert matriz[2][0] == 0              # Presenças recalculado

    def test_desligado_preserva(self):
        """
        Padrão do fluxo semanal: um scan parcial não pode zerar as linhas que
        ele simplesmente não leu.
        """
        ordem, matriz = self._montar(limpar=False)
        idx_segunda = 1 + ordem.index("Segunda")

        assert matriz[2][idx_segunda] == "A"

    def test_linhas_cobertas_sao_sobrescritas_nos_dois_modos(self):
        for limpar in (True, False):
            ordem, matriz = self._montar(limpar)
            idx_segunda = 1 + ordem.index("Segunda")

            assert matriz[0][idx_segunda] == ""    # Ana perdeu a marca (correto)
            assert matriz[1][idx_segunda] == "AJ"  # Bruno ganhou a dele

    def test_nao_toca_em_dias_fora_da_escrita(self):
        """
        O FDS grava só o Sábado no grupo da semana. Mesmo limpando ausentes, os
        dias úteis já gravados têm que sobreviver intactos.
        """
        dias_todos = ["Segunda", "Sábado"]
        valores = _grade(
            [("Ana Souza", "111", 2, {"Segunda": "AJ", "Sábado": "A"})],
            dias=dias_todos,
        )
        grupo = gs._ler_grupos(valores)[0]

        ordem, matriz = gs._montar_matriz_grupo(
            valores, grupo, ["Sábado"], _dados({}), 3, True
        )

        assert matriz[0][1 + ordem.index("Segunda")] == "AJ"
        assert matriz[0][1 + ordem.index("Sábado")] == ""

    def test_presencas_recalculado_a_partir_das_marcas(self):
        valores = _grade([("Ana Souza", "111", 0, {})])
        grupo = gs._ler_grupos(valores)[0]

        ordem, matriz = gs._montar_matriz_grupo(
            valores, grupo, DIAS,
            _dados({3: {"Segunda": "AJ", "Terça": "J"}}), 3, True,
        )
        assert matriz[0][0] == 2


class TestLerGrupos:

    def test_le_periodo_e_colunas_de_dia(self):
        grupo = gs._ler_grupos(_grade(ROSTER_COM_FANTASMA))[0]

        assert grupo["periodo"] == "12/05 a 16/05"
        assert grupo["dias"] == DIAS
        assert grupo["col"] == gs._COL_FIXAS
        assert grupo["largura"] == 3

    def test_grade_vazia_nao_quebra(self):
        assert gs._ler_grupos([]) == []


class TestColunasFixasExtras:

    def test_cabecalho_inclui_presencas_mes_e_justificativa(self):
        assert gs._CABECALHO_FIXO == [
            "Nº", "Nome", "Matrícula", "Presenças no Mês", "Justificou Ausência?",
            "Justificativa", "Quantidade de dias justificados",
        ]
        assert gs._COL_FIXAS == len(gs._CABECALHO_FIXO)

    def test_formula_presencas_mes_ancora_na_ultima_coluna_fixa(self):
        # Separador ";" — a planilha é pt_BR, "," dá "Formula parse error."
        assert gs._formula_presencas_mes(7) == (
            '=SUMIF(G$2:$2;"Presenças";G7:7)'
        )

    def test_frequencia_10_conta_e_9_nao(self):
        # O limite é 10 em todas as unidades: 9 presenças não é frequente
        assert gs._formula_frequencia(5) == (
            '=IF(D5<10;"Não Frequente";"Frequente")'
        )
        assert gs._LIMITE_FREQUENCIA == 10

    def test_justificativa_usa_dias_justificados_da_coluna_g(self):
        assert gs._formula_justificativa(5) == (
            '=IF(D5>=10;"Frequente";'
            'IF(D5+G5>=10;"Justificado";"Não justificado"))'
        )

    def test_colunas_fixas_atuais_acha_o_primeiro_grupo(self):
        assert gs._colunas_fixas_atuais(_grade([("A", "1", 0, {})])) == gs._COL_FIXAS
        # Aba antiga de 4 colunas fixas
        antiga = [
            ["", "", "", "", "Período: X", ""],
            ["Nº", "Nome", "Matrícula", "Presenças no Mês", "Presenças", "Segunda"],
        ]
        assert gs._colunas_fixas_atuais(antiga) == 4
        assert gs._colunas_fixas_atuais([["Nº", "Nome"], ["a"]]) is None


class TestReordenarGruposPreservaFormula:

    def test_presencas_no_mes_continua_formula_apos_reordenar(self):
        # Períodos fora de ordem cronológica: força a permuta de blocos
        f = gs._COL_FIXAS
        grade = [
            [""] * f + [gs._PREFIXO_PERIODO + "19/05 a 23/05", "", "",
                        gs._PREFIXO_PERIODO + "12/05 a 16/05", "", ""],
            list(gs._CABECALHO_FIXO) + ["Presenças", "Segunda", "Terça"] * 2,
            ["1", "Fulana", "111", "3"] + [""] * (f - 4)
            + ["1", "X", "", "2", "X", "X"],
            ["2", "Ciclana", "222", "0"] + [""] * (f - 4)
            + ["0", "", "", "0", "", ""],
        ]
        grupos = gs._ler_grupos(grade)

        captura = {}

        class Aba:
            id = 1

            def update(self, values, range_name, value_input_option):
                captura["values"] = values

        class Planilha:
            def batch_update(self, body):
                pass

        gs._reordenar_grupos(Planilha(), Aba(), grade, grupos, linha_fim=4)

        d = gs._COL_PRESENCAS_MES
        assert captura["values"][2][d] == gs._formula_presencas_mes(3)
        assert captura["values"][3][d] == gs._formula_presencas_mes(4)
        assert captura["values"][2][gs._COL_FREQUENCIA] == gs._formula_frequencia(3)
        assert captura["values"][2][gs._COL_JUSTIFICATIVA] == gs._formula_justificativa(3)
        # Cabeçalhos não são tocados
        assert captura["values"][1][d] == "Presenças no Mês"


class TestRepararPresencasMes:

    def test_detecta_so_as_linhas_sem_a_formula_esperada(self):
        d = gs._COL_PRESENCAS_MES
        cab = [""] * 6
        linha_ok = [""] * 6
        linha_ok[d] = gs._formula_presencas_mes(3)
        linha_fixa = [""] * 6
        linha_fixa[d] = "7"
        linha_vazia = [""] * 6
        formulas = [cab, cab, linha_ok, linha_fixa, linha_vazia]

        assert gs._linhas_presencas_mes_a_reparar(formulas, 5) == [4, 5]

    def test_tudo_certo_nao_repara_nada(self):
        d = gs._COL_PRESENCAS_MES
        linha = [""] * 6
        linha[d] = gs._formula_presencas_mes(3)

        assert gs._linhas_presencas_mes_a_reparar([[], [], linha], 3) == []


class TestDetectarMatriculasDuplicadas:

    def test_mesma_matricula_nomes_diferentes_e_reportada(self):
        alunos = [("Fulana Silva", "111"), ("Fulana Silva Batista", "111")]

        achadas = gs._detectar_matriculas_duplicadas(alunos)

        assert achadas == [{
            "matricula": "111",
            "nomes": ["Fulana Silva", "Fulana Silva Batista"],
        }]

    def test_mesma_matricula_mesmo_nome_nao_e_reportada(self):
        alunos = [("Fulana Silva", "111"), ("fulana  silva", "111")]

        assert gs._detectar_matriculas_duplicadas(alunos) == []

    def test_matricula_vazia_e_ignorada(self):
        alunos = [("Fulana Silva", ""), ("Ciclana Souza", "")]

        assert gs._detectar_matriculas_duplicadas(alunos) == []

    def test_sem_duplicata_nao_reporta_nada(self):
        alunos = [("Fulana Silva", "111"), ("Ciclana Souza", "222")]

        assert gs._detectar_matriculas_duplicadas(alunos) == []
