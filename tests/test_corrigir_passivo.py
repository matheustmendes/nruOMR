"""
Testes do casamento arquivo × período na correção do passivo.

É a parte perigosa da automação: escolher o template ou o scan errado produz
uma correção errada gravada no Sheets — pior do que não corrigir nada. Por isso
o casamento só vale quando é inequívoco; no empate a linha fica em branco para
decisão humana.

Rodar:
    venv\\Scripts\\pytest tests\\
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corrigir_passivo as cp
import reconciliar


def _item(caminho, restaurante, datas):
    return {"caminho": caminho, "restaurante": restaurante, "datas": tuple(datas)}


def _pdf(caminho, linhas):
    """Gera um PDF de uma página com as linhas de texto informadas."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(caminho))
    for i, linha in enumerate(linhas):
        c.drawString(50, 800 - i * 14, linha)
    c.save()


# ---------------------------------------------------------------------------
# Extração de datas
# ---------------------------------------------------------------------------

class TestDatas:

    @pytest.mark.parametrize("periodo, esperado", [
        ("29/06 a 04/07", ("29/06", "04/07")),
        ("DATA: 09/05", ("09/05",)),
        ("20/06", ("20/06",)),
        ("", ()),
        ("sem data nenhuma", ()),
    ])
    def test_datas_do_periodo(self, periodo, esperado):
        assert cp._datas_do_periodo(periodo) == esperado

    @pytest.mark.parametrize("nome, esperado", [
        ("ondina_29-06_04-07.pdf", ("04/07", "29/06")),
        ("scan 15.06 a 19.06.pdf", ("15/06", "19/06")),
        ("canela_20_07.pdf", ("20/07",)),
        ("digitalizacao.pdf", ()),
    ])
    def test_datas_do_nome(self, nome, esperado):
        assert cp._datas_do_nome(nome) == esperado

    def test_ignora_numeros_que_nao_sao_data(self):
        """99/99 não é data; deixar passar geraria casamento falso."""
        assert cp._datas_do_nome("relatorio_99-99.pdf") == ()


# ---------------------------------------------------------------------------
# Escolha do arquivo
# ---------------------------------------------------------------------------

class TestMelhorArquivo:

    def test_casa_pelo_par_de_datas(self):
        itens = [
            _item("a.pdf", "ondina", ["29/06", "04/07"]),
            _item("b.pdf", "ondina", ["01/06", "06/06"]),
        ]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") == "a.pdf"

    def test_nao_cruza_restaurantes(self):
        itens = [_item("canela.pdf", "canela", ["29/06", "04/07"])]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") is None

    def test_arquivo_sem_unidade_identificada_ainda_serve(self):
        """
        Scans são imagem: muitas vezes só o nome do arquivo tem a data e nada
        indica a unidade. Descartá-los tornaria a automação inútil justamente
        onde ela mais ajuda.
        """
        itens = [_item("scan.pdf", None, ["29/06", "04/07"])]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") == "scan.pdf"

    def test_empate_devolve_nada(self):
        """Dois candidatos igualmente prováveis: quem decide é a pessoa."""
        itens = [
            _item("a.pdf", "ondina", ["29/06", "04/07"]),
            _item("b.pdf", "ondina", ["29/06", "04/07"]),
        ]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") is None

    def test_exige_datas_identicas_e_nao_sobreposicao(self):
        """
        Caso real: a folha de sabado 13/06 tem header "13/06" e compartilha uma
        data com a semana "08/06 a 13/06". Sao lotes diferentes, com outras
        colunas e outro roster — aceitar a sobreposicao ofereceria o template
        errado para a semana errada.
        """
        itens = [
            _item("fds_13_06.pdf", "sao_lazaro", ["13/06"]),
            _item("semana.pdf", "sao_lazaro", ["08/06", "13/06"]),
        ]
        assert cp._melhor_arquivo(itens, "sao_lazaro", "08/06 a 13/06") == "semana.pdf"
        assert cp._melhor_arquivo(itens, "sao_lazaro", "13/06") == "fds_13_06.pdf"

    def test_sobreposicao_parcial_sozinha_nao_basta(self):
        itens = [_item("parcial.pdf", "ondina", ["29/06"])]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") is None

    def test_sem_data_no_periodo_nao_arrisca(self):
        itens = [_item("a.pdf", "ondina", ["29/06"])]
        assert cp._melhor_arquivo(itens, "ondina", "") is None

    def test_nenhuma_data_em_comum(self):
        itens = [_item("a.pdf", "ondina", ["01/06", "06/06"])]
        assert cp._melhor_arquivo(itens, "ondina", "29/06 a 04/07") is None


