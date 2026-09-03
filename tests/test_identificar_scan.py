"""
Testes da identificação de scans pelo cabeçalho impresso.

O nome do arquivo e a data de modificação não valem nada: scanners salvam como
"Digitalizar0007.pdf" e copiar a pasta reescreve as datas. A informação
confiável está impressa na folha.

E o scan de uma semana quase nunca é um arquivo só — o maço sai em partes
("1 a 10", "11 a 23"), e quando o alimentador pula uma folha a parte é refeita.
Então completude é propriedade do **grupo**, não do arquivo: é a união dos
pedaços que precisa cobrir o lote inteiro.

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import identificar_scan as ident

CABECALHO_ONDINA = [
    "Pro-Reitoria de Assistencia Estudantil",
    "RELACAO DE BOLSISTAS ONDINA",
    "JUNHO 2026  |  29/06 a 04/07",
]


def _folha(caminho, paginas_lote, total=None, cabecalho=None, invertido=False):
    """
    PDF simulando um pedaço de maço.

    Args:
        paginas_lote: números de página do lote contidos no arquivo.
        total: o "de Y" do rodapé (padrão: o maior número informado).
        invertido: escreve na ordem inversa, como o scanner costuma entregar.
    """
    from reportlab.pdfgen import canvas

    total = total or max(paginas_lote)
    linhas_base = cabecalho if cabecalho is not None else CABECALHO_ONDINA
    ordem = sorted(paginas_lote, reverse=invertido)

    c = canvas.Canvas(str(caminho))
    for numero in ordem:
        for i, linha in enumerate(linhas_base):
            c.drawString(50, 800 - i * 14, linha)
        c.drawString(50, 800 - len(linhas_base) * 14, f"Pagina {numero} de {total}")
        c.showPage()
    c.save()
    return str(caminho)


# ---------------------------------------------------------------------------
# Extração de datas
# ---------------------------------------------------------------------------

class TestExtrairDatas:

    @pytest.mark.parametrize("texto, esperado", [
        ("JUNHO 2026  |  29/06 a 04/07", ["29/06", "04/07"]),
        ("MES MAIO DE 2026 | DATA: 29/06 - 04/07", ["29/06", "04/07"]),
        ("DATA: 09/05", ["09/05"]),
    ])
    def test_prefere_o_formato_do_cabecalho(self, texto, esperado):
        assert ident._extrair_datas(texto) == esperado

    def test_intervalo_vence_datas_soltas_no_corpo(self):
        """
        Pegar qualquer par de datas do texto produzia períodos impossíveis como
        "12/03 a 16/01" — visto de verdade na pasta de scans, onde o OCR do
        corpo da página joga números no meio.
        """
        texto = "16/01 lixo 12/03 RELACAO ONDINA 29/06 a 04/07 mais lixo 01/01"
        assert ident._extrair_datas(texto) == ["29/06", "04/07"]

    def test_datas_invalidas_sao_descartadas(self):
        assert ident._extrair_datas("99/99 a 40/13") == []

    def test_sem_data_nenhuma(self):
        assert ident._extrair_datas("documento qualquer") == []


# ---------------------------------------------------------------------------
# Interpretação do cabeçalho
# ---------------------------------------------------------------------------

class TestInterpretar:

    def test_le_unidade_datas_mes_e_paginacao(self):
        d = ident._interpretar("\n".join(CABECALHO_ONDINA) + "\nPagina 1 de 26")

        assert d["restaurante"] == "ondina"
        assert d["datas"] == ["29/06", "04/07"]
        assert d["mes_ano"] == "Junho 2026"
        assert d["total_paginas_lote"] == 26

    def test_sobrevive_aos_erros_tipicos_de_ocr_de_scanner(self):
        """O OCR embutido nos scanners troca Ç por Q e come acentos."""
        d = ident._interpretar(
            "Pro-Reitorla de Assistencia Estudantil\n"
            "RELAQAO DE BOLSISTAS ONDINA\n"
            "MES JUNHO DE 2026 | DATA: 29/06 - 04/07\n"
        )
        assert d["restaurante"] == "ondina"
        assert d["datas"] == ["29/06", "04/07"]

    def test_reconhece_as_siglas_das_unidades(self):
        assert ident._interpretar("RELACAO PDSL FDS")["restaurante"] == "sao_lazaro"
        assert ident._interpretar("RELACAO PDCA FDS")["restaurante"] == "canela"

    def test_mes_errado_no_titulo_nao_contamina_o_periodo(self):
        """
        Um bug antigo do gerador imprimiu folhas de junho com "MAIO" no título.
        A data sempre esteve certa, e é só ela que decide.
        """
        d = ident._interpretar(
            "RELACAO DE BOLSISTAS ONDINA\n"
            "MES MAIO DE 2026 | DATA: 29/06 - 04/07"
        )
        assert d["datas"] == ["29/06", "04/07"]
        assert ident._mes_divergente(d["mes_ano"], d["datas"]) == "Junho"

    def test_mes_coerente_nao_gera_divergencia(self):
        assert ident._mes_divergente("Junho 2026", ["29/06", "04/07"]) is None

    def test_sem_mes_ou_sem_data_nao_ha_o_que_comparar(self):
        assert ident._mes_divergente("", ["29/06"]) is None
        assert ident._mes_divergente("Junho 2026", []) is None

    def test_texto_vazio_nao_quebra(self):
        d = ident._interpretar("")
        assert d["restaurante"] is None
        assert d["datas"] == []


# ---------------------------------------------------------------------------
# Identificação de arquivo
# ---------------------------------------------------------------------------

class TestIdentificar:

    def test_identifica_e_mapeia_as_paginas_do_lote(self, tmp_path):
        caminho = _folha(tmp_path / "Digitalizar0007.pdf", range(1, 12), total=27)

        r = ident.identificar(caminho, usar_ocr=False)

        assert r["restaurante"] == "ondina"
        assert r["periodo_sugerido"] == "29/06 a 04/07"
        assert r["paginas_lote"] == tuple(range(1, 12))
        assert r["total_paginas_lote"] == 27
        assert r["cobertura_confiavel"] is True

    def test_nome_do_arquivo_e_irrelevante(self, tmp_path):
        """O nome pode mentir; o cabeçalho impresso, não."""
        caminho = _folha(tmp_path / "canela 01-01 a 05-01.pdf", [1, 2], total=2)

        r = ident.identificar(caminho, usar_ocr=False)
        assert r["restaurante"] == "ondina"
        assert r["periodo_sugerido"] == "29/06 a 04/07"

    def test_ordem_invertida_nao_atrapalha_o_mapeamento(self, tmp_path):
        """O scanner entrega da última folha para a primeira."""
        caminho = _folha(tmp_path / "scan.pdf", range(1, 12), total=27, invertido=True)

        r = ident.identificar(caminho, usar_ocr=False)
        assert r["paginas_lote"] == tuple(range(1, 12))

    def test_pedaco_do_meio_do_maco(self, tmp_path):
        caminho = _folha(tmp_path / "parte2.pdf", range(12, 21), total=27)

        r = ident.identificar(caminho, usar_ocr=False)
        assert r["paginas_lote"] == tuple(range(12, 21))

    def test_pdf_sem_cabecalho_e_reportado_e_nao_adivinhado(self, tmp_path):
        from reportlab.pdfgen import canvas

        caminho = tmp_path / "qualquer.pdf"
        c = canvas.Canvas(str(caminho))
        c.drawString(50, 800, "documento sem relacao com o sistema")
        c.save()

        r = ident.identificar(str(caminho), usar_ocr=False)

        assert r["restaurante"] is None
        assert r["periodo_sugerido"] == ""
        assert r["paginas_lote"] == ()


# ---------------------------------------------------------------------------
# Agrupamento das partes de um maço
# ---------------------------------------------------------------------------

class TestAgrupar:

    def _partes(self, tmp_path):
        return [
            ident.identificar(_folha(tmp_path / "p1.pdf", range(1, 12), 27), False),
            ident.identificar(_folha(tmp_path / "p2.pdf", range(12, 21), 27), False),
            ident.identificar(_folha(tmp_path / "p3.pdf", range(21, 28), 27), False),
        ]

    def test_uniao_das_partes_cobre_o_lote(self, tmp_path):
        grupos = ident.agrupar_por_periodo(self._partes(tmp_path))
        g = grupos[("ondina", "29/06 a 04/07")]

        assert g["completo"] is True
        assert g["total_paginas"] == 27
        assert len(g["arquivos"]) == 3
        assert g["paginas_faltando"] == ()

    def test_redigitalizacao_vira_redundante(self, tmp_path):
        """
        Quando o alimentador pula uma folha, a parte é refeita. A cópia velha
        não acrescenta página nenhuma e não precisa entrar no processamento.
        """
        partes = self._partes(tmp_path)
        partes.append(
            ident.identificar(_folha(tmp_path / "p1_refeito.pdf", range(1, 11), 27), False)
        )

        g = ident.agrupar_por_periodo(partes)[("ondina", "29/06 a 04/07")]

        assert g["completo"] is True
        assert len(g["arquivos"]) == 3
        assert len(g["redundantes"]) == 1

    def test_maco_furado_e_apontado_pagina_a_pagina(self, tmp_path):
        partes = [
            ident.identificar(_folha(tmp_path / "p1.pdf", range(1, 12), 27), False),
            ident.identificar(_folha(tmp_path / "p3.pdf", range(21, 28), 27), False),
        ]

        g = ident.agrupar_por_periodo(partes)[("ondina", "29/06 a 04/07")]

        assert g["completo"] is False
        assert g["verificavel"] is True
        assert g["paginas_faltando"] == tuple(range(12, 21))
        assert any("FALTAM" in a for a in g["avisos"])

    def test_sem_paginacao_legivel_e_nao_verificado_e_nao_incompleto(self, tmp_path):
        """
        Distinção que evita retrabalho: "não consegui conferir" não é a mesma
        coisa que "está furado". Tratar as duas igual manda reescanear folha
        que está boa — e, pior, dá por verificado o que não foi.
        """
        from reportlab.pdfgen import canvas

        caminho = tmp_path / "sem_paginacao.pdf"
        c = canvas.Canvas(str(caminho))
        for linha in CABECALHO_ONDINA:
            c.drawString(50, 800, linha)
        c.save()

        g = ident.agrupar_por_periodo(
            [ident.identificar(str(caminho), usar_ocr=False)]
        )[("ondina", "29/06 a 04/07")]

        assert g["completo"] is False
        assert g["verificavel"] is False
        assert any("NÃO VERIFICADO" in a for a in g["avisos"])

    def test_semanas_diferentes_nao_se_misturam(self, tmp_path):
        outra = ["RELACAO DE BOLSISTAS ONDINA", "JUNHO 2026 | 08/06 a 13/06"]
        partes = [
            ident.identificar(_folha(tmp_path / "a.pdf", [1, 2], 2), False),
            ident.identificar(
                _folha(tmp_path / "b.pdf", [1, 2], 2, cabecalho=outra), False
            ),
        ]

        grupos = ident.agrupar_por_periodo(partes)
        assert len(grupos) == 2


# ---------------------------------------------------------------------------
# Casamento com os períodos do Sheets
# ---------------------------------------------------------------------------

class TestCasarComPeriodos:

    @staticmethod
    def _ident(arquivo, restaurante, datas):
        return {"arquivo": arquivo, "restaurante": restaurante,
                "datas": tuple(datas), "periodo_sugerido": ""}

    def test_casa_pelo_conjunto_de_datas(self):
        identificacoes = [
            self._ident("a.pdf", "ondina", ["29/06", "04/07"]),
            self._ident("b.pdf", "ondina", ["08/06", "13/06"]),
        ]
        periodos = [{"restaurante": "ondina", "periodo": "29/06 a 04/07"}]

        casados, ambiguos = ident.casar_com_periodos(identificacoes, periodos)

        assert casados[("ondina", "29/06 a 04/07")]["arquivo"] == "a.pdf"
        assert ambiguos == {}

    def test_rotulos_diferentes_mesmas_datas(self):
        identificacoes = [self._ident("a.pdf", "ondina", ["29/06", "04/07"])]
        periodos = [{"restaurante": "ondina", "periodo": "DATA: 29/06 - 04/07"}]

        casados, _ = ident.casar_com_periodos(identificacoes, periodos)
        assert len(casados) == 1

    def test_dois_scans_para_a_mesma_semana_ficam_ambiguos(self):
        identificacoes = [
            self._ident("a.pdf", "ondina", ["29/06", "04/07"]),
            self._ident("b.pdf", "ondina", ["29/06", "04/07"]),
        ]
        periodos = [{"restaurante": "ondina", "periodo": "29/06 a 04/07"}]

        casados, ambiguos = ident.casar_com_periodos(identificacoes, periodos)

        assert casados == {}
        assert len(ambiguos[("ondina", "29/06 a 04/07")]) == 2

    def test_nao_cruza_unidades(self):
        identificacoes = [self._ident("a.pdf", "canela", ["29/06", "04/07"])]
        periodos = [{"restaurante": "ondina", "periodo": "29/06 a 04/07"}]

        casados, ambiguos = ident.casar_com_periodos(identificacoes, periodos)
        assert casados == {} and ambiguos == {}
