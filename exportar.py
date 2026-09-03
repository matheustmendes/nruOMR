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
    import re as _re
    from utils import obter_pytesseract

    pytesseract = obter_pytesseract()
    if pytesseract is None:
        return None

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


# --- RECONSTRUÇÃO DA NUMERAÇÃO DAS PÁGINAS ---

def _inferir_numeros_paginas(detectados):
    """
    Completa a numeração das páginas a partir das que o OCR conseguiu ler.

    Por que isso é necessário
    -------------------------
    O OCR do número de página falha em algumas folhas — num maço real de
    Ondina, em 5 de 27. A lógica anterior era tudo-ou-nada: bastava uma falha
    para o sistema desistir de ordenar e usar a ordem do arquivo. E a ordem do
    arquivo costuma estar **invertida** (o scanner entrega da última folha para
    a primeira), o que faz cada pessoa receber a presença de outra sem nenhum
    sinal de erro.

    Pior que falhar é errar
    -----------------------
    No mesmo maço, a página 14 foi lida como "4" — o traço do "1" se perdeu.
    Um número errado é mais perigoso do que um ausente, porque parece
    confiável. Por isso a reconstrução não trata as leituras como verdade: ela
    procura a progressão que explica o **maior número** delas e descarta as que
    destoam, do mesmo jeito que se ajusta uma reta ignorando pontos fora da
    curva.

    Como funciona
    -------------
    Páginas escaneadas em sequência formam uma progressão de passo ±1. Cada
    leitura propõe uma progressão (passo e origem); a que reúne mais leituras
    vence e passa a valer para todo o seu trecho, sobrepondo as discordantes.
    Um maço fatiado em vários arquivos gera vários trechos, resolvidos um a um.

    Nada é adivinhado sem base: se sobrar página sem número, ou se dois
    resultados colidirem, devolve None e o chamador mantém a ordem do scan —
    com aviso.

    Args:
        detectados: número lido em cada página, None onde o OCR falhou.

    Returns:
        (lista de números, aviso) ou (None, motivo da desistência)
    """
    total = len(detectados)
    if total == 0:
        return None, "nenhuma página"

    ancoras = [(i, n) for i, n in enumerate(detectados) if n is not None]
    if len(ancoras) < 2:
        return None, "menos de duas páginas com número legível"

    numeros = [None] * total
    corrigidas = 0
    restantes = list(ancoras)

    while restantes:
        # Cada âncora propõe duas progressões (crescente e decrescente). A
        # progressão é identificada por (passo, origem): origem = n - i*passo.
        propostas = {}
        for i, n in restantes:
            for passo in (1, -1):
                propostas.setdefault((passo, n - i * passo), []).append((i, n))

        # Em empate de votos, prefere o span MENOR — não o maior. Duas
        # âncoras longe uma da outra caindo por coincidência na mesma reta é
        # bem mais provável do que duas âncoras próximas fazendo o mesmo; um
        # cluster apertado é evidência mais forte que um espalhado. Achado
        # com dado real: um "14" mal lido (era "11") e um "19" verdadeiro,
        # a 5 posições de distância, empataram em votos com o par verdadeiro
        # (20,19) adjacente (span 1) — preferir o span maior escolhia a
        # coincidência e corrompia a leitura inteira.
        (passo, origem), apoios = max(
            propostas.items(),
            key=lambda item: (len(item[1]),
                              -(item[1][-1][0] - item[1][0][0])),
        )
        if len(apoios) < 2:
            break

        # O trecho vale do primeiro ao último apoio, e o modelo sobrepõe
        # qualquer leitura discordante que caia dentro dele.
        i0, i1 = apoios[0][0], apoios[-1][0]
        for i in range(i0, i1 + 1):
            if numeros[i] is None:
                numeros[i] = origem + i * passo

        for i, n in list(restantes):
            if i0 <= i <= i1:
                restantes.remove((i, n))
                if n != origem + i * passo:
                    corrigidas += 1

    # Estende cada trecho para as pontas ainda vazias, sem invadir o vizinho.
    for _ in range(total):
        mudou = False
        for i in range(total):
            if numeros[i] is not None:
                continue
            for vizinho, direcao in ((i - 1, -1), (i + 1, +1)):
                if not (0 <= vizinho < total) or numeros[vizinho] is None:
                    continue
                base = vizinho + direcao
                if not (0 <= base < total) or numeros[base] is None:
                    continue
                passo = numeros[vizinho] - numeros[base]
                if abs(passo) != 1:
                    continue
                candidato = numeros[vizinho] + passo
                if candidato >= 1 and candidato not in numeros:
                    numeros[i] = candidato
                    mudou = True
                    break
        if not mudou:
            break

    # Última página de um trecho de redigitalização pode ter sido inserida
    # fora da ordem física (achado real: um arquivo trazia as páginas
    # 23,22,20,19 e só então a 21, fora de sequência). Não é erro de leitura
    # — é a ordem real do arquivo — e por isso a extensão por vizinhança
    # acima não alcança essa posição. Mas sobrando exatamente uma posição
    # vazia e exatamente um número de página ainda não usado, não há
    # ambiguidade nenhuma: só existe um valor possível.
    vazias = [i for i in range(total) if numeros[i] is None]
    if len(vazias) == 1:
        usados = {n for n in numeros if n is not None}
        faltando = [n for n in range(1, total + 1) if n not in usados]
        if len(faltando) == 1:
            numeros[vazias[0]] = faltando[0]

    if any(n is None for n in numeros):
        faltam = sum(1 for n in numeros if n is None)
        return None, f"{faltam} página(s) sem número reconstruível"
    if len(set(numeros)) != total:
        return None, "a reconstrução gerou números repetidos"
    if any(n < 1 for n in numeros):
        return None, "a reconstrução gerou número de página inválido"

    inferidas = sum(1 for n in detectados if n is None)
    partes = []
    if inferidas:
        partes.append(f"{inferidas} página(s) sem número legível tiveram a "
                      f"posição deduzida pela sequência das vizinhas")
    if corrigidas:
        partes.append(f"{corrigidas} número(s) mal lidos pelo OCR foram "
                      f"corrigidos pela sequência")
    return numeros, ("; ".join(partes) + "." if partes else "")


