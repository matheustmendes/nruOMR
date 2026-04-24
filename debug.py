import cv2
import numpy as np
import base64
import webbrowser
import tempfile
from pdf2image import convert_from_path
from skimage import filters, exposure


def mostrar_no_navegador(*pares):
    cards = ""
    for titulo, imagem in pares:
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


# --- pipeline ---

paginas = convert_from_path("lista.pdf", dpi=200)
img = np.array(paginas[1])
img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
denoised = cv2.bilateralFilter(gray, 9, 75, 75)
enhanced = exposure.rescale_intensity(denoised)
thresh = filters.threshold_sauvola(enhanced, window_size=51)
binary = (enhanced > thresh).astype(np.uint8) * 255

print(f"shape da imagem: {img.shape}")
print(f"valores únicos no binário: {np.unique(binary)}")

# --- crop pra inspecionar detalhe ---
# ajusta essas coordenadas pra cair em cima de uma região com bolhas
y1, y2 = 500, 800
x1, x2 = 600, 1200

mostrar_no_navegador(
    ("original completo",   img),
    ("crop — original",     img[y1:y2, x1:x2]),
    ("crop — gray",         gray[y1:y2, x1:x2]),
    ("crop — denoised",     denoised[y1:y2, x1:x2]),
    ("crop — enhanced",     enhanced[y1:y2, x1:x2]),
    ("crop — binary",       binary[y1:y2, x1:x2])
)