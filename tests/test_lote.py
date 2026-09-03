"""
Testes do snapshot de lote — a camada que garante que a presença lida numa
linha do papel vá para a pessoa que estava impressa naquela linha.

Cobertura:
    - roster_do_lote (a ordem impressa vira a lista indexada por número)
    - comparar_com_planilha (o que mudou desde a impressão)
    - validar_roster (duplicadas, sem matrícula, nomes repetidos)
    - ciclo de vida (arquiva só com sincronização confirmada)
    - ler_circulo (medição interior x quadrada) e get_zona_ambigua

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lote as lote_mod
from ler_bolhas import ler_circulo, get_zona_ambigua

DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(alunos_por_pagina=3):
    return {
        "scan": {"dpi": 200},
        "circulos": {"raio_mm": 2.5, "borda_pt": 1.5},
        "layout": {
            "alunos_por_pagina": alunos_por_pagina,
            "linha_altura_mm": 8.0,
            "primeira_linha_y_mm": 62.8,
            "dias": DIAS,
            "circulos_por_dia": {d: {"almoco_x": 105.0, "janta_x": 113.0} for d in DIAS},
        },
    }


def _paginacao(pessoas, alunos_por_pagina=3):
    """pessoas: lista de (nome, matricula) na ordem impressa."""
    return [
        {
            "numero": i + 1,
            "pagina": i // alunos_por_pagina + 1,
            "linha_pagina": i % alunos_por_pagina + 1,
            "nome": nome,
            "nome_impresso": nome,
            "matricula": matricula,
        }
        for i, (nome, matricula) in enumerate(pessoas)
    ]


def _lote(pessoas, lote_id="teste_20260101-000000", alunos_por_pagina=3):
    return lote_mod.montar_lote(
        lote_id=lote_id,
        restaurante_key="canela",
        restaurante_nome="Canela",
        aba="CANELA IMPRESSÃO",
        dias=DIAS,
        paginacao=_paginacao(pessoas, alunos_por_pagina),
        config=_config(alunos_por_pagina),
        info={"datas": "05/05 a 09/05", "mes_ano": "Maio 2026"},
    )


PESSOAS = [
    ("Ana Souza", "111111111"),
    ("Bruno Lima", "222222222"),
    ("Carla Dias", "333333333"),
    ("Diego Reis", "444444444"),
]


# ---------------------------------------------------------------------------
# roster_do_lote
# ---------------------------------------------------------------------------

class TestRosterDoLote:

    def test_preserva_a_ordem_impressa(self):
        roster = lote_mod.roster_do_lote(_lote(PESSOAS))
        assert roster == [
            ("Ana Souza", "111111111"),
            ("Bruno Lima", "222222222"),
            ("Carla Dias", "333333333"),
            ("Diego Reis", "444444444"),
        ]

    def test_indice_bate_com_numero_menos_um(self):
        """É esse contrato que exportar/revisar/sheets consomem."""
        lote = _lote(PESSOAS)
        roster = lote_mod.roster_do_lote(lote)
        for aluno in lote["alunos"]:
            assert roster[aluno["numero"] - 1] == (aluno["nome"], aluno["matricula"])

    def test_paginacao_respeita_alunos_por_pagina(self):
        lote = _lote(PESSOAS, alunos_por_pagina=3)
        paginas = [(a["numero"], a["pagina"], a["linha_pagina"]) for a in lote["alunos"]]
        assert paginas == [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 2, 1)]

    def test_buraco_vira_placeholder_sem_deslizar(self):
        """
        Um número ausente não pode fazer o resto da lista subir uma posição —
        é exatamente assim que a presença ia parar na pessoa errada.
        """
        lote = _lote(PESSOAS)
        del lote["alunos"][1]  # remove o número 2

        roster = lote_mod.roster_do_lote(lote)
        assert len(roster) == 4
        assert roster[0] == ("Ana Souza", "111111111")
        assert roster[1][1] == ""
        assert "linha 2" in roster[1][0]
        assert roster[2] == ("Carla Dias", "333333333")

    def test_ate_numero_estende_a_lista(self):
        roster = lote_mod.roster_do_lote(_lote(PESSOAS), ate_numero=6)
        assert len(roster) == 6
        assert roster[5][1] == ""


# ---------------------------------------------------------------------------
# comparar_com_planilha
# ---------------------------------------------------------------------------

class TestCompararComPlanilha:

    def test_planilha_identica(self):
        comp = lote_mod.comparar_com_planilha(_lote(PESSOAS), PESSOAS)
        assert comp["deslocados"] == []
        assert comp["removidos"] == []
        assert comp["novos"] == []
        assert comp["iguais"] == 4
        assert "idêntica" in comp["resumo"]

    def test_detecta_quem_mudou_de_linha(self):
        """Alguém sai do topo e todo mundo abaixo sobe uma posição."""
        atual = [p for p in PESSOAS if p[0] != "Ana Souza"]
        comp = lote_mod.comparar_com_planilha(_lote(PESSOAS), atual)

        assert [d["nome"] for d in comp["removidos"]] == ["Ana Souza"]
        deslocados = {d["nome"]: (d["numero_lote"], d["numero_planilha"])
                      for d in comp["deslocados"]}
        assert deslocados == {
            "Bruno Lima": (2, 1),
            "Carla Dias": (3, 2),
            "Diego Reis": (4, 3),
        }

    def test_detecta_entrada_posterior(self):
        atual = PESSOAS + [("Elisa Nunes", "555555555")]
        comp = lote_mod.comparar_com_planilha(_lote(PESSOAS), atual)
        assert [n["nome"] for n in comp["novos"]] == ["Elisa Nunes"]
        assert comp["deslocados"] == []

    def test_matricula_duplicada_fica_fora_da_comparacao(self):
        """
        Duas linhas com a mesma matrícula não identificam ninguém: incluí-las
        produziria "mudou de linha" para quem não mudou.
        """
        pessoas = PESSOAS + [("Ana Souza (2)", "111111111")]
        comp = lote_mod.comparar_com_planilha(_lote(pessoas), pessoas)
        assert comp["ambiguas"] == ["111111111"]
        assert comp["deslocados"] == []

    def test_matricula_normalizada_ignora_formato(self):
        atual = [(n, float(m)) for n, m in PESSOAS]
        comp = lote_mod.comparar_com_planilha(_lote(PESSOAS), atual)
        assert comp["iguais"] == 4


# ---------------------------------------------------------------------------
# validar_roster
# ---------------------------------------------------------------------------

class TestValidarRoster:

    def test_roster_limpo_nao_gera_aviso(self):
        assert lote_mod.validar_roster(PESSOAS)["avisos"] == []

    def test_aponta_matricula_duplicada_com_as_duas_linhas(self):
        pessoas = PESSOAS + [("Ana Souza", "111111111")]
        resultado = lote_mod.validar_roster(pessoas)
        assert len(resultado["duplicadas"]) == 1
        assert resultado["duplicadas"][0]["numeros"] == [1, 5]

    def test_sem_matricula_e_nota_e_nao_aviso(self):
        """Matrícula em branco acontece; é informação, não pendência."""
        pessoas = PESSOAS + [("Sem Registro", "")]
        resultado = lote_mod.validar_roster(pessoas)
        assert [s["numero"] for s in resultado["sem_matricula"]] == [5]
        assert resultado["avisos"] == []
        assert resultado["notas"]

    def test_tamanhos_diferentes_de_matricula_sao_validos(self):
        """8, 9 e 10 dígitos convivem: graduação, pós e estrangeiros."""
        pessoas = [("A", "12345678"), ("B", "123456789"), ("C", "1234567890")]
        assert lote_mod.validar_roster(pessoas)["avisos"] == []

    def test_nao_levanta_excecao_com_roster_problematico(self):
        """Avisar não pode virar bloquear a impressão da semana."""
        pessoas = [("A", "111"), ("A", "111"), ("B", None)]
        resultado = lote_mod.validar_roster(pessoas)
        assert resultado["avisos"]
        assert [s["numero"] for s in resultado["sem_matricula"]] == [3]


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------

class TestCicloDeVida:

    @pytest.fixture(autouse=True)
    def _pastas_temporarias(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lote_mod, "LOTES_DIR", str(tmp_path / "lotes"))
        monkeypatch.setattr(lote_mod, "PROCESSADOS_DIR", str(tmp_path / "lotes" / "processados"))

    def test_salvar_e_carregar_preservam_o_roster(self):
        lote_mod.salvar_lote(_lote(PESSOAS))
        recarregado = lote_mod.carregar_lote("teste_20260101-000000")
        assert lote_mod.roster_do_lote(recarregado) == PESSOAS

    def test_sincronizacao_confirmada_arquiva(self):
        lote_mod.salvar_lote(_lote(PESSOAS))
        lote_mod.registrar_processamento(
            "teste_20260101-000000", "05/05 a 09/05", sincronizado_sheets=True
        )

        ativo = os.path.join(lote_mod.LOTES_DIR, "teste_20260101-000000.json")
        arquivado = os.path.join(lote_mod.PROCESSADOS_DIR, "teste_20260101-000000.json")
        assert not os.path.exists(ativo)
        assert os.path.exists(arquivado)

    def test_falha_no_sheets_mantem_o_lote_ativo(self):
        """
        É esse lote que permite reprocessar sem reescanear a folha — arquivar
        aqui esconderia o trabalho pendente.
        """
        lote_mod.salvar_lote(_lote(PESSOAS))
        lote_mod.registrar_processamento(
            "teste_20260101-000000", "05/05 a 09/05", sincronizado_sheets=False
        )

        ativo = os.path.join(lote_mod.LOTES_DIR, "teste_20260101-000000.json")
        assert os.path.exists(ativo)

        recarregado = lote_mod.carregar_lote("teste_20260101-000000")
        assert recarregado["processamentos"][0]["sincronizado_com_sheets"] is False

    def test_carregar_encontra_lote_arquivado(self):
        lote_mod.salvar_lote(_lote(PESSOAS))
        lote_mod.registrar_processamento("teste_20260101-000000", "x", True)
        assert lote_mod.carregar_lote("teste_20260101-000000")["total_alunos"] == 4

    def test_listar_marca_o_que_foi_arquivado(self):
        lote_mod.salvar_lote(_lote(PESSOAS, lote_id="a_20260101-000000"))
        lote_mod.salvar_lote(_lote(PESSOAS, lote_id="b_20260102-000000"))
        lote_mod.registrar_processamento("a_20260101-000000", "x", True)

        por_id = {l["lote_id"]: l for l in lote_mod.listar_lotes("canela")}
        assert por_id["a_20260101-000000"]["processado"] is True
        assert por_id["b_20260102-000000"]["processado"] is False

    def test_limpeza_nao_apaga_sem_aplicar(self):
        lote_mod.salvar_lote(_lote(PESSOAS))
        lote_mod.registrar_processamento("teste_20260101-000000", "x", True)

        alvos = lote_mod.limpar_antigos(dias_retencao=0, aplicar=False)
        assert alvos == ["teste_20260101-000000"]
        assert os.path.exists(
            os.path.join(lote_mod.PROCESSADOS_DIR, "teste_20260101-000000.json")
        )

    def test_limpeza_poupa_lote_nao_sincronizado(self):
        lote_mod.salvar_lote(_lote(PESSOAS))
        lote_mod.registrar_processamento(
            "teste_20260101-000000", "x", False, arquivar=True
        )
        assert lote_mod.limpar_antigos(dias_retencao=0, aplicar=True) == []


# ---------------------------------------------------------------------------
# Medição das bolhas
# ---------------------------------------------------------------------------

class TestLerCirculo:

    @staticmethod
    def _circulo(raio_px=20, borda_px=4, preenchido=False):
        """Imagem BINARY_INV: 255 = tinta. Desenha só a borda, ou o miolo cheio."""
        lado = raio_px * 4
        img = np.zeros((lado, lado), dtype=np.uint8)
        centro = lado // 2
        yy, xx = np.ogrid[:lado, :lado]
        dist2 = (yy - centro) ** 2 + (xx - centro) ** 2

        anel = (dist2 <= raio_px ** 2) & (dist2 >= (raio_px - borda_px) ** 2)
        img[anel] = 255
        if preenchido:
            img[dist2 <= raio_px ** 2] = 255
        return img, centro, raio_px

    def test_circulo_vazio_le_quase_zero_no_modo_interior(self):
        img, c, r = self._circulo(preenchido=False)
        assert ler_circulo(img, c, c, r, "interior", 0.65) < 0.02

    def test_circulo_preenchido_le_quase_um(self):
        img, c, r = self._circulo(preenchido=True)
        assert ler_circulo(img, c, c, r, "interior", 0.65) > 0.98

    def test_modo_quadrado_confunde_a_borda_com_marcacao(self):
        """
        A borda impressa sozinha ocupa ~30% da ROI quadrada. É essa a origem
        dos scans em que tudo cai na zona ambígua de uma vez.
        """
        img, c, r = self._circulo(preenchido=False)
        assert ler_circulo(img, c, c, r, "quadrado", 1.0) > 0.20

    def test_interior_tolera_desalinhamento_de_alguns_pixels(self):
        """
        Deslocar o centro em alguns pixels faz o disco raspar a borda impressa,
        mas o valor continua muito abaixo do corte de 0,20 — a decisão não muda.
        No modo quadrado o mesmo deslocamento já parte de ~0,30.
        """
        img, c, r = self._circulo(preenchido=False)
        for desvio in (-3, -1, 0, 1, 3):
            lido = ler_circulo(img, c + desvio, c + desvio, r, "interior", 0.65)
            assert lido < 0.10, f"desvio {desvio} leu {lido:.3f}"

    def test_fora_da_pagina_devolve_zero(self):
        img, c, r = self._circulo()
        assert ler_circulo(img, 1, 1, r, "interior", 0.65) == 0.0


class TestZonaAmbigua:

    def test_padrao_acompanha_o_modo_de_medicao(self):
        interior = get_zona_ambigua({"scan": {"medicao": "interior"}})
        quadrado = get_zona_ambigua({"scan": {"medicao": "quadrado"}})
        assert interior == pytest.approx((0.10, 0.45))
        assert quadrado == pytest.approx((0.36, 0.50))

    def test_threshold_explicito_vence_o_padrao(self):
        zona = get_zona_ambigua({"scan": {"medicao": "interior", "threshold": 0.50}})
        assert zona == pytest.approx((0.40, 0.75))

    def test_margens_explicitas_vencem_o_padrao(self):
        zona = get_zona_ambigua({
            "scan": {"medicao": "interior", "threshold": 0.30,
                     "ambiguo_margem_abaixo": 0.05, "ambiguo_margem_acima": 0.05}
        })
        assert zona == pytest.approx((0.25, 0.35))
