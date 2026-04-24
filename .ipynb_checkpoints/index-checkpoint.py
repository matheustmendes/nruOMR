import cv2
import numpy as np
from pdf2image import convert_from_path
from skimage import filters, exposure


def preprocessar(caminho: str, pagina: int = 0, debug: bool = False) -> np.ndarray:
    """
    Lê e pré-processa uma página de um PDF de gabarito.

    Args:
        caminho: Caminho para o arquivo PDF.
        pagina:  Índice da página (0 = primeira). Default: 0.
        debug:   Se True, salva imagens intermediárias para inspeção.

    Returns:
        Imagem binária (uint8, valores 0 ou 255).
    """
    # 1. Leitura do PDF
    paginas = convert_from_path(caminho, dpi=200)
    if not paginas:
        raise ValueError("PDF sem páginas ou corrompido")
    if pagina >= len(paginas):
        raise IndexError(f"Página {pagina} não existe — o PDF tem {len(paginas)} página(s)")

    # 2. Conversão para array NumPy no formato BGR (OpenCV)
    img = np.array(paginas[pagina])
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # 3. Escala de cinza
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 4. Filtro bilateral — suaviza ruído preservando bordas das marcações
    denoised = cv2.bilateralFilter(gray, 9, 75, 75)

    # 5. Normalização de intensidade — compensa variações do papel reciclado
    enhanced = exposure.rescale_intensity(denoised)

    # 6. Binarização com Sauvola — threshold local, robusto para fundo irregular
    # window_size ~ 2x o diâmetro da bolinha em pixels (a 200 DPI, ~5mm ≈ 40px)
    thresh = filters.threshold_sauvola(enhanced, window_size=51)
    binary = (enhanced > thresh).astype(np.uint8) * 255

    if debug:
        cv2.imwrite("debug_gray.png", gray)
        cv2.imwrite("debug_denoised.png", denoised)
        cv2.imwrite("debug_binary.png", binary)
        print(f"[debug] shape: {img.shape} | páginas no PDF: {len(paginas)}")

    return binary


if __name__ == "__main__":
    resultado = preprocessar("lista.pdf", pagina=0, debug=True)
    print(f"Shape final: {resultado.shape}")
    print(f"Dtype: {resultado.dtype}")
    print(f"Valores únicos: {np.unique(resultado)}")  # deve ser [0, 255]