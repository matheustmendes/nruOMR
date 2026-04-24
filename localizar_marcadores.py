"""
localizar_marcadores.py

Detecta os 4 marcadores fiduciais no scan e alinha a imagem
usando homografia. Usa o config.yaml pra saber onde os marcadores
deveriam estar.

Uso standalone (pra debug):
    python localizar_marcadores.py scan.png config_canela.yaml
"""

import sys
import cv2
import numpy as np
import yaml
from reportlab.lib.units import mm


def mm_para_px(valor_mm: float, dpi: int = 200) -> float:
    """Converte milímetros para pixels dado um DPI."""
    return (valor_mm / 25.4) * dpi


def encontrar_marcadores(binary: np.ndarray) -> list:
    """
    Encontra os 4 marcadores fiduciais na imagem binarizada.

    A detecção usa três filtros:
      1. Área: entre 800 e 5000 px² (5mm a 200dpi ≈ 39px → ~1521px²)
      2. Aspecto: entre 0.7 e 1.3 (deve ser quadrado)
      3. Solidez: acima de 0.85 (quadrado preenchido vs círculo oco ~0.75)

    Args:
        binary: imagem binarizada com BINARY_INV (objetos em branco)

    Returns:
        Lista de (cx, cy, area, solidez) dos candidatos
    """
    contornos, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    candidatos = []
    for cnt in contornos:
        area = cv2.contourArea(cnt)

        # Filtro 1: área esperada de um marcador de 5mm a 200dpi
        if area < 800 or area > 5000:
            continue

        # Filtro 2: deve ser aproximadamente quadrado
        x, y, w, h = cv2.boundingRect(cnt)
        aspecto = w / h if h > 0 else 0
        if aspecto < 0.7 or aspecto > 1.3:
            continue

        # Filtro 3: solidez alta = preenchido (marcador), baixa = oco (círculo)
        solidez = area / (w * h)
        if solidez < 0.85:
            continue

        cx = x + w // 2
        cy = y + h // 2
        candidatos.append((cx, cy, area, solidez))

    return candidatos


def classificar_cantos(candidatos: list, largura_img: int, altura_img: int) -> dict:
    """
    Identifica qual marcador pertence a qual canto da imagem.

    Cada marcador é atribuído ao canto mais próximo dele.

    Args:
        candidatos: lista de (cx, cy, area, solidez)
        largura_img: largura da imagem em pixels
        altura_img: altura da imagem em pixels

    Returns:
        dict com chaves sup_esq, sup_dir, inf_esq, inf_dir
        cada valor é (cx, cy)
    """
    if len(candidatos) < 4:
        raise ValueError(
            f"Esperados 4 marcadores, encontrados {len(candidatos)}. "
            "Verifique se o scan está limpo e os marcadores estão visíveis."
        )

    cantos_ref = {
        "superior_esquerdo": (0, 0),
        "superior_direito": (largura_img, 0),
        "inferior_esquerdo": (0, altura_img),
        "inferior_direito": (largura_img, altura_img),
    }

    resultado = {}
    usados = set()

    for nome, (rx, ry) in cantos_ref.items():
        melhor = None
        menor_dist = float("inf")

        for i, (cx, cy, *_) in enumerate(candidatos):
            if i in usados:
                continue
            dist = ((cx - rx) ** 2 + (cy - ry) ** 2) ** 0.5
            if dist < menor_dist:
                menor_dist = dist
                melhor = i

        if melhor is not None:
            resultado[nome] = (candidatos[melhor][0], candidatos[melhor][1])
            usados.add(melhor)

    return resultado


def carregar_marcadores_esperados(config: dict) -> dict:
    """
    Lê as posições esperadas dos marcadores do config.yaml
    e converte de mm pra pixels.

    No config, as coordenadas Y são medidas do TOPO da página
    (mesmo sistema do OpenCV). O reportlab usa Y do fundo,
    mas o config já foi salvo na convenção do topo.

    Args:
        config: dict carregado do config.yaml

    Returns:
        dict com chaves sup_esq, sup_dir, inf_esq, inf_dir
        cada valor é (x_px, y_px)
    """
    dpi = config["scan"]["dpi"]
    marc = config["marcadores"]
    tam = marc["tamanho_mm"]

    # O centro do marcador é a posição + metade do tamanho
    meio = tam / 2

    posicoes = marc["posicoes"]
    resultado = {}

    for nome, pos in posicoes.items():
        x_mm = pos["x"] + meio
        y_mm = pos["y"] + meio
        x_px = mm_para_px(x_mm, dpi)
        y_px = mm_para_px(y_mm, dpi)
        resultado[nome] = (x_px, y_px)

    return resultado


