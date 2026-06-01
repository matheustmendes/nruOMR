"""
exportar.py

Processa o PDF escaneado completo e exporta os resultados
como uma planilha com a contagem de presenças por aluno.

Uso:
    python exportar.py <scan.pdf> <config.yaml> <planilha_alunos.xlsx> <aba> [opções]

Opções:
    --merge scan2.pdf ...      Mescla múltiplos PDFs antes de processar
    --pagina-inicio N          Página do formulário onde o scan começa (padrão: 1)
    --correcoes arquivo.json   Aplica correções manuais da revisão

Exemplos:
    python exportar.py scan.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO"
    python exportar.py scan3.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO" --pagina-inicio 3
    python exportar.py scan1.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO" --merge scan2.pdf
"""

import sys
import os
import json
import traceback
import cv2
import numpy as np
import yaml
from openpyxl import Workbook

from localizar_marcadores import processar, carregar_marcadores_esperados, encontrar_marcadores, classificar_cantos
from ler_bolhas import ler_pagina, OFFSET_Y, THRESHOLD
from utils import converter_pdf


# --- MERGE DE PDFs ---

def carregar_todas_paginas(*caminhos_pdf, dpi=200):
    """
    Carrega todas as páginas de um ou mais PDFs.

    Se um PDF estiver corrompido, imprime aviso e continua com os demais
    em vez de abortar tudo.

    Args:
        caminhos_pdf: um ou mais caminhos de arquivos PDF
        dpi: resolução pra conversão

    Returns:
        Lista de imagens BGR (NumPy arrays)
    """
    todas = []
    for caminho in caminhos_pdf:
        if not os.path.isfile(caminho):
            print(f"  AVISO: arquivo não encontrado — {caminho}")
            continue

        print(f"Carregando {caminho}...")
        try:
            paginas = converter_pdf(caminho, dpi=dpi)
        except Exception as e:
            print(f"  ERRO ao carregar {caminho}: {e}")
            print(f"  Este PDF será ignorado.")
            continue

        n_antes = len(todas)
        for pag in paginas:
            img = np.array(pag)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            todas.append(img)
        print(f"  {len(todas) - n_antes} páginas carregadas")

    if not todas:
        raise RuntimeError("Nenhuma página foi carregada. Verifique os arquivos PDF.")

    print(f"Total: {len(todas)} páginas")
    return todas


# --- DETECÇÃO DE PÁGINAS EM BRANCO ---

