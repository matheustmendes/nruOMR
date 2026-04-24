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

import cv2
import numpy as np
import yaml

from localizar_marcadores import processar
from ler_bolhas import ler_pagina, THRESHOLD
from exportar import (
    carregar_todas_paginas, processar_pdf_completo,
    contar_presencas, ler_nomes_alunos, exportar_xlsx
)
from gerar_template import (
    ler_planilha, gerar_template, gerar_config, CONFIG_ABAS
)


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
        "config": "config_são_lázaro.yaml",
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


@app.route("/processar", methods=["POST"])
def rota_processar():
    try:
        restaurante_key = request.form.get("restaurante")
        if restaurante_key not in RESTAURANTES:
            return jsonify({"erro": "Restaurante inválido"}), 400

        scan_file = request.files.get("scan")
        alunos_file = request.files.get("alunos")

        if not scan_file or not alunos_file:
            return jsonify({"erro": "Envie o scan e a planilha de alunos"}), 400

        # Salva arquivos temporários
        scan_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        scan_file.save(scan_tmp.name)

        alunos_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        alunos_file.save(alunos_tmp.name)

        # Carrega config
        caminho_config = encontrar_config(restaurante_key)
        if not caminho_config:
            return jsonify({"erro": f"Configuração não encontrada para {RESTAURANTES[restaurante_key]['nome']}. Gere o template primeiro."}), 400

        with open(caminho_config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        dias = config["layout"]["dias"]
        restaurante = RESTAURANTES[restaurante_key]

        # Processa
        paginas = carregar_todas_paginas(scan_tmp.name, dpi=config["scan"]["dpi"])
        resultados = processar_pdf_completo(paginas, config)
        contagem = contar_presencas(resultados, dias)
        alunos = ler_nomes_alunos(alunos_tmp.name, restaurante["aba"])

        # Conta ambíguos
        ambiguos = 0
        for r in resultados:
            for dia in dias:
                for tipo in ["almoco", "janta"]:
                    pct = r["dias"][dia][f"{tipo}_pct"]
                    if abs(pct - THRESHOLD) < 0.08:
                        ambiguos += 1

        # Gera xlsx em memória
        saida_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        exportar_xlsx(contagem, alunos, dias, saida_tmp.name)

        # Limpa
        os.unlink(scan_tmp.name)
        os.unlink(alunos_tmp.name)

        # Guarda caminho pra download
        app.config["ULTIMO_RESULTADO"] = saida_tmp.name
        app.config["ULTIMO_NOME"] = f"presencas_{restaurante_key}.xlsx"

        paginas_processadas = sum(1 for c in contagem if True)
        com_presenca = sum(1 for c in contagem if c["presencas"] > 0)

        return jsonify({
            "sucesso": True,
            "alunos": len(alunos),
            "com_presenca": com_presenca,
            "ambiguos": ambiguos,
            "paginas": len(paginas),
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/gerar_template", methods=["POST"])
def rota_gerar_template():
    try:
        restaurante_key = request.form.get("restaurante")
        alunos_file = request.files.get("alunos")

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
        alunos_file.save(alunos_tmp.name)

        resultados_geracao = []

        for key in keys:
            rest = RESTAURANTES[key]
            try:
                info = ler_planilha(alunos_tmp.name, rest["aba"])

                if rest["aba"] in CONFIG_ABAS:
                    dias = CONFIG_ABAS[rest["aba"]]
                else:
                    dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]

                # PDF temporário
                pdf_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                resultado = gerar_template(info, dias, pdf_tmp.name)

                # Config no diretório do script
                config_path = os.path.join(SCRIPT_DIR, "configs", rest["config"])
                if not os.path.isdir(os.path.join(SCRIPT_DIR, "configs")):
                    config_path = os.path.join(SCRIPT_DIR, rest["config"])
                gerar_config(info, dias, resultado, config_path)

                resultados_geracao.append({
                    "restaurante": rest["nome"],
                    "alunos": len(info["alunos"]),
                    "paginas": resultado["total_paginas"],
                    "arquivo": pdf_tmp.name,
                })

            except Exception as e:
                resultados_geracao.append({
                    "restaurante": rest["nome"],
                    "erro": str(e),
                })

        os.unlink(alunos_tmp.name)

        # Se gerou um só, guarda pra download
        pdfs_ok = [r for r in resultados_geracao if "arquivo" in r]
        if len(pdfs_ok) == 1:
            app.config["ULTIMO_RESULTADO"] = pdfs_ok[0]["arquivo"]
            app.config["ULTIMO_NOME"] = f"template_{keys[0]}.pdf"
        elif len(pdfs_ok) > 1:
            # Guarda todos pra download individual
            app.config["TEMPLATES_GERADOS"] = {
                r["restaurante"].lower().replace(" ", "_").replace("ã", "a").replace("á", "a"): r["arquivo"]
                for r in pdfs_ok
            }
            # O primeiro fica como download padrão
            app.config["ULTIMO_RESULTADO"] = pdfs_ok[0]["arquivo"]
            app.config["ULTIMO_NOME"] = f"template_{keys[0]}.pdf"

        return jsonify({
            "sucesso": True,
            "resultados": [{
                "restaurante": r["restaurante"],
                "alunos": r.get("alunos", 0),
                "paginas": r.get("paginas", 0),
                "erro": r.get("erro"),
            } for r in resultados_geracao],
        })

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


@app.route("/download/<restaurante>")
def rota_download_template(restaurante):
    templates = app.config.get("TEMPLATES_GERADOS", {})
    caminho = templates.get(restaurante)
    if not caminho or not os.path.exists(caminho):
        return "Template não encontrado", 404
    return send_file(caminho, as_attachment=True, download_name=f"template_{restaurante}.pdf")


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
</style>
</head>
<body>

<div class="container">
    <div class="header">
        <h1>Sistema de presença</h1>
        <p>Programa Canela — PROAE/UFBA</p>
    </div>

    <div class="tabs">
        <button class="tab active" onclick="switchTab('template')">Gerar template</button>
        <button class="tab" onclick="switchTab('processar')">Processar scan</button>
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
                </div>
            </div>

            <div class="section">
                <div class="label">Scan escaneado (.pdf)</div>
                <div id="upload-scan-zone" class="upload-zone">
                    <input type="file" name="scan" accept=".pdf" onchange="fileSelected(this, 'scan')">
                    <div class="upload-icon">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 18h16"/></svg>
                    </div>
                    <div class="upload-text">Arraste o arquivo ou clique para selecionar</div>
                    <div class="upload-hint">.pdf escaneado</div>
                </div>
                <div id="file-scan" class="file-pill" style="display:none;">
                    <div class="file-icon">PDF</div>
                    <div class="file-name" id="file-scan-name"></div>
                    <div class="file-remove" onclick="removeFile('scan')">remover</div>
                </div>
            </div>

            <div class="section">
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
            <a href="/download" class="btn-download" id="btn-download-proc">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 4v12m0 0l4-4m-4 4l-4-4M4 18h16"/></svg>
                <span id="download-nome">Baixar planilha</span>
            </a>
        </div>
    </div>
</div>

<script>
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));

    if (tab === 'template') {
        document.querySelectorAll('.tab')[0].classList.add('active');
        document.getElementById('panel-template').classList.add('active');
    } else {
        document.querySelectorAll('.tab')[1].classList.add('active');
        document.getElementById('panel-processar').classList.add('active');
    }
}

