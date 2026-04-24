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
    Procura em:
      1. ./poppler/Library/bin/
      2. ./poppler/bin/
      3. ./poppler/
    Se não encontrar, retorna None (usa o PATH do sistema).
    """
    candidatos = [
        os.path.join(SCRIPT_DIR, "poppler", "Library", "bin"),
        os.path.join(SCRIPT_DIR, "poppler", "bin"),
        os.path.join(SCRIPT_DIR, "poppler"),
    ]
    for c in candidatos:
        if os.path.isdir(c) and any(
            f.startswith("pdftoppm") for f in os.listdir(c)
        ):
            return c
    return None


def converter_pdf(caminho: str, dpi: int = 200) -> list:
    """
    Converte PDF em lista de imagens PIL.
    Usa Poppler portátil se disponível.
    """
    poppler = _poppler_path()
    kwargs = {"dpi": dpi}
    if poppler:
        kwargs["poppler_path"] = poppler
    return _convert(caminho, **kwargs)