def alinhar_imagem(img: np.ndarray, marcadores_reais: dict, marcadores_esperados: dict) -> np.ndarray:
    """
    Aplica homografia pra alinhar a imagem escaneada.

    A homografia é uma transformação que corrige translação,
    rotação e distorção de perspectiva de uma vez. Ela mapeia
    as posições reais dos marcadores nas posições ideais.

    Args:
        img: imagem original (colorida ou cinza)
        marcadores_reais: posições detectadas no scan
        marcadores_esperados: posições do config.yaml (em pixels)

    Returns:
        Imagem alinhada com as mesmas dimensões
    """
    ordem = ["superior_esquerdo", "superior_direito", "inferior_esquerdo", "inferior_direito"]

    pts_reais = np.float32([marcadores_reais[k] for k in ordem])
    pts_esperados = np.float32([marcadores_esperados[k] for k in ordem])

    H, _ = cv2.findHomography(pts_reais, pts_esperados)

    altura, largura = img.shape[:2]
    alinhada = cv2.warpPerspective(img, H, (largura, altura))

    return alinhada


def processar(img: np.ndarray, config: dict, debug: bool = False) -> np.ndarray:
    """
    Pipeline completo: preprocessa, encontra marcadores, alinha.

    Args:
        img: imagem BGR do scan
        config: dict carregado do config.yaml
        debug: se True, imprime informações de diagnóstico

    Returns:
        Imagem alinhada (BGR)
    """
    # Preprocessamento
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Encontra marcadores
    candidatos = encontrar_marcadores(binary)
    if debug:
        print(f"Candidatos a marcador: {len(candidatos)}")
        for c in candidatos:
            print(f"  ({c[0]}, {c[1]}) area={c[2]:.0f} solidez={c[3]:.2f}")

    # Classifica cantos
    h, w = img.shape[:2]
    marcadores_reais = classificar_cantos(candidatos, w, h)
    if debug:
        print(f"\nMarcadores classificados:")
        for nome, (cx, cy) in marcadores_reais.items():
            print(f"  {nome}: ({cx}, {cy})")

    # Posições esperadas do config
    marcadores_esperados = carregar_marcadores_esperados(config)
    if debug:
        print(f"\nMarcadores esperados (px):")
        for nome, (cx, cy) in marcadores_esperados.items():
            print(f"  {nome}: ({cx:.0f}, {cy:.0f})")

        # Calcula deslocamento
        print(f"\nDeslocamento (real - esperado):")
        for nome in marcadores_reais:
            rx, ry = marcadores_reais[nome]
            ex, ey = marcadores_esperados[nome]
            print(f"  {nome}: dx={rx-ex:.0f}px, dy={ry-ey:.0f}px")

    # Alinha
    alinhada = alinhar_imagem(img, marcadores_reais, marcadores_esperados)

    return alinhada


# --- EXECUÇÃO STANDALONE (debug) ---

