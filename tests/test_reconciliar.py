"""
Testes da reconciliação de períodos já lançados.

O cenário que importa: a folha foi impressa com um roster, o processamento
antigo usou uma planilha em que alguém tinha saído do topo, e todo mundo abaixo
recebeu a presença do vizinho. Estes testes verificam que a comparação encontra
exatamente essas linhas — casando por matrícula, nunca por posição.

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lote as lote_mod
import reconciliar

DIAS = ["Segunda", "Terça"]

PESSOAS = [
    ("Ana Souza", "111111111"),
    ("Bruno Lima", "222222222"),
    ("Carla Dias", "333333333"),
    ("Diego Reis", "444444444"),
]


def _lote():
    paginacao = [
        {"numero": i + 1, "pagina": 1, "linha_pagina": i + 1,
         "nome": nome, "nome_impresso": nome, "matricula": mat}
        for i, (nome, mat) in enumerate(PESSOAS)
    ]
    return lote_mod.montar_lote(
        lote_id="canela_20260512-090000",
        restaurante_key="canela",
        restaurante_nome="Canela",
        aba="CANELA IMPRESSÃO",
        dias=DIAS,
        paginacao=paginacao,
        config={"scan": {"dpi": 200},
                "layout": {"alunos_por_pagina": 4, "dias": DIAS}},
        info={"datas": "12/05 a 16/05"},
    )


def _contagem(marcas_por_numero):
    """marcas_por_numero: {numero: {dia: "AJ"|"A"|"J"|""}}"""
    saida = []
    for numero, marcas in sorted(marcas_por_numero.items()):
        detalhes, total = {}, 0
        for dia in DIAS:
            valor = marcas.get(dia, "")
            almoco, janta = "A" in valor, "J" in valor
            presente = almoco or janta
            total += presente
            detalhes[dia] = {"presente": presente, "almoco": almoco, "janta": janta}
        saida.append({"numero": numero, "presencas": total, "detalhes": detalhes})
    return saida


def _sheets(linhas):
    """linhas: [(nome, matricula, {dia: marca})] na ordem gravada."""
    return {
        "ok": True,
        "aba": "Maio 2026",
        "periodo": "12/05 a 16/05",
        "dias": DIAS,
        "linhas": [
            {"linha": i + 3, "nome": nome, "matricula": mat,
             "presencas": str(sum(1 for v in marcas.values() if v)),
             "marcas": dict(marcas)}
            for i, (nome, mat, marcas) in enumerate(linhas)
        ],
    }


@pytest.fixture
def sheets_mock(monkeypatch):
    """Substitui a leitura do Google Sheets por um retorno controlado."""
    estado = {}

    def _fake(restaurante_key, periodo, nome_aba=None):
        return estado["retorno"]

    monkeypatch.setattr(reconciliar.gs, "ler_periodo", _fake)
    return estado


# ---------------------------------------------------------------------------
# comparar_com_sheets
# ---------------------------------------------------------------------------

class TestCompararComSheets:

    def test_lancamento_correto_nao_gera_divergencia(self, sheets_mock):
        marcas = {
            1: {"Segunda": "AJ"},
            2: {"Terça": "A"},
            3: {},
            4: {"Segunda": "J", "Terça": "AJ"},
        }
        sheets_mock["retorno"] = _sheets([
            ("Ana Souza", "111111111", {"Segunda": "AJ", "Terça": ""}),
            ("Bruno Lima", "222222222", {"Segunda": "", "Terça": "A"}),
            ("Carla Dias", "333333333", {"Segunda": "", "Terça": ""}),
            ("Diego Reis", "444444444", {"Segunda": "J", "Terça": "AJ"}),
        ])

        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )
        assert resultado["ok"]
        assert resultado["divergentes"] == []
        assert resultado["iguais"] == 4

    def test_deslocamento_de_uma_linha_aparece_inteiro(self, sheets_mock):
        """
        Ana saiu da planilha usada no processamento: cada presença desceu uma
        pessoa. As três linhas afetadas têm que ser apontadas.
        """
        marcas = {
            1: {"Segunda": "AJ"},
            2: {"Terça": "A"},
            3: {},
            4: {"Segunda": "J"},
        }
        # Como estava no Sheets: Bruno recebeu o de Ana, Carla o de Bruno, etc.
        sheets_mock["retorno"] = _sheets([
            ("Ana Souza", "111111111", {"Segunda": "", "Terça": ""}),
            ("Bruno Lima", "222222222", {"Segunda": "AJ", "Terça": ""}),
            ("Carla Dias", "333333333", {"Segunda": "", "Terça": "A"}),
            ("Diego Reis", "444444444", {"Segunda": "", "Terça": ""}),
        ])

        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )

        por_nome = {d["nome"]: d["diferencas"] for d in resultado["divergentes"]}
        assert set(por_nome) == {"Ana Souza", "Bruno Lima", "Carla Dias", "Diego Reis"}
        assert por_nome["Ana Souza"]["Segunda"] == ("", "AJ")
        assert por_nome["Bruno Lima"]["Segunda"] == ("AJ", "")
        assert por_nome["Bruno Lima"]["Terça"] == ("", "A")
        assert por_nome["Diego Reis"]["Segunda"] == ("", "J")

    def test_casa_por_matricula_e_nao_por_posicao(self, sheets_mock):
        """
        A ordem das linhas no Sheets é irrelevante: quem manda é a matrícula.
        Usar posição aqui reintroduziria o próprio bug que se quer detectar.
        """
        marcas = {1: {"Segunda": "AJ"}, 2: {}, 3: {}, 4: {}}
        sheets_mock["retorno"] = _sheets([
            ("Diego Reis", "444444444", {"Segunda": "", "Terça": ""}),
            ("Carla Dias", "333333333", {"Segunda": "", "Terça": ""}),
            ("Bruno Lima", "222222222", {"Segunda": "", "Terça": ""}),
            ("Ana Souza", "111111111", {"Segunda": "AJ", "Terça": ""}),
        ])

        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )
        assert resultado["divergentes"] == []

    def test_sem_matricula_casa_por_nome(self, sheets_mock):
        marcas = {1: {"Segunda": "A"}, 2: {}, 3: {}, 4: {}}
        lote = _lote()
        lote["alunos"][0]["matricula"] = ""

        sheets_mock["retorno"] = _sheets([
            ("ANA  SOUZA", "", {"Segunda": "A", "Terça": ""}),
            ("Bruno Lima", "222222222", {"Segunda": "", "Terça": ""}),
            ("Carla Dias", "333333333", {"Segunda": "", "Terça": ""}),
            ("Diego Reis", "444444444", {"Segunda": "", "Terça": ""}),
        ])

        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )
        assert resultado["divergentes"] == []
        assert resultado["iguais"] == 4

    def test_pessoa_da_folha_ausente_do_sheets_e_reportada(self, sheets_mock):
        marcas = {1: {}, 2: {}, 3: {}, 4: {"Segunda": "AJ"}}
        sheets_mock["retorno"] = _sheets([
            ("Ana Souza", "111111111", {"Segunda": "", "Terça": ""}),
            ("Bruno Lima", "222222222", {"Segunda": "", "Terça": ""}),
            ("Carla Dias", "333333333", {"Segunda": "", "Terça": ""}),
        ])

        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )
        assert [n["nome"] for n in resultado["nao_encontrados"]] == ["Diego Reis"]

    def test_ausencia_sem_marcacao_nao_vira_alarme(self, sheets_mock):
        """Quem não tem presença nenhuma e não está no Sheets não é problema."""
        marcas = {1: {}, 2: {}, 3: {}, 4: {}}
        sheets_mock["retorno"] = _sheets([
            ("Ana Souza", "111111111", {"Segunda": "", "Terça": ""}),
        ])

        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem(marcas), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "12/05 a 16/05",
        )
        assert resultado["nao_encontrados"] == []
        assert resultado["divergentes"] == []

    def test_periodo_inexistente_devolve_erro(self, sheets_mock):
        sheets_mock["retorno"] = {"ok": False, "erro": "Período não encontrado."}
        lote = _lote()
        resultado = reconciliar.comparar_com_sheets(
            lote, _contagem({1: {}}), lote_mod.roster_do_lote(lote),
            DIAS, "canela", "99/99 a 99/99",
        )
        assert resultado["ok"] is False
        assert "não encontrado" in resultado["erro"]


# ---------------------------------------------------------------------------
# releitura_correta — linhas além do fim do lote
# ---------------------------------------------------------------------------

class TestReleituraForaDoLote:
    """
    A última página sempre lê `alunos_por_pagina` posições de bolha, mesmo
    quando a página real impressa tem menos gente. Sem tratamento, esse
    padding vira uma linha nova no Sheets com o nome literal
    "[linha N fora do lote]" — o mesmo mecanismo que gera os fantasmas
    "Aluno N" que a triagem encontra. Achado revisando o maço real de Ondina
    29/06 a 04/07 antes de aplicar a correção.
    """

    @staticmethod
    def _mockar_releitura(monkeypatch, resultados_por_numero, ordem_confiavel=True):
        """
        resultados_por_numero: {numero: {dia: (almoco, janta)}} — omitido
        vira ausente em todos os dias.
        """
        import exportar

        def _resultados(paginas, config, pagina_inicial=1, diagnostico=None):
            saida = []
            for numero, marcas in sorted(resultados_por_numero.items()):
                dias_dict = {}
                for dia in DIAS:
                    a, j = marcas.get(dia, (False, False))
                    dias_dict[dia] = {
                        "almoco": a, "janta": j,
                        "almoco_pct": 1.0 if a else 0.0,
                        "janta_pct": 1.0 if j else 0.0,
                    }
                saida.append({"numero": numero, "dias": dias_dict})
            if diagnostico is not None:
                diagnostico["ordem_confiavel"] = ordem_confiavel
                diagnostico["motivo_ordem"] = "" if ordem_confiavel else "números repetidos"
            return saida

        monkeypatch.setattr(exportar, "carregar_todas_paginas", lambda *a, **k: ["img"])
        monkeypatch.setattr(exportar, "processar_pdf_completo", _resultados)

    def test_padding_sem_marca_e_apenas_descontado(self, monkeypatch):
        """Padding em branco (o caso comum) não gera aviso nenhum de marca."""
        lote = _lote()  # 4 pessoas
        self._mockar_releitura(monkeypatch, {
            1: {"Segunda": (True, False)}, 2: {}, 3: {}, 4: {},
            5: {}, 6: {},  # além do lote, sem marca — padding normal
        })

        contagem, roster, dias, resumo = reconciliar.releitura_correta(lote, ["scan.pdf"])

        assert [c["numero"] for c in contagem] == [1, 2, 3, 4]
        assert len(roster) == 4
        assert resumo["fora_do_lote"] == 2
        assert resumo["excedentes_com_marca"] == []

    def test_padding_com_marca_e_descartado_e_reportado(self, monkeypatch):
        """
        O caso perigoso: ruído na borda da página em branco leu como presença.
        Tem que sumir do que vai para o Sheets, mas não pode desaparecer do
        relatório — é sinal de algo errado que o operador precisa ver.
        """
        lote = _lote()
        self._mockar_releitura(monkeypatch, {
            1: {}, 2: {}, 3: {}, 4: {},
            5: {"Segunda": (True, True)},  # fantasma com marca
        })

        contagem, roster, dias, resumo = reconciliar.releitura_correta(lote, ["scan.pdf"])

        assert 5 not in [c["numero"] for c in contagem]
        assert len(resumo["excedentes_com_marca"]) == 1
        assert resumo["excedentes_com_marca"][0]["numero"] == 5

    def test_roster_nunca_maior_que_o_lote(self, monkeypatch):
        """
        `comparar_com_sheets` e `exportar_para_sheets` indexam por
        `numero - 1` neste roster — do tamanho errado, uma marca no padding
        acabaria caindo em outra pessoa, ou o roster criaria a linha fantasma
        que este teste inteiro existe para evitar.
        """
        lote = _lote()
        self._mockar_releitura(monkeypatch, {1: {}, 2: {}, 3: {}, 4: {}, 9: {"Segunda": (True, False)}})

        _, roster, _, _ = reconciliar.releitura_correta(lote, ["scan.pdf"])
        assert len(roster) == lote["total_alunos"]
        assert all(not nome.startswith("[linha") for nome, _ in roster)

    def test_resumo_carrega_a_confiabilidade_da_ordem(self, monkeypatch):
        """
        Achado real: um maço com arquivos sobrepostos (redigitalização que
        recobriu páginas já lidas) fez `_inferir_numeros_paginas` desistir e
        cair para a ordem bruta do arquivo — e a aplicação seguiu em frente e
        gravou no Sheets como se a ordem estivesse certa. `resumo` precisa
        carregar esse sinal para quem grava poder recusar.
        """
        lote = _lote()
        self._mockar_releitura(monkeypatch, {1: {}, 2: {}, 3: {}, 4: {}},
                               ordem_confiavel=False)

        _, _, _, resumo = reconciliar.releitura_correta(lote, ["scan.pdf"])
        assert resumo["ordem_confiavel"] is False
        assert resumo["motivo_ordem"]

    def test_ordem_confiavel_por_padrao_quando_reconstrucao_funciona(self, monkeypatch):
        lote = _lote()
        self._mockar_releitura(monkeypatch, {1: {}, 2: {}, 3: {}, 4: {}})

        _, _, _, resumo = reconciliar.releitura_correta(lote, ["scan.pdf"])
        assert resumo["ordem_confiavel"] is True


# ---------------------------------------------------------------------------
# Triagem
# ---------------------------------------------------------------------------

class TestTriagem:

    @staticmethod
    def _monta(monkeypatch, diag):
        monkeypatch.setattr(reconciliar.gs, "listar_abas_mes", lambda k: ["Maio 2026"])
        monkeypatch.setattr(reconciliar.gs, "diagnosticar_aba", lambda k, a: diag)

    def test_linha_fantasma_e_prova(self, monkeypatch):
        self._monta(monkeypatch, {
            "ok": True, "aba": "Maio 2026", "total_linhas": 30,
            "fantasmas": [{"linha": 32, "nome": "Aluno 30", "mat": ""}],
            "duplicadas": [],
            "periodos": [{"periodo": "05/05 a 09/05", "dias": DIAS,
                          "com_marcacao": 25, "ultima_linha": 32,
                          "fantasmas_marcados": 1}],
        })
        info = reconciliar.triar("canela")[0]
        assert len(info["provas"]) == 2   # a linha fantasma e a marcação nela
        assert info["indicios"] == []

    def test_matricula_repetida_com_nomes_diferentes_e_prova(self, monkeypatch):
        self._monta(monkeypatch, {
            "ok": True, "aba": "Maio 2026", "total_linhas": 30,
            "fantasmas": [], "duplicadas": [
                {"matricula": "111", "linhas": [5, 9],
                 "nomes": ["Ana Souza", "Bruno Lima"]}],
            "periodos": [{"periodo": "05/05 a 09/05", "dias": DIAS,
                          "com_marcacao": 25, "ultima_linha": 30,
                          "fantasmas_marcados": 0}],
        })
        info = reconciliar.triar("canela")[0]
        assert len(info["provas"]) == 1
        assert "DUAS PESSOAS" in info["provas"][0]
        assert info["indicios"] == []

    def test_matricula_repetida_com_mesmo_nome_e_so_indicio(self, monkeypatch):
        """
        Mesma pessoa digitada duas vezes desperdiça uma linha impressa, mas
        ninguém fica sem frequência — tratar como prova geraria retrabalho.
        """
        self._monta(monkeypatch, {
            "ok": True, "aba": "Maio 2026", "total_linhas": 30,
            "fantasmas": [], "duplicadas": [
                {"matricula": "111", "linhas": [5, 6],
                 "nomes": ["Ana Souza", "ANA SOUZA"]}],
            "periodos": [{"periodo": "05/05 a 09/05", "dias": DIAS,
                          "com_marcacao": 25, "ultima_linha": 30,
                          "fantasmas_marcados": 0}],
        })
        info = reconciliar.triar("canela")[0]
        assert info["provas"] == []
        assert len(info["indicios"]) == 1

    def test_ultima_linha_desigual_e_apenas_indicio(self, monkeypatch):
        self._monta(monkeypatch, {
            "ok": True, "aba": "Maio 2026", "total_linhas": 40,
            "fantasmas": [], "duplicadas": [],
            "periodos": [
                {"periodo": "05/05", "dias": DIAS, "com_marcacao": 25,
                 "ultima_linha": 30, "fantasmas_marcados": 0},
                {"periodo": "12/05", "dias": DIAS, "com_marcacao": 25,
                 "ultima_linha": 42, "fantasmas_marcados": 0},
            ],
        })
        info = reconciliar.triar("canela")[0]
        assert info["provas"] == []
        assert len(info["indicios"]) == 1

    def test_variacao_pequena_nao_alarma(self, monkeypatch):
        self._monta(monkeypatch, {
            "ok": True, "aba": "Maio 2026", "total_linhas": 40,
            "fantasmas": [], "duplicadas": [],
            "periodos": [
                {"periodo": "05/05", "dias": DIAS, "com_marcacao": 25,
                 "ultima_linha": 40, "fantasmas_marcados": 0},
                {"periodo": "12/05", "dias": DIAS, "com_marcacao": 24,
                 "ultima_linha": 39, "fantasmas_marcados": 0},
            ],
        })
        info = reconciliar.triar("canela")[0]
        assert info["provas"] == []
        assert info["indicios"] == []
