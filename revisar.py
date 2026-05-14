"""
revisar.py

Tela de revisão interativa para casos ambíguos.
Abre no navegador uma página onde o usuário pode confirmar
ou corrigir as leituras antes de exportar.

Fluxo:
    1. Processa o PDF
    2. Identifica casos ambíguos
    3. Abre tela de revisão no navegador
    4. Usuário corrige no navegador e clica "Exportar"
    5. Gera arquivo JSON com as correções
    6. O exportar.py aplica as correções

Uso:
    python revisar.py <scan.pdf> <config.yaml> <planilha_alunos.xlsx> <aba> [--merge scan2.pdf ...]
"""

import sys
import os
import json
import cv2
import numpy as np
import yaml
import base64
import webbrowser
import tempfile
import http.server
import threading
from urllib.parse import parse_qs

from localizar_marcadores import carregar_imagem, processar
from ler_bolhas import (
    ler_pagina, carregar_posicoes,
    _get_offset_y, _get_threshold,
    mm_para_px
)
from exportar import (
    carregar_todas_paginas, processar_pdf_completo,
    contar_presencas, ler_nomes_alunos, exportar_xlsx,
    eh_pagina_branca
)


def extrair_recortes(paginas_alinhadas, binarios, config, resultados_por_pagina, alunos):
    """
    Extrai recortes dos círculos ambíguos pra mostrar na revisão.

    Returns:
        Lista de dicts com info do caso ambíguo + imagem recortada em base64
    """
    pos = carregar_posicoes(config)
    dias = config["layout"]["dias"]
    offset_y = _get_offset_y(config)
    threshold = _get_threshold(config)
    scan = config.get("scan", {})
    # Margem assimétrica: abaixo do threshold é pequena pra não capturar bolhas
    # vazias (que leem ~33%); acima é maior pra pegar marcações leves.
    margem_abaixo = scan.get("ambiguo_margem_abaixo", 0.04)
    margem_acima = scan.get("ambiguo_margem_acima", 0.10)
    ambiguo_min = threshold - margem_abaixo
    ambiguo_max = threshold + margem_acima
    ambiguos = []

    for pag_idx, (alinhada, binary, resultados) in enumerate(
        zip(paginas_alinhadas, binarios, resultados_por_pagina)
    ):
        for local_i, aluno in enumerate(resultados):
            cy = int(pos["primeira_linha_y_px"] + offset_y + local_i * pos["linha_altura_px"])
            raio = int(pos["raio_px"])

            # Número global do aluno
            num_global = aluno["numero"]

            for dia in dias:
                for tipo in ["almoco", "janta"]:
                    pct = aluno["dias"][dia][f"{tipo}_pct"]

                    # Só interessa se está na zona ambígua
                    if pct < ambiguo_min or pct > ambiguo_max:
                        continue

                    # Posição X do círculo
                    if tipo == "almoco":
                        cx = int(pos["circulos_x"][dia][0])
                    else:
                        cx = int(pos["circulos_x"][dia][1])

                    # Recorta a região ao redor do círculo (com margem)
                    margem = raio + 10
                    h, w = alinhada.shape[:2]
                    x1 = max(0, cx - margem)
                    y1 = max(0, cy - margem)
                    x2 = min(w, cx + margem)
                    y2 = min(h, cy + margem)

                    recorte = alinhada[y1:y2, x1:x2]

                    # Converte pra base64
                    _, buffer = cv2.imencode(".png", recorte)
                    b64 = base64.b64encode(buffer).decode("utf-8")

                    # Nome do aluno
                    nome = alunos[num_global - 1][0] if num_global - 1 < len(alunos) else f"Aluno {num_global}"

                    ambiguos.append({
                        "id": f"{num_global}_{dia}_{tipo}",
                        "numero": int(num_global),
                        "nome": nome,
                        "dia": dia,
                        "tipo": tipo,
                        "pct": float(pct),
                        "marcado_auto": bool(pct >= threshold),
                        "imagem_b64": b64,
                    })

    return ambiguos


