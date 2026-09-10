"""
web.py

Interface web local para o sistema de presença.
Um único arquivo, sem dependências de frontend.

Uso:
    python web.py
    (abre automaticamente no navegador em http://localhost:5000)
"""

import os
import sys
import json
import threading
import webbrowser
import tempfile
import traceback
from io import BytesIO

# Garante imports locais
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, send_file, jsonify, Response
from pypdf import PdfWriter, PdfReader

import cv2
import numpy as np
import yaml

from localizar_marcadores import processar
from utils import stdout_tolerante
from ler_bolhas import (
    ler_pagina, THRESHOLD, carregar_posicoes,
    _get_threshold, _get_offset_y, get_zona_ambigua,
)
from exportar import (
    carregar_todas_paginas, processar_pdf_completo,
    contar_presencas, ler_nomes_alunos, exportar_xlsx, eh_pagina_branca,
    aplicar_correcoes,
)
from revisar import extrair_recortes, gerar_html_revisao
from gerar_template import (
    ler_planilha, gerar_template, gerar_config, gerar_com_lote, CONFIG_ABAS
)
from google_sheets import exportar_para_sheets
import lote as lote_mod
import corrigir_passivo


app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

RESTAURANTES = {
    "canela": {
        "nome": "Canela",
        "aba": "CANELA IMPRESSÃO",
        "config": "config_canela.yaml",
    },
    "ondina": {
        "nome": "Ondina",
        "aba": "ONDINA IMPRESSÃO",
        "config": "config_ondina.yaml",
    },
    "sao_lazaro": {
        "nome": "São Lázaro",
        "aba": "SÃO LÁZARO IMPRESSÃO",
        "config": "config_sao_lazaro.yaml",
    },
    "canela_fds": {
        "nome": "Canela FDS",
        "aba": " PDCA FDS",
        "config": "config_canela_fds.yaml",
    },
    "sao_lazaro_fds": {
        "nome": "São Lázaro FDS",
        "aba": "PDSL FDS",
        "config": "config_sao_lazaro_fds.yaml",
    },
    "canela_fds_especial": {
        "nome": "Canela FDS Especial",
        "aba": " PDCA FDS",
        "config": "config_canela_fds_especial.yaml",
    },
    "sao_lazaro_fds_especial": {
        "nome": "São Lázaro FDS Especial",
        "aba": "PDSL FDS",
        "config": "config_sao_lazaro_fds_especial.yaml",
    },
    "ondina_fds_especial": {
        "nome": "Ondina Especial",
        "aba": "ONDINA IMPRESSÃO",
        "config": "config_ondina_fds_especial.yaml",
    },
}


def encontrar_config(restaurante_key):
    """Encontra o arquivo de config do restaurante."""
    info = RESTAURANTES[restaurante_key]
    caminho = os.path.join(SCRIPT_DIR, "configs", info["config"])
    if not os.path.exists(caminho):
        caminho = os.path.join(SCRIPT_DIR, info["config"])
    return caminho if os.path.exists(caminho) else None


# --- ROTAS ---

@app.route("/")
def index():
    return HTML_PAGE


def _mesclar_pdfs(paths):
    writer = PdfWriter()
    for path in paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
    merged = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    merged.close()
    with open(merged.name, "wb") as f:
        writer.write(f)
    return merged.name


