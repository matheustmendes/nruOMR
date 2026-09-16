"""
recuperar_lote.py

Reconstrói o snapshot de um lote que foi impresso antes de os lotes existirem
— ou cuja planilha de referência se perdeu.

O problema que resolve
----------------------
Scans antigos foram processados indexando a planilha de referência do dia do
processamento. Se ela já tinha mudado desde a impressão, as presenças foram
atribuídas às pessoas erradas, sem nenhum sinal de erro. Para conferir ou
refazer esses lançamentos é preciso recuperar quem *de fato* estava em cada
linha daquela folha.

Três caminhos, do mais confiável ao menos:

  1. --pdf <template.pdf>
     O PDF gerado pelo sistema tem camada de texto real (ReportLab). Nome e
     matrícula saem exatos, sem OCR. Se o arquivo do template ainda existir,
     é sempre esta a opção.

  2. --scan <scan.pdf>
     A folha escaneada tem os nomes impressos nela. Um OCR da coluna de
     matrícula, conferido contra um cadastro mestre, reconstrói a ordem. Menos
     exato, mas funciona quando só sobrou o scan.

  3. --planilha <arquivo.xlsx>
     Última opção: uma cópia da planilha da época (backup, versão antiga no
     Drive, e-mail). Só vale se houver certeza de que é a versão que gerou a
     impressão — é justamente essa certeza que costuma faltar.

E ainda:

  --auditar <lote_id> --contra <planilha.xlsx> --aba <aba>
     Diz, linha a linha, quem o processamento antigo teria atribuído e quem
     realmente era. É o relatório para decidir quais períodos relançar.

Uso:
    python recuperar_lote.py --pdf template_canela.pdf --restaurante canela
    python recuperar_lote.py --scan scan.pdf --restaurante canela \\
        --config configs/config_canela.yaml --cadastro impressao.xlsx
    python recuperar_lote.py --auditar canela_20260512-090000 \\
        --contra impressao.xlsx --aba "CANELA IMPRESSÃO"
"""

import os
import re
import sys
import csv
import json

import lote as lote_mod

# Nomes e abas espelham os de web.py; repetidos aqui para a ferramenta rodar
# sozinha, sem subir o Flask.
RESTAURANTES = {
    "canela":                  ("Canela",              "CANELA IMPRESSÃO"),
    "ondina":                  ("Ondina",              "ONDINA IMPRESSÃO"),
    "sao_lazaro":              ("São Lázaro",          "SÃO LÁZARO IMPRESSÃO"),
    "canela_fds":              ("Canela FDS",          " PDCA FDS"),
    "sao_lazaro_fds":          ("São Lázaro FDS",      "PDSL FDS"),
    "canela_fds_especial":     ("Canela FDS Especial", " PDCA FDS"),
    "sao_lazaro_fds_especial": ("São Lázaro FDS Esp.", "PDSL FDS"),
    "ondina_fds_especial":     ("Ondina Especial",     "ONDINA IMPRESSÃO"),
}


# --- 1. Recuperação a partir do PDF do template ----------------------------

_RE_SO_DIGITOS = re.compile(r"^\d+$")
_RE_PAGINA = re.compile(r"P[aá]gina\s+(\d+)\s+de\s+(\d+)", re.IGNORECASE)
_RE_LOTE = re.compile(r"lote\s+([A-Za-z_]+_\d{8}-\d{6})")
_RE_DATAS = re.compile(r"(\d{2}/\d{2}\s*[-a]\s*\d{2}/\d{2})")

# Cabeçalhos e rótulos fixos do formulário — nunca são nome de pessoa.
_RUIDO = {
    "nº", "n°", "no", "nome", "matrícula", "matricula", "a", "j",
    "segunda", "terça", "terca", "quarta", "quinta", "sexta",
    "sábado", "sabado", "domingo",
}


def _linhas_uteis(texto: str):
    for bruta in texto.split("\n"):
        linha = bruta.strip()
        if linha:
            yield linha