def _exportar_js(submit_url):
    if submit_url:
        return f"""
        const btn = document.getElementById('btn-exportar');
        btn.disabled = true;
        btn.textContent = 'Salvando...';
        fetch('{submit_url}', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(correcoes)
        }})
        .then(r => r.json())
        .then(data => {{
            btn.disabled = false;
            btn.textContent = 'Salvar correções';
            if (data.sucesso) {{
                document.getElementById('export-msg').style.display = 'block';
            }} else {{
                alert('Erro ao salvar: ' + (data.erro || 'desconhecido'));
            }}
        }})
        .catch(() => {{
            btn.disabled = false;
            btn.textContent = 'Salvar correções';
            alert('Erro de conexão.');
        }});"""
    else:
        return """
        const blob = new Blob([JSON.stringify(correcoes, null, 2)], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'correcoes.json';
        a.click();
        URL.revokeObjectURL(url);
        document.getElementById('export-msg').style.display = 'block';"""


def _msg_exportacao(submit_url):
    if submit_url:
        return ('Correções aplicadas! Feche esta aba e clique em '
                '<strong>Baixar planilha</strong> na tela anterior.')
    else:
        return ('Correções salvas! Feche esta aba e rode:<br>'
                '<code style="background:#0f172a;padding:4px 12px;border-radius:4px;'
                'margin-top:8px;display:inline-block;">'
                'python exportar.py ... --correcoes correcoes.json</code>')


