"""
google_sheets.py

Exporta resultados de presença para Google Sheets — layout horizontal
(paisagem): cada período/semana é um grupo de colunas lado a lado.

    Linha 1:  (vazio A:C)       | Período: 05/05 a 09/05           | Período: 12/05 a 16/05
    Linha 2:  Nº | Nome | Matr. | Presenças | Seg | ... | Sex      | Presenças | Seg | ...
    Linha 3+: 1  | João | 12345 | 3         | AJ  |     | J        | 4         | A   | ...

As linhas 1–2 e as colunas A–C ficam congeladas: a rolagem horizontal
mantém Nº/Nome/Matrícula sempre visíveis.

Cada aluno ocupa uma única linha durante o mês inteiro (identificado pela
matrícula, com o nome como fallback). Dias de FDS entram como colunas
extras no grupo da própria semana, à direita dos dias úteis.

Os períodos ficam sempre em ordem cronológica, independente da ordem em que
foram escaneados: um período novo é inserido na posição certa e uma aba que
já esteja fora de ordem é reordenada na próxima exportação.

Configuração em config_sheets.yaml:
    credentials: credentials_sheets.json
    restaurantes:
      canela:
        spreadsheet_id: "1abc..."
      ondina:
        spreadsheet_id: "1def..."
      sao_lazaro:
        spreadsheet_id: "1ghi..."
"""

import os
import re
import yaml
from datetime import datetime, timedelta

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _DISPONIVEL = True
except ImportError:
    _DISPONIVEL = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_MESES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_COR_TITULO    = {"red": 0.20, "green": 0.44, "blue": 0.65}   # azul UFBA
_COR_CABECALHO = {"red": 0.84, "green": 0.91, "blue": 0.97}   # azul claro
_COR_FIXAS     = {"red": 0.95, "green": 0.95, "blue": 0.95}   # cinza claro
_COR_BORDA     = {"red": 0.65, "green": 0.65, "blue": 0.65}
_BRANCO        = {"red": 1.0,  "green": 1.0,  "blue": 1.0}

# --- Geometria do layout horizontal ---
_PREFIXO_PERIODO = "Período: "
_COL_FIXAS       = 3   # Nº, Nome, Matrícula
_LINHA_PERIODO   = 1   # 1-indexed
_LINHA_CABECALHO = 2
_LINHA_DADOS     = 3   # primeira linha de aluno

_DIA_SEMANA_PT = {
    "Segunda": 0,
    "Terça":   1,
    "Quarta":  2,
    "Quinta":  3,
    "Sexta":   4,
    "Sábado":  5,
    "Domingo": 6,
}


def _ordenar_dias(dias):
    """
    Ordena os dias da semana (Segunda → Domingo).

    Sem isso as colunas ficariam na ordem de escaneamento: um FDS exportado
    antes da semana deixaria "Sábado" à esquerda de "Segunda". Dias fora do
    calendário conhecido vão para o fim, preservando a ordem entre si.
    """
    return sorted(dias, key=lambda d: _DIA_SEMANA_PT.get(d, 99))


def _carregar_config():
    caminho = os.path.join(SCRIPT_DIR, "config_sheets.yaml")
    if not os.path.exists(caminho):
        raise FileNotFoundError(
            "config_sheets.yaml não encontrado. "
            "Crie o arquivo com as credenciais e IDs das planilhas."
        )
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _obter_cliente(config):
    if not _DISPONIVEL:
        raise ImportError(
            "Bibliotecas 'gspread' e 'google-auth' não instaladas. "
            "Execute: pip install gspread google-auth"
        )
    creds_path = config.get("credentials", "credentials_sheets.json")
    if not os.path.isabs(creds_path):
        creds_path = os.path.join(SCRIPT_DIR, creds_path)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Credenciais não encontradas: {creds_path}")
    creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
    return gspread.authorize(creds)


def _nome_aba_mes(periodo=None):
    """
    Retorna o nome da aba mensal (ex: "Abril 2026").
    Se `periodo` for fornecido (ex: "07/04 a 11/04"), extrai o mês dele.
    Se o mês extraído for maior que o mês atual, assume ano anterior.
    """
    hoje = datetime.now()
    if periodo:
        try:
            parte = periodo.split(" a ")[0].strip()
            mes = int(parte.split("/")[1])
            ano = hoje.year if mes <= hoje.month else hoje.year - 1
            return f"{_MESES[mes - 1]} {ano}"
        except Exception:
            pass
    return f"{_MESES[hoje.month - 1]} {hoje.year}"


def _obter_ou_criar_aba(spreadsheet, nome_aba):
    try:
        return spreadsheet.worksheet(nome_aba)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=nome_aba, rows=600, cols=120)


def _marcador_periodo(periodo):
    return f"{_PREFIXO_PERIODO}{periodo}"


# --- Primitivas do layout horizontal ---------------------------------------