# ---------------------------------------------------------------------------
# Filtro de formulário
# ---------------------------------------------------------------------------

class TestFiltroFormulario:

    def test_descarta_pdf_que_nao_e_formulario(self, tmp_path):
        """
        Num teste com a pasta real de downloads, um "Parecer Técnico" casou com
        uma semana de Ondina só por conter duas datas. Usar esse arquivo
        gravaria uma correção errada no Sheets.
        """
        _pdf(tmp_path / "parecer.pdf", [
            "Parecer Tecnico referente a reabertura do PDV",
            "Periodo de 03/08 a 08/08",
        ])
        assert cp._indexar_pdfs(str(tmp_path), exigir_formulario=True) == []

    def test_mantem_o_formulario_do_sistema(self, tmp_path):
        _pdf(tmp_path / "template_ondina.pdf", [
            "Pro-Reitoria de Assistencia Estudantil",
            "RELACAO DE BOLSISTAS ONDINA",
            "MES AGOSTO DE 2026  |  DATA: 03/08 - 08/08",
        ])
        itens = cp._indexar_pdfs(str(tmp_path), exigir_formulario=True)

        assert len(itens) == 1
        assert itens[0]["restaurante"] == "ondina"
        assert set(itens[0]["datas"]) == {"03/08", "08/08"}

    def test_scan_sem_texto_nao_e_descartado(self, tmp_path):
        """Scan é imagem: exigir texto ali eliminaria justamente os alvos."""
        _pdf(tmp_path / "ondina 03-08 a 08-08.pdf", [" "])
        itens = cp._indexar_pdfs(str(tmp_path), exigir_formulario=False)

        assert len(itens) == 1
        assert itens[0]["restaurante"] == "ondina"
        assert set(itens[0]["datas"]) == {"03/08", "08/08"}

    def test_pasta_inexistente_nao_quebra(self):
        assert cp._indexar_pdfs("/pasta/que/nao/existe", exigir_formulario=True) == []


# ---------------------------------------------------------------------------
# Plano em disco
# ---------------------------------------------------------------------------

class TestPlano:

    def test_ida_e_volta_preserva_as_colunas(self, tmp_path):
        caminho = str(tmp_path / "plano.csv")
        linhas = [{
            "restaurante": "ondina", "aba": "Junho 2026",
            "periodo": "29/06 a 04/07", "marcacoes_perdidas": 259,
            "template_pdf": "", "scan_pdf": "D:/scans/x.pdf",
            "pagina_inicial": 1, "lote_id": "", "status": "pendente",
            "divergentes": "", "csv": "",
        }]
        cp._salvar_plano(linhas, caminho)
        lido = cp._carregar_plano(caminho)

        assert len(lido) == 1
        assert lido[0]["periodo"] == "29/06 a 04/07"
        assert lido[0]["scan_pdf"] == "D:/scans/x.pdf"
        assert list(lido[0].keys()) == cp.COLUNAS

    def test_plano_inexistente_da_instrucao(self, tmp_path):
        with pytest.raises(SystemExit) as erro:
            cp._carregar_plano(str(tmp_path / "nao_existe.csv"))
        assert "preparar" in str(erro.value)


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