def gerar_html_revisao(ambiguos, contagem, alunos, dias, arquivo_json, submit_url=None):
    """
    Gera a página HTML de revisão.
    """
    # Dados dos ambíguos como JSON pro JavaScript
    ambiguos_js = json.dumps([{
        "id": a["id"],
        "numero": a["numero"],
        "nome": a["nome"],
        "dia": a["dia"],
        "tipo": a["tipo"],
        "pct": a["pct"],
        "marcado": a["marcado_auto"],
    } for a in ambiguos])

    # Dados da contagem completa como JSON
    contagem_js = json.dumps(contagem)

    # Gera cards dos ambíguos
    cards_html = ""
    for a in ambiguos:
        tipo_label = "Almoço" if a["tipo"] == "almoco" else "Janta"
        status = "marcado" if a["marcado_auto"] else "vazio"
        cor_borda = "#4ade80" if a["marcado_auto"] else "#f87171"

        cards_html += f"""
        <div class="card" id="card-{a['id']}" data-id="{a['id']}" style="border-left: 4px solid {cor_borda};">
            <div class="card-img">
                <img src="data:image/png;base64,{a['imagem_b64']}" alt="Recorte">
            </div>
            <div class="card-info">
                <div class="card-aluno">#{a['numero']} {a['nome']}</div>
                <div class="card-detalhe">{a['dia']} — {tipo_label} — {a['pct']:.0%}</div>
                <div class="card-acoes">
                    <button class="btn btn-marcado {'btn-ativo' if a['marcado_auto'] else ''}"
                            onclick="setMarcado('{a['id']}', true)">● Marcado</button>
                    <button class="btn btn-vazio {'btn-ativo' if not a['marcado_auto'] else ''}"
                            onclick="setMarcado('{a['id']}', false)">○ Vazio</button>
                </div>
            </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Revisão OMR — Casos Ambíguos</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        font-family: 'Segoe UI', system-ui, sans-serif;
        background: #0f172a;
        color: #e2e8f0;
        padding: 2rem;
    }}
    .header {{
        max-width: 800px;
        margin: 0 auto 2rem;
    }}
    .header h1 {{
        font-size: 1.5rem;
        font-weight: 600;
        margin-bottom: 0.5rem;
    }}
    .header p {{
        color: #94a3b8;
        font-size: 0.9rem;
    }}
    .stats {{
        display: flex;
        gap: 1rem;
        margin: 1rem 0;
    }}
    .stat {{
        background: #1e293b;
        border-radius: 8px;
        padding: 0.75rem 1.25rem;
        flex: 1;
        text-align: center;
    }}
    .stat-num {{
        font-size: 1.5rem;
        font-weight: 700;
    }}
    .stat-label {{
        font-size: 0.75rem;
        color: #94a3b8;
        margin-top: 2px;
    }}
    .no-ambiguos {{
        max-width: 800px;
        margin: 3rem auto;
        text-align: center;
        padding: 3rem;
        background: #1e293b;
        border-radius: 12px;
    }}
    .no-ambiguos h2 {{
        font-size: 1.25rem;
        color: #4ade80;
        margin-bottom: 0.5rem;
    }}
    .cards {{
        max-width: 800px;
        margin: 0 auto;
        display: flex;
        flex-direction: column;
        gap: 1rem;
    }}
    .card {{
        background: #1e293b;
        border-radius: 8px;
        padding: 1rem;
        display: flex;
        gap: 1rem;
        align-items: center;
        transition: border-color 0.2s;
    }}
    .card-img {{
        flex-shrink: 0;
    }}
    .card-img img {{
        width: 60px;
        height: 60px;
        border-radius: 6px;
        object-fit: cover;
        border: 1px solid #334155;
        image-rendering: pixelated;
    }}
    .card-info {{
        flex: 1;
    }}
    .card-aluno {{
        font-weight: 500;
        font-size: 0.95rem;
        margin-bottom: 2px;
    }}
    .card-detalhe {{
        font-size: 0.8rem;
        color: #94a3b8;
        margin-bottom: 0.5rem;
    }}
    .card-acoes {{
        display: flex;
        gap: 0.5rem;
    }}
    .btn {{
        padding: 0.4rem 1rem;
        border-radius: 6px;
        border: 1px solid #475569;
        background: transparent;
        color: #94a3b8;
        cursor: pointer;
        font-size: 0.8rem;
        transition: all 0.15s;
    }}
    .btn:hover {{
        background: #334155;
    }}
    .btn-marcado.btn-ativo {{
        background: #166534;
        border-color: #4ade80;
        color: #4ade80;
    }}
    .btn-vazio.btn-ativo {{
        background: #7f1d1d;
        border-color: #f87171;
        color: #f87171;
    }}
    .footer {{
        max-width: 800px;
        margin: 2rem auto;
        display: flex;
        justify-content: flex-end;
        gap: 1rem;
    }}
    .btn-exportar {{
        padding: 0.75rem 2rem;
        border-radius: 8px;
        border: none;
        background: #2563eb;
        color: white;
        font-size: 1rem;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s;
    }}
    .btn-exportar:hover {{
        background: #1d4ed8;
    }}
    .export-msg {{
        display: none;
        max-width: 800px;
        margin: 2rem auto;
        padding: 1.5rem;
        background: #166534;
        border-radius: 8px;
        text-align: center;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>Revisão OMR — Casos Ambíguos</h1>
    <p>Confirme ou corrija as leituras abaixo antes de exportar. Clique em "Marcado" ou "Vazio" para cada caso.</p>
    <div class="stats">
        <div class="stat">
            <div class="stat-num" id="total-ambiguos">{len(ambiguos)}</div>
            <div class="stat-label">Ambíguos</div>
        </div>
        <div class="stat">
            <div class="stat-num" id="total-revisados">0</div>
            <div class="stat-label">Revisados</div>
        </div>
    </div>
</div>

{"" if ambiguos else '<div class="no-ambiguos"><h2>Nenhum caso ambíguo</h2><p>Todas as leituras ficaram claras. Pode exportar direto.</p></div>'}

<div class="cards">
    {cards_html}
</div>

<div class="footer">
    <button class="btn-exportar" id="btn-exportar" onclick="exportar()">Salvar correções</button>
</div>

<div class="export-msg" id="export-msg">
    {_msg_exportacao(submit_url)}
</div>

<script>
    const ambiguos = {ambiguos_js};
    const decisoes = {{}};
    let revisados = 0;

    // Inicializa decisões com os valores automáticos
    ambiguos.forEach(a => {{
        decisoes[a.id] = a.marcado;
    }});

    function setMarcado(id, marcado) {{
        decisoes[id] = marcado;

        // Atualiza UI
        const card = document.getElementById('card-' + id);
        const btns = card.querySelectorAll('.btn');
        btns[0].classList.toggle('btn-ativo', marcado);
        btns[1].classList.toggle('btn-ativo', !marcado);
        card.style.borderLeftColor = marcado ? '#4ade80' : '#f87171';

        // Conta revisados (diferentes do automático)
        const item = ambiguos.find(a => a.id === id);
        revisados = Object.keys(decisoes).filter(k => {{
            const orig = ambiguos.find(a => a.id === k);
            return decisoes[k] !== orig.marcado;
        }}).length;
        document.getElementById('total-revisados').textContent = revisados;
    }}

    function exportar() {{
        const correcoes = {{}};
        for (const [id, marcado] of Object.entries(decisoes)) {{
            correcoes[id] = marcado;
        }}

        {_exportar_js(submit_url)}
    }}
</script>

</body>
</html>"""

    return html