def _norm_mat(valor):
    """Normaliza matrícula para comparação (remove '.0', pontuação e caixa)."""
    s = str(valor or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return "".join(ch for ch in s if ch.isalnum()).lower()


def _norm_nome(valor):
    return " ".join(str(valor or "").split()).lower()


def _garantir_grade(aba, linhas, colunas):
    """Expande a grade da aba se o layout exigir mais linhas/colunas."""
    if aba.row_count < linhas:
        aba.add_rows(linhas - aba.row_count + 50)
    if aba.col_count < colunas:
        aba.add_cols(colunas - aba.col_count + 20)


def _ler_grupos(valores):
    """
    Lê os grupos de período a partir das linhas 1 e 2.

    Retorna lista de dicts:
        {periodo, col (0-idx da coluna "Presenças"), dias, cols_dias, largura}
    """
    if not valores:
        return []

    row1 = valores[0]
    row2 = valores[1] if len(valores) > 1 else []

    # Só a partir das colunas fixas: no layout vertical antigo o marcador fica
    # na coluna A e não pode ser confundido com um grupo horizontal.
    inicios = [
        j for j, v in enumerate(row1)
        if j >= _COL_FIXAS and str(v).strip().startswith(_PREFIXO_PERIODO)
    ]

    grupos = []
    for k, col in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else max(len(row1), len(row2))
        dias = []
        for c in range(col + 1, fim):
            h = str(row2[c]).strip() if c < len(row2) else ""
            if h:
                dias.append((h, c))
        grupos.append({
            "periodo":   str(row1[col]).strip()[len(_PREFIXO_PERIODO):].strip(),
            "col":       col,
            "dias":      [d for d, _ in dias],
            "cols_dias": {d: c for d, c in dias},
            "largura":   1 + len(dias),
        })
    return grupos


def _proxima_col_periodo(valores):
    """Índice 0-based da próxima coluna livre para um novo grupo de período."""
    max_col = _COL_FIXAS - 1
    for row in valores:
        for j in range(len(row) - 1, -1, -1):
            if str(row[j]).strip():
                if j > max_col:
                    max_col = j
                break
    return max_col + 1


def _grupo_tem_dados(valores, grupo, dias):
    """True se alguma linha de aluno já tem marcação nos `dias` do grupo."""
    cols = [grupo["cols_dias"][d] for d in dias if d in grupo["cols_dias"]]
    if not cols:
        return False
    for i in range(_LINHA_DADOS - 1, len(valores)):
        row = valores[i]
        for c in cols:
            if c < len(row) and str(row[c]).strip():
                return True
    return False


def _inserir_colunas(spreadsheet, aba, col_0idx, quantidade):
    """Insere colunas no meio da aba (empurra os grupos seguintes à direita)."""
    spreadsheet.batch_update({"requests": [{
        "insertDimension": {
            "range": {
                "sheetId": aba.id, "dimension": "COLUMNS",
                "startIndex": col_0idx, "endIndex": col_0idx + quantidade,
            },
            "inheritFromBefore": False,
        }
    }]})


def _data_inicio_periodo(periodo):
    """
    Data de início de um período, usada para ordenar as semanas.

    O período é texto livre digitado no formulário, então aceita as variações
    usuais: "05/05 a 09/05", "05/05 - 09/05", "05/05 à 09/05",
    "05/05/2026 até 09/05/2026". Basta a primeira data aparecer como DD/MM.
    """
    m = re.search(r"(\d{1,2})\s*[/.]\s*(\d{1,2})(?:\s*[/.]\s*(\d{2,4}))?", str(periodo))
    if not m:
        raise ValueError(f"período sem data reconhecível: {periodo!r}")

    dd, mm = int(m.group(1)), int(m.group(2))
    if m.group(3):
        ano = int(m.group(3))
        if ano < 100:
            ano += 2000
    else:
        hoje = datetime.now()
        ano = hoje.year if mm <= hoje.month else hoje.year - 1
    return datetime(ano, mm, dd)


def _localizar_grupo_semana_fds(grupos, periodo_fds):
    """
    Para FDS: encontra o grupo de dias úteis da mesma semana, mesmo que o
    período FDS ("10/05 a 10/05") não coincida com o da semana ("05/05 a 09/05").
    Aceita grupos cujo início seja 0–6 dias antes do FDS.
    """
    try:
        data_fds = _data_inicio_periodo(periodo_fds)
    except Exception:
        return None

    melhor = None  # (grupo, diff_dias)
    for g in grupos:
        try:
            diff = (data_fds - _data_inicio_periodo(g["periodo"])).days
        except Exception:
            continue
        if 0 <= diff <= 6 and (melhor is None or diff < melhor[1]):
            melhor = (g, diff)

    return melhor[0] if melhor else None


# --- Ordem cronológica dos períodos ----------------------------------------

def _posicao_cronologica(grupos, periodo):
    """
    Coluna 0-idx onde um período novo deve entrar para manter a ordem de data,
    ou None se ele for o mais recente (vai para o fim da planilha).
    """
    try:
        data = _data_inicio_periodo(periodo)
    except Exception:
        return None

    for g in grupos:
        try:
            if _data_inicio_periodo(g["periodo"]) > data:
                return g["col"]
        except Exception:
            continue
    return None


def _periodos_sem_data(grupos):
    """Períodos cuja data não é legível — impedem a ordenação automática."""
    ilegiveis = []
    for g in grupos:
        try:
            _data_inicio_periodo(g["periodo"])
        except Exception:
            ilegiveis.append(g["periodo"])
    return ilegiveis


def _grupos_fora_de_ordem(grupos):
    """True se os períodos gravados não estão em ordem cronológica."""
    if _periodos_sem_data(grupos):
        return False  # sem saber a data de todos, não mexe na ordem
    datas = [_data_inicio_periodo(g["periodo"]) for g in grupos]
    return any(datas[i] > datas[i + 1] for i in range(len(datas) - 1))


def _precisa_reordenar(grupos):
    """True se os períodos ou os dias dentro de algum grupo estão fora de ordem."""
    if any(g["dias"] != _ordenar_dias(g["dias"]) for g in grupos):
        return True
    return _grupos_fora_de_ordem(grupos)


def _reordenar_grupos(spreadsheet, aba, valores, grupos, linha_fim):
    """
    Reescreve os grupos de período em ordem cronológica, preservando os dados.

    Permuta blocos inteiros de colunas: a largura total não muda, então a aba é
    reescrita de uma vez só — não existe instante em que ela fique vazia.

    Returns: a nova lista de grupos, já com as colunas atualizadas.
    """
    if _periodos_sem_data(grupos):
        # Sem as datas de todos, mantém a ordem atual e só ajeita os dias
        ordenados = list(grupos)
    else:
        ordenados = sorted(grupos, key=lambda g: _data_inicio_periodo(g["periodo"]))

    largura = max(
        max((len(r) for r in valores), default=0),
        _COL_FIXAS + sum(g["largura"] for g in ordenados),
    )

    def celula(row, c):
        return row[c] if c < len(row) else ""

    # Colunas de origem de cada grupo, com os dias já em ordem de semana
    origem = []
    for g in ordenados:
        dias_ord = _ordenar_dias(g["dias"])
        origem.append([g["col"]] + [g["cols_dias"][d] for d in dias_ord])
        g["dias"] = dias_ord

    matriz = []
    for i in range(linha_fim):
        row = valores[i] if i < len(valores) else []
        nova = [celula(row, c) for c in range(_COL_FIXAS)]
        for cols in origem:
            nova += [celula(row, c) for c in cols]
        nova += [""] * (largura - len(nova))
        matriz.append(nova)

    # Os títulos mesclados impedem a reescrita da linha 1
    spreadsheet.batch_update({"requests": [{"unmergeCells": {"range": {
        "sheetId": aba.id,
        "startRowIndex": 0, "endRowIndex": 1,
        "startColumnIndex": 0, "endColumnIndex": largura,
    }}}]})

    ultima = gspread.utils.rowcol_to_a1(linha_fim, largura)
    aba.update(values=matriz, range_name=f"A1:{ultima}",
               value_input_option="USER_ENTERED")

    novos, col = [], _COL_FIXAS
    for g in ordenados:
        novos.append({
            "periodo":   g["periodo"],
            "col":       col,
            "dias":      g["dias"],
            "cols_dias": {d: col + 1 + k for k, d in enumerate(g["dias"])},
            "largura":   g["largura"],
        })
        col += g["largura"]
    return novos


# --- Sincronização das linhas de alunos ------------------------------------

def _ler_roster(valores):
    """Linhas de aluno já existentes: [{linha (1-idx), nome, mat}]."""
    roster = []
    for i in range(_LINHA_DADOS - 1, len(valores)):
        row = valores[i]
        nome = str(row[1]).strip() if len(row) > 1 else ""
        mat  = str(row[2]).strip() if len(row) > 2 else ""
        if not nome and not mat:
            break
        roster.append({"linha": i + 1, "nome": nome, "mat": mat})
    return roster


def _sincronizar_roster(valores, alunos):
    """
    Casa a lista de alunos com as linhas já existentes na aba.

    Casa por matrícula (primário) ou nome normalizado (fallback). Alunos novos
    são acrescentados no fim, preservando a linha dos que já estavam lá.

    Returns:
        (linhas, novos, linha_fim)
            linhas    : {índice em `alunos` → linha 1-indexed}
            novos     : [{linha, num, nome, mat}] a serem gravados
            linha_fim : última linha de aluno (1-indexed)
    """
    roster = _ler_roster(valores)

    por_mat, por_nome = {}, {}
    for r in roster:
        chave_m = _norm_mat(r["mat"])
        if chave_m and chave_m not in por_mat:
            por_mat[chave_m] = r
        chave_n = _norm_nome(r["nome"])
        if chave_n and chave_n not in por_nome:
            por_nome[chave_n] = r

    linhas, novos = {}, []
    proxima = (roster[-1]["linha"] + 1) if roster else _LINHA_DADOS

    for i, (nome, mat) in enumerate(alunos):
        r = por_mat.get(_norm_mat(mat)) or por_nome.get(_norm_nome(nome))
        if r:
            linhas[i] = r["linha"]
            continue
        linhas[i] = proxima
        novos.append({
            "linha": proxima,
            "num":   proxima - _LINHA_DADOS + 1,
            "nome":  nome,
            "mat":   mat,
        })
        proxima += 1

    return linhas, novos, proxima - 1


# --- Montagem e escrita do grupo de colunas --------------------------------

def _montar_matriz_grupo(valores, grupo, dias_novos, dados_por_linha, linha_fim):
    """
    Monta a matriz [Presenças, dia1, dia2, ...] de todas as linhas de aluno.

    Marcas já gravadas em dias que não estão em `dias_novos` são preservadas
    (é o que mantém os dias úteis intactos quando o FDS entra no mesmo grupo,
    e vice-versa). "Presenças" é recalculado como o número de dias marcados.

    Returns: (ordem_dias, matriz)
    """
    dias_exist = grupo["dias"] if grupo else []
    cols_exist = grupo["cols_dias"] if grupo else {}
    col_pres   = grupo["col"] if grupo else None

    ordem = _ordenar_dias(
        list(dias_exist) + [d for d in dias_novos if d not in dias_exist]
    )

    matriz = []
    for linha in range(_LINHA_DADOS, linha_fim + 1):
        i = linha - 1
        row = valores[i] if i < len(valores) else []

        marcas = {}
        for dia in dias_exist:
            c = cols_exist[dia]
            marcas[dia] = str(row[c]).strip() if c < len(row) else ""

        dados = dados_por_linha.get(linha)
        if dados is not None:
            for dia in dias_novos:
                marcas[dia] = dados["marcas"].get(dia, "")

        if ordem and (dados is not None or any(marcas.values())):
            presencas = sum(1 for dia in ordem if marcas.get(dia))
        elif not ordem and dados is not None:
            # Importação legada: só o total, sem detalhamento por dia
            presencas = dados["presencas"]
        elif col_pres is not None and col_pres < len(row):
            presencas = str(row[col_pres]).strip()
        else:
            presencas = ""

        matriz.append([presencas] + [marcas.get(dia, "") for dia in ordem])

    return ordem, matriz


def _escrever_grupo(aba, col_inicio, periodo, ordem_dias, matriz, novos, linha_fim):
    """Grava cabeçalhos, alunos novos e o retângulo de dados do grupo."""
    a1 = gspread.utils.rowcol_to_a1
    col_fim = col_inicio + len(ordem_dias)  # 0-idx da última coluna do grupo

    updates = [
        {
            "range":  a1(_LINHA_PERIODO, col_inicio + 1),
            "values": [[_marcador_periodo(periodo)]],
        },
        {
            "range":  f"A{_LINHA_CABECALHO}:C{_LINHA_CABECALHO}",
            "values": [["Nº", "Nome", "Matrícula"]],
        },
        {
            "range": (
                f"{a1(_LINHA_CABECALHO, col_inicio + 1)}"
                f":{a1(_LINHA_CABECALHO, col_fim + 1)}"
            ),
            "values": [["Presenças"] + list(ordem_dias)],
        },
    ]

    if novos:
        updates.append({
            "range":  f"A{novos[0]['linha']}:C{linha_fim}",
            "values": [[n["num"], n["nome"], n["mat"]] for n in novos],
        })

    if matriz:
        updates.append({
            "range": (
                f"{a1(_LINHA_DADOS, col_inicio + 1)}"
                f":{a1(linha_fim, col_fim + 1)}"
            ),
            "values": matriz,
        })

    aba.batch_update(updates, value_input_option="USER_ENTERED")


# --- Formatação ------------------------------------------------------------

def _formatar_colunas_fixas(spreadsheet, aba, linha_fim):
    """Congela linhas 1–2 / colunas A–C e formata o cabeçalho fixo."""
    sheet_id = aba.id
    borda = {"style": "SOLID", "width": 1, "color": _COR_BORDA}

    def rng(r0, r1, c0=0, c1=_COL_FIXAS):
        return {
            "sheetId": sheet_id,
            "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1,
        }

    requests = [
        # Congela cabeçalho e colunas de identificação
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": _COL_FIXAS},
                },
                "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
            }
        },
        # A1:C1 — faixa vazia acima do cabeçalho fixo
        {"unmergeCells": {"range": rng(0, 1)}},
        {"mergeCells": {"range": rng(0, 1), "mergeType": "MERGE_ALL"}},
        {
            "repeatCell": {
                "range": rng(0, 1),
                "cell": {"userEnteredFormat": {"backgroundColor": _COR_TITULO}},
                "fields": "userEnteredFormat(backgroundColor)",
            }
        },
        # Cabeçalho Nº | Nome | Matrícula
        {
            "repeatCell": {
                "range": rng(1, 2),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _COR_CABECALHO,
                    "textFormat": {"bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        # Dados das colunas fixas
        {
            "repeatCell": {
                "range": rng(2, linha_fim),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _COR_FIXAS,
                    "textFormat": {"fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "CLIP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        # Nome alinhado à esquerda
        {
            "repeatCell": {
                "range": rng(2, linha_fim, c0=1, c1=2),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                "fields": "userEnteredFormat(horizontalAlignment)",
            }
        },
        {
            "updateBorders": {
                "range": rng(1, linha_fim),
                "top": borda, "bottom": borda, "left": borda, "right": borda,
                "innerHorizontal": borda, "innerVertical": borda,
            }
        },
        # Alturas das linhas de cabeçalho
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 32}, "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 28}, "fields": "pixelSize",
            }
        },
    ]

    requests += [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": col, "endIndex": col + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize",
            }
        }
        for col, px in [(0, 45), (1, 250), (2, 110)]
    ]

    spreadsheet.batch_update({"requests": requests})