def eh_pagina_branca(img, threshold_pct=2.0):
    """
    Detecta se uma página é branca (verso do duplex).

    Páginas em branco têm < 1% de pixels marcados.
    Páginas reais têm > 5%.
    Threshold de 2% separa bem.

    Args:
        img: imagem BGR
        threshold_pct: porcentagem mínima de pixels pra considerar não-branca

    Returns:
        True se a página for branca
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    pct = (np.count_nonzero(binary) / binary.size) * 100
    return pct < threshold_pct


# --- DETECÇÃO DE NÚMERO DE PÁGINA VIA OCR ---

def _detectar_numero_pagina(alinhada: np.ndarray, config: dict):
    """
    Lê o número da página do cabeçalho ("Página X de Y") via OCR.

    Usa pytesseract se disponível. A posição do texto é calculada a partir
    do layout fixo de gerar_template.py (47mm do topo, borda direita a 195mm).

    Retorna int com o número da página, ou None se OCR não estiver disponível
    ou o texto não foi reconhecido.

    Para ativar: pip install pytesseract
                 + Tesseract-OCR: https://github.com/UB-Mannheim/tesseract/wiki
    """
    try:
        import pytesseract
        import re as _re
    except ImportError:
        return None

    pytesseract.pytesseract.tesseract_cmd = (
        r"C:\Users\matheus.torres\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"
    )

    dpi = config["scan"]["dpi"]

    def mm_px(mm):
        return (mm / 25.4) * dpi

    # "Página X de Y" é desenhado em y ≈ 47mm do topo no formulário alinhado.
    # Derivado de gerar_template.py:
    #   y = altura - MARCADOR_MARGEM(15) - MARCADOR_TAM(5) - 12mm = 32mm do topo
    #   y -= 5mm (título "Pró-Reitoria")      → 37mm
    #   y -= 5mm (título "RELAÇÃO...")         → 42mm
    #   y -= 5mm (linha de datas)              → 47mm  ← texto aqui
    y_base = int(mm_px(47))
    x_dir = int(mm_px(195))  # borda direita do texto (largura - margem = 210-15)

    # Recorta apenas a linha do número de página, com pequena folga
    y1 = max(0, y_base - int(mm_px(4)))
    y2 = min(alinhada.shape[0], y_base + int(mm_px(4)))
    x1 = max(0, x_dir - int(mm_px(75)))  # 75mm de largura a partir da borda direita
    x2 = min(alinhada.shape[1], x_dir + 5)

    if y2 <= y1 or x2 <= x1:
        return None

    crop = alinhada[y1:y2, x1:x2]
    if len(crop.shape) == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    # Binariza e amplia 3× para melhorar reconhecimento de dígitos pequenos
    _, crop_bin = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    crop_up = cv2.resize(crop_bin, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)

    try:
        text = pytesseract.image_to_string(
            crop_up,
            config="--psm 7 --oem 3"
        ).strip()
    except Exception:
        return None

    m = _re.search(r"[Pp][aá]gina\s+(\d+)\s+de\b", text, _re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


# --- PROCESSAMENTO COMPLETO ---

def processar_pdf_completo(paginas, config, retornar_imagens=False):
    """
    Processa todas as páginas do scan:
    1. Pula páginas em branco
    2. Alinha cada página usando marcadores
    3. Lê as bolhas
    4. Reordena pelo número impresso no formulário (via OCR), se disponível

    A reordenação automática corrige casos em que o scanner embaralha as folhas
    durante o scan em massa. Se o OCR não estiver disponível (pytesseract não
    instalado), as páginas são processadas na ordem do scan.

    Args:
        paginas: lista de imagens BGR
        config: dict do config.yaml
        retornar_imagens: se True, retorna também (paginas_alinhadas, binarios,
            resultados_por_pagina) para uso no fluxo de revisão.

    Returns:
        Se retornar_imagens=False: lista de resultados por aluno
        Se retornar_imagens=True: (resultados, paginas_alinhadas, binarios, resultados_por_pagina)
    """
    alunos_por_pagina = config["layout"]["alunos_por_pagina"]
    todos_resultados = []
    paginas_alinhadas_out = []
    binarios_out = []
    resultados_por_pagina_out = []
    paginas_brutas = []  # acumula páginas antes de ordenar
    paginas_brancas = 0
    paginas_com_erro = 0

    for i, img in enumerate(paginas):
        # Detecta página em branco
        if eh_pagina_branca(img):
            paginas_brancas += 1
            print(f"  [BRANCA] PDF página {i + 1} — ignorada")
            continue

        print(f"  Processando PDF página {i + 1}...")

        try:
            # Alinha
            alinhada = processar(img, config)

            # Binariza
            gray = cv2.cvtColor(alinhada, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            # Lê bolhas (numeração local: 1 a alunos_por_pagina)
            resultados = ler_pagina(binary, config, alunos_por_pagina)

            # Tenta detectar o número da página via OCR
            num_detectado = _detectar_numero_pagina(alinhada, config)

            paginas_brutas.append({
                "resultados": resultados,
                "num_detectado": num_detectado,
                "alinhada": alinhada,
                "binary": binary,
            })

            label = f"página {num_detectado}" if num_detectado is not None else "número não detectado"
            print(f"    ✓ {len(resultados)} alunos lidos ({label})")

        except Exception as e:
            paginas_com_erro += 1
            print(f"    ERRO na página {i + 1}: {e}")
            print("    --- Traceback ---")
            traceback.print_exc()
            print("    -----------------")
            print("    Esta página será ignorada. Verifique se os marcadores estão visíveis no scan.")
            continue

    # Reordenar páginas se todos os números foram detectados com sucesso
    todos_detectados = bool(paginas_brutas) and all(
        p["num_detectado"] is not None for p in paginas_brutas
    )

    if todos_detectados and len(paginas_brutas) > 1:
        nums_antes = [p["num_detectado"] for p in paginas_brutas]
        paginas_brutas.sort(key=lambda p: p["num_detectado"])
        nums_depois = [p["num_detectado"] for p in paginas_brutas]
        if nums_antes != nums_depois:
            print(f"\n  Páginas reordenadas: {nums_antes} → {nums_depois}")
        else:
            print(f"\n  Ordem já estava correta (páginas: {nums_depois})")
    elif paginas_brutas:
        n_det = sum(1 for p in paginas_brutas if p["num_detectado"] is not None)
        if n_det == 0:
            print(f"\n  OCR indisponível — usando ordem do scan.")
            print(f"  Para reordenamento automático: pip install pytesseract + instalar Tesseract-OCR")
        else:
            print(f"\n  AVISO: número detectado em {n_det}/{len(paginas_brutas)} páginas — mantendo ordem do scan.")

    # Atribuir numeração global de aluno com base na ordem correta
    for idx, p in enumerate(paginas_brutas):
        pagina_template = p["num_detectado"] if todos_detectados else (1 + idx)

        for r in p["resultados"]:
            r["numero"] = (pagina_template - 1) * alunos_por_pagina + r["numero"]

        todos_resultados.extend(p["resultados"])

        if retornar_imagens:
            paginas_alinhadas_out.append(p["alinhada"])
            binarios_out.append(p["binary"])
            resultados_por_pagina_out.append(p["resultados"])

    n_processadas = len(paginas_brutas)
    print(f"\nResumo:")
    print(f"  {n_processadas} páginas processadas")
    print(f"  {paginas_brancas} em branco puladas")
    if paginas_com_erro:
        print(f"  {paginas_com_erro} com ERRO (marcadores não detectados?)")
    print(f"  {len(todos_resultados)} alunos lidos no total")

    # Se absolutamente nada foi processado, levanta erro claro
    if n_processadas > 0 and not todos_resultados:
        raise RuntimeError(
            f"Nenhuma página foi processada com sucesso — {paginas_com_erro} página(s) com erro. "
            f"Causa mais provável: marcadores fiduciais não detectados no scan. "
            f"Rode 'python localizar_marcadores.py <scan.pdf> <config.yaml>' pra diagnosticar."
        )

    if retornar_imagens:
        return todos_resultados, paginas_alinhadas_out, binarios_out, resultados_por_pagina_out
    return todos_resultados


# --- CONTAGEM DE PRESENÇAS ---

def contar_presencas(resultados, dias):
    """
    Converte os resultados de bolhas em contagem de presenças por aluno.

    Regra: se almoço OU janta está marcado num dia, conta 1 presença.

    Args:
        resultados: lista de dicts do ler_bolhas
        dias: lista de nomes dos dias

    Returns:
        Lista de dicts:
        {
            "numero": 1,
            "presencas": 3,
            "detalhes": {
                "Segunda": {"presente": True, "almoco": True, "janta": False},
                ...
            }
        }
    """
    contagem = []

    for aluno in resultados:
        total_dias = 0
        detalhes = {}

        for dia in dias:
            d = aluno["dias"][dia]
            presente = d["almoco"] or d["janta"]
            if presente:
                total_dias += 1

            detalhes[dia] = {
                "presente": presente,
                "almoco": d["almoco"],
                "janta": d["janta"],
            }

        contagem.append({
            "numero": aluno["numero"],
            "presencas": total_dias,
            "detalhes": detalhes,
        })

    return contagem


# --- LEITURA DE NOMES DA PLANILHA ---

def ler_nomes_alunos(caminho_xlsx, nome_aba):
    """
    Lê os nomes e matrículas da planilha de impressão.

    Returns:
        Lista de (nome, matricula)
    """
    from openpyxl import load_workbook
    wb = load_workbook(caminho_xlsx, read_only=True)
    ws = wb[nome_aba]

    alunos = []
    for row in ws.iter_rows(min_row=8, values_only=True):
        if len(row) < 3:
            continue
        nome = row[1]
        matricula = row[2]
        if not nome or not str(nome).strip():
            continue

        nome = str(nome).strip()
        if matricula is None:
            matricula = ""
        elif isinstance(matricula, float):
            matricula = str(int(matricula))
        else:
            matricula = str(matricula).strip()

        alunos.append((nome, matricula))

    wb.close()
    return alunos


# --- EXPORTAÇÃO XLSX ---

def exportar_xlsx(contagem, alunos, dias, arquivo_saida):
    """
    Gera a planilha de saída com presenças por aluno.

    Formato:
        Nº | Nome | Matrícula | Presenças | Seg | Ter | Qua | Qui | Sex

    Args:
        contagem: lista de dicts com presenças
        alunos: lista de (nome, matricula)
        dias: lista de nomes dos dias
        arquivo_saida: caminho do xlsx de saída
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Presenças"

    # Cabeçalho
    headers = ["Nº", "Nome", "Matrícula", "Presenças"]
    for dia in dias:
        headers.append(dia)
    ws.append(headers)

    # Dados
    for c in contagem:
        idx = c["numero"] - 1
        if 0 <= idx < len(alunos):
            nome, matricula = alunos[idx]
        else:
            nome, matricula = f"Aluno {c['numero']}", ""

        row = [c["numero"], nome, matricula, c["presencas"]]
        for dia in dias:
            d = c["detalhes"][dia]
            if d["presente"]:
                marcas = ""
                if d["almoco"]:
                    marcas += "A"
                if d["janta"]:
                    marcas += "J"
                row.append(marcas)
            else:
                row.append("")
        ws.append(row)

    # Ajusta largura das colunas
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12
    for i, dia in enumerate(dias):
        col_letter = chr(ord("E") + i)
        ws.column_dimensions[col_letter].width = 10

    wb.save(arquivo_saida)
    print(f"\nPlanilha exportada: {arquivo_saida}")

    total_alunos = len(contagem)
    com_presenca = sum(1 for c in contagem if c["presencas"] > 0)
    print(f"  {total_alunos} alunos")
    print(f"  {com_presenca} com pelo menos 1 presença")