class TestExecutar:

    @staticmethod
    def _plano(tmp_path, **campos):
        base = {
            "restaurante": "ondina", "aba": "Junho 2026",
            "periodo": "29/06 a 04/07", "marcacoes_perdidas": 259,
            "template_pdf": "", "scan_pdf": "", "pagina_inicial": 1,
            "lote_id": "", "status": "pendente", "divergentes": "", "csv": "",
        }
        base.update(campos)
        caminho = str(tmp_path / "plano.csv")
        cp._salvar_plano([base], caminho)
        return caminho

    def test_linha_sem_scan_e_pulada_e_registrada(self, tmp_path, capsys):
        caminho = self._plano(tmp_path)
        cp.executar(caminho, aplicar=False)

        assert cp._carregar_plano(caminho)[0]["status"] == "falta scan"
        assert "PULADO" in capsys.readouterr().out

    def test_scan_inexistente_nao_derruba_a_execucao(self, tmp_path):
        caminho = self._plano(tmp_path, scan_pdf=str(tmp_path / "fantasma.pdf"))
        cp.executar(caminho, aplicar=False)
        assert cp._carregar_plano(caminho)[0]["status"] == "scan inexistente"

    def test_filtro_de_periodo_inexistente_avisa(self, tmp_path):
        caminho = self._plano(tmp_path)
        with pytest.raises(SystemExit):
            cp.executar(caminho, aplicar=False, periodo_filtro="01/01 a 02/01")

    def test_conferir_nunca_grava_no_sheets(self, tmp_path, monkeypatch):
        """
        Garantia estrutural: no modo conferir, nenhuma escrita pode partir
        daqui, mesmo que algo mais adiante falhe.
        """
        import google_sheets as gs

        def _proibido(*a, **k):
            raise AssertionError("exportar_para_sheets foi chamado no modo conferir")

        monkeypatch.setattr(gs, "exportar_para_sheets", _proibido)
        caminho = self._plano(tmp_path, scan_pdf=str(tmp_path / "fantasma.pdf"))
        cp.executar(caminho, aplicar=False)

    def test_ordem_de_paginas_nao_confirmada_bloqueia_aplicar(self, tmp_path, monkeypatch):
        """
        Achado real: um maço com arquivos sobrepostos (redigitalização que
        recobriu páginas já lidas) fez a reconstrução da ordem desistir, e a
        aplicação seguiu em frente e gravou como se a ordem estivesse certa.
        `aplicar` tem que recusar; `conferir` pode mostrar mesmo assim.
        """
        import google_sheets as gs
        import lote as lote_mod

        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"dummy")

        fake_lote = {"restaurante_key": "ondina", "total_alunos": 4, "dias": [],
                     "config": {}, "avisos": [], "alunos": []}
        monkeypatch.setattr(cp, "_obter_lote", lambda linha, cad: ("lote_teste", "geracao"))
        monkeypatch.setattr(lote_mod, "carregar_lote", lambda lid: fake_lote)
        monkeypatch.setattr(reconciliar, "releitura_correta", lambda *a, **k: (
            [], [], [], {"linhas_lidas": 4, "ambiguos": 0, "maior_numero": 4,
                        "fora_do_lote": 0, "excedentes_com_marca": [],
                        "ordem_confiavel": False, "motivo_ordem": "números repetidos"}
        ))

        def _proibido(*a, **k):
            raise AssertionError("exportar_para_sheets não pode ser chamado aqui")
        monkeypatch.setattr(gs, "exportar_para_sheets", _proibido)
        monkeypatch.setattr(gs, "ler_periodo", lambda *a, **k: {
            "ok": True, "aba": "Junho 2026", "dias": [], "linhas": [],
        })

        caminho = self._plano(tmp_path, scan_pdf=str(scan))

        cp.executar(caminho, aplicar=True)
        assert cp._carregar_plano(caminho)[0]["status"] == "ordem das páginas não confirmada"

        # Conferir pode rodar até o fim (só não grava) — é assim que o
        # operador vê o que está errado com a ordem antes de mexer no scan.
        cp.executar(caminho, aplicar=False)
