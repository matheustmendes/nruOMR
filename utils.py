"""
utils.py

Utilitários compartilhados entre os módulos.
Centraliza o caminho do Poppler pra funcionar
no Windows sem instalação global.
"""

import os
import sys
from pdf2image import convert_from_path as _convert

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _poppler_path():
    """
    Encontra o Poppler portátil na pasta do projeto.
    Procura recursivamente por pdftoppm.exe dentro da pasta poppler/.
    """
    pasta_poppler = os.path.join(SCRIPT_DIR, "poppler")
    if not os.path.isdir(pasta_poppler):
        return None

    # Percorre toda a árvore da pasta poppler procurando pdftoppm.exe
    for raiz, dirs, arquivos in os.walk(pasta_poppler):
        for arquivo in arquivos:
            if arquivo.lower().startswith("pdftoppm"):
                print(f"[utils] Poppler encontrado em: {raiz}")
                return raiz

    return None


def converter_pdf(caminho: str, dpi: int = 200) -> list:
    """
    Converte PDF em lista de imagens PIL.
    Usa Poppler portátil se disponível.
    """
    poppler = _poppler_path()
    if poppler:
        return _convert(caminho, dpi=dpi, poppler_path=poppler)
    else:
        print("[utils] Poppler portátil não encontrado, tentando PATH do sistema...")
        return _convert(caminho, dpi=dpi)

# --- OCR ---

def _caminhos_tesseract():
    """Locais onde o Tesseract costuma estar, em ordem de preferência."""
    return [
        os.environ.get("TESSERACT_CMD"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]


def obter_pytesseract():
    """
    Devolve o módulo pytesseract já apontado para um executável existente,
    ou None se o OCR não estiver disponível nesta máquina.

    O caminho era fixo no perfil de um usuário específico, o que fazia o OCR
    simplesmente não funcionar em qualquer outro computador — sem erro visível,
    só resultados piores. Aqui ele é procurado nos lugares usuais e pode ser
    apontado por TESSERACT_CMD.
    """
    try:
        import pytesseract
    except ImportError:
        return None

    import shutil

    for caminho in _caminhos_tesseract():
        if caminho and os.path.isfile(caminho):
            pytesseract.pytesseract.tesseract_cmd = caminho
            return pytesseract

    encontrado = shutil.which("tesseract")
    if encontrado:
        pytesseract.pytesseract.tesseract_cmd = encontrado
        return pytesseract

    return None


def stdout_tolerante():
    """
    Impede que um caractere fora da tabela do console derrube o programa.

    O console do Windows costuma abrir em cp1252, que não tem "OK" (U+2713)
    nem setas. Como o `print` de progresso fica dentro do `try` que processa
    cada página, um UnicodeEncodeError ali era capturado pelo `except` e a
    página inteira aparecia como "ERRO na página N" — um problema de console
    disfarçado de falha de leitura.

    Mantém a codificação atual (acentos continuam corretos onde já eram) e
    troca só o que não couber.
    """
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