def main():
    if len(sys.argv) < 5:
        print("Uso: python revisar.py <scan.pdf> <config.yaml> <planilha_alunos.xlsx> <aba> [--merge scan2.pdf ...]")
        sys.exit(1)

    caminho_scan = sys.argv[1]
    caminho_config = sys.argv[2]
    caminho_alunos = sys.argv[3]
    nome_aba = sys.argv[4]

    pdfs = [caminho_scan]

    with open(caminho_config, "r") as f:
        config = yaml.safe_load(f)

    dias = config["layout"]["dias"]
    alunos_por_pagina = config["layout"]["alunos_por_pagina"]

    if "--merge" in sys.argv:
        idx = sys.argv.index("--merge")
        for arg in sys.argv[idx + 1:]:
            if arg.startswith("--"):
                break
            pdfs.append(arg)

    pagina_inicio = 1
    if "--pagina-inicio" in sys.argv:
        idx = sys.argv.index("--pagina-inicio")
        pagina_inicio = int(sys.argv[idx + 1])
        print(f"Página de início: {pagina_inicio}")

    # Carrega
    print("=== Carregando PDFs ===")
    todas_paginas = carregar_todas_paginas(*pdfs, dpi=config["scan"]["dpi"])

    # Separa páginas reais e processa cada uma
    print("\n=== Processando páginas ===")
    paginas_alinhadas = []
    binarios = []
    resultados_por_pagina = []
    paginas_processadas = 0

    for i, img in enumerate(todas_paginas):
        if eh_pagina_branca(img):
            continue

        paginas_processadas += 1
        pagina_template = pagina_inicio + paginas_processadas - 1
        print(f"  Processando página {paginas_processadas} (PDF pág {i + 1} → template pág {pagina_template})...")

        try:
            alinhada = processar(img, config)
            gray = cv2.cvtColor(alinhada, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            resultados = ler_pagina(binary, config, alunos_por_pagina)

            # Ajusta numeração global com base na página do template
            offset = (pagina_template - 1) * alunos_por_pagina
            for r in resultados:
                r["numero"] = offset + r["numero"]

            paginas_alinhadas.append(alinhada)
            binarios.append(binary)
            resultados_por_pagina.append(resultados)

        except Exception as e:
            print(f"    ERRO: {e}")
            continue

    # Junta todos os resultados
    todos_resultados = []
    for r in resultados_por_pagina:
        todos_resultados.extend(r)

    # Conta presenças
    contagem = contar_presencas(todos_resultados, dias)

    # Nomes
    alunos = ler_nomes_alunos(caminho_alunos, nome_aba)

    # Extrai recortes ambíguos
    print("\n=== Identificando casos ambíguos ===")
    ambiguos = extrair_recortes(
        paginas_alinhadas, binarios, config, resultados_por_pagina, alunos
    )
    print(f"  {len(ambiguos)} casos ambíguos encontrados")

    if not ambiguos:
        print("\nNenhum caso ambíguo! Exportando direto...")
        nome_restaurante = config.get("restaurante", "resultado").lower().replace(" ", "_")
        exportar_xlsx(contagem, alunos, dias, f"presencas_{nome_restaurante}.xlsx")
        return

    # Gera HTML
    html = gerar_html_revisao(ambiguos, contagem, alunos, dias, "correcoes.json")

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        webbrowser.open(f"file://{f.name}")

    print(f"\nTela de revisão aberta no navegador.")
    print(f"Após revisar, clique em 'Exportar planilha' pra baixar o correcoes.json.")
    print(f"Depois rode:")
    print(f"  python exportar.py {caminho_scan} {caminho_config} {caminho_alunos} \"{nome_aba}\" --correcoes correcoes.json")


if __name__ == "__main__":
    main()