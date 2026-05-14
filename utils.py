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