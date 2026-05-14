"""
debug_bolhas.py

Diagnóstico visual completo da leitura de bolhas.
Integrado ao pipeline atual (Otsu + GaussianBlur + homografia).

Uso:
    python debug_bolhas.py <scan.pdf> <config.yaml> [pagina_pdf]

Exemplos:
    python debug_bolhas.py scan.pdf config_canela.yaml
    python debug_bolhas.py scan.pdf config_canela.yaml 2   # página 3 do PDF (0-indexed)

O que abre no navegador:
    1. Original — imagem crua do scan
    2. Binarizada — resultado do Otsu antes do alinhamento
    3. Marcadores — detectados (verde) e estimados por afim (laranja)
    4. Alinhada — após homografia com marcadores
    5. Binarizada alinhada — o que o pipeline realmente lê
    6. Mapa de bolhas — cada círculo colorido por resultado:
         Verde   = marcado com confiança
         Azul    = vazio
         Amarelo = AMBÍGUO (marcado mas fraco — investigar aqui)
    7. Histograma de % por bolha — distribuição geral
"""

import sys
import os
import cv2
import numpy as np
import yaml
import base64
import webbrowser
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import converter_pdf
from localizar_marcadores import (
    processar,
    encontrar_marcadores_com_fallback,
    classificar_cantos,
    carregar_marcadores_esperados,
    completar_marcadores_faltantes,
)
from ler_bolhas import ler_pagina, carregar_posicoes, THRESHOLD, OFFSET_Y, _get_threshold, _get_offset_y


# --- UTILITÁRIOS VISUAIS ---

def mostrar_no_navegador(*pares):
    """Abre uma página HTML com todas as imagens passadas como (título, imagem)."""
    cards = ""
    for titulo, imagem in pares:
        if imagem.dtype != np.uint8:
            imagem = (imagem * 255).astype(np.uint8)
        if len(imagem.shape) == 2:
            imagem = cv2.cvtColor(imagem, cv2.COLOR_GRAY2BGR)
        _, buffer = cv2.imencode(".png", imagem)
        b64 = base64.b64encode(buffer).decode("utf-8")
        cards += f"""
        <div style="margin-bottom: 2rem;">
            <h2 style="font-family: monospace; font-size: 14px; margin-bottom: 0.5rem;">{titulo}</h2>
            <img src="data:image/png;base64,{b64}" style="max-width: 100%; border: 1px solid #ccc;">
        </div>
        """
    html = f"""<!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: sans-serif; padding: 2rem; background: #1a1a1a; color: #eee; }}
            h2 {{ color: #aaa; }}
        </style>
    </head>
    <body>{cards}</body>
    </html>"""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
        f.write(html)
        webbrowser.open(f"file://{f.name}")