def extrair_do_pdf(caminho_pdf: str) -> dict:
    """
    Lê o roster direto da camada de texto do PDF do template.

    O PDF é gerado pelo ReportLab, então cada célula sai como uma linha de
    texto na ordem em que foi desenhada: número, nome, matrícula. Um aluno sem
    matrícula simplesmente pula esse terceiro token, e o número seguinte
    aparece logo depois — daí a checagem de sequência em vez de posição fixa.

    Returns:
        {"paginacao": [...], "total_paginas": int, "datas": str,
         "mes_ano": str, "lote_id_impresso": str|None, "avisos": [...]}
    """
    from pypdf import PdfReader

    reader = PdfReader(caminho_pdf)
    paginacao, avisos = [], []
    datas = mes_ano = ""
    lote_impresso = None
    esperado = 1

    for idx_pdf, pagina_pdf in enumerate(reader.pages, start=1):
        texto = pagina_pdf.extract_text() or ""
        if not texto.strip():
            avisos.append(f"Página {idx_pdf} do PDF não tem camada de texto — ignorada.")
            continue

        m_pag = _RE_PAGINA.search(texto)
        num_pagina = int(m_pag.group(1)) if m_pag else idx_pdf

        if not lote_impresso:
            m_lote = _RE_LOTE.search(texto)
            if m_lote:
                lote_impresso = m_lote.group(1)
        if not datas:
            m_datas = _RE_DATAS.search(texto)
            if m_datas:
                datas = m_datas.group(1)
        if not mes_ano:
            m_mes = re.search(r"(M[ÊE]S\s+[A-ZÇÃÕÁÉÍÓÚ]+\s+DE\s+\d{4})", texto, re.IGNORECASE)
            if m_mes:
                mes_ano = m_mes.group(1)

        linhas = [l for l in _linhas_uteis(texto) if l.lower() not in _RUIDO]
        linha_na_pagina = 0
        i = 0
        while i < len(linhas):
            atual = linhas[i]

            # Um registro começa no número de ordem esperado.
            if not (_RE_SO_DIGITOS.match(atual) and int(atual) == esperado):
                i += 1
                continue

            numero = int(atual)
            nome = linhas[i + 1] if i + 1 < len(linhas) else ""

            # Nome vazio ou puramente numérico significa que a sequência
            # quebrou (rodapé, instrução, cabeçalho da página seguinte).
            if not nome or _RE_SO_DIGITOS.match(nome):
                i += 1
                continue

            matricula = ""
            passo = 2
            if i + 2 < len(linhas) and _RE_SO_DIGITOS.match(linhas[i + 2]):
                candidato = linhas[i + 2]
                # O próximo número de ordem também é só dígitos: distingue-se
                # pela grandeza — matrícula tem 6+ dígitos, ordem tem no máximo
                # 4 e vale exatamente numero+1.
                if not (len(candidato) <= 4 and int(candidato) == numero + 1):
                    matricula = candidato
                    passo = 3

            linha_na_pagina += 1
            paginacao.append({
                "numero": numero,
                "pagina": num_pagina,
                "linha_pagina": linha_na_pagina,
                "nome": nome,
                "nome_impresso": nome,
                "matricula": matricula,
            })
            esperado = numero + 1
            i += passo

    if not paginacao:
        raise RuntimeError(
            f"Nenhum registro reconhecido em {caminho_pdf}. "
            "O arquivo é mesmo o template gerado pelo sistema (e não um scan)?"
        )

    faltando = [n for n in range(1, paginacao[-1]["numero"] + 1)
                if n not in {p["numero"] for p in paginacao}]
    if faltando:
        avisos.append(
            f"{len(faltando)} número(s) de ordem não foram lidos do PDF: "
            f"{faltando[:20]}{'...' if len(faltando) > 20 else ''}"
        )

    return {
        "paginacao": paginacao,
        "total_paginas": max(p["pagina"] for p in paginacao),
        "datas": datas,
        "mes_ano": mes_ano,
        "lote_id_impresso": lote_impresso,
        "avisos": avisos,
    }


# --- 2. Recuperação a partir do scan (OCR) ---------------------------------

def _geometria_texto(config: dict):
    """
    Posições em mm das colunas de texto, derivadas do mesmo layout que
    gerar_template.py usa para desenhar.
    """
    layout = config["layout"]
    dias = layout["dias"]
    raio_mm = config["circulos"]["raio_mm"]

    primeiro_circulo_x = min(
        layout["circulos_por_dia"][d]["almoco_x"] for d in dias
    )
    matricula_x2 = primeiro_circulo_x - raio_mm - 2.0  # antes do 1º círculo
    return {
        "nome_x1": 15.0 + 6.0,                # depois do "Nº"
        "nome_x2": matricula_x2 - 28.0,        # antes da coluna de matrícula
        "matricula_x2": matricula_x2,
        "primeira_linha_y": layout["primeira_linha_y_mm"],
        "linha_altura": layout["linha_altura_mm"],
        "alunos_por_pagina": layout["alunos_por_pagina"],
    }