# --- PROCESSAMENTO COMPLETO ---

def processar_pdf_completo(paginas, config, retornar_imagens=False, pagina_inicial=1,
                           diagnostico=None):
    """
    Processa todas as páginas do scan:
    1. Pula páginas em branco
    2. Alinha cada página usando marcadores
    3. Lê as bolhas
    4. Reordena pelo número impresso no formulário (via OCR), se disponível

    A reordenação automática corrige casos em que o scanner embaralha as folhas
    durante o scan em massa. Se o OCR não estiver disponível (pytesseract não
    instalado), as páginas são processadas na ordem do scan, numeradas a partir
    de `pagina_inicial` — sem isso, um scan que começa na página 2 do lote
    impresso seria erroneamente tratado como página 1 (puxando os nomes/alunos
    errados da planilha).

    Args:
        paginas: lista de imagens BGR
        config: dict do config.yaml
        retornar_imagens: se True, retorna também (paginas_alinhadas, binarios,
            resultados_por_pagina) para uso no fluxo de revisão.
        pagina_inicial: número da página impressa em que a 1ª página do scan
            começa. Só importa quando o OCR de número de página não está
            disponível/falha; nesse caso é a base da numeração sequencial.
        diagnostico: dict opcional preenchido com `ordem_confiavel` (bool) e
            `motivo_ordem` (str). Quando `ordem_confiavel` é False, a numeração
            caiu de volta para a ordem bruta do arquivo — se o scanner entregou
            as folhas invertidas, cada presença sai atribuída à pessoa errada
            sem nenhum outro sinal de erro. Quem grava em planilha compartilhada
            (`reconciliar.py`, `corrigir_passivo.py`) trata isso como bloqueio.

    Returns:
        Se retornar_imagens=False: lista de resultados por aluno
        Se retornar_imagens=True: (resultados, paginas_alinhadas, binarios, resultados_por_pagina)
    """
    if diagnostico is not None:
        diagnostico["ordem_confiavel"] = False
        diagnostico["motivo_ordem"] = ""

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

    # Reconstrói a numeração e reordena. Detecção parcial não é motivo para
    # desistir: a ordem do arquivo costuma vir invertida, e usá-la atribui a
    # presença de cada pessoa a outra, silenciosamente.
    todos_detectados = False
    if paginas_brutas:
        detectados = [p["num_detectado"] for p in paginas_brutas]
        numeros, aviso = _inferir_numeros_paginas(detectados)

        if numeros is not None:
            for p, n in zip(paginas_brutas, numeros):
                p["num_detectado"] = n
            todos_detectados = True

            nums_antes = list(numeros)
            paginas_brutas.sort(key=lambda p: p["num_detectado"])
            nums_depois = [p["num_detectado"] for p in paginas_brutas]

            if aviso:
                print(f"\n  {aviso}")
            if nums_antes != nums_depois:
                print(f"  Páginas reordenadas: {nums_antes} → {nums_depois}")
            else:
                print(f"  Ordem já estava correta (páginas: {nums_depois})")

            if diagnostico is not None:
                diagnostico["ordem_confiavel"] = True
                diagnostico["motivo_ordem"] = aviso
        else:
            n_det = sum(1 for n in detectados if n is not None)
            print(f"\n  AVISO: não foi possível reconstruir a numeração das "
                  f"páginas ({aviso}).")
            if n_det == 0:
                print("  Nenhum número foi lido. Se o Tesseract não estiver "
                      "instalado, instale-o: sem ele não há como conferir a ordem.")
            print(f"  Usando a ordem do arquivo e numerando a partir da página "
                  f"{pagina_inicial}. CONFIRA o resultado: se o scanner entregou "
                  f"as folhas de trás para frente, as presenças sairão trocadas.")

            if diagnostico is not None:
                diagnostico["ordem_confiavel"] = False
                diagnostico["motivo_ordem"] = aviso

    # Atribuir numeração global de aluno com base na ordem correta
    for idx, p in enumerate(paginas_brutas):
        pagina_template = p["num_detectado"] if todos_detectados else (pagina_inicial + idx)

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
        print("  --merge scan2.pdf ...           Mescla múltiplos PDFs antes de processar")
        print("  --correcoes arquivo.json        Aplica correções manuais da revisão")
        print('  --periodo "05/05 a 09/05"       Exporta para o dashboard analítico (Looker Studio)')
        print()
        print("Exemplos:")
        print('  python exportar.py scan.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO"')
        print('  python exportar.py scan1.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO" --merge scan2.pdf')
        print('  python exportar.py scan.pdf config_canela.yaml alunos.xlsx "CANELA IMPRESSÃO" --periodo "05/05 a 09/05"')
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

    # Checa se tem período informado (para exportação ao dashboard)
    periodo = None
    if "--periodo" in sys.argv:
        idx = sys.argv.index("--periodo")
        periodo = sys.argv[idx + 1]

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

    # Exporta para dashboard analítico (Looker Studio)
    if periodo:
        from google_sheets import exportar_para_dashboard
        resultado_dash = exportar_para_dashboard(contagem, alunos, dias, nome_restaurante, periodo)
        if not resultado_dash.get("ok"):
            print(f"  Aviso: dashboard não atualizado — {resultado_dash.get('erro')}")
        else:
            print("  Dashboard analítico atualizado.")


if __name__ == "__main__":
    from utils import stdout_tolerante

    stdout_tolerante()
    main()