def aplicar_correcoes(resultados, correcoes):
    """
    Aplica as correções da revisão manual nos resultados.

    O correcoes.json tem o formato:
        {"1_Segunda_almoco": true, "3_Terça_janta": false, ...}
    """
    alterados = 0
    for r in resultados:
        for dia in r["dias"]:
            for tipo in ["almoco", "janta"]:
                chave = f"{r['numero']}_{dia}_{tipo}"
                if chave in correcoes:
                    valor_original = r["dias"][dia][tipo]
                    valor_novo = correcoes[chave]
                    if valor_original != valor_novo:
                        r["dias"][dia][tipo] = valor_novo
                        alterados += 1

    if alterados:
        print(f"  {alterados} correções aplicadas")

    return resultados


# --- EXECUÇÃO ---

def main():
    if len(sys.argv) < 5:
        print("Uso: python exportar.py <scan.pdf> <config.yaml> <planilha_alunos.xlsx> <aba> [opções]")
        print()
        print("Opções:")
        print("  --merge scan2.pdf ...      Mescla múltiplos PDFs antes de processar")
        print("  --correcoes arquivo.json   Aplica correções manuais da revisão")
        print()
        print("Exemplos:")
        print('  python exportar.py scan.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO"')
        print('  python exportar.py scan1.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO" --merge scan2.pdf')
        sys.exit(1)

    caminho_scan = sys.argv[1]
    caminho_config = sys.argv[2]
    caminho_alunos = sys.argv[3]
    nome_aba = sys.argv[4]

    # Checa se tem merge
    pdfs = [caminho_scan]
    if "--merge" in sys.argv:
        idx = sys.argv.index("--merge")
        extras = []
        for arg in sys.argv[idx + 1:]:
            if arg.startswith("--"):
                break
            extras.append(arg)
        pdfs.extend(extras)

    # Checa se tem correções
    correcoes = None
    if "--correcoes" in sys.argv:
        idx = sys.argv.index("--correcoes")
        caminho_correcoes = sys.argv[idx + 1]
        with open(caminho_correcoes, "r") as f:
            correcoes = json.load(f)
        print(f"Correções carregadas de {caminho_correcoes}")

    # Carrega config
    with open(caminho_config, "r") as f:
        config = yaml.safe_load(f)

    dias = config["layout"]["dias"]

    # Carrega todas as páginas
    print("=== Carregando PDFs ===")
    paginas = carregar_todas_paginas(*pdfs, dpi=config["scan"]["dpi"])

    # Processa
    print("\n=== Processando páginas ===")
    resultados = processar_pdf_completo(paginas, config)

    # Aplica correções se houver
    if correcoes:
        print("\n=== Aplicando correções ===")
        resultados = aplicar_correcoes(resultados, correcoes)

    # Conta presenças
    contagem = contar_presencas(resultados, dias)

    # Lê nomes dos alunos
    alunos = ler_nomes_alunos(caminho_alunos, nome_aba)

    # Exporta
    nome_restaurante = config.get("restaurante", "resultado").lower().replace(" ", "_")
    arquivo_saida = f"presencas_{nome_restaurante}.xlsx"
    exportar_xlsx(contagem, alunos, dias, arquivo_saida)


if __name__ == "__main__":
    main()