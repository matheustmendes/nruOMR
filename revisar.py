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
    ler_pagina, carregar_posicoes, OFFSET_Y, THRESHOLD,
    mm_para_px
)
from exportar import (
    carregar_todas_paginas, processar_pdf_completo,
    contar_presencas, ler_nomes_alunos, exportar_xlsx,
    eh_pagina_branca
)


# Zona ambígua: porcentagens entre esses dois valores precisam de revisão
AMBIGUO_MIN = THRESHOLD - 0.08
AMBIGUO_MAX = THRESHOLD + 0.08


def extrair_recortes(paginas_alinhadas, binarios, config, resultados_por_pagina, alunos):
    """
    Extrai recortes dos círculos ambíguos pra mostrar na revisão.

    Returns:
        Lista de dicts com info do caso ambíguo + imagem recortada em base64
    """
    pos = carregar_posicoes(config)
    dias = config["layout"]["dias"]
    ambiguos = []

    for pag_idx, (alinhada, binary, resultados) in enumerate(
        zip(paginas_alinhadas, binarios, resultados_por_pagina)
    ):
        for aluno in resultados:
            i = aluno["numero"] - 1  # índice local na página
            cy = int(pos["primeira_linha_y_px"] + OFFSET_Y + i * pos["linha_altura_px"])
            raio = int(pos["raio_px"])

            # Número global do aluno
            num_global = aluno["numero"]

            for dia in dias:
                for tipo in ["almoco", "janta"]:
                    pct = aluno["dias"][dia][f"{tipo}_pct"]

                    # Só interessa se está na zona ambígua
                    if pct < AMBIGUO_MIN or pct > AMBIGUO_MAX:
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
                        "numero": num_global,
                        "nome": nome,
                        "dia": dia,
                        "tipo": tipo,
                        "pct": pct,
                        "marcado_auto": pct >= THRESHOLD,
                        "imagem_b64": b64,
                    })

    return ambiguos


def gerar_html_revisao(ambiguos, contagem, alunos, dias, arquivo_json):
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
    <button class="btn-exportar" onclick="exportar()">Exportar planilha</button>
</div>

<div class="export-msg" id="export-msg">
    Correções salvas! Feche esta aba e rode:<br>
    <code style="background:#0f172a;padding:4px 12px;border-radius:4px;margin-top:8px;display:inline-block;">
        python exportar.py ... --correcoes correcoes.json
    </code>
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
        // Gera JSON com as correções
        const correcoes = {{}};
        for (const [id, marcado] of Object.entries(decisoes)) {{
            correcoes[id] = marcado;
        }}

        // Faz download do JSON
        const blob = new Blob([JSON.stringify(correcoes, null, 2)], {{type: 'application/json'}});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'correcoes.json';
        a.click();
        URL.revokeObjectURL(url);

        document.getElementById('export-msg').style.display = 'block';
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
    if "--merge" in sys.argv:
        idx = sys.argv.index("--merge")
        pdfs.extend(sys.argv[idx + 1:])

    with open(caminho_config, "r") as f:
        config = yaml.safe_load(f)

    dias = config["layout"]["dias"]
    alunos_por_pagina = config["layout"]["alunos_por_pagina"]

    # Carrega
    print("=== Carregando PDFs ===")
    todas_paginas = carregar_todas_paginas(*pdfs, dpi=config["scan"]["dpi"])

    # Separa páginas reais e processa cada uma
    print("\n=== Processando páginas ===")
    paginas_alinhadas = []
    binarios = []
    resultados_por_pagina = []
    num_global = 0

    for i, img in enumerate(todas_paginas):
        if eh_pagina_branca(img):
            continue

        print(f"  Processando página {len(paginas_alinhadas) + 1} (PDF página {i + 1})...")

        try:
            alinhada = processar(img, config)
            gray = cv2.cvtColor(alinhada, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            resultados = ler_pagina(binary, config, alunos_por_pagina)

            # Ajusta numeração global
            for r in resultados:
                r["numero"] = num_global + r["numero"]

            num_global += alunos_por_pagina
            paginas_alinhadas.append(alinhada)
            binarios.append(binary)
            resultados_por_pagina.append(resultados)

        except Exception as e:
            print(f"    ERRO: {e}")
            num_global += alunos_por_pagina
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