def debug_visual(img, marcadores_reais, marcadores_esperados, alinhada):
    """
    Gera uma página HTML com:
    1. Imagem original com marcadores detectados circulados em verde
    2. Imagem alinhada com posições esperadas circuladas em azul
    """
    import base64
    import webbrowser
    import tempfile

    # Copia pra não alterar a original
    vis_original = img.copy()
    vis_alinhada = alinhada.copy()

    # Desenha marcadores reais (verde) na imagem original
    for nome, (cx, cy) in marcadores_reais.items():
        # Círculo verde no marcador
        cv2.circle(vis_original, (cx, cy), 30, (0, 255, 0), 3)
        # Cruz no centro
        cv2.line(vis_original, (cx - 15, cy), (cx + 15, cy), (0, 255, 0), 2)
        cv2.line(vis_original, (cx, cy - 15), (cx, cy + 15), (0, 255, 0), 2)
        # Label
        cv2.putText(vis_original, nome, (cx + 35, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

    # Desenha posições esperadas (azul) na imagem alinhada
    for nome, (cx, cy) in marcadores_esperados.items():
        cx, cy = int(cx), int(cy)
        cv2.circle(vis_alinhada, (cx, cy), 30, (255, 0, 0), 3)
        cv2.line(vis_alinhada, (cx - 15, cy), (cx + 15, cy), (255, 0, 0), 2)
        cv2.line(vis_alinhada, (cx, cy - 15), (cx, cy + 15), (255, 0, 0), 2)
        cv2.putText(vis_alinhada, nome, (cx + 35, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)

    # Gera HTML
    cards = ""
    for titulo, imagem in [("Original — marcadores detectados (verde)", vis_original),
                            ("Alinhada — posições esperadas (azul)", vis_alinhada)]:
        _, buffer = cv2.imencode(".png", imagem)
        b64 = base64.b64encode(buffer).decode("utf-8")
        cards += f"""
        <div style="margin-bottom: 2rem;">
            <h2 style="font-family: monospace; margin-bottom: 0.5rem;">{titulo}</h2>
            <img src="data:image/png;base64,{b64}" style="max-width: 100%; border: 1px solid #ccc;">
        </div>
        """

    html = f"""<!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: sans-serif; padding: 2rem; background: #f5f5f5; }}
        </style>
    </head>
    <body>{cards}</body>
    </html>"""

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
        f.write(html)
        webbrowser.open(f"file://{f.name}")


def carregar_imagem(caminho: str, pagina: int = 0) -> np.ndarray:
    """
    Carrega imagem de PNG/JPG ou PDF.

    Args:
        caminho: caminho do arquivo (.png, .jpg, .pdf)
        pagina: índice da página (só pra PDF, 0 = primeira)

    Returns:
        Imagem BGR como array NumPy
    """
    if caminho.lower().endswith(".pdf"):
        from pdf2image import convert_from_path
        paginas = convert_from_path(caminho, dpi=200)
        if not paginas:
            raise ValueError("PDF sem páginas ou corrompido")
        if pagina >= len(paginas):
            raise IndexError(f"Página {pagina} não existe — o PDF tem {len(paginas)} página(s)")
        img = np.array(paginas[pagina])
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        img = cv2.imread(caminho)
        if img is None:
            raise FileNotFoundError(f"Imagem não encontrada: {caminho}")
    return img


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python localizar_marcadores.py <scan.pdf ou scan.png> <config.yaml> [pagina]")
        print()
        print("Exemplos:")
        print("  python localizar_marcadores.py scan.pdf config_canela.yaml")
        print("  python localizar_marcadores.py scan.pdf config_canela.yaml 1  # página 2 (duplex)")
        sys.exit(1)

    caminho_scan = sys.argv[1]
    caminho_config = sys.argv[2]
    pagina = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    img = carregar_imagem(caminho_scan, pagina)

    with open(caminho_config, "r") as f:
        config = yaml.safe_load(f)

    # Preprocessamento pra encontrar marcadores
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Encontra e classifica
    candidatos = encontrar_marcadores(binary)
    h, w = img.shape[:2]
    marcadores_reais = classificar_cantos(candidatos, w, h)
    marcadores_esperados = carregar_marcadores_esperados(config)

    print(f"Marcadores encontrados: {len(candidatos)}")
    print(f"\nDeslocamento (real - esperado):")
    for nome in marcadores_reais:
        rx, ry = marcadores_reais[nome]
        ex, ey = marcadores_esperados[nome]
        print(f"  {nome}: dx={rx-ex:.0f}px, dy={ry-ey:.0f}px")

    # Alinha
    alinhada = alinhar_imagem(img, marcadores_reais, marcadores_esperados)

    # Abre debug visual no navegador
    debug_visual(img, marcadores_reais, marcadores_esperados, alinhada)
    print("\nDebug visual aberto no navegador.")