function selectRadio(el, name) {
    el.closest('.radio-group').querySelectorAll('.radio').forEach(r => r.classList.remove('selected'));
    el.classList.add('selected');
    el.querySelector('input').checked = true;
}

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
                    var key = r.restaurante.toLowerCase().replace(/ /g, '_').replace(/ã/g, 'a').replace(/á/g, 'a');
                    html += '<div class="template-item"><div><div>' + r.restaurante + '</div><div class="template-info">' + r.alunos + ' alunos, ' + r.paginas + ' páginas</div></div><a href="/download/' + key + '" class="template-download">baixar PDF</a></div>';
                }
            });

            document.getElementById('result-template').innerHTML = html;
        })
        .catch(err => {
            showLoading('template', false);
            showError('template', 'Erro de conexão: ' + err.message);
        });

    return false;
}

function submitProcessar(e) {
    e.preventDefault();

    var form = document.getElementById('form-processar');
    var data = new FormData();
    data.set('restaurante', form.querySelector('input[name="rest-processar"]:checked').value);

    var scan = form.querySelector('input[name="scan"]');
    var alunos = form.querySelector('input[name="alunos"]');

    if (!scan.files.length) { showError('processar', 'Selecione o PDF do scan.'); return false; }
    if (!alunos.files.length) { showError('processar', 'Selecione a planilha de alunos.'); return false; }

    data.set('scan', scan.files[0]);
    data.set('alunos', alunos.files[0]);

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

            var ambEl = document.getElementById('r-ambiguos');
            ambEl.textContent = data.ambiguos;
            ambEl.className = 'result-value ' + (data.ambiguos > 0 ? 'result-warn' : 'result-ok');

            document.getElementById('download-nome').textContent = 'Baixar planilha';
            document.getElementById('result-processar').classList.add('show');
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

// Drag and drop
document.querySelectorAll('.upload-zone').forEach(function(zone) {
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
</script>

</body>
</html>"""


if __name__ == "__main__":
    port = 5000
    print()
    print("=" * 50)
    print("  Sistema de presença — Interface web")
    print("=" * 50)
    print(f"\n  Acesse: http://localhost:{port}\n")

    # Abre o navegador automaticamente
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    app.run(host="127.0.0.1", port=port, debug=False)