"""
Testes unitários para as funções de lógica pura do pipeline.

Cobertura:
    - contar_presencas
    - aplicar_correcoes
    - exportar_xlsx  (inclui regressão do bug de pagina_inicio > 1)
    - ler_nomes_alunos
    - eh_pagina_branca

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import numpy as np
import pytest
from openpyxl import Workbook, load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exportar import (
    aplicar_correcoes,
    contar_presencas,
    eh_pagina_branca,
    exportar_xlsx,
    ler_nomes_alunos,
)

DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aluno(numero, **presencas_por_dia):
    """
    Cria dict de aluno com os dias de DIAS.
    presencas_por_dia: dia=(almoco, janta)  — omitido → (False, False)
    """
    dias = {}
    for dia in DIAS:
        almoco, janta = presencas_por_dia.get(dia, (False, False))
        dias[dia] = {
            "almoco": almoco,
            "janta": janta,
            "almoco_pct": 0.75 if almoco else 0.05,
            "janta_pct": 0.75 if janta else 0.05,
        }
    return {"numero": numero, "dias": dias}


def _xlsx_alunos(tmp_path, nomes, aba="CANELA IMPRESSÃO"):
    """Cria xlsx no formato esperado por ler_nomes_alunos (dados a partir da linha 8)."""
    caminho = str(tmp_path / "alunos.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = aba
    for i, (nome, matricula) in enumerate(nomes, start=8):
        ws.cell(i, 2, nome)
        ws.cell(i, 3, matricula)
    wb.save(caminho)
    return caminho


# ---------------------------------------------------------------------------
# contar_presencas
# ---------------------------------------------------------------------------

class TestContarPresencas:
    def test_sem_nenhuma_presenca(self):
        r = contar_presencas([_aluno(1)], DIAS)
        assert r[0]["presencas"] == 0

    def test_almoco_conta_como_presenca(self):
        r = contar_presencas([_aluno(1, Segunda=(True, False))], DIAS)
        assert r[0]["presencas"] == 1
        assert r[0]["detalhes"]["Segunda"]["presente"] is True
        assert r[0]["detalhes"]["Segunda"]["almoco"] is True
        assert r[0]["detalhes"]["Segunda"]["janta"] is False

    def test_janta_conta_como_presenca(self):
        r = contar_presencas([_aluno(1, Segunda=(False, True))], DIAS)
        assert r[0]["presencas"] == 1

    def test_almoco_e_janta_contam_um_unico_dia(self):
        r = contar_presencas([_aluno(1, Segunda=(True, True))], DIAS)
        assert r[0]["presencas"] == 1

    def test_multiplos_dias_somam(self):
        aluno = _aluno(1, Segunda=(True, False), Terça=(False, True), Quinta=(True, True))
        r = contar_presencas([aluno], DIAS)
        assert r[0]["presencas"] == 3

    def test_semana_cheia(self):
        aluno = _aluno(1, **{d: (True, False) for d in DIAS})
        r = contar_presencas([aluno], DIAS)
        assert r[0]["presencas"] == 5

    def test_numero_do_aluno_preservado(self):
        r = contar_presencas([_aluno(42, Segunda=(True, False))], DIAS)
        assert r[0]["numero"] == 42

    def test_multiplos_alunos_independentes(self):
        alunos = [
            _aluno(1, Segunda=(True, False)),
            _aluno(2),
            _aluno(3, Segunda=(True, False), Terça=(True, False)),
        ]
        r = contar_presencas(alunos, DIAS)
        assert r[0]["presencas"] == 1
        assert r[1]["presencas"] == 0
        assert r[2]["presencas"] == 2

    def test_lista_vazia(self):
        assert contar_presencas([], DIAS) == []


# ---------------------------------------------------------------------------
# aplicar_correcoes
# ---------------------------------------------------------------------------

class TestAplicarCorrecoes:
    def test_vazio_vira_marcado(self):
        aluno = _aluno(1)
        aplicar_correcoes([aluno], {"1_Segunda_almoco": True})
        assert aluno["dias"]["Segunda"]["almoco"] is True

    def test_marcado_vira_vazio(self):
        aluno = _aluno(1, Segunda=(True, False))
        aplicar_correcoes([aluno], {"1_Segunda_almoco": False})
        assert aluno["dias"]["Segunda"]["almoco"] is False

    def test_chave_de_outro_aluno_nao_afeta(self):
        aluno = _aluno(1)
        aplicar_correcoes([aluno], {"99_Segunda_almoco": True})
        assert aluno["dias"]["Segunda"]["almoco"] is False

    def test_multiplas_correcoes_aplicadas(self):
        aluno = _aluno(1)
        aplicar_correcoes([aluno], {
            "1_Segunda_almoco": True,
            "1_Terça_janta": True,
            "1_Quarta_almoco": True,
        })
        assert aluno["dias"]["Segunda"]["almoco"] is True
        assert aluno["dias"]["Terça"]["janta"] is True
        assert aluno["dias"]["Quarta"]["almoco"] is True
        assert aluno["dias"]["Quinta"]["almoco"] is False  # não alterado

    def test_sem_correcoes_nao_altera_nada(self):
        aluno = _aluno(1, Segunda=(True, False))
        aplicar_correcoes([aluno], {})
        assert aluno["dias"]["Segunda"]["almoco"] is True

    def test_retorna_a_propria_lista(self):
        alunos = [_aluno(1)]
        retorno = aplicar_correcoes(alunos, {})
        assert retorno is alunos

    def test_correcao_de_janta(self):
        aluno = _aluno(1)
        aplicar_correcoes([aluno], {"1_Sexta_janta": True})
        assert aluno["dias"]["Sexta"]["janta"] is True

    def test_correcao_afeta_contagem_posterior(self):
        aluno = _aluno(1)
        aplicar_correcoes([aluno], {"1_Segunda_almoco": True, "1_Terça_almoco": True})
        r = contar_presencas([aluno], DIAS)
        assert r[0]["presencas"] == 2


# ---------------------------------------------------------------------------
# exportar_xlsx
# ---------------------------------------------------------------------------

class TestExportarXlsx:
    def _contagem_um_aluno(self, numero=1, **presencas):
        return contar_presencas([_aluno(numero, **presencas)], DIAS)

    def test_arquivo_criado(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        exportar_xlsx(self._contagem_um_aluno(), [("João", "1")], DIAS, saida)
        assert os.path.exists(saida)

    def test_cabecalho_tem_colunas_obrigatorias(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        exportar_xlsx(self._contagem_um_aluno(), [("João", "1")], DIAS, saida)
        ws = load_workbook(saida).active
        header = [ws.cell(1, c).value for c in range(1, len(DIAS) + 5)]
        assert header[0] == "Nº"
        assert header[1] == "Nome"
        assert header[2] == "Matrícula"
        assert header[3] == "Presenças"
        for dia in DIAS:
            assert dia in header

    def test_nome_e_matricula_corretos(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        exportar_xlsx(self._contagem_um_aluno(), [("Maria Souza", "456")], DIAS, saida)
        ws = load_workbook(saida).active
        assert ws.cell(2, 2).value == "Maria Souza"
        assert ws.cell(2, 3).value == "456"

    def test_contagem_presencas_correta(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        contagem = self._contagem_um_aluno(Segunda=(True, False), Terça=(False, True))
        exportar_xlsx(contagem, [("João", "1")], DIAS, saida)
        ws = load_workbook(saida).active
        assert ws.cell(2, 4).value == 2

    def test_dia_marcado_exibe_a_ou_j(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        contagem = self._contagem_um_aluno(Segunda=(True, False), Terça=(False, True))
        exportar_xlsx(contagem, [("João", "1")], DIAS, saida)
        ws = load_workbook(saida).active
        segunda_col = 5  # coluna E = primeiro dia
        assert ws.cell(2, segunda_col).value == "A"
        assert ws.cell(2, segunda_col + 1).value == "J"

    def test_dia_vazio_nao_exibe_nada(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        exportar_xlsx(self._contagem_um_aluno(), [("João", "1")], DIAS, saida)
        ws = load_workbook(saida).active
        for c in range(5, 5 + len(DIAS)):
            assert ws.cell(2, c).value in (None, "")

    def test_multiplos_alunos_geram_linhas_corretas(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        alunos_data = [_aluno(i + 1) for i in range(3)]
        contagem = contar_presencas(alunos_data, DIAS)
        nomes = [(f"Nome {i+1}", str(i+1)) for i in range(3)]
        exportar_xlsx(contagem, nomes, DIAS, saida)
        ws = load_workbook(saida).active
        assert ws.max_row == 4  # 1 cabeçalho + 3 alunos

    def test_numero_fora_da_lista_usa_nome_fallback(self, tmp_path):
        saida = str(tmp_path / "saida.xlsx")
        # aluno numero=10 mas lista só tem 1 entrada
        contagem = contar_presencas([_aluno(10, Segunda=(True, False))], DIAS)
        exportar_xlsx(contagem, [("Único", "1")], DIAS, saida)
        ws = load_workbook(saida).active
        assert "Aluno 10" in str(ws.cell(2, 2).value)

    def test_regressao_pagina_inicio_2_usa_nome_correto(self, tmp_path):
        """
        Bug corrigido: ao escanear só a página 2 (pagina_inicio=2), os alunos
        recebem números 26-50. O xlsx deve associar cada número ao nome correto
        da planilha (alunos[25] para numero=26), e não ao índice local (alunos[0]).
        """
        saida = str(tmp_path / "saida.xlsx")
        # Simula aluno 26 (primeira linha da página 2)
        aluno_pag2 = _aluno(26, Segunda=(True, False))
        contagem = contar_presencas([aluno_pag2], DIAS)
        # Lista com 30 alunos; o aluno 26 deve usar nomes[25]
        nomes = [(f"Aluno {i+1}", str(i+1)) for i in range(30)]
        exportar_xlsx(contagem, nomes, DIAS, saida)
        ws = load_workbook(saida).active
        assert ws.cell(2, 2).value == "Aluno 26"


# ---------------------------------------------------------------------------
# ler_nomes_alunos
# ---------------------------------------------------------------------------

class TestLerNomesAlunos:
    def test_le_nomes_e_matriculas(self, tmp_path):
        nomes = [("Ana Lima", "111"), ("Bruno Silva", "222")]
        caminho = _xlsx_alunos(tmp_path, nomes)
        assert ler_nomes_alunos(caminho, "CANELA IMPRESSÃO") == nomes

    def test_ignora_linhas_vazias(self, tmp_path):
        caminho = str(tmp_path / "alunos.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "CANELA IMPRESSÃO"
        ws.cell(8, 2, "Ana")
        ws.cell(8, 3, "111")
        # linha 9 propositalmente vazia
        ws.cell(10, 2, "Bruno")
        ws.cell(10, 3, "222")
        wb.save(caminho)
        resultado = ler_nomes_alunos(caminho, "CANELA IMPRESSÃO")
        assert len(resultado) == 2
        assert resultado[0][0] == "Ana"
        assert resultado[1][0] == "Bruno"

    def test_matricula_float_vira_string_sem_decimal(self, tmp_path):
        caminho = str(tmp_path / "alunos.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "CANELA IMPRESSÃO"
        ws.cell(8, 2, "Carlos")
        ws.cell(8, 3, 12345.0)
        wb.save(caminho)
        resultado = ler_nomes_alunos(caminho, "CANELA IMPRESSÃO")
        assert resultado[0][1] == "12345"

    def test_sem_matricula_retorna_string_vazia(self, tmp_path):
        # Coluna C explicitamente None para que max_column >= 3 e a linha não seja pulada
        caminho = str(tmp_path / "alunos.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "CANELA IMPRESSÃO"
        ws.cell(8, 2, "Diana")
        ws.cell(8, 3, "")  # força max_column=3; valor vazio → matricula=""
        wb.save(caminho)
        resultado = ler_nomes_alunos(caminho, "CANELA IMPRESSÃO")
        assert resultado[0] == ("Diana", "")

    def test_lista_vazia_quando_sem_dados(self, tmp_path):
        caminho = _xlsx_alunos(tmp_path, [])
        assert ler_nomes_alunos(caminho, "CANELA IMPRESSÃO") == []


# ---------------------------------------------------------------------------
# eh_pagina_branca
# ---------------------------------------------------------------------------

class TestEhPaginaBranca:
    # eh_pagina_branca retorna numpy.bool_ — usar == em vez de "is"

    def test_imagem_totalmente_branca(self):
        img = np.full((300, 200, 3), 255, dtype=np.uint8)
        assert eh_pagina_branca(img)

    def test_imagem_com_conteudo_significativo(self):
        img = np.full((300, 200, 3), 255, dtype=np.uint8)
        img[50:250, 30:170] = 0  # bloco preto grande (~40% da imagem)
        assert not eh_pagina_branca(img)

    def test_imagem_com_pouquissimo_conteudo_e_branca(self):
        img = np.full((300, 200, 3), 255, dtype=np.uint8)
        img[0:2, 0:2] = 0  # 4 pixels em 60000 = 0.007%
        assert eh_pagina_branca(img)

    def test_imagem_com_5_porcento_de_conteudo_nao_e_branca(self):
        img = np.full((200, 200, 3), 255, dtype=np.uint8)
        img[0:20, 0:100] = 0  # 5% de pixels escuros
        assert not eh_pagina_branca(img)