def _aplicar_formatacao_horizontal(spreadsheet, aba, col_inicio, num_dias, linha_fim):
    """Formata o grupo de colunas de um período (título mesclado + dias)."""
    sheet_id = aba.id
    c0 = col_inicio
    c1 = col_inicio + 1 + num_dias  # exclusive

    borda = {"style": "SOLID", "width": 1, "color": _COR_BORDA}

    def rng(r0, r1, ci=c0, cf=c1):
        return {
            "sheetId": sheet_id,
            "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": ci, "endColumnIndex": cf,
        }

    # A API recusa mesclar uma célula sozinha (grupo sem colunas de dia)
    merge = (
        [
            {"unmergeCells": {"range": rng(0, 1)}},
            {"mergeCells": {"range": rng(0, 1), "mergeType": "MERGE_ALL"}},
        ]
        if num_dias else []
    )

    requests = merge + [
        {
            "repeatCell": {
                "range": rng(0, 1),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _COR_TITULO,
                    "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": _BRANCO},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        # Sub-cabeçalho (Presenças + dias)
        {
            "repeatCell": {
                "range": rng(1, 2),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _COR_CABECALHO,
                    "textFormat": {"bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        # Dados
        {
            "repeatCell": {
                "range": rng(2, linha_fim),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _BRANCO,
                    "textFormat": {"fontSize": 10},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        {
            "updateBorders": {
                "range": rng(0, linha_fim),
                "top": borda, "bottom": borda, "left": borda, "right": borda,
                "innerHorizontal": borda, "innerVertical": borda,
            }
        },
        # Coluna "Presenças"
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": c0, "endIndex": c0 + 1},
                "properties": {"pixelSize": 80}, "fields": "pixelSize",
            }
        },
    ]

    if num_dias:
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": c0 + 1, "endIndex": c1},
                "properties": {"pixelSize": 55}, "fields": "pixelSize",
            }
        })

    spreadsheet.batch_update({"requests": requests})


# Restaurantes FDS não têm planilha própria: usam a planilha do restaurante pai
# e entram no grupo de colunas da semana já existente.
# FDS especial = feriado emendado com dias customizáveis (ex: Qui+Sex+Sab).
_RESTAURANTE_PAI = {
    "canela_fds":              "canela",
    "sao_lazaro_fds":          "sao_lazaro",
    "canela_fds_especial":     "canela",
    "sao_lazaro_fds_especial": "sao_lazaro",
    "ondina_fds_especial":     "ondina",
}

_UNIDADE_LEGIVEL = {
    "canela":                    "Canela",
    "ondina":                    "Ondina",
    "sao_lazaro":                "São Lázaro",
    "canela_fds":                "Canela",
    "sao_lazaro_fds":            "São Lázaro",
    "canela_fds_especial":       "Canela",
    "sao_lazaro_fds_especial":   "São Lázaro",
    "ondina_fds_especial":       "Ondina",
}

_HEADERS_PRESENCAS = [
    "matricula", "nome", "unidade", "periodo_letivo", "semana",
    "data", "dia_semana", "status", "almoco", "janta",
]

_HEADERS_RESUMO = [
    "matricula", "nome", "unidade", "periodo_letivo", "semana",
    "total_sessoes_semana", "presencas_semana", "ausencias_semana",
    "pct_presenca_semana", "irregular", "faltas_consecutivas", "mes",
    "data_inicio_semana",
]


def exportar_para_sheets(contagem, alunos, dias, restaurante_key, periodo, forcar=False):
    """
    Exporta dados de presença para Google Sheets (layout horizontal).

    Cada período vira um grupo de colunas na aba do mês, sempre em ordem
    cronológica: escanear uma semana fora de ordem insere as colunas na posição
    certa, empurrando os períodos posteriores para a direita.

    FDS usa a planilha do restaurante pai e entra como colunas extras no grupo
    da semana correspondente (mantendo "Presenças" somado).

    Args:
        contagem: lista de dicts com presenças (saída de contar_presencas)
        alunos: lista de (nome, matricula)
        dias: lista de nomes dos dias
        restaurante_key: "canela", "ondina", "sao_lazaro" ou variantes _fds
        periodo: string do período, ex: "05/05 a 09/05"
        forcar: se True, sobrescreve o período existente sem perguntar

    Returns:
        dict:
            {"ok": True, "aba": "Maio 2026"}                          — sucesso
            {"ok": True, "aba": "...", "aviso_formatacao": "..."}     — dados salvos, formatação falhou
            {"ok": True, "aba": "...", "aviso_ordem": "..."}          — dados salvos, ordenação desligada
            {"ok": False, "duplicado": True, "aba": "..."}            — período já existe
            {"ok": False, "erro": "mensagem"}                         — erro
    """
    try:
        config = _carregar_config()

        # FDS usa a planilha do restaurante pai (ex: canela_fds → canela)
        restaurante_efetivo = _RESTAURANTE_PAI.get(restaurante_key, restaurante_key)
        eh_fds = restaurante_key in _RESTAURANTE_PAI

        rest = config.get("restaurantes", {}).get(restaurante_efetivo, {})
        spreadsheet_id = rest.get("spreadsheet_id", "").strip()
        if not spreadsheet_id:
            return {
                "ok": False,
                "erro": (
                    f"spreadsheet_id não configurado para '{restaurante_efetivo}' "
                    "em config_sheets.yaml"
                ),
            }

        cliente = _obter_cliente(config)
        spreadsheet = cliente.open_by_key(spreadsheet_id)
        nome_aba = _nome_aba_mes(periodo)
        aba = _obter_ou_criar_aba(spreadsheet, nome_aba)

        valores = aba.get_all_values()
        grupos  = _ler_grupos(valores)

        # Localiza o grupo de destino: o do próprio período ou, no FDS, o da semana
        grupo = next((g for g in grupos if g["periodo"] == periodo), None)
        if grupo is None and eh_fds:
            grupo = _localizar_grupo_semana_fds(grupos, periodo)

        if grupo is not None and not forcar:
            # Só é duplicata se já houver marcação nos dias que estão sendo
            # gravados: as colunas podem existir no cabeçalho e estarem vazias
            # (FDS num grupo de semana, ou a semana num grupo criado pelo FDS).
            # Sem dias (importação legada), a existência do grupo já basta.
            ja_gravado = _grupo_tem_dados(valores, grupo, dias) if dias else True
            if ja_gravado:
                return {"ok": False, "duplicado": True, "aba": nome_aba}

        # O título do grupo é o do período da semana (o FDS não o renomeia)
        periodo_grupo = grupo["periodo"] if grupo is not None else periodo

        if grupo is None:
            # Período novo: entra na posição cronológica, empurrando os
            # períodos posteriores para a direita.
            col_inicio = _posicao_cronologica(grupos, periodo)
            if col_inicio is None:
                col_inicio = _proxima_col_periodo(valores)
            else:
                _inserir_colunas(spreadsheet, aba, col_inicio, 1 + len(dias))
                valores = aba.get_all_values()
        else:
            col_inicio = grupo["col"]
            # Dias sem coluna no grupo (ex: Sábado do FDS) abrem espaço à direita
            dias_extra = [d for d in dias if d not in grupo["dias"]]
            if dias_extra and grupo is not grupos[-1]:
                _inserir_colunas(
                    spreadsheet, aba,
                    grupo["col"] + grupo["largura"], len(dias_extra),
                )
                valores = aba.get_all_values()
                grupos  = _ler_grupos(valores)
                grupo   = next((g for g in grupos if g["col"] == col_inicio), grupo)

        # Alunos lidos além da lista informada ganham linha própria
        max_num = max((c["numero"] for c in contagem), default=0)
        roster_alunos = list(alunos)
        while len(roster_alunos) < max_num:
            roster_alunos.append((f"Aluno {len(roster_alunos) + 1}", ""))

        linhas, novos, linha_fim = _sincronizar_roster(valores, roster_alunos)

        dados_por_linha = {}
        for c in contagem:
            linha = linhas.get(c["numero"] - 1)
            if linha is None:
                continue
            marcas = {}
            for dia in dias:
                d = c["detalhes"].get(dia, {})
                marcas[dia] = (
                    ("A" if d.get("almoco") else "") + ("J" if d.get("janta") else "")
                    if d.get("presente") else ""
                )
            dados_por_linha[linha] = {"marcas": marcas, "presencas": c["presencas"]}

        ordem_dias, matriz = _montar_matriz_grupo(
            valores, grupo, dias, dados_por_linha, linha_fim
        )

        _garantir_grade(aba, linha_fim, col_inicio + 1 + len(ordem_dias))
        _escrever_grupo(
            aba, col_inicio, periodo_grupo, ordem_dias, matriz, novos, linha_fim
        )

        # Confere a ordem depois de gravar: a garantia não depende de a
        # inserção ter acertado a posição, nem do estado anterior da aba.
        valores = aba.get_all_values()
        grupos  = _ler_grupos(valores)

        ilegiveis = _periodos_sem_data(grupos)
        aviso_ordem = (
            "Ordem cronológica desligada: não consegui ler a data de "
            + ", ".join(repr(p) for p in ilegiveis)
            + ". Use o formato '05/05 a 09/05'."
        ) if ilegiveis else None

        if _precisa_reordenar(grupos):
            grupos_formatar = _reordenar_grupos(
                spreadsheet, aba, valores, grupos, linha_fim
            )
        else:
            atual = next((g for g in grupos if g["col"] == col_inicio), None)
            grupos_formatar = [atual or {"col": col_inicio, "dias": ordem_dias}]

        resposta = {"ok": True, "aba": nome_aba}
        if aviso_ordem:
            resposta["aviso_ordem"] = aviso_ordem

        try:
            _formatar_colunas_fixas(spreadsheet, aba, linha_fim)
            for g in grupos_formatar:
                _aplicar_formatacao_horizontal(
                    spreadsheet, aba, g["col"], len(g["dias"]), linha_fim
                )
        except Exception as e_fmt:
            resposta["aviso_formatacao"] = str(e_fmt)

        return resposta

    except Exception as e:
        return {"ok": False, "erro": str(e)}


# --- DASHBOARD ANALÍTICO (Looker Studio) ---

def _datas_por_dia(periodo, dias):
    """
    Mapeia cada nome de dia para sua data exata dentro do período.

    Usa a data de início do período como âncora e calcula o offset pelo
    dia da semana, o que suporta tanto semanas regulares (Seg–Sex) quanto
    FDS e FDS especial (ex: Qui+Sex+Sab).
    """
    parte_inicio = periodo.split(" a ")[0].strip()
    dd, mm = parte_inicio.split("/")
    dd, mm = int(dd), int(mm)
    hoje = datetime.now()
    ano = hoje.year if mm <= hoje.month else hoje.year - 1
    data_inicio = datetime(ano, mm, dd)
    start_weekday = data_inicio.weekday()

    resultado = {}
    for dia in dias:
        alvo = _DIA_SEMANA_PT.get(dia)
        if alvo is None:
            resultado[dia] = data_inicio
            continue
        offset = (alvo - start_weekday) % 7
        resultado[dia] = data_inicio + timedelta(days=offset)

    return resultado


def _periodo_letivo(data):
    return f"{data.year}.1" if data.month <= 6 else f"{data.year}.2"


def _semana_ancora(data):
    """Retorna 'DD/MM a DD/MM' da Seg–Sex da semana que contém `data` (para FDS)."""
    monday = data - timedelta(days=data.weekday())
    friday = monday + timedelta(days=4)
    return f"{monday.strftime('%d/%m')} a {friday.strftime('%d/%m')}"


def _faltas_consecutivas(detalhes, dias):
    max_seq = seq = 0
    for dia in dias:
        if not detalhes.get(dia, {}).get("presente", True):
            seq += 1
            max_seq = max(max_seq, seq)
        else:
            seq = 0
    return max_seq


def _obter_ou_criar_aba_dashboard(spreadsheet, nome_aba, headers):
    """Obtém a aba ou cria nova com cabeçalhos na linha 1."""
    try:
        return spreadsheet.worksheet(nome_aba)
    except gspread.WorksheetNotFound:
        aba = spreadsheet.add_worksheet(title=nome_aba, rows=5000, cols=len(headers))
        aba.append_rows([headers], value_input_option="USER_ENTERED")
        return aba


def _upsert_aba(aba, novos_dados, key_cols):
    """
    Faz upsert eficiente em uma aba do Sheets.
    Máximo 2 chamadas à API por aba: 1 batch_update + 1 append_rows.

    Args:
        aba: worksheet gspread
        novos_dados: lista de listas, sem linha de cabeçalho
        key_cols: tupla de índices de coluna (0-indexed) que formam a chave de upsert
    """
    valores = aba.get_all_values()

    # Constrói índice {chave: row_1indexed} pulando o cabeçalho (valores[0])
    index = {}
    for i, row in enumerate(valores[1:], start=2):
        chave = tuple(row[c] if c < len(row) else "" for c in key_cols)
        index[chave] = i

    updates = []
    to_append = []

    for row_data in novos_dados:
        chave = tuple(str(row_data[c]) if c < len(row_data) else "" for c in key_cols)
        if chave in index:
            row_num = index[chave]
            last_col = chr(ord("A") + len(row_data) - 1)
            updates.append({
                "range": f"A{row_num}:{last_col}{row_num}",
                "values": [row_data],
            })
        else:
            to_append.append(row_data)

    if updates:
        aba.batch_update(updates, value_input_option="USER_ENTERED")
    if to_append:
        aba.append_rows(to_append, value_input_option="USER_ENTERED")


def _upsert_resumo_fds(aba, linhas_fds, threshold):
    """
    Mescla dados FDS no resumo_semanal de forma aditiva.

    Quando a linha da semana regular já existe (mesma matricula+semana+unidade),
    soma presenças e sessões e recalcula pct/irregular. Caso contrário insere nova.
    faltas_consecutivas mantém o valor da semana regular (mais representativo).
    """
    valores = aba.get_all_values()

    index = {}
    for i, row in enumerate(valores[1:], start=2):
        if len(row) >= 5:
            chave = (row[0], row[4], row[2])  # matricula, semana, unidade
            index[chave] = i

    updates = []
    to_append = []

    for row_data in linhas_fds:
        chave = (str(row_data[0]), str(row_data[4]), str(row_data[2]))

        if chave in index:
            row_num = index[chave]
            ex = valores[row_num - 1]

            def _int(col):
                try:
                    return int(ex[col]) if col < len(ex) and ex[col].strip() else 0
                except ValueError:
                    return 0

            novo_total      = _int(5) + row_data[5]
            novas_presencas = _int(6) + row_data[6]
            novas_ausencias = novo_total - novas_presencas
            novo_pct        = round(novas_presencas / novo_total, 4) if novo_total > 0 else 0.0

            nova_linha = [
                row_data[0], row_data[1], row_data[2], row_data[3], row_data[4],
                novo_total, novas_presencas, novas_ausencias,
                novo_pct, novo_pct < threshold,
                _int(10),
                row_data[11],  # mes
                row_data[12],  # data_inicio_semana
            ]
            last_col = chr(ord("A") + len(nova_linha) - 1)
            updates.append({
                "range": f"A{row_num}:{last_col}{row_num}",
                "values": [nova_linha],
            })
        else:
            to_append.append(row_data)

    if updates:
        aba.batch_update(updates, value_input_option="USER_ENTERED")
    if to_append:
        aba.append_rows(to_append, value_input_option="USER_ENTERED")


def exportar_para_dashboard(contagem, alunos, dias, restaurante_key, periodo):
    """
    Exporta dados analíticos para a planilha do Looker Studio.

    Popula duas abas fixas em `dashboard_spreadsheet_id`:
      - presencas: uma linha por aluno por dia (upsert por matricula+data)
      - resumo_semanal: uma linha por aluno por semana (upsert por matricula+semana+unidade)

    Sempre silenciosa em caso de erro — não interrompe o pipeline.

    Returns:
        {"ok": True} ou {"ok": False, "erro": "..."}
    """
    try:
        config = _carregar_config()

        dashboard_id = config.get("dashboard_spreadsheet_id", "").strip()
        if not dashboard_id:
            return {
                "ok": False,
                "erro": "dashboard_spreadsheet_id não configurado em config_sheets.yaml",
            }

        threshold = config.get("irregularidade_threshold", 0.75)

        cliente = _obter_cliente(config)
        spreadsheet = cliente.open_by_key(dashboard_id)

        aba_presencas = _obter_ou_criar_aba_dashboard(
            spreadsheet, "presencas", _HEADERS_PRESENCAS
        )
        aba_resumo = _obter_ou_criar_aba_dashboard(
            spreadsheet, "resumo_semanal", _HEADERS_RESUMO
        )

        eh_fds = restaurante_key in _RESTAURANTE_PAI
        unidade = _UNIDADE_LEGIVEL.get(restaurante_key, restaurante_key)
        datas = _datas_por_dia(periodo, dias)

        data_base = next(iter(datas.values())) if datas else datetime.now()
        pl = _periodo_letivo(data_base)
        semana_resumo = _semana_ancora(data_base) if eh_fds else periodo
        mes = f"{_MESES[data_base.month - 1]} {data_base.year}"
        monday = data_base - timedelta(days=data_base.weekday())
        data_inicio_semana = monday.strftime("%Y-%m-%d")

        # Linhas para `presencas` — uma por aluno por dia
        linhas_presencas = []
        for c in contagem:
            idx = c["numero"] - 1
            nome, matricula = (alunos[idx] if 0 <= idx < len(alunos)
                               else (f"Aluno {c['numero']}", ""))
            for dia in dias:
                det = c["detalhes"].get(
                    dia, {"presente": False, "almoco": False, "janta": False}
                )
                data_dia = datas.get(dia, data_base)
                linhas_presencas.append([
                    matricula,
                    nome,
                    unidade,
                    pl,
                    periodo,
                    data_dia.strftime("%d/%m/%Y"),
                    dia,
                    "presente" if det["presente"] else "ausente",
                    det["almoco"],
                    det["janta"],
                ])

        # Linhas para `resumo_semanal` — uma por aluno por semana
        total_sessoes = len(dias)
        linhas_resumo = []
        for c in contagem:
            idx = c["numero"] - 1
            nome, matricula = (alunos[idx] if 0 <= idx < len(alunos)
                               else (f"Aluno {c['numero']}", ""))
            presencas_sem = c["presencas"]
            ausencias_sem = total_sessoes - presencas_sem
            pct = presencas_sem / total_sessoes if total_sessoes > 0 else 0.0
            linhas_resumo.append([
                matricula,
                nome,
                unidade,
                pl,
                semana_resumo,
                total_sessoes,
                presencas_sem,
                ausencias_sem,
                round(pct, 4),
                pct < threshold,
                _faltas_consecutivas(c["detalhes"], dias),
                mes,
                data_inicio_semana,
            ])

        # presencas: chave = matricula (col 0) + data (col 5) — FDS tem datas distintas, sem conflito
        _upsert_aba(aba_presencas, linhas_presencas, (0, 5))
        # resumo_semanal: FDS soma na linha da semana regular; caso contrário upsert normal
        if eh_fds:
            _upsert_resumo_fds(aba_resumo, linhas_resumo, threshold)
        else:
            _upsert_aba(aba_resumo, linhas_resumo, (0, 4, 2))

        return {"ok": True}

    except Exception as e:
        return {"ok": False, "erro": str(e)}