@app.route("/processar", methods=["POST"])
def rota_processar():
    try:
        restaurante_key = request.form.get("restaurante")
        if restaurante_key not in RESTAURANTES:
            return jsonify({"erro": "Restaurante inválido"}), 400

        scan_files = request.files.getlist("scan")
        alunos_file = request.files.get("alunos")
        lote_id = (request.form.get("lote_id") or "").strip()

        if not scan_files or not scan_files[0].filename:
            return jsonify({"erro": "Envie o PDF do scan"}), 400
        if not lote_id and not alunos_file:
            return jsonify({
                "erro": "Selecione o lote impresso que originou este scan "
                        "(ou, no modo legado, envie a planilha de alunos)."
            }), 400

        # Salva arquivos temporários — fecha imediatamente após salvar (necessário no Windows)
        if len(scan_files) == 1:
            scan_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            scan_tmp.close()
            scan_files[0].save(scan_tmp.name)
            scan_path = scan_tmp.name
        else:
            partes = []
            for sf in scan_files:
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.close()
                sf.save(tmp.name)
                partes.append(tmp.name)
            scan_path = _mesclar_pdfs(partes)
            for p in partes:
                try:
                    os.unlink(p)
                except Exception:
                    pass

        alunos_tmp = None
        if alunos_file and alunos_file.filename:
            alunos_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
            alunos_tmp.close()
            alunos_file.save(alunos_tmp.name)

        restaurante = RESTAURANTES[restaurante_key]
        lote = None
        avisos_identidade = []

        if lote_id:
            # Caminho correto: identidade e geometria vêm congeladas do lote
            # impresso. A planilha atual não entra na resolução de quem é quem.
            try:
                lote = lote_mod.carregar_lote(lote_id)
            except FileNotFoundError as e:
                return jsonify({"erro": str(e)}), 400

            if lote["restaurante_key"] != restaurante_key:
                return jsonify({
                    "erro": f"O lote {lote_id} é de {lote['restaurante_nome']}, "
                            f"não de {restaurante['nome']}."
                }), 400

            config = lote["config"]
            dias = lote["dias"]
            alunos = lote_mod.roster_do_lote(lote)

            # Os avisos do lote são da hora da impressão e já foram mostrados
            # ali. Repetir a lista inteira a cada scan vira ruído e esconde os
            # avisos que importam agora — basta o ponteiro.
            n_avisos = len(lote.get("avisos", []))
            if n_avisos:
                avisos_identidade.append(
                    f"O lote {lote_id} foi impresso com {n_avisos} pendência(s) "
                    f"na planilha de origem (matrícula duplicada ou em branco). "
                    f"Detalhes em: python lote.py ver {lote_id}"
                )
        else:
            # Modo legado: sem lote, a identidade volta a depender da planilha
            # informada agora — exatamente a origem dos scans trocados. Mantido
            # só para não travar quem ainda tem folhas impressas antes do lote.
            caminho_config = encontrar_config(restaurante_key)
            if not caminho_config:
                return jsonify({"erro": f"Configuração não encontrada para {RESTAURANTES[restaurante_key]['nome']}. Gere o template primeiro."}), 400

            with open(caminho_config, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            dias = config["layout"]["dias"]
            alunos = ler_nomes_alunos(alunos_tmp.name, restaurante["aba"])
            avisos_identidade.append(
                "Processado SEM lote: os nomes vieram da planilha enviada agora. "
                "Se ela mudou depois da impressão, as presenças saem trocadas. "
                "Confira antes de dar como fechado."
            )

        periodo_semana = request.form.get("periodo_semana", "").strip()
        if not periodo_semana:
            return jsonify({"erro": "Informe o período da semana (ex: 05/05 a 09/05)"}), 400

        try:
            pagina_inicial = int(request.form.get("pagina_inicial", "").strip() or 1)
        except ValueError:
            pagina_inicial = 1
        pagina_inicial = max(1, pagina_inicial)

        # Limpa scan anterior se houver
        old_scan = app.config.get("ULTIMO_SCAN")
        if old_scan and os.path.exists(old_scan):
            try:
                os.unlink(old_scan)
            except Exception:
                pass

        # Processa
        paginas = carregar_todas_paginas(scan_path, dpi=config["scan"]["dpi"])
        resultados, pags_alinhadas, binarios, resultados_por_pag = processar_pdf_completo(
            paginas, config, retornar_imagens=True, pagina_inicial=pagina_inicial
        )
        # O lote sabe quantas páginas foram impressas; ler mais do que isso
        # significa que entrou folha de outro lote no maço.
        if lote:
            maior_num = max((r["numero"] for r in resultados), default=0)
            if maior_num > lote["total_alunos"]:
                avisos_identidade.append(
                    f"O scan chegou até a linha {maior_num}, mas o lote {lote_id} "
                    f"tem {lote['total_alunos']} pessoas em {lote['total_paginas']} "
                    f"página(s). Confira se não entrou folha de outro lote."
                )

        # Casos ambíguos são contados como presença
        _threshold = _get_threshold(config)
        amb_min, amb_max = get_zona_ambigua(config)
        for r in resultados:
            for dia in dias:
                for tipo in ["almoco", "janta"]:
                    pct = r["dias"][dia][f"{tipo}_pct"]
                    if amb_min <= pct < _threshold:
                        r["dias"][dia][tipo] = True

        contagem = contar_presencas(resultados, dias)

        # Alunos efetivamente lidos no scan (não o total da planilha)
        alunos_lidos = len(resultados)
        com_presenca = sum(1 for c in contagem if c["presencas"] > 0)

        total_celulas = max(1, alunos_lidos * len(dias) * 2)
        ambiguos = sum(
            1
            for r in resultados
            for dia in dias
            for tipo in ["almoco", "janta"]
            if amb_min <= r["dias"][dia][f"{tipo}_pct"] <= amb_max
        )

        # Ambiguidade generalizada não é caso a caso para revisar — é sinal de
        # que a leitura inteira está ruim (scan escuro demais, folha torta,
        # geometria errada). Revisar 200 recortes um a um não conserta isso.
        if ambiguos > total_celulas * 0.25:
            avisos_identidade.append(
                f"{ambiguos} de {total_celulas} marcações caíram na zona de dúvida "
                f"({ambiguos * 100 // total_celulas}%). Isso indica problema na "
                "digitalização ou no alinhamento, não dúvidas reais de "
                "preenchimento — vale reescanear antes de revisar caso a caso."
            )

        # Gera xlsx de saída
        saida_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        saida_tmp.close()
        exportar_xlsx(contagem, alunos, dias, saida_tmp.name)

        # Com lote, a planilha enviada (se houver) serve só para mostrar o que
        # mudou desde a impressão — nunca para decidir quem é quem.
        divergencia = None
        if lote and alunos_tmp:
            try:
                atuais = ler_nomes_alunos(alunos_tmp.name, restaurante["aba"])
                divergencia = lote_mod.comparar_com_planilha(lote, atuais)
            except Exception as e:
                print(f"  Aviso: não foi possível comparar com a planilha enviada: {e}")

        if alunos_tmp:
            try:
                os.unlink(alunos_tmp.name)
            except Exception:
                pass

        # Exporta para Google Sheets (não bloqueia em caso de falha)
        resultado_sheets = exportar_para_sheets(
            contagem, alunos, dias, restaurante_key, periodo_semana
        )
        app.config["ULTIMO_PERIODO"] = periodo_semana

        if resultado_sheets.get("ok"):
            sheets_resp = {"sheets_status": "ok", "sheets_aba": resultado_sheets.get("aba", "")}
            aviso = resultado_sheets.get("aviso_ordem") or resultado_sheets.get("aviso_formatacao")
            if aviso:
                sheets_resp["sheets_aviso"] = aviso
        elif resultado_sheets.get("duplicado"):
            sheets_resp = {"sheets_status": "duplicado", "sheets_aba": resultado_sheets.get("aba", "")}
        else:
            sheets_resp = {"sheets_status": "erro", "sheets_erro": resultado_sheets.get("erro", "")}

        # Registra no lote o que aconteceu. Ele só sai da pasta ativa quando o
        # Sheets confirmou de verdade — se falhou, continua disponível para
        # reprocessar sem reescanear a folha.
        if lote:
            lote_mod.registrar_processamento(
                lote_id,
                periodo_semana,
                sincronizado_sheets=bool(resultado_sheets.get("ok")),
                detalhe=sheets_resp.get("sheets_erro", ""),
            )

        # Guarda caminho pra download e dados de preview
        app.config["ULTIMO_RESULTADO"] = saida_tmp.name
        app.config["ULTIMO_NOME"] = f"presencas_{restaurante_key}.xlsx"
        app.config["ULTIMO_SCAN"] = scan_path
        app.config["ULTIMO_CONFIG"] = config
        app.config["REVISAO_DATA"] = {
            "paginas_alinhadas": pags_alinhadas,
            "binarios": binarios,
            "resultados_por_pagina": resultados_por_pag,
            "resultados": resultados,
            "alunos": alunos,
            "config": config,
            "dias": dias,
            "restaurante_key": restaurante_key,
            "lote_id": lote_id or None,
        }

        return jsonify({
            "sucesso": True,
            "alunos": alunos_lidos,
            "com_presenca": com_presenca,
            "ambiguos": ambiguos,
            "paginas": len(paginas),
            "paginas_validas": len(pags_alinhadas),
            "modo": "lote" if lote else "legado",
            "lote_id": lote_id or "",
            "lote_datas": lote.get("datas", "") if lote else "",
            "avisos": avisos_identidade,
            "divergencia": divergencia and {
                "resumo": divergencia["resumo"],
                "deslocados": len(divergencia["deslocados"]),
                "removidos": len(divergencia["removidos"]),
                "novos": len(divergencia["novos"]),
            },
            **sheets_resp,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/gerar_template", methods=["POST"])
def rota_gerar_template():
    try:
        restaurante_key = request.form.get("restaurante")
        alunos_file = request.files.get("alunos")
        data_periodo = request.form.get("data_periodo", "").strip()

        if not alunos_file:
            return jsonify({"erro": "Envie a planilha de alunos"}), 400

        # Suporte a "todos"
        if restaurante_key == "todos":
            keys = ["canela", "ondina", "sao_lazaro"]
        elif restaurante_key in RESTAURANTES:
            keys = [restaurante_key]
        else:
            return jsonify({"erro": "Restaurante inválido"}), 400

        alunos_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        alunos_tmp.close()
        alunos_file.save(alunos_tmp.name)

        resultados_geracao = []

        for key in keys:
            rest = RESTAURANTES[key]
            try:
                info = ler_planilha(alunos_tmp.name, rest["aba"])
                if not info["restaurante"]:
                    info["restaurante"] = rest["nome"]
                if data_periodo:
                    info["datas"] = data_periodo
                    try:
                        from datetime import datetime as _dt
                        _MESES_PT = [
                            "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                            "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
                        ]
                        _parte = data_periodo.split(" a ")[0].strip()
                        _mes = int(_parte.split("/")[1])
                        _hoje = _dt.now()
                        _ano = _hoje.year if _mes <= _hoje.month else _hoje.year - 1
                        info["mes_ano"] = f"{_MESES_PT[_mes - 1]} {_ano}"
                    except Exception:
                        pass

                if key in ("canela_fds_especial", "sao_lazaro_fds_especial", "ondina_fds_especial"):
                    dias_str = request.form.get("dias_especial", "")
                    dias = [d.strip() for d in dias_str.split(",") if d.strip()]
                    if not dias:
                        resultados_geracao.append({
                            "restaurante": rest["nome"],
                            "erro": "Selecione ao menos um dia para o FDS especial.",
                        })
                        continue
                elif rest["aba"] in CONFIG_ABAS:
                    dias = CONFIG_ABAS[rest["aba"]]
                else:
                    dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]

                # Config no diretório do script (mantido para as ferramentas
                # de diagnóstico; a cópia que vale no processamento é a que vai
                # congelada dentro do lote)
                config_path = os.path.join(SCRIPT_DIR, "configs", rest["config"])
                if not os.path.isdir(os.path.join(SCRIPT_DIR, "configs")):
                    config_path = os.path.join(SCRIPT_DIR, rest["config"])

                # O PDF e o snapshot nascem juntos e ficam guardados em lotes/.
                novo_lote, caminho_pdf = gerar_com_lote(
                    info, dias, key, rest["nome"], rest["aba"], config_path
                )

                resultados_geracao.append({
                    "restaurante": rest["nome"],
                    "alunos": novo_lote["total_alunos"],
                    "paginas": novo_lote["total_paginas"],
                    "arquivo": caminho_pdf,
                    "lote_id": novo_lote["lote_id"],
                    "avisos": novo_lote["avisos"],
                })

            except Exception as e:
                resultados_geracao.append({
                    "restaurante": rest["nome"],
                    "erro": str(e),
                })

        try:
            os.unlink(alunos_tmp.name)
        except Exception:
            pass

        pdfs_ok = [r for r in resultados_geracao if "arquivo" in r]
        if pdfs_ok:
            app.config["ULTIMO_RESULTADO"] = pdfs_ok[0]["arquivo"]
            app.config["ULTIMO_NOME"] = f"{pdfs_ok[0]['lote_id']}.pdf"

        return jsonify({
            "sucesso": True,
            "resultados": [{
                "restaurante": r["restaurante"],
                "alunos": r.get("alunos", 0),
                "paginas": r.get("paginas", 0),
                "lote_id": r.get("lote_id", ""),
                "avisos": r.get("avisos", []),
                "erro": r.get("erro"),
            } for r in resultados_geracao],
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/revisar_ambiguos")
def rota_revisar_ambiguos():
    dados = app.config.get("REVISAO_DATA")
    if not dados:
        return "Nenhum scan processado ainda.", 404
    ambiguos = extrair_recortes(
        dados["paginas_alinhadas"],
        dados["binarios"],
        dados["config"],
        dados["resultados_por_pagina"],
        dados["alunos"],
    )
    if not ambiguos:
        return "<h2>Nenhum caso ambíguo encontrado.</h2>", 200
    html = gerar_html_revisao(
        ambiguos,
        [],
        dados["alunos"],
        dados["dias"],
        None,
        submit_url="/aplicar_correcoes",
    )
    return html


@app.route("/aplicar_correcoes", methods=["POST"])
def rota_aplicar_correcoes():
    dados = app.config.get("REVISAO_DATA")
    if not dados:
        return jsonify({"erro": "Nenhum scan em memória. Processe novamente."}), 400
    try:
        correcoes = request.get_json(force=True)
        resultados = dados["resultados"]
        aplicar_correcoes(resultados, correcoes)
        contagem = contar_presencas(resultados, dados["dias"])
        saida_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        saida_tmp.close()
        exportar_xlsx(contagem, dados["alunos"], dados["dias"], saida_tmp.name)
        app.config["ULTIMO_RESULTADO"] = saida_tmp.name
        app.config["ULTIMO_NOME"] = f"presencas_{dados['restaurante_key']}.xlsx"

        # A revisão manual é a palavra final: ela tem que reescrever o Sheets,
        # não só o xlsx local. Uma falha aqui precisa aparecer — engolir a
        # exceção deixava o operador achando que a correção tinha subido.
        periodo = app.config.get("ULTIMO_PERIODO")
        sheets_resp = {}
        if periodo:
            try:
                sheets_resp = exportar_para_sheets(
                    contagem, dados["alunos"], dados["dias"],
                    dados["restaurante_key"], periodo, forcar=True,
                )
            except Exception as e:
                traceback.print_exc()
                sheets_resp = {"ok": False, "erro": str(e)}

            if dados.get("lote_id"):
                lote_mod.registrar_processamento(
                    dados["lote_id"], periodo,
                    sincronizado_sheets=bool(sheets_resp.get("ok")),
                    detalhe="revisão manual aplicada",
                )
        else:
            sheets_resp = {
                "ok": False,
                "erro": "Nenhum período em memória — correções salvas apenas no xlsx.",
            }

        return jsonify({
            "sucesso": True,
            "sheets_ok": bool(sheets_resp.get("ok")),
            "sheets_erro": sheets_resp.get("erro", ""),
            "sheets_aba": sheets_resp.get("aba", ""),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/sheets_exportar", methods=["POST"])
def rota_sheets_exportar():
    dados = app.config.get("REVISAO_DATA")
    periodo = app.config.get("ULTIMO_PERIODO")

    if not dados or not periodo:
        return jsonify({"erro": "Nenhum scan em memória. Processe novamente."}), 400

    try:
        contagem = contar_presencas(dados["resultados"], dados["dias"])
        resultado = exportar_para_sheets(
            contagem, dados["alunos"], dados["dias"],
            dados["restaurante_key"], periodo, forcar=True,
        )

        if dados.get("lote_id"):
            lote_mod.registrar_processamento(
                dados["lote_id"], periodo,
                sincronizado_sheets=bool(resultado.get("ok")),
                detalhe="substituição manual da semana",
            )

        if resultado.get("ok"):
            return jsonify({"sucesso": True, "aba": resultado.get("aba", "")})
        else:
            return jsonify({"erro": resultado.get("erro", "Erro desconhecido")}), 500

    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/download")
def rota_download():
    caminho = app.config.get("ULTIMO_RESULTADO")
    nome = app.config.get("ULTIMO_NOME", "resultado")
    if not caminho or not os.path.exists(caminho):
        return "Nenhum arquivo disponível", 404
    return send_file(caminho, as_attachment=True, download_name=nome)


@app.route("/lotes")
def rota_lotes():
    """Lotes disponíveis para processar, mais recentes primeiro."""
    restaurante_key = request.args.get("restaurante") or None
    itens = lote_mod.listar_lotes(restaurante_key, limite=60)
    return jsonify({"lotes": itens})


@app.route("/lote/<lote_id>/pdf")
def rota_download_lote(lote_id):
    """
    Reimprime o lote exatamente como saiu da primeira vez.

    Guardar o PDF junto do snapshot é o que impede o problema de voltar: se a
    folha física sumir ou vier rasgada, dá para reimprimir a mesma lista, na
    mesma ordem, sem passar de novo pela planilha viva.
    """
    caminho = lote_mod.garantir_pdf_local(lote_id)
    if not caminho:
        return "PDF deste lote não está guardado", 404
    return send_file(caminho, as_attachment=True, download_name=f"{lote_id}.pdf")


@app.route("/recuperar_lotes", methods=["POST"])
def rota_recuperar_lotes():
    """
    Recupera o lote (roster) de scans já impressos e preenchidos que não têm
    lote localmente — casando cada scan com seu template pelo período (mesma
    lógica de corrigir_passivo.py, já testada) e caindo para OCR do scan
    quando não há template. Não grava nada no Sheets: só cria o lote, que
    depois é processado normalmente pela aba "Processar scan".
    """
    dados = request.get_json(silent=True) or request.form
    pasta_scans = (dados.get("pasta_scans") or "").strip()
    pasta_templates = (dados.get("pasta_templates") or "").strip()
    restaurante_filtro = (dados.get("restaurante") or "").strip() or None

    if not pasta_scans or not os.path.isdir(pasta_scans):
        return jsonify({"erro": "Pasta de scans não encontrada."}), 400
    if pasta_templates and not os.path.isdir(pasta_templates):
        return jsonify({"erro": "Pasta de templates não encontrada."}), 400

    try:
        grupos = corrigir_passivo._agrupar_scans(pasta_scans)
        templates = (corrigir_passivo._indexar_pdfs(pasta_templates, exigir_formulario=True)
                     if pasta_templates else [])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": f"Falha ao ler as pastas: {e}"}), 500

    resultados = []
    for (restaurante_key, periodo), grupo in grupos.items():
        if restaurante_filtro and restaurante_key != restaurante_filtro:
            continue

        nome_rest = RESTAURANTES.get(restaurante_key, {}).get("nome", restaurante_key)
        item = {"restaurante": nome_rest, "periodo": periodo}

        if grupo.get("paginas_faltando"):
            item["status"] = "incompleto"
            item["detalhe"] = (
                f"Faltam as páginas {sorted(grupo['paginas_faltando'])} do maço "
                "— reescaneie antes de recuperar (senão a presença de quem "
                "estava nessas páginas se perde)."
            )
            resultados.append(item)
            continue

        template_pdf = (corrigir_passivo._melhor_arquivo(templates, restaurante_key, periodo)
                         if templates else None)
        linha = {
            "restaurante": restaurante_key,
            "periodo": periodo,
            "template_pdf": template_pdf or "",
            "scan_pdf": corrigir_passivo.SEPARADOR_SCANS.join(grupo["arquivos"]),
            "lote_id": "",
        }

        try:
            lote_id, origem = corrigir_passivo._obter_lote(linha, None)
        except Exception as e:
            item["status"] = "erro"
            item["detalhe"] = str(e)
            resultados.append(item)
            continue

        lote = lote_mod.carregar_lote(lote_id)
        item["lote_id"] = lote_id
        item["total_alunos"] = lote.get("total_alunos", 0)
        item["status"] = origem  # "reaproveitado" | "recuperado_pdf" | "recuperado_ocr"
        item["avisos"] = lote.get("avisos", [])
        if origem == lote_mod.ORIGEM_OCR:
            item["sem_match"] = [
                {"numero": a["numero"], "lido": a.get("ocr_bruto", "")}
                for a in lote.get("alunos", [])
                if a.get("situacao") == "sem_match"
            ]
        resultados.append(item)

    resultados.sort(key=lambda r: (r["restaurante"], r["periodo"]))
    return jsonify({"resultados": resultados})


@app.route("/preview/<int:idx>")
def rota_preview(idx):
    dados = app.config.get("REVISAO_DATA")
    config = app.config.get("ULTIMO_CONFIG")

    if not dados or not config:
        return "Sem dados de preview", 404

    paginas_alinhadas = dados.get("paginas_alinhadas", [])
    binarios = dados.get("binarios", [])
    resultados_por_pagina = dados.get("resultados_por_pagina", [])

    if idx < 0 or idx >= len(paginas_alinhadas):
        return "Página inválida", 404

    try:
        # Usa os dados já processados e ordenados — não re-carrega o PDF
        alinhada = paginas_alinhadas[idx]
        resultados = resultados_por_pagina[idx]

        vis = alinhada.copy()
        pos = carregar_posicoes(config)
        threshold = _get_threshold(config)
        offset_y = _get_offset_y(config)

        verde = (0, 200, 0)
        azul = (0, 0, 180)
        amarelo = (0, 200, 255)
        amb_min, amb_max = get_zona_ambigua(config)

        for local_i, aluno in enumerate(resultados):
            cy = int(pos["primeira_linha_y_px"] + offset_y + local_i * pos["linha_altura_px"])
            raio = int(pos["raio_px"])

            for dia in pos["dias"]:
                ax, jx = pos["circulos_x"][dia]
                for cx_float, tipo in [(ax, "almoco"), (jx, "janta")]:
                    cx = int(cx_float)
                    pct = aluno["dias"][dia][f"{tipo}_pct"]

                    if pct > amb_max:
                        cor = verde
                    elif pct < amb_min:
                        cor = azul
                    else:
                        cor = amarelo

                    cv2.circle(vis, (cx, cy), raio + 2, cor, 2)
                    cv2.putText(vis, f"{int(pct * 100)}", (cx - 9, cy + 3),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.25, cor, 1)

        _, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(buf.tobytes(), mimetype="image/jpeg")

    except Exception as e:
        traceback.print_exc()
        return f"Erro ao gerar preview: {e}", 500


# --- HTML ---

HTML_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sistema de presença</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;
    background: #f8f8f6;
    color: #1a1a1a;
    min-height: 100vh;
    display: flex;
    justify-content: center;
    padding: 3rem 1rem;
}
.container { width: 100%; max-width: 480px; }

.header { margin-bottom: 2rem; }
.header h1 { font-size: 20px; font-weight: 600; margin-bottom: 2px; }
.header p { font-size: 14px; color: #666; }

.tabs {
    display: flex;
    border-bottom: 1px solid #e5e5e3;
    margin-bottom: 2rem;
    gap: 0;
}
.tab {
    padding: 10px 20px;
    font-size: 14px;
    color: #888;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    background: none;
    border-top: none; border-left: none; border-right: none;
    font-family: inherit;
    transition: all 0.15s;
}
.tab:hover { color: #555; }
.tab.active { color: #1a1a1a; font-weight: 500; border-bottom-color: #1a1a1a; }

.panel { display: none; }
.panel.active { display: block; }

.section { margin-bottom: 1.5rem; }
.label { font-size: 13px; font-weight: 500; color: #666; margin-bottom: 8px; }

.radio-group { display: flex; gap: 8px; flex-wrap: wrap; }
.radio {
    padding: 8px 16px;
    border-radius: 8px;
    border: 1px solid #e5e5e3;
    font-size: 14px;
    color: #888;
    background: #fff;
    cursor: pointer;
    transition: all 0.15s;
    font-family: inherit;
}
.radio:hover { border-color: #ccc; color: #555; }
.radio.selected { border-color: #2563eb; background: #eff6ff; color: #2563eb; }
.radio input { display: none; }

.upload-zone {
    border: 1.5px dashed #d4d4d2;
    border-radius: 12px;
    padding: 2rem;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s;
    position: relative;
}
.upload-zone:hover { border-color: #aaa; }
.upload-zone.dragover { border-color: #2563eb; background: #fafcff; }
.upload-zone input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.upload-icon { margin-bottom: 8px; color: #bbb; }
.upload-text { font-size: 14px; color: #888; }
.upload-hint { font-size: 12px; color: #aaa; margin-top: 4px; }

.file-pill {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    background: #f0f0ee;
    border-radius: 8px;
}
.file-icon {
    width: 32px; height: 32px;
    border-radius: 8px;
    background: #eff6ff;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 600; color: #2563eb;
}
.file-name { font-size: 14px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-remove { font-size: 12px; color: #e55; cursor: pointer; }
.file-remove:hover { text-decoration: underline; }

.scan-parte-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: #f0f0ee;
    border-radius: 8px;
    margin-bottom: 6px;
}
.scan-parte-num {
    width: 20px;
    font-size: 12px;
    font-weight: 600;
    color: #aaa;
    text-align: right;
    flex-shrink: 0;
}
.scan-parte-icon {
    width: 30px; height: 30px;
    border-radius: 6px;
    background: #eff6ff;
    display: flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 700; color: #2563eb;
    flex-shrink: 0;
}
.scan-parte-name {
    flex: 1;
    font-size: 13px;
    color: #333;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.scan-parte-acoes { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
.scan-ord-btn {
    border: 1px solid #ddd;
    background: #fff;
    border-radius: 5px;
    width: 24px; height: 24px;
    font-size: 12px;
    cursor: pointer;
    color: #555;
    padding: 0;
    display: flex; align-items: center; justify-content: center;
}
.scan-ord-btn:hover:not(:disabled) { background: #f0f0ee; }
.scan-ord-btn:disabled { opacity: 0.35; cursor: default; }
.scan-parte-remove {
    border: none;
    background: none;
    font-size: 13px;
    color: #e55;
    cursor: pointer;
    padding: 0 2px;
    line-height: 1;
}
.scan-parte-remove:hover { color: #c00; }
.scan-partes-merge-hint {
    font-size: 12px;
    color: #2563eb;
    background: #eff6ff;
    border-radius: 6px;
    padding: 6px 10px;
    margin-top: 4px;
}

.btn {
    width: 100%;
    padding: 12px;
    border-radius: 8px;
    border: none;
    background: #1a1a1a;
    color: #fff;
    font-size: 15px;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    transition: opacity 0.15s;
    margin-top: 0.5rem;
}
.btn:hover { opacity: 0.85; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }

.result {
    margin-top: 1.5rem;
    padding: 1.25rem;
    background: #f0f0ee;
    border-radius: 12px;
    display: none;
}
.result.show { display: block; }
.result-row {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    font-size: 14px;
}
.result-label { color: #888; }
.result-value { font-weight: 500; }
.result-ok { color: #16a34a; }
.result-warn { color: #d97706; }

.btn-download {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    width: 100%;
    padding: 12px;
    border-radius: 8px;
    border: 1px solid #bbf7d0;
    background: #f0fdf4;
    color: #16a34a;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    margin-top: 1rem;
    text-decoration: none;
}
.btn-download:hover { background: #dcfce7; }

.btn-revisar {
    display: block;
    width: 100%;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid #fde68a;
    background: #fffbeb;
    color: #b45309;
    font-size: 13px;
    font-weight: 500;
    text-align: center;
    text-decoration: none;
    margin-top: 8px;
}
.btn-revisar:hover { background: #fef3c7; }

.btn-substituir {
    display: block;
    width: 100%;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid #e9d5ff;
    background: #faf5ff;
    color: #7c3aed;
    font-size: 13px;
    font-weight: 500;
    text-align: center;
    cursor: pointer;
    font-family: inherit;
    margin-top: 8px;
}
.btn-substituir:hover { background: #f3e8ff; }
.btn-substituir:disabled { opacity: 0.5; cursor: not-allowed; }

.error-msg {
    margin-top: 1rem;
    padding: 1rem;
    background: #fef2f2;
    border-radius: 8px;
    color: #dc2626;
    font-size: 14px;
    display: none;
}
.error-msg.show { display: block; }

.loading {
    display: none;
    text-align: center;
    padding: 2rem;
    color: #888;
    font-size: 14px;
}
.loading.show { display: block; }
.spinner {
    width: 24px; height: 24px;
    border: 2.5px solid #e5e5e3;
    border-top-color: #1a1a1a;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 12px;
}
@keyframes spin { to { transform: rotate(360deg); } }

.template-results { margin-top: 1rem; }
.template-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    background: #f0f0ee;
    border-radius: 8px;
    margin-bottom: 8px;
    font-size: 14px;
}
.template-info { color: #888; font-size: 12px; }
.template-download {
    font-size: 13px;
    color: #2563eb;
    cursor: pointer;
    text-decoration: none;
}
.template-download:hover { text-decoration: underline; }

.preview-section { margin-top: 1.5rem; }
.preview-nav { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 0.75rem; }
.preview-nav-btn {
    padding: 5px 12px;
    border-radius: 6px;
    border: 1px solid #e5e5e3;
    font-size: 12px;
    color: #888;
    background: #fff;
    cursor: pointer;
    font-family: inherit;
    transition: all 0.15s;
}
.preview-nav-btn:hover { border-color: #ccc; color: #555; }
.preview-nav-btn.active { border-color: #2563eb; background: #eff6ff; color: #2563eb; }
.preview-img-wrap {
    background: #f0f0ee;
    border-radius: 8px;
    overflow: hidden;
    position: relative;
    min-height: 80px;
    display: flex;
    align-items: center;
    justify-content: center;
}
.preview-img-wrap img { width: 100%; display: block; border-radius: 8px; }
.preview-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(240,240,238,0.85);
}
.preview-legend {
    display: flex;
    gap: 14px;
    margin-bottom: 0.5rem;
    font-size: 12px;
    color: #666;
}
.preview-legend span { display: flex; align-items: center; gap: 5px; }
.legend-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    display: inline-block;
    border: 2px solid;
}
</style>
</head>
<body>

<div class="container">
    <div class="header" style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;">
        <div>
            <h1>Sistema de presença</h1>
            <p>Programa Canela — PROAE/UFBA</p>
        </div>
        <a href="http://localhost:5001" target="_blank"
           style="display:inline-flex;align-items:center;gap:6px;padding:7px 14px;
                  border-radius:8px;border:1px solid #e5e5e3;background:#fff;
                  font-size:13px;color:#555;text-decoration:none;white-space:nowrap;
                  font-family:inherit;transition:all .15s;"
           onmouseover="this.style.borderColor='#2563eb';this.style.color='#2563eb';"
           onmouseout="this.style.borderColor='#e5e5e3';this.style.color='#555';">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
                <rect x="3" y="14" width="7" height="7"/><circle cx="17.5" cy="17.5" r="3.5"/>
            </svg>
            Painel de irregulares
        </a>
    </div>

    <div class="tabs">
        <button class="tab active" onclick="switchTab('template', this)">Gerar template</button>
        <button class="tab" onclick="switchTab('processar', this)">Processar scan</button>
        <button class="tab" onclick="switchTab('recuperar', this)">Recuperar lote</button>
    </div>

    <!-- ABA: GERAR TEMPLATE -->
    <div class="panel active" id="panel-template">
        <form id="form-template" onsubmit="return submitTemplate(event)">
            <div class="section">
                <div class="label">Restaurante</div>
                <div class="radio-group">
                    <label class="radio selected" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="canela" checked> Canela
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="ondina"> Ondina
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="sao_lazaro"> São Lázaro
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="canela_fds"> Canela FDS
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="sao_lazaro_fds"> S. Lázaro FDS
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="canela_fds_especial"> Canela FDS Esp.
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="sao_lazaro_fds_especial"> S. Lázaro FDS Esp.
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="ondina_fds_especial"> Ondina Esp.
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-template')">
                        <input type="radio" name="rest-template" value="todos"> Todos
                    </label>
                </div>
            </div>

            <div class="section">
                <div class="label">Planilha de alunos (.xlsx)</div>
                <div id="upload-template-zone" class="upload-zone">
                    <input type="file" name="alunos" accept=".xlsx" onchange="fileSelected(this, 'template')">
                    <div class="upload-icon">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 18h16"/></svg>
                    </div>
                    <div class="upload-text">Arraste o arquivo ou clique para selecionar</div>
                    <div class="upload-hint">.xlsx exportado do Google Sheets</div>
                </div>
                <div id="file-template" class="file-pill" style="display:none;">
                    <div class="file-icon">XLS</div>
                    <div class="file-name" id="file-template-name"></div>
                    <div class="file-remove" onclick="removeFile('template')">remover</div>
                </div>
            </div>

            <div class="section">
                <div class="label">Data do período <span style="font-weight:400;color:#aaa">(opcional — ex: 10/05 ou 05/05 a 09/05)</span></div>
                <input type="text" name="data_periodo" placeholder="10/05"
                       style="width:200px;padding:8px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;">
            </div>

            <div class="section" id="dias-especial-section" style="display:none;">
                <div class="label">Dias da lista especial <span style="font-weight:400;color:#aaa">(selecione todos os dias do feriado emendado)</span></div>
                <div class="radio-group" id="dias-especial-group">
                    <label class="radio" data-dia="Segunda" onclick="toggleDiaEspecial(this)">Segunda</label>
                    <label class="radio" data-dia="Terça" onclick="toggleDiaEspecial(this)">Terça</label>
                    <label class="radio" data-dia="Quarta" onclick="toggleDiaEspecial(this)">Quarta</label>
                    <label class="radio" data-dia="Quinta" onclick="toggleDiaEspecial(this)">Quinta</label>
                    <label class="radio" data-dia="Sexta" onclick="toggleDiaEspecial(this)">Sexta</label>
                    <label class="radio" data-dia="Sábado" onclick="toggleDiaEspecial(this)">Sábado</label>
                    <label class="radio" data-dia="Domingo" onclick="toggleDiaEspecial(this)">Domingo</label>
                </div>
                <input type="hidden" name="dias_especial" id="input-dias-especial">
            </div>

            <button type="submit" class="btn" id="btn-template">Gerar template</button>
        </form>

        <div class="loading" id="loading-template">
            <div class="spinner"></div>
            Gerando template...
        </div>

        <div class="error-msg" id="error-template"></div>

        <div class="template-results" id="result-template"></div>
    </div>

    <!-- ABA: PROCESSAR SCAN -->
    <div class="panel" id="panel-processar">
        <form id="form-processar" onsubmit="return submitProcessar(event)">
            <div class="section">
                <div class="label">Restaurante</div>
                <div class="radio-group">
                    <label class="radio selected" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="canela" checked> Canela
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="ondina"> Ondina
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="sao_lazaro"> São Lázaro
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="canela_fds"> Canela FDS
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="sao_lazaro_fds"> S. Lázaro FDS
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="canela_fds_especial"> Canela FDS Esp.
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="sao_lazaro_fds_especial"> S. Lázaro FDS Esp.
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-processar')">
                        <input type="radio" name="rest-processar" value="ondina_fds_especial"> Ondina Esp.
                    </label>
                </div>
            </div>

            <div class="section">
                <div class="label">Scan escaneado (.pdf)</div>
                <div id="upload-scan-zone" class="upload-zone" ondragover="event.preventDefault()" ondrop="onScanDrop(event)">
                    <input type="file" accept=".pdf" multiple onchange="onScanSelected(this)">
                    <div class="upload-icon">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 18h16"/></svg>
                    </div>
                    <div class="upload-text" id="scan-zone-text">Arraste o arquivo ou clique para selecionar</div>
                    <div class="upload-hint">.pdf escaneado · selecione vários para mesclar em ordem</div>
                </div>
                <div id="scan-partes-lista" style="display:none;margin-top:8px;"></div>
                <div id="scan-partes-info" style="font-size:12px;color:#888;margin-top:6px;"></div>
            </div>

            <div class="section">
                <div class="label">Lote impresso que originou este scan</div>
                <select id="select-lote" onchange="onLoteChange()"
                        style="width:100%;padding:9px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;background:#fff;">
                    <option value="">Carregando lotes...</option>
                </select>
                <div id="lote-detalhe" style="font-size:12px;color:#888;margin-top:6px;"></div>
                <div class="upload-hint" style="margin-top:6px;">
                    O lote guarda quem estava em cada linha no dia da impressão.
                    É ele que garante que a presença vá para a pessoa certa mesmo
                    que a planilha tenha mudado depois. O código do lote está
                    impresso no rodapé da folha.
                </div>
            </div>

            <div class="section">
                <div style="font-size:12px;color:#999;cursor:pointer;user-select:none;"
                     onclick="toggleLegado()" id="legado-toggle">▸ Folha antiga, sem código de lote?</div>
                <div id="legado-box" style="display:none;margin-top:10px;">
                    <div style="background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;padding:10px 12px;font-size:12px;color:#7a5a10;margin-bottom:10px;">
                        Sem lote, os nomes voltam a sair da planilha enviada agora.
                        Se ela mudou desde a impressão, as presenças saem trocadas.
                        Prefira reconstruir o lote com <code>recuperar_lote.py</code>.
                    </div>
                    <div class="label">Planilha de alunos (.xlsx)</div>
                    <div id="upload-alunos-zone" class="upload-zone">
                        <input type="file" name="alunos" accept=".xlsx" onchange="fileSelected(this, 'alunos')">
                        <div class="upload-icon">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 18h16"/></svg>
                        </div>
                        <div class="upload-text">Arraste o arquivo ou clique para selecionar</div>
                        <div class="upload-hint">.xlsx exportado do Google Sheets</div>
                    </div>
                    <div id="file-alunos" class="file-pill" style="display:none;">
                        <div class="file-icon">XLS</div>
                        <div class="file-name" id="file-alunos-name"></div>
                        <div class="file-remove" onclick="removeFile('alunos')">remover</div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="label">Período da semana <span style="font-weight:400;color:#aaa">(ex: 05/05 a 09/05)</span></div>
                <input type="text" name="periodo_semana" placeholder="05/05 a 09/05"
                       style="width:200px;padding:8px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;">
            </div>

            <div class="section">
                <div class="label">Página inicial do scan <span style="font-weight:400;color:#aaa">(só preencha se NÃO começar na página 1 do lote impresso)</span></div>
                <input type="number" name="pagina_inicial" min="1" value="1" placeholder="1"
                       style="width:100px;padding:8px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;">
            </div>

            <button type="submit" class="btn" id="btn-processar">Processar presenças</button>
        </form>

        <div class="loading" id="loading-processar">
            <div class="spinner"></div>
            Processando presenças... isso pode levar alguns minutos.
        </div>

        <div class="error-msg" id="error-processar"></div>

        <div class="result" id="result-processar">
            <div class="result-row">
                <span class="result-label">Alunos processados</span>
                <span class="result-value" id="r-alunos"></span>
            </div>
            <div class="result-row">
                <span class="result-label">Com presença</span>
                <span class="result-value result-ok" id="r-presenca"></span>
            </div>
            <div class="result-row">
                <span class="result-label">Casos ambíguos</span>
                <span class="result-value" id="r-ambiguos"></span>
            </div>
            <div class="result-row" id="lote-row" style="display:none;">
                <span class="result-label">Lote usado</span>
                <span class="result-value" id="r-lote"></span>
            </div>
            <div class="result-row" id="sheets-row" style="display:none;">
                <span class="result-label">Google Sheets</span>
                <span class="result-value" id="sheets-msg"></span>
            </div>
            <div id="avisos-box" style="display:none;margin-top:10px;"></div>
            <a href="/download" class="btn-download" id="btn-download-proc">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4v12m0 0l4-4m-4 4l-4-4M4 18h16"/></svg>
                <span id="download-nome">Baixar planilha</span>
            </a>
            <a href="/revisar_ambiguos" target="_blank" class="btn-revisar" id="btn-revisar" style="display:none;">
                Revisar casos ambíguos →
            </a>
            <button type="button" onclick="substituirSemana()" class="btn-substituir" id="btn-substituir" style="display:none;">
                Substituir semana no Google Sheets
            </button>
        </div>

        <div class="preview-section" id="preview-section" style="display:none;">
            <div class="label" style="margin-bottom:8px;">Visualizar bolhas</div>
            <div class="preview-legend">
                <span><span class="legend-dot" style="border-color:#00c800;"></span> Marcado</span>
                <span><span class="legend-dot" style="border-color:#0000b4;"></span> Vazio</span>
                <span><span class="legend-dot" style="border-color:#00c8ff;"></span> Ambíguo</span>
            </div>
            <div class="preview-nav" id="preview-nav"></div>
            <div class="preview-img-wrap" id="preview-wrap">
                <img id="preview-img" src="" alt="Preview das bolhas" style="display:none;">
                <div class="preview-overlay" id="preview-overlay">
                    <div class="spinner"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- ABA: RECUPERAR LOTE -->
    <div class="panel" id="panel-recuperar">
        <form id="form-recuperar" onsubmit="return submitRecuperar(event)">
            <div class="section">
                <div class="label">Restaurante <span style="font-weight:400;color:#aaa">(opcional — deixe em "Todos" pra varrer tudo)</span></div>
                <div class="radio-group">
                    <label class="radio selected" onclick="selectRadio(this, 'rest-recuperar')">
                        <input type="radio" name="rest-recuperar" value="" checked> Todos
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-recuperar')">
                        <input type="radio" name="rest-recuperar" value="canela"> Canela
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-recuperar')">
                        <input type="radio" name="rest-recuperar" value="ondina"> Ondina
                    </label>
                    <label class="radio" onclick="selectRadio(this, 'rest-recuperar')">
                        <input type="radio" name="rest-recuperar" value="sao_lazaro"> São Lázaro
                    </label>
                </div>
            </div>

            <div class="section">
                <div class="label">Pasta com os scans preenchidos</div>
                <input type="text" id="input-pasta-scans" placeholder="C:\\caminho\\para\\os\\scans"
                       style="width:100%;max-width:480px;padding:8px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;box-sizing:border-box;">
            </div>

            <div class="section">
                <div class="label">Pasta com os templates originais <span style="font-weight:400;color:#aaa">(opcional — sem ela, tenta reconstruir lendo o próprio scan)</span></div>
                <input type="text" id="input-pasta-templates" placeholder="C:\\caminho\\para\\os\\templates"
                       style="width:100%;max-width:480px;padding:8px 12px;border-radius:8px;border:1px solid #e5e5e3;font-size:14px;font-family:inherit;box-sizing:border-box;">
            </div>

            <div style="font-size:12px;color:#999;margin-bottom:14px;">
                O sistema lê o cabeçalho impresso em cada folha pra descobrir sozinho de qual
                restaurante e período é cada scan — não importa o nome do arquivo. Com muitos
                scans pode levar alguns minutos.
            </div>

            <button type="submit" class="btn" id="btn-recuperar">Buscar e recuperar</button>
        </form>

        <div class="loading" id="loading-recuperar">
            <div class="spinner"></div>
            Lendo as pastas e recuperando os lotes...
        </div>

        <div class="error-msg" id="error-recuperar"></div>

        <div class="template-results" id="result-recuperar"></div>
    </div>
</div>

<script>
function switchTab(tab, btn) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));

    btn.classList.add('active');
    document.getElementById('panel-' + tab).classList.add('active');
}

function selectRadio(el, name) {
    el.closest('.radio-group').querySelectorAll('.radio').forEach(r => r.classList.remove('selected'));
    el.classList.add('selected');
    el.querySelector('input').checked = true;

    if (name === 'rest-template') {
        var val = el.querySelector('input').value;
        var isFdsEsp = val === 'canela_fds_especial' || val === 'sao_lazaro_fds_especial' || val === 'ondina_fds_especial';
        document.getElementById('dias-especial-section').style.display = isFdsEsp ? '' : 'none';
        if (!isFdsEsp) {
            document.querySelectorAll('#dias-especial-group .radio').forEach(function(d) { d.classList.remove('selected'); });
        }
    }

    if (name === 'rest-processar') {
        carregarLotes();
    }
}

function toggleDiaEspecial(el) {
    el.classList.toggle('selected');
}

// --- Scan multi-parte ---
var scanFiles = [];

function onScanSelected(input) {
    for (var i = 0; i < input.files.length; i++) {
        scanFiles.push(input.files[i]);
    }
    input.value = '';
    renderScanLista();
}

function onScanDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    document.getElementById('upload-scan-zone').classList.remove('dragover');
    var files = e.dataTransfer.files;
    for (var i = 0; i < files.length; i++) {
        if (files[i].type === 'application/pdf' || files[i].name.endsWith('.pdf')) {
            scanFiles.push(files[i]);
        }
    }
    renderScanLista();
}

function removeScanParte(idx) {
    scanFiles.splice(idx, 1);
    renderScanLista();
}

function moverScanParte(idx, dir) {
    var novo = idx + dir;
    if (novo < 0 || novo >= scanFiles.length) return;
    var tmp = scanFiles[idx];
    scanFiles[idx] = scanFiles[novo];
    scanFiles[novo] = tmp;
    renderScanLista();
}

function renderScanLista() {
    var lista = document.getElementById('scan-partes-lista');
    var info = document.getElementById('scan-partes-info');
    var zoneText = document.getElementById('scan-zone-text');

    if (scanFiles.length === 0) {
        lista.style.display = 'none';
        lista.innerHTML = '';
        info.textContent = '';
        zoneText.textContent = 'Arraste o arquivo ou clique para selecionar';
        return;
    }

    zoneText.textContent = scanFiles.length > 1 ? '+ Adicionar mais uma parte' : '+ Adicionar outra parte';

    var html = '';
    for (var i = 0; i < scanFiles.length; i++) {
        var isFirst = i === 0;
        var isLast = i === scanFiles.length - 1;
        var n = i;
        html += '<div class="scan-parte-item">' +
            '<span class="scan-parte-num">' + (i + 1) + '</span>' +
            '<span class="scan-parte-icon">PDF</span>' +
            '<span class="scan-parte-name">' + scanFiles[i].name + '</span>' +
            '<div class="scan-parte-acoes">' +
            (scanFiles.length > 1 ? '<button type="button" class="scan-ord-btn" onclick="moverScanParte(' + n + ',-1)"' + (isFirst ? ' disabled' : '') + '>↑</button>' +
             '<button type="button" class="scan-ord-btn" onclick="moverScanParte(' + n + ',1)"' + (isLast ? ' disabled' : '') + '>↓</button>' : '') +
            '<button type="button" class="scan-parte-remove" onclick="removeScanParte(' + n + ')">✕</button>' +
            '</div></div>';
    }

    if (scanFiles.length > 1) {
        html += '<div class="scan-partes-merge-hint">As ' + scanFiles.length + ' partes serão mescladas em ordem antes do processamento</div>';
    }

    lista.innerHTML = html;
    lista.style.display = 'block';
    info.textContent = '';
}
// --- fim scan multi-parte ---

function fileSelected(input, tipo) {
    if (!input.files.length) return;
    var file = input.files[0];
    var zone = document.getElementById('upload-' + tipo + '-zone');
    var pill = document.getElementById('file-' + tipo);
    var nameEl = document.getElementById('file-' + tipo + '-name');

    zone.style.display = 'none';
    pill.style.display = 'flex';
    nameEl.textContent = file.name;
}

function removeFile(tipo) {
    var zone = document.getElementById('upload-' + tipo + '-zone');
    var pill = document.getElementById('file-' + tipo);
    var input = zone.querySelector('input');

    zone.style.display = '';
    pill.style.display = 'none';
    input.value = '';
}

function submitTemplate(e) {
    e.preventDefault();

    var form = document.getElementById('form-template');
    var data = new FormData(form);
    data.set('restaurante', form.querySelector('input[name="rest-template"]:checked').value);

    var alunos = form.querySelector('input[name="alunos"]');
    if (!alunos.files.length) {
        showError('template', 'Selecione a planilha de alunos.');
        return false;
    }
    data.set('alunos', alunos.files[0]);

    var isFdsEsp = data.get('restaurante').endsWith('_fds_especial');
    if (isFdsEsp) {
        var dias = [];
        document.querySelectorAll('#dias-especial-group .radio.selected').forEach(function(el) {
            dias.push(el.getAttribute('data-dia'));
        });
        if (!dias.length) {
            showError('template', 'Selecione ao menos um dia para o FDS especial.');
            return false;
        }
        data.set('dias_especial', dias.join(','));
    }

    showLoading('template', true);
    hideError('template');
    document.getElementById('result-template').innerHTML = '';

    fetch('/gerar_template', { method: 'POST', body: data })
        .then(r => r.json())
        .then(data => {
            showLoading('template', false);
            if (data.erro) {
                showError('template', data.erro);
                return;
            }

            var html = '';
            data.resultados.forEach(function(r) {
                if (r.erro) {
                    html += '<div class="template-item"><span>' + r.restaurante + '</span><span style="color:#e55;">' + r.erro + '</span></div>';
                } else {
                    html += '<div class="template-item"><div><div>' + r.restaurante + '</div>' +
                            '<div class="template-info">' + r.alunos + ' alunos, ' + r.paginas + ' páginas</div>' +
                            '<div class="template-info" style="font-family:monospace;">lote ' + r.lote_id + '</div>' +
                            '</div><a href="/lote/' + r.lote_id + '/pdf" class="template-download">baixar PDF</a></div>';
                    (r.avisos || []).forEach(function(a) {
                        html += '<div style="background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;' +
                                'padding:9px 12px;font-size:12px;color:#7a5a10;margin-top:6px;">' + a + '</div>';
                    });
                }
            });

            document.getElementById('result-template').innerHTML = html;
            // O lote recém-criado precisa aparecer na aba de processar sem F5.
            carregarLotes();
        })
        .catch(err => {
            showLoading('template', false);
            showError('template', 'Erro de conexão: ' + err.message);
        });

    return false;
}

function submitRecuperar(e) {
    e.preventDefault();

    var pastaScans = document.getElementById('input-pasta-scans').value.trim();
    if (!pastaScans) {
        showError('recuperar', 'Informe a pasta com os scans preenchidos.');
        return false;
    }
    var pastaTemplates = document.getElementById('input-pasta-templates').value.trim();
    var rest = document.querySelector('input[name="rest-recuperar"]:checked').value;

    showLoading('recuperar', true);
    hideError('recuperar');
    document.getElementById('result-recuperar').innerHTML = '';

    fetch('/recuperar_lotes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            pasta_scans: pastaScans,
            pasta_templates: pastaTemplates,
            restaurante: rest,
        }),
    })
        .then(r => r.json())
        .then(data => {
            showLoading('recuperar', false);
            if (data.erro) {
                showError('recuperar', data.erro);
                return;
            }
            renderResultadosRecuperar(data.resultados || []);
            // Os lotes recem-recuperados precisam aparecer em "Processar scan" sem F5.
            carregarLotes();
        })
        .catch(function(err) {
            showLoading('recuperar', false);
            showError('recuperar', 'Erro de conexão: ' + err.message);
        });

    return false;
}

var _ROTULOS_RECUPERAR = {
    recuperado_pdf: { texto: 'Recuperado do template', cor: '#1a7a3c' },
    recuperado_ocr: { texto: 'Recuperado lendo o scan', cor: '#b57c10' },
    reaproveitado:  { texto: 'Já existia',              cor: '#666'    },
    incompleto:     { texto: 'Scan incompleto',         cor: '#dc2626' },
    erro:           { texto: 'Erro',                    cor: '#dc2626' },
};

function renderResultadosRecuperar(resultados) {
    var el = document.getElementById('result-recuperar');
    if (!resultados.length) {
        el.innerHTML = '<div style="color:#888;font-size:14px;padding:12px 0;">' +
            'Nenhum scan reconhecido nessa pasta — confira se os PDFs são mesmo os scans preenchidos ' +
            '(o sistema lê o cabeçalho impresso, não o nome do arquivo).</div>';
        return;
    }

    var html = '';
    resultados.forEach(function(r) {
        var info = _ROTULOS_RECUPERAR[r.status] || { texto: r.status, cor: '#666' };
        html += '<div class="template-item" style="flex-direction:column;align-items:stretch;">';
        html += '  <div style="display:flex;justify-content:space-between;">';
        html += '    <div><div>' + r.restaurante + ' — ' + r.periodo + '</div>';
        if (r.total_alunos) {
            html += '      <div class="template-info">' + r.total_alunos + ' pessoas' +
                    (r.lote_id ? '  |  lote ' + r.lote_id : '') + '</div>';
        }
        html += '    </div>';
        html += '    <span style="font-size:12px;font-weight:600;color:' + info.cor + ';white-space:nowrap;">' + info.texto + '</span>';
        html += '  </div>';

        if (r.detalhe) {
            html += '  <div style="font-size:12px;color:' + info.cor + ';margin-top:4px;">' + r.detalhe + '</div>';
        }
        (r.avisos || []).forEach(function(a) {
            html += '  <div style="background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;' +
                    'padding:9px 12px;font-size:12px;color:#7a5a10;margin-top:6px;">' + a + '</div>';
        });
        if (r.sem_match && r.sem_match.length) {
            var linhas = r.sem_match.map(function(s) {
                return 'linha ' + s.numero + ' (leu "' + s.lido + '")';
            }).join(', ');
            html += '  <div style="background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;' +
                    'padding:9px 12px;font-size:12px;color:#7a5a10;margin-top:6px;">' +
                    r.sem_match.length + ' matrícula(s) não bateram com ninguém no cadastro — ' +
                    'confira antes de processar: ' + linhas + '</div>';
        }
        html += '</div>';
    });

    html += '<div style="font-size:13px;color:#666;margin-top:10px;">Prontos? Vá na aba ' +
            '<b>Processar scan</b>, escolha o restaurante — os lotes recuperados aparecem ' +
            'sozinhos na lista.</div>';

    el.innerHTML = html;
}

var lotesCarregados = [];

function carregarLotes() {
    var sel = document.getElementById('select-lote');
    if (!sel) return;
    var rest = document.querySelector('input[name="rest-processar"]:checked').value;
    sel.innerHTML = '<option value="">Carregando...</option>';
    document.getElementById('lote-detalhe').textContent = '';

    fetch('/lotes?restaurante=' + encodeURIComponent(rest))
        .then(r => r.json())
        .then(data => {
            lotesCarregados = data.lotes || [];
            if (!lotesCarregados.length) {
                sel.innerHTML = '<option value="">Nenhum lote impresso para este restaurante</option>';
                document.getElementById('lote-detalhe').innerHTML =
                    '<span style="color:#b57c10;">Gere o template por aqui para que o lote passe a ser registrado.</span>';
                return;
            }
            var html = '<option value="">- selecione o lote -</option>';
            lotesCarregados.forEach(function(l) {
                var rotulo = (l.datas || l.criado_em.slice(0, 10)) +
                             '  |  ' + l.total_alunos + ' alunos' +
                             '  |  ' + l.lote_id;
                if (l.processado) rotulo += '  (ja processado)';
                if (l.origem !== 'geracao') rotulo += '  [' + l.origem + ']';
                html += '<option value="' + l.lote_id + '">' + rotulo + '</option>';
            });
            sel.innerHTML = html;
            // Um unico lote ativo quase sempre e o certo - pre-seleciona.
            var ativos = lotesCarregados.filter(function(l) { return !l.processado; });
            if (ativos.length === 1) {
                sel.value = ativos[0].lote_id;
                onLoteChange();
            }
        })
        .catch(function() {
            sel.innerHTML = '<option value="">Erro ao listar lotes</option>';
        });
}

function onLoteChange() {
    var sel = document.getElementById('select-lote');
    var det = document.getElementById('lote-detalhe');
    var l = lotesCarregados.filter(function(x) { return x.lote_id === sel.value; })[0];
    if (!l) { det.textContent = ''; return; }

    var txt = l.total_alunos + ' pessoas em ' + l.total_paginas + ' pagina(s) - dias: ' +
              (l.dias || []).join(', ') + ' - impresso em ' + l.criado_em.replace('T', ' ');
    if (l.tem_pdf) {
        txt += ' - <a href="/lote/' + l.lote_id + '/pdf" target="_blank" style="color:#666;">reimprimir</a>';
    }
    if (l.processado) {
        txt += '<br><span style="color:#b57c10;">Este lote ja foi processado e sincronizado. ' +
               'Processar de novo vai sobrescrever a semana no Sheets.</span>';
    }
    (l.avisos || []).forEach(function(a) {
        txt += '<br><span style="color:#b57c10;">' + a + '</span>';
    });
    det.innerHTML = txt;
}

function toggleLegado() {
    var box = document.getElementById('legado-box');
    var tog = document.getElementById('legado-toggle');
    var aberto = box.style.display !== 'none';
    box.style.display = aberto ? 'none' : '';
    tog.textContent = (aberto ? '\u25b8' : '\u25be') + ' Folha antiga, sem codigo de lote?';
}

function mostrarAvisos(avisos) {
    var box = document.getElementById('avisos-box');
    if (!avisos || !avisos.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    var html = '';
    avisos.forEach(function(a) {
        html += '<div style="background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;' +
                'padding:9px 12px;font-size:12px;color:#7a5a10;margin-top:6px;">' + a + '</div>';
    });
    box.innerHTML = html;
    box.style.display = '';
}

function submitProcessar(e) {
    e.preventDefault();

    var form = document.getElementById('form-processar');
    var data = new FormData();
    data.set('restaurante', form.querySelector('input[name="rest-processar"]:checked').value);

    var alunos = form.querySelector('input[name="alunos"]');
    var periodo = form.querySelector('input[name="periodo_semana"]').value.trim();
    var paginaInicial = form.querySelector('input[name="pagina_inicial"]').value.trim();
    var loteId = document.getElementById('select-lote').value;

    if (!scanFiles.length) { showError('processar', 'Selecione o PDF do scan.'); return false; }
    if (!loteId && !alunos.files.length) {
        showError('processar', 'Selecione o lote impresso deste scan — ou, se a folha for anterior aos lotes, abra "Folha antiga" e envie a planilha.');
        return false;
    }
    if (!periodo) { showError('processar', 'Informe o período da semana (ex: 05/05 a 09/05).'); return false; }

    scanFiles.forEach(function(f) { data.append('scan', f); });
    if (loteId) data.set('lote_id', loteId);
    if (alunos.files.length) data.set('alunos', alunos.files[0]);
    data.set('periodo_semana', periodo);
    data.set('pagina_inicial', paginaInicial || '1');

    showLoading('processar', true);
    hideError('processar');
    document.getElementById('result-processar').classList.remove('show');

    fetch('/processar', { method: 'POST', body: data })
        .then(r => r.json())
        .then(data => {
            showLoading('processar', false);
            if (data.erro) {
                showError('processar', data.erro);
                return;
            }

            document.getElementById('r-alunos').textContent = data.alunos;
            document.getElementById('r-presenca').textContent = data.com_presenca;

            var loteRow = document.getElementById('lote-row');
            var loteVal = document.getElementById('r-lote');
            loteRow.style.display = '';
            if (data.modo === 'lote') {
                loteVal.textContent = data.lote_id + (data.lote_datas ? ' (' + data.lote_datas + ')' : '');
                loteVal.className = 'result-value result-ok';
            } else {
                loteVal.textContent = 'nenhum — identidade veio da planilha enviada';
                loteVal.className = 'result-value result-warn';
            }

            var avisos = (data.avisos || []).slice();
            if (data.divergencia && data.divergencia.resumo) {
                avisos.push('Comparado com a planilha enviada: ' + data.divergencia.resumo +
                            '. As presenças foram atribuídas pelo lote impresso, não por ela.');
            }
            mostrarAvisos(avisos);

            var ambEl = document.getElementById('r-ambiguos');
            ambEl.textContent = data.ambiguos + (data.ambiguos > 0 ? ' (incluídos como presença)' : '');
            ambEl.className = 'result-value ' + (data.ambiguos > 0 ? 'result-warn' : 'result-ok');

            var btnRevisar = document.getElementById('btn-revisar');
            if (data.ambiguos > 0) {
                btnRevisar.textContent = 'Revisar ' + data.ambiguos + ' caso(s) ambíguo(s) →';
                btnRevisar.style.display = '';
            } else {
                btnRevisar.style.display = 'none';
            }

            document.getElementById('download-nome').textContent = 'Baixar planilha';
            document.getElementById('result-processar').classList.add('show');

            var sheetsRow = document.getElementById('sheets-row');
            var sheetsMsg = document.getElementById('sheets-msg');
            var btnSubst = document.getElementById('btn-substituir');
            btnSubst.style.display = 'none';

            if (data.sheets_status === 'ok') {
                sheetsRow.style.display = '';
                sheetsMsg.textContent = '✓ Exportado (' + data.sheets_aba + ')';
                sheetsMsg.className = 'result-value result-ok';
            } else if (data.sheets_status === 'duplicado') {
                sheetsRow.style.display = '';
                sheetsMsg.textContent = 'Semana já existe em ' + data.sheets_aba;
                sheetsMsg.className = 'result-value result-warn';
                btnSubst.style.display = '';
                btnSubst.disabled = false;
                btnSubst.textContent = 'Substituir semana no Google Sheets';
            } else if (data.sheets_status === 'erro') {
                sheetsRow.style.display = '';
                sheetsMsg.textContent = 'Erro no Sheets: ' + (data.sheets_erro || 'falha desconhecida');
                sheetsMsg.className = 'result-value result-warn';
            } else {
                sheetsRow.style.display = 'none';
            }

            var npag = data.paginas_validas || 0;
            if (npag > 0) {
                var nav = document.getElementById('preview-nav');
                nav.innerHTML = '';
                for (var p = 0; p < npag; p++) {
                    (function(idx) {
                        var btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'preview-nav-btn' + (idx === 0 ? ' active' : '');
                        btn.textContent = 'Página ' + (idx + 1);
                        btn.onclick = function() {
                            document.querySelectorAll('.preview-nav-btn').forEach(function(b) { b.classList.remove('active'); });
                            btn.classList.add('active');
                            loadPreview(idx);
                        };
                        nav.appendChild(btn);
                    })(p);
                }
                document.getElementById('preview-section').style.display = '';
                loadPreview(0);
            }
        })
        .catch(err => {
            showLoading('processar', false);
            showError('processar', 'Erro de conexão: ' + err.message);
        });

    return false;
}

function showLoading(tab, show) {
    document.getElementById('loading-' + tab).classList.toggle('show', show);
    document.getElementById('form-' + tab).style.display = show ? 'none' : '';
}

function showError(tab, msg) {
    var el = document.getElementById('error-' + tab);
    el.textContent = msg;
    el.classList.add('show');
}

function hideError(tab) {
    document.getElementById('error-' + tab).classList.remove('show');
}

function loadPreview(idx) {
    var img = document.getElementById('preview-img');
    var overlay = document.getElementById('preview-overlay');
    overlay.style.display = 'flex';
    img.style.opacity = '0.3';

    var ts = Date.now();
    var newImg = new Image();
    newImg.onload = function() {
        img.src = newImg.src;
        img.style.display = 'block';
        img.style.opacity = '';
        overlay.style.display = 'none';
    };
    newImg.onerror = function() {
        overlay.style.display = 'none';
        img.style.opacity = '';
    };
    newImg.src = '/preview/' + idx + '?t=' + ts;
}

function substituirSemana() {
    var btn = document.getElementById('btn-substituir');
    btn.disabled = true;
    btn.textContent = 'Substituindo...';

    fetch('/sheets_exportar', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var msg = document.getElementById('sheets-msg');
            btn.style.display = 'none';
            if (data.erro) {
                msg.textContent = 'Erro no Sheets: ' + data.erro;
                msg.className = 'result-value result-warn';
            } else {
                msg.textContent = '✓ Exportado (' + (data.aba || '') + ')';
                msg.className = 'result-value result-ok';
            }
        })
        .catch(function() {
            btn.disabled = false;
            btn.textContent = 'Substituir semana no Google Sheets';
        });
}

// Drag and drop — exclui a zona do scan (que tem handler próprio via ondrop)
document.querySelectorAll('.upload-zone:not(#upload-scan-zone)').forEach(function(zone) {
    zone.addEventListener('dragover', function(e) {
        e.preventDefault();
        zone.classList.add('dragover');
    });
    zone.addEventListener('dragleave', function() {
        zone.classList.remove('dragover');
    });
    zone.addEventListener('drop', function(e) {
        e.preventDefault();
        zone.classList.remove('dragover');
        var input = zone.querySelector('input');
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event('change'));
    });
});

carregarLotes();

// Drag visual (hover) para a zona do scan
document.getElementById('upload-scan-zone').addEventListener('dragover', function(e) {
    e.preventDefault();
    this.classList.add('dragover');
});
document.getElementById('upload-scan-zone').addEventListener('dragleave', function() {
    this.classList.remove('dragover');
});
</script>

</body>
</html>"""


if __name__ == "__main__":
    stdout_tolerante()

    port = 5000
    print()
    print("=" * 50)
    print("  Sistema de presença — Interface web")
    print("=" * 50)

    # O servidor carrega os módulos uma única vez, na inicialização. Mostrar a
    # data do arquivo deixa claro se esta janela está com o código atualizado
    # ou se precisa ser fechada e reaberta depois de uma alteração.
    try:
        from datetime import datetime as _dt
        import google_sheets as _gs
        _quando = _dt.fromtimestamp(os.path.getmtime(_gs.__file__))
        print(f"\n  Código de planilhas carregado: {_quando:%d/%m/%Y %H:%M}")
    except Exception:
        pass

    print(f"\n  Acesse: http://localhost:{port}\n")

    # Abre o navegador automaticamente
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    app.run(host="127.0.0.1", port=port, debug=False)