def binarizar(img_bgr):
    """Mesmo pipeline de binarização usado no exportar.py."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


# --- DIAGNÓSTICO DE MARCADORES ---

def diagnosticar_marcadores(img, config):
    """
    Verifica se os marcadores fiduciais foram encontrados e onde estão.
    Usa fallback progressivo de solidez (0.80 -> 0.72 -> 0.65) e
    estimativa afim para marcadores faltantes.

    Retorna:
        vis               — imagem anotada
        marcadores_completos — dict com 4 marcadores (reais + estimados)
        ok                — True se pelo menos 1 marcador foi detectado
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    candidatos = encontrar_marcadores_com_fallback(binary, debug=True)
    esperados = carregar_marcadores_esperados(config)
    vis = img.copy()

    print(f"\n[MARCADORES] {len(candidatos)} candidatos encontrados:")
    for cx, cy, area, solidez in candidatos:
        print(f"  ({cx:4d}, {cy:4d})  área={area:.0f}  solidez={solidez:.2f}")
        cv2.circle(vis, (cx, cy), 20, (0, 255, 255), 2)
        cv2.putText(vis, f"s={solidez:.2f}", (cx + 22, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

    if len(candidatos) == 0:
        print(f"  ERRO NENHUM marcador encontrado — impossível estimar posições.")
        return vis, {}, False

    h, w = img.shape[:2]
    try:
        marcadores_reais = classificar_cantos(candidatos, w, h)
    except ValueError as e:
        print(f"  ERRO Erro ao classificar cantos: {e}")
        return vis, {}, False

    n_reais = len(marcadores_reais)
    print(f"\n  {n_reais}/4 marcadores detectados:")

    # Verde = detectado
    for nome, (cx, cy) in marcadores_reais.items():
        cx, cy = int(cx), int(cy)
        ex, ey = esperados[nome]
        print(f"    OK {nome}: ({cx}, {cy})  dx={cx-ex:+.0f}px  dy={cy-ey:+.0f}px")
        cv2.rectangle(vis, (cx-12, cy-12), (cx+12, cy+12), (0, 220, 0), 2)
        cv2.putText(vis, nome[:3], (cx+14, cy+5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 0), 1)

    # Laranja = estimado por afim
    marcadores_completos = marcadores_reais
    if n_reais < 4:
        print(f"\n  Estimando {4 - n_reais} marcador(es) faltante(s)...")
        marcadores_completos = completar_marcadores_faltantes(
            marcadores_reais, esperados, debug=True
        )
        for nome, (cx, cy) in marcadores_completos.items():
            if nome not in marcadores_reais:
                cx, cy = int(cx), int(cy)
                cv2.rectangle(vis, (cx-12, cy-12), (cx+12, cy+12), (0, 140, 255), 2)
                cv2.putText(vis, f"~{nome[:3]}", (cx+14, cy+5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 140, 255), 1)

    return vis, marcadores_completos, True


# --- MAPA DE BOLHAS ---

def gerar_mapa_bolhas(img_alinhada, binary, config, resultados):
    """
    Desenha cada círculo na imagem alinhada com cor baseada no resultado:
      Verde   = marcado com confiança  (pct >= threshold + margem)
      Azul    = vazio                  (pct < threshold)
      Amarelo = marcado mas fraco      (marcado e pct < threshold + margem)
    """
    pos = carregar_posicoes(config)
    vis = img_alinhada.copy()
    margem = 0.08
    offset_y = _get_offset_y(config)
    threshold = _get_threshold(config)

    suspeitos = []

    for aluno in resultados:
        i = aluno["numero"] - 1
        cy = int(pos["primeira_linha_y_px"] + offset_y + i * pos["linha_altura_px"])
        raio = int(pos["raio_px"])

        cv2.putText(vis, str(aluno["numero"]),
                    (5, cy + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (100, 100, 100), 1)

        for dia in pos["dias"]:
            ax, jx = pos["circulos_x"][dia]

            for cx_f, tipo in [(ax, "almoco"), (jx, "janta")]:
                cx = int(cx_f)
                pct = aluno["dias"][dia][f"{tipo}_pct"]
                marcado = aluno["dias"][dia][tipo]

                # Cor base
                if pct >= threshold + margem:
                    cor = (0, 200, 0)      # verde — marcado confiante
                elif pct < threshold:
                    cor = (0, 0, 200)      # azul — vazio

                # Amarelo: marcado mas perto do threshold
                if marcado and pct < threshold + margem:
                    cor = (0, 200, 255)    # amarelo — marcado mas fraco
                    suspeitos.append((aluno["numero"], dia, tipo, pct, "marcado mas fraco"))

                cv2.circle(vis, (cx, cy), raio + 4, cor, 2)
                cv2.putText(vis, f"{pct:.0%}",
                            (cx - 14, cy + 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.28, cor, 1)

    return vis, suspeitos


# --- HISTOGRAMA DE PERCENTUAIS ---

def gerar_histograma(resultados, config):
    """
    Gera um histograma mostrando a distribuição de % de preenchimento
    de todas as bolhas. Bom pra ver se o threshold está bem posicionado.
    """
    pos = carregar_posicoes(config)
    threshold = _get_threshold(config)
    valores = []

    for aluno in resultados:
        for dia in pos["dias"]:
            for tipo in ["almoco", "janta"]:
                valores.append(aluno["dias"][dia][f"{tipo}_pct"])

    if not valores:
        return None

    h, w = 300, 600
    hist_img = np.ones((h, w, 3), dtype=np.uint8) * 30

    n_bins = 50
    counts, _ = np.histogram(valores, bins=n_bins, range=(0, 1))
    max_count = max(counts) if max(counts) > 0 else 1

    bin_w = w // n_bins
    for i, count in enumerate(counts):
        bar_h = int((count / max_count) * (h - 40))
        x = i * bin_w
        bin_center = (i + 0.5) / n_bins
        if bin_center >= threshold + 0.08:
            cor = (0, 180, 0)
        elif bin_center < threshold:
            cor = (120, 120, 120)
        else:
            cor = (0, 180, 220)
        cv2.rectangle(hist_img, (x, h - 30 - bar_h), (x + bin_w - 1, h - 30), cor, -1)

    tx = int(threshold * w)
    cv2.line(hist_img, (tx, 0), (tx, h - 30), (0, 0, 255), 2)
    cv2.putText(hist_img, f"threshold={threshold:.2f}",
                (tx + 4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1)
    cv2.putText(hist_img, "0%", (2, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    cv2.putText(hist_img, "100%", (w - 40, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    cv2.putText(hist_img, f"Total: {len(valores)} bolhas",
                (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

    print(f"\n[HISTOGRAMA] Distribuição de {len(valores)} bolhas:")
    print(f"  Mín: {min(valores):.1%}  Máx: {max(valores):.1%}  "
          f"Média: {np.mean(valores):.1%}  Mediana: {np.median(valores):.1%}")
    vazios   = [v for v in valores if v < threshold]
    ambiguos = [v for v in valores if threshold <= v < threshold + 0.08]
    marcados = [v for v in valores if v >= threshold + 0.08]
    print(f"  Vazios    : {len(vazios)} ({len(vazios)/len(valores):.0%})")
    print(f"  Ambíguos  : {len(ambiguos)} ({len(ambiguos)/len(valores):.0%})")
    print(f"  Marcados  : {len(marcados)} ({len(marcados)/len(valores):.0%})")

    return hist_img


# --- MAIN ---

def main():
    if len(sys.argv) < 3:
        print("Uso: python debug_bolhas.py <scan.pdf> <config.yaml> [pagina_pdf]")
        print()
        print("Exemplos:")
        print("  python debug_bolhas.py scan.pdf config_canela.yaml")
        print("  python debug_bolhas.py scan.pdf config_canela.yaml 2")
        sys.exit(1)

    caminho_pdf = sys.argv[1]
    caminho_config = sys.argv[2]
    pagina_idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    print(f"\n{'='*50}")
    print(f"  DEBUG DE BOLHAS — nruOMR")
    print(f"{'='*50}")
    print(f"  PDF    : {caminho_pdf}")
    print(f"  Config : {caminho_config}")
    print(f"  Página : {pagina_idx} (0-indexed)")

    with open(caminho_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    print(f"\n[CARREGANDO] PDF...")
    paginas = converter_pdf(caminho_pdf, dpi=config["scan"]["dpi"])
    if pagina_idx >= len(paginas):
        print(f"  ERRO: página {pagina_idx} não existe. PDF tem {len(paginas)} página(s).")
        sys.exit(1)

    img = np.array(paginas[pagina_idx])
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    print(f"  Shape: {img.shape}  DPI: {config['scan']['dpi']}")

    binary_crua = binarizar(img)

    print(f"\n[MARCADORES] Diagnosticando...")
    vis_marcadores, marcadores_completos, marcadores_ok = diagnosticar_marcadores(img, config)

    if not marcadores_ok:
        print("\n  ERRO NENHUM marcador detectado — impossível alinhar.")
        print("  Sugestões:")
        print("    1. Verifique se o scan está com boa resolução (200 DPI)")
        print("    2. Verifique se os cantos da página estão visíveis e não cortados")
        print("    3. Escaneie em preto e branco ou escala de cinza, não colorido")
        mostrar_no_navegador(
            ("1 — Original", img),
            ("2 — Binarizada (Otsu)", binary_crua),
            ("3 — Candidatos a marcador (amarelo)", vis_marcadores),
        )
        return

    print(f"\n[ALINHAMENTO] Aplicando homografia...")
    alinhada = processar(img, config, debug=True)
    binary_alinhada = binarizar(alinhada)

    print(f"\n[BOLHAS] Lendo círculos...")
    alunos_por_pagina = config["layout"]["alunos_por_pagina"]
    resultados = ler_pagina(binary_alinhada, config, alunos_por_pagina)

    vis_bolhas, suspeitos = gerar_mapa_bolhas(alinhada, binary_alinhada, config, resultados)
    hist = gerar_histograma(resultados, config)

    if suspeitos:
        print(f"\n[SUSPEITOS] {len(suspeitos)} bolhas com leitura duvidosa:")
        for num, dia, tipo, pct, motivo in suspeitos:
            print(f"  Aluno {num:3d}  {dia}  {tipo:6s}  {pct:.0%}  -> {motivo}")
    else:
        print(f"\n[SUSPEITOS] Nenhum — todas as leituras foram confiantes.")

    print(f"\n[BROWSER] Abrindo visualização...")
    pares = [
        ("1 — Original", img),
        ("2 — Binarizada crua (antes do alinhamento)", binary_crua),
        ("3 — Marcadores  [verde=detectado | laranja=estimado por afim | amarelo=candidato]", vis_marcadores),
        ("4 — Alinhada", alinhada),
        ("5 — Binarizada alinhada (o que o OMR lê)", binary_alinhada),
        ("6 — Mapa de bolhas  [verde=marcado | azul=vazio | amarelo=marcado mas fraco]", vis_bolhas),
    ]
    if hist is not None:
        pares.append((
            f"7 — Histograma de preenchimento  [threshold={THRESHOLD:.2f} em vermelho]",
            hist
        ))
    mostrar_no_navegador(*pares)

    print(f"\n{'='*50}")
    print(f"  Visualização aberta no navegador.")
    print(f"  Se houver muitos amarelos -> ajuste THRESHOLD em ler_bolhas.py")
    print(f"  Se marcadores falharem   -> rode localizar_marcadores.py pra diagnóstico")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()