def extrair_do_scan(caminhos_scan, config: dict, cadastro=None) -> dict:
    """
    Reconstrói o roster lendo, por OCR, a coluna de matrícula de cada linha do
    scan alinhado.

    A matrícula é o alvo por ser só dígitos: com a whitelist do Tesseract o
    reconhecimento é muito mais estável do que o de nomes com acento. O nome
    vem depois, do cadastro mestre — não do OCR. Só quando a matrícula não
    bate de jeito nenhum (nem exata, nem por 1 dígito de diferença) é que a
    coluna do nome é lida por OCR, como último recurso, para achar a
    matrícula da mesma pessoa pelo nome impresso — ver `_melhor_candidato_nome`.

    A posição de cada linha no lote **não** vem da ordem do arquivo. Vem do
    "Página X de Y" impresso na folha, com os buracos reconstruídos por
    `_inferir_numeros_paginas`. Confiar na ordem do arquivo produziria um
    roster invertido — o scanner entrega da última folha para a primeira — e
    um maço fatiado em vários PDFs numeraria as pessoas erradas em cada parte.

    Args:
        caminhos_scan: caminho único ou lista com todas as partes do maço.
        cadastro: {matricula: nome} para resolver os nomes e corrigir erros de
            um dígito no OCR. Sem ele o lote sai só com matrículas.

    Returns:
        {"paginacao": [...], "total_paginas": int, "avisos": [...],
         "confianca": {"exatas": n, "corrigidas": n, "sem_match": n}}
    """
    import cv2
    import numpy as np
    from utils import converter_pdf, obter_pytesseract
    from localizar_marcadores import processar
    from exportar import (
        _detectar_numero_pagina, _inferir_numeros_paginas, eh_pagina_branca,
    )

    pytesseract = obter_pytesseract()
    if pytesseract is None:
        raise RuntimeError(
            "OCR indisponível: instale o Tesseract-OCR e/ou aponte a variável "
            "de ambiente TESSERACT_CMD para o executável."
        )

    if isinstance(caminhos_scan, str):
        caminhos_scan = [caminhos_scan]

    dpi = config["scan"]["dpi"]
    geo = _geometria_texto(config)
    cadastro = cadastro or {}

    def mm_px(valor):
        return int(round((valor / 25.4) * dpi))

    paginacao, avisos = [], []
    exatas = corrigidas = por_nome = sem_match = 0

    # Passo 1: alinhar tudo e descobrir a que página do lote cada imagem
    # corresponde, antes de ler qualquer matrícula.
    alinhadas, detectados = [], []
    for caminho in caminhos_scan:
        for idx, pil in enumerate(converter_pdf(caminho, dpi=dpi), start=1):
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
            if eh_pagina_branca(img):
                continue
            try:
                alinhada = processar(img, config)
            except Exception as e:
                avisos.append(
                    f"{os.path.basename(caminho)} página {idx}: "
                    f"não foi possível alinhar ({e})."
                )
                continue
            alinhadas.append(alinhada)
            detectados.append(_detectar_numero_pagina(alinhada, config))

    if not alinhadas:
        raise RuntimeError("Nenhuma página do scan pôde ser alinhada.")

    paginas_lote, aviso_ordem = _inferir_numeros_paginas(detectados)
    if paginas_lote is None:
        raise RuntimeError(
            f"Não foi possível determinar a que página do lote cada folha "
            f"corresponde ({aviso_ordem}). Sem isso o roster sairia embaralhado. "
            f"Use o template do período com --pdf, ou reescaneie."
        )
    if aviso_ordem:
        avisos.append(aviso_ordem)

    # Passo 2: ler a coluna de matrícula de cada linha, já sabendo a página.
    for alinhada, pagina_lote in zip(alinhadas, paginas_lote):
        cinza = cv2.cvtColor(alinhada, cv2.COLOR_BGR2GRAY)
        altura_img, largura_img = cinza.shape[:2]

        for i in range(geo["alunos_por_pagina"]):
            centro_y = geo["primeira_linha_y"] + i * geo["linha_altura"]
            y1 = max(0, mm_px(centro_y - 4.0))
            y2 = min(altura_img, mm_px(centro_y + 2.0))
            x1 = max(0, mm_px(geo["matricula_x2"] - 26.0))
            x2 = min(largura_img, mm_px(geo["matricula_x2"] + 1.0))
            if y2 <= y1 or x2 <= x1:
                continue

            recorte = cinza[y1:y2, x1:x2]
            _, bin_rec = cv2.threshold(recorte, 0, 255,
                                       cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            ampliado = cv2.resize(bin_rec, None, fx=3, fy=3,
                                  interpolation=cv2.INTER_CUBIC)
            try:
                bruto = pytesseract.image_to_string(
                    ampliado,
                    config="--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789",
                ).strip()
            except Exception:
                bruto = ""

            lido = re.sub(r"\D", "", bruto)
            numero = (pagina_lote - 1) * geo["alunos_por_pagina"] + i + 1

            if not lido:
                # Fim da lista na última página: linhas vazias são esperadas.
                continue

            nome, situacao, matricula = "", "sem_match", lido
            if lido in cadastro:
                nome, situacao = cadastro[lido], "exata"
            elif cadastro:
                candidato = _melhor_candidato(lido, cadastro)
                if candidato:
                    matricula, nome, situacao = candidato, cadastro[candidato], "corrigida"

            # Última tentativa: a matrícula não bateu de nenhuma forma, mas a
            # pessoa está na mesma linha impressa — lê o nome ao lado por OCR
            # e busca no cadastro por quem mais se parece, em vez de desistir.
            nome_ocr = ""
            if situacao == "sem_match" and cadastro:
                xn1 = max(0, mm_px(geo["nome_x1"]))
                xn2 = min(largura_img, mm_px(geo["nome_x2"]))
                if xn2 > xn1:
                    recorte_nome = cinza[y1:y2, xn1:xn2]
                    _, bin_nome = cv2.threshold(recorte_nome, 0, 255,
                                                cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                    ampliado_nome = cv2.resize(bin_nome, None, fx=3, fy=3,
                                               interpolation=cv2.INTER_CUBIC)
                    try:
                        nome_ocr = pytesseract.image_to_string(
                            ampliado_nome, config="--psm 7 --oem 3",
                        ).strip()
                    except Exception:
                        nome_ocr = ""

                if nome_ocr:
                    candidato_nome = _melhor_candidato_nome(nome_ocr, cadastro)
                    if candidato_nome:
                        matricula, nome, situacao = (
                            candidato_nome, cadastro[candidato_nome], "por_nome"
                        )

            if situacao == "exata":
                exatas += 1
            elif situacao == "corrigida":
                corrigidas += 1
            elif situacao == "por_nome":
                por_nome += 1
            else:
                sem_match += 1

            registro = {
                "numero": numero,
                "pagina": pagina_lote,
                "linha_pagina": i + 1,
                "nome": nome or f"[matrícula {lido} — nome não resolvido]",
                "nome_impresso": nome,
                "matricula": matricula,
                "ocr_bruto": lido,
                "situacao": situacao,
            }
            if nome_ocr:
                registro["nome_ocr"] = nome_ocr

            # Sem correspondência automática, o trabalho manual fica muito mais
            # rápido com uma lista curta de candidatos do que com a matrícula
            # crua e o cadastro inteiro para vasculhar.
            if situacao == "sem_match" and cadastro:
                registro["candidatos"] = _sugestoes(lido, cadastro)
                if nome_ocr:
                    candidatos_nome = _sugestoes_nome(nome_ocr, cadastro)
                    if candidatos_nome:
                        registro["candidatos_nome"] = candidatos_nome

            paginacao.append(registro)

    if not paginacao:
        raise RuntimeError(
            "Nenhuma matrícula reconhecida no scan. Verifique se o config "
            "corresponde a este formulário (mesmos dias e mesma quantidade de "
            "alunos por página)."
        )

    if sem_match:
        avisos.append(
            f"{sem_match} matrícula(s) lidas não foram encontradas no cadastro. "
            "Confira essas linhas à mão antes de usar o lote."
        )
    if corrigidas:
        avisos.append(
            f"{corrigidas} matrícula(s) foram corrigidas por aproximação com o "
            "cadastro (erro de um dígito no OCR). Confira as marcadas como "
            "'corrigida' no JSON do lote."
        )
    if por_nome:
        avisos.append(
            f"{por_nome} matrícula(s) não bateram por dígito, mas foram "
            "resolvidas pelo nome impresso na linha (OCR do nome casou com "
            "folga contra um único nome do cadastro). Confira as marcadas "
            "como 'por_nome' no JSON do lote."
        )

    return {
        "paginacao": paginacao,
        "total_paginas": max(p["pagina"] for p in paginacao),
        "avisos": avisos,
        "confianca": {
            "exatas": exatas, "corrigidas": corrigidas,
            "por_nome": por_nome, "sem_match": sem_match,
        },
    }


def _melhor_candidato(lido: str, cadastro: dict):
    """
    Acha a matrícula do cadastro que difere da lida por no máximo um dígito.

    Só aceita quando o candidato é único: duas matrículas igualmente próximas
    significam que o OCR não tem como decidir, e chutar aqui é exatamente o
    tipo de erro silencioso que este módulo existe para evitar.
    """
    candidatos = []
    for mat in cadastro:
        if abs(len(mat) - len(lido)) > 1:
            continue
        if _distancia_max1(lido, mat):
            candidatos.append(mat)
            if len(candidatos) > 1:
                return None
    return candidatos[0] if len(candidatos) == 1 else None


def _sugestoes(lido: str, cadastro: dict, quantos: int = 5):
    """
    Matrículas do cadastro mais parecidas com a lida, para conferência humana.

    Ordena por distância de edição real; empates ficam na ordem do cadastro.
    Nada aqui é aplicado automaticamente — é material de decisão, não decisão.
    """
    pontuados = [
        (_distancia(lido, mat), mat, nome)
        for mat, nome in cadastro.items()
        if abs(len(mat) - len(lido)) <= 3
    ]
    pontuados.sort(key=lambda x: x[0])
    return [
        {"matricula": mat, "nome": nome, "distancia": dist}
        for dist, mat, nome in pontuados[:quantos]
    ]


def _melhor_candidato_nome(nome_lido: str, cadastro: dict):
    """
    Acha a matrícula do cadastro cujo nome mais se parece com o nome lido por
    OCR na própria linha — último recurso quando a matrícula não bateu nem
    exata nem por 1 dígito de diferença.

    OCR de nome com acento tem muito mais ruído que o de dígitos (é por isso
    que a matrícula é a leitura preferida, ver `extrair_do_scan`), então a
    distância aceita cresce com o tamanho do nome em vez de ficar fixa em 1
    como em `_melhor_candidato`. Só aceita quando o melhor candidato se
    destaca claramente do segundo melhor — dois nomes parecidos demais
    significam que não dá pra decidir com segurança, e chutar aqui é
    exatamente o erro silencioso que este módulo existe para evitar.
    """
    alvo = lote_mod.norm_nome(nome_lido)
    if not alvo:
        return None

    pontuados = sorted(
        (_distancia(alvo, lote_mod.norm_nome(nome)), mat)
        for mat, nome in cadastro.items()
    )
    if not pontuados:
        return None

    melhor_dist, melhor_mat = pontuados[0]
    limiar = max(2, len(alvo) // 4)
    if melhor_dist > limiar:
        return None
    if len(pontuados) > 1 and pontuados[1][0] <= melhor_dist + 1:
        return None
    return melhor_mat


def _sugestoes_nome(nome_lido: str, cadastro: dict, quantos: int = 5):
    """
    Nomes do cadastro mais parecidos com o nome lido por OCR, para
    conferência humana quando `_melhor_candidato_nome` não se arriscou.

    Mesma lógica de `_sugestoes`, mas comparando nome normalizado em vez de
    matrícula. Nada aqui é aplicado automaticamente — é material de decisão.
    """
    alvo = lote_mod.norm_nome(nome_lido)
    if not alvo:
        return []
    pontuados = [
        (_distancia(alvo, lote_mod.norm_nome(nome)), mat, nome)
        for mat, nome in cadastro.items()
    ]
    pontuados.sort(key=lambda x: x[0])
    return [
        {"matricula": mat, "nome": nome, "distancia": dist}
        for dist, mat, nome in pontuados[:quantos]
    ]


def _distancia(a: str, b: str) -> int:
    """Levenshtein simples — usado tanto para matrícula (~12 chars) quanto
    para nome completo (~40 chars); o cadastro cabe inteiro na memória, então
    não vale a pena complicar por causa do tamanho da string."""
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        atual = [i]
        for j, cb in enumerate(b, start=1):
            atual.append(min(
                anterior[j] + 1,
                atual[j - 1] + 1,
                anterior[j - 1] + (ca != cb),
            ))
        anterior = atual
    return anterior[-1]


def _distancia_max1(a: str, b: str) -> bool:
    """True se `a` vira `b` com no máximo uma substituição, inserção ou remoção."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if abs(la - lb) != 1:
        return False
    curto, longo = (a, b) if la < lb else (b, a)
    i = j = 0
    pulou = False
    while i < len(curto) and j < len(longo):
        if curto[i] == longo[j]:
            i += 1
            j += 1
        elif pulou:
            return False
        else:
            pulou = True
            j += 1
    return True


# --- Cadastro mestre -------------------------------------------------------

def carregar_cadastro(caminho_xlsx: str, abas=None) -> dict:
    """
    Monta {matricula: nome} varrendo as abas da planilha.

    Varre todas as abas por padrão: o objetivo é ter o maior universo possível
    de matrículas conhecidas para resolver o OCR, e não importa em qual unidade
    a pessoa estava.
    """
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, read_only=True, data_only=True)
    cadastro = {}

    for nome_aba in (abas or wb.sheetnames):
        if nome_aba not in wb.sheetnames:
            continue
        ws = wb[nome_aba]
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 3:
                continue
            nome, matricula = row[1], row[2]
            if not nome or not str(nome).strip():
                continue
            mat = lote_mod.norm_mat(matricula)
            if len(mat) >= 6:
                cadastro.setdefault(mat, str(nome).strip())

    wb.close()
    return cadastro


def carregar_cadastro_sheets(restaurantes=("canela", "ondina", "sao_lazaro")) -> dict:
    """
    Monta {matricula: nome} a partir das abas mensais do Google Sheets.

    Vale mais que a planilha de impressão atual porque acumula todo mundo que
    já apareceu em qualquer mês — inclusive quem saiu do benefício depois. E é
    exatamente essa gente que o OCR de um scan antigo precisa resolver: numa
    medição real do maço de Ondina 29/06, trocar a planilha atual pelo Sheets
    baixou as linhas sem correspondência de 56 para 36.

    Falha de rede não é fatal aqui: devolve o que conseguiu.
    """
    import google_sheets as gs

    cadastro = {}
    for restaurante in restaurantes:
        try:
            abas = gs.listar_abas_mes(restaurante)
        except Exception as e:
            print(f"  Aviso: não foi possível ler as abas de {restaurante}: {e}")
            continue

        for nome_aba in abas:
            try:
                _, valores = gs._abrir_aba(restaurante, nome_aba)
            except Exception:
                continue
            if not isinstance(valores, list):
                continue
            for linha in gs._ler_roster(valores):
                mat = lote_mod.norm_mat(linha["mat"])
                if len(mat) >= 6 and linha["nome"]:
                    cadastro.setdefault(mat, linha["nome"])

    return cadastro


_ABAS_BOLSISTAS = {"ondina", "canela", "sao lazaro"}


def _normalizar_texto(valor: str) -> str:
    """Sem acento, sem caixa, sem espaço nas pontas — só para casar rótulos."""
    import unicodedata

    txt = unicodedata.normalize("NFKD", str(valor or ""))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return txt.strip().lower()


def carregar_cadastro_bolsistas(spreadsheet_id: str = None) -> dict:
    """
    Monta {matricula: nome} a partir da planilha oficial de bolsistas do RU.

    É uma base mantida por fora deste sistema (PROAE), mais completa que o
    cadastro mestre (planilha de impressão + histórico do Sheets): serve pra
    resolver o caso em que uma matrícula lida por OCR não bate com nenhum dos
    dois — a pessoa pode ser bolsista há pouco tempo e ainda não ter aparecido
    em nenhuma exportação. Considera só as abas ONDINA, CANELA e SÃO LÁZARO;
    as demais abas da planilha (removidos, inclusão semanal etc.) não são
    cadastro de bolsista e ficam de fora.

    O layout de cada aba varia (linha de título, linha de cabeçalho em
    posições diferentes, linhas em branco no meio), então o cabeçalho é
    localizado dinamicamente pela célula "MATRÍCULA" em vez de por posição
    fixa. Falha de rede ou planilha não configurada não é fatal aqui: devolve
    o que conseguiu (vazio, na pior das hipóteses).
    """
    import google_sheets as gs

    try:
        config = gs._carregar_config()
    except Exception as e:
        print(f"  Aviso: não foi possível carregar config_sheets.yaml: {e}")
        return {}

    spreadsheet_id = spreadsheet_id or config.get("bolsistas", {}).get("spreadsheet_id", "").strip()
    if not spreadsheet_id:
        return {}

    try:
        cliente = gs._obter_cliente(config)
        planilha = gs._com_retry(lambda: cliente.open_by_key(spreadsheet_id))
        abas = gs._com_retry(planilha.worksheets)
    except Exception as e:
        print(f"  Aviso: não foi possível abrir a planilha de bolsistas: {e}")
        return {}

    cadastro = {}
    for aba in abas:
        if _normalizar_texto(aba.title) not in _ABAS_BOLSISTAS:
            continue
        try:
            valores = gs._com_retry(aba.get_all_values)
        except Exception as e:
            print(f"  Aviso: não foi possível ler a aba '{aba.title}' de bolsistas: {e}")
            continue

        col_nome = col_mat = None
        for linha in valores[:10]:
            rotulos = [_normalizar_texto(c) for c in linha]
            if "matricula" in rotulos:
                col_nome = rotulos.index("nome") if "nome" in rotulos else None
                col_mat = rotulos.index("matricula")
                break
        if col_mat is None or col_nome is None:
            print(f"  Aviso: cabeçalho não encontrado na aba '{aba.title}' de bolsistas.")
            continue

        for linha in valores:
            nome = linha[col_nome].strip() if col_nome < len(linha) else ""
            mat = lote_mod.norm_mat(linha[col_mat]) if col_mat < len(linha) else ""
            if nome and len(mat) >= 6:
                cadastro.setdefault(mat, nome)

    return cadastro


def cadastro_combinado(caminho_xlsx=None, usar_sheets=True, usar_bolsistas=True) -> dict:
    """
    Junta a planilha de impressão, o histórico do Sheets e a base de bolsistas.

    A planilha atual tem os nomes como estão hoje; o Sheets tem quem já saiu.
    Na dúvida entre as duas grafias de um mesmo nome, a planilha vence, porque
    é a fonte que gera as impressões. A base de bolsistas entra por último e
    só preenche matrículas que as outras duas não conhecem — é fonte de
    verificação adicional (fallback), não deve sobrepor um nome já resolvido.
    """
    cadastro = {}
    if usar_sheets:
        cadastro.update(carregar_cadastro_sheets())
    if caminho_xlsx and os.path.isfile(caminho_xlsx):
        cadastro.update(carregar_cadastro(caminho_xlsx))
    if usar_bolsistas:
        for mat, nome in carregar_cadastro_bolsistas().items():
            cadastro.setdefault(mat, nome)
    return cadastro


def extrair_da_planilha(caminho_xlsx: str, aba: str, config: dict) -> dict:
    """Roster a partir de uma cópia da planilha da época."""
    from gerar_template import ler_planilha

    info = ler_planilha(caminho_xlsx, aba)
    por_pagina = config["layout"]["alunos_por_pagina"]

    paginacao = [
        {
            "numero": i + 1,
            "pagina": i // por_pagina + 1,
            "linha_pagina": i % por_pagina + 1,
            "nome": nome,
            "nome_impresso": nome,
            "matricula": matricula,
        }
        for i, (nome, matricula) in enumerate(info["alunos"])
    ]

    return {
        "paginacao": paginacao,
        "total_paginas": (len(paginacao) + por_pagina - 1) // por_pagina,
        "datas": info.get("datas", ""),
        "mes_ano": info.get("mes_ano", ""),
        "avisos": [
            "Lote reconstruído de uma planilha, não do papel impresso. "
            "Só é confiável se esta for comprovadamente a versão que gerou a "
            "impressão."
        ],
    }


# --- Auditoria de períodos já lançados -------------------------------------

def _mesmo_nome(a: str, b: str) -> bool:
    """
    Compara nomes tolerando o corte de quem não coube na coluna impressa.

    Templates antigos truncavam com reticências e abreviavam sobrenomes do
    meio, então o nome recuperado do PDF nem sempre é o nome inteiro. Exigir
    igualdade literal marcaria essas linhas como trocadas sem terem sido.
    """
    na, nb = lote_mod.norm_nome(a), lote_mod.norm_nome(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    # Truncamento: um é prefixo do outro.
    corte = na.rstrip(".…").rstrip()
    corte_b = nb.rstrip(".…").rstrip()
    if corte and (nb.startswith(corte) or na.startswith(corte_b)):
        return True

    # Abreviação de sobrenomes do meio: primeiro e último nome preservados.
    pa, pb = na.split(), nb.split()
    return bool(pa and pb and pa[0] == pb[0] and pa[-1] == pb[-1])


def auditar(lote: dict, alunos_planilha, saida_csv=None) -> dict:
    """
    Mostra, linha a linha, a diferença entre quem o processamento antigo teria
    atribuído (a planilha) e quem realmente estava ali (o lote).

    Cada linha divergente é uma presença lançada na pessoa errada — em ambas
    as direções: alguém ganhou a presença de outro e alguém perdeu a sua.
    """
    comparacao = lote_mod.comparar_com_planilha(lote, alunos_planilha)

    linhas = []
    for a in lote["alunos"]:
        idx = a["numero"] - 1
        if 0 <= idx < len(alunos_planilha):
            nome_atribuido, mat_atribuida = alunos_planilha[idx]
        else:
            nome_atribuido, mat_atribuida = "", ""

        bate = bool(a["matricula"]) and lote_mod.norm_mat(mat_atribuida) == a["matricula"]
        if not bate:
            bate = _mesmo_nome(nome_atribuido, a["nome"])

        linhas.append({
            "numero": a["numero"],
            "pagina": a["pagina"],
            "correto_nome": a["nome"],
            "correto_matricula": a["matricula"],
            "atribuido_nome": nome_atribuido,
            "atribuido_matricula": lote_mod.norm_mat(mat_atribuida),
            "situacao": "ok" if bate else "TROCADO",
        })

    trocados = [l for l in linhas if l["situacao"] == "TROCADO"]

    if saida_csv:
        with open(saida_csv, "w", encoding="utf-8-sig", newline="") as f:
            escritor = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
            escritor.writeheader()
            escritor.writerows(linhas)

    return {
        "linhas": linhas,
        "trocados": trocados,
        "total": len(linhas),
        "comparacao": comparacao,
    }


# --- CLI -------------------------------------------------------------------

def _arg(nome, padrao=None):
    if nome in sys.argv:
        idx = sys.argv.index(nome)
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
            return sys.argv[idx + 1]
    return padrao


def _args_multiplos(nome):
    """Todos os valores de um argumento — um maço tem várias partes."""
    if nome not in sys.argv:
        return []
    valores = []
    for arg in sys.argv[sys.argv.index(nome) + 1:]:
        if arg.startswith("--"):
            break
        valores.append(arg)
    return valores


def _carregar_config(caminho, restaurante_key):
    import yaml

    if not caminho:
        for tentativa in (
            os.path.join("configs", f"config_{restaurante_key}.yaml"),
            f"config_{restaurante_key}.yaml",
        ):
            if os.path.isfile(tentativa):
                caminho = tentativa
                break
    if not caminho or not os.path.isfile(caminho):
        raise SystemExit(
            f"Config não encontrado para '{restaurante_key}'. Informe com --config."
        )
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    # --- Auditoria ---
    lote_auditar = _arg("--auditar")
    if lote_auditar:
        from exportar import ler_nomes_alunos

        contra = _arg("--contra")
        if not contra:
            raise SystemExit("Informe a planilha com --contra <arquivo.xlsx>.")

        alvo = lote_mod.carregar_lote(lote_auditar)
        aba = _arg("--aba") or alvo.get("aba")
        atuais = ler_nomes_alunos(contra, aba)

        csv_saida = _arg("--csv", f"auditoria_{lote_auditar}.csv")
        resultado = auditar(alvo, atuais, csv_saida)

        print(f"Lote auditado : {lote_auditar}  ({resultado['total']} linhas)")
        print(f"Planilha      : {contra}  (aba '{aba}')")
        print(f"Mudanças      : {resultado['comparacao']['resumo']}")
        print(f"Linhas trocadas: {len(resultado['trocados'])} de {resultado['total']}")
        print()
        for l in resultado["trocados"][:25]:
            print(f"  linha {l['numero']:>4} (pág {l['pagina']}): "
                  f"a presença foi para '{l['atribuido_nome']}' "
                  f"mas era de '{l['correto_nome']}'")
        if len(resultado["trocados"]) > 25:
            print(f"  ... e mais {len(resultado['trocados']) - 25}.")
        print(f"\nRelatório completo: {csv_saida}")
        return

    # --- Recuperação ---
    restaurante_key = _arg("--restaurante")
    if restaurante_key not in RESTAURANTES:
        raise SystemExit(
            f"Informe --restaurante com um destes: {', '.join(RESTAURANTES)}"
        )
    nome_rest, aba = RESTAURANTES[restaurante_key]

    caminho_pdf = _arg("--pdf")
    caminhos_scan = _args_multiplos("--scan")
    caminho_planilha = _arg("--planilha")

    if not any([caminho_pdf, caminhos_scan, caminho_planilha]):
        raise SystemExit("Informe a origem: --pdf, --scan ou --planilha.")

    config = _carregar_config(_arg("--config"), restaurante_key)

    if caminho_pdf:
        dados = extrair_do_pdf(caminho_pdf)
        origem = lote_mod.ORIGEM_PDF
        fonte = caminho_pdf
    elif caminhos_scan:
        print("Montando o cadastro mestre...")
        cadastro = cadastro_combinado(
            _arg("--cadastro"),
            usar_sheets="--sem-sheets" not in sys.argv,
            usar_bolsistas="--sem-bolsistas" not in sys.argv,
        )
        if cadastro:
            print(f"Cadastro mestre: {len(cadastro)} matrículas")
        else:
            print("AVISO: cadastro vazio — os nomes não serão resolvidos, "
                  "só as matrículas.")
        dados = extrair_do_scan(caminhos_scan, config, cadastro)
        origem = lote_mod.ORIGEM_OCR
        fonte = ", ".join(os.path.basename(c) for c in caminhos_scan)
        c = dados["confianca"]
        print(f"OCR: {c['exatas']} exatas, {c['corrigidas']} corrigidas, "
              f"{c['por_nome']} por nome, {c['sem_match']} sem correspondência")
    else:
        dados = extrair_da_planilha(caminho_planilha, _arg("--aba") or aba, config)
        origem = lote_mod.ORIGEM_XLSX
        fonte = caminho_planilha

    dias = config["layout"]["dias"]
    info = {
        "restaurante": nome_rest,
        "datas": _arg("--datas") or dados.get("datas", ""),
        "mes_ano": _arg("--mes") or dados.get("mes_ano", ""),
    }

    lote_id = _arg("--id") or dados.get("lote_id_impresso") or lote_mod.novo_lote_id(
        f"{restaurante_key}_rec"
    )

    avisos = list(dados.get("avisos", []))
    avisos.append(f"Lote reconstruído a partir de {fonte} (origem: {origem}).")
    avisos.extend(lote_mod.validar_roster(
        [(p["nome"], p["matricula"]) for p in dados["paginacao"]]
    )["avisos"])

    novo = lote_mod.montar_lote(
        lote_id=lote_id,
        restaurante_key=restaurante_key,
        restaurante_nome=nome_rest,
        aba=aba,
        dias=dias,
        paginacao=dados["paginacao"],
        config=config,
        info=info,
        origem=origem,
        avisos=avisos,
    )
    # Preserva os campos de diagnóstico do OCR, que montar_lote não conhece.
    for destino, bruto in zip(novo["alunos"], dados["paginacao"]):
        for extra in ("ocr_bruto", "situacao", "candidatos"):
            if extra in bruto:
                destino[extra] = bruto[extra]

    caminho = lote_mod.salvar_lote(novo)

    print()
    print(f"Lote recuperado : {lote_id}")
    print(f"Origem          : {origem}")
    print(f"Registros       : {novo['total_alunos']} em {novo['total_paginas']} página(s)")
    print(f"Arquivo         : {caminho}")
    for aviso in avisos:
        print(f"  AVISO: {aviso}")
    print()
    print("Próximo passo: processe o scan escolhendo este lote na interface web,")
    print("ou audite o lançamento antigo com:")
    print(f"  python recuperar_lote.py --auditar {lote_id} --contra <planilha.xlsx>")


if __name__ == "__main__":
    main()
