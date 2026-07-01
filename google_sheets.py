"""
google_sheets.py

Exporta resultados de presença para Google Sheets.

Estrutura da aba mensal (ex: "Maio 2026") — layout horizontal:
    Row 1: (vazio) | (vazio) | (vazio) | Período: 05/05 a 09/05 | ... | Período: 12/05 a 16/05 | ...
    Row 2: Nº | Nome | Matrícula | Presenças | Seg | Ter | Qua | Qui | Sex | Presenças | Seg | ...
    Row 3+: 1 | João | 123456 | 3 | AJ | | A | | J | 4 | A | AJ | ...

Cada semana/período vira um grupo de colunas. Rows 1 e 2 são congeladas para
scroll horizontal. Colunas Nº/Nome/Mat são fixas (frozen).
"""

import os
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
_COR_BORDA     = {"red": 0.65, "green": 0.65, "blue": 0.65}
_BRANCO        = {"red": 1.0,  "green": 1.0,  "blue": 1.0}


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
        return spreadsheet.add_worksheet(title=nome_aba, rows=600, cols=100)


def _marcador_periodo(periodo):
    return f"Período: {periodo}"


# --- LAYOUT HORIZONTAL: períodos lado a lado como colunas ---
# Estrutura da aba mensal:
#   Row 1: vazia nas cols fixas | "Período: X" (mesclado) | "Período: Y" (mesclado) | ...
#   Row 2: Nº | Nome | Matrícula | Presenças | Seg | Ter | ... | Presenças | Seg | ...
#   Row 3+: uma linha por aluno, permanente durante o mês
#
# FDS recebe bloco próprio (não mescla no período de semana) usando a planilha
# do restaurante pai (ex: canela_fds → planilha de canela).

# Restaurantes FDS usam a planilha do restaurante pai.
_RESTAURANTE_PAI = {
    "canela_fds":              "canela",
    "sao_lazaro_fds":          "sao_lazaro",
    "canela_fds_especial":     "canela",
    "sao_lazaro_fds_especial": "sao_lazaro",
}

_COL_FIXAS = 3  # Nº, Nome, Matrícula


def _encontrar_col_periodo(valores, periodo):
    """Retorna índice 0-based da coluna onde o período aparece em row 1, ou None."""
    if not valores:
        return None
    marcador = _marcador_periodo(periodo)
    for j, v in enumerate(valores[0]):
        if str(v).strip() == marcador:
            return j
    return None


def _proxima_col_periodo(valores):
    """Retorna índice 0-based da próxima coluna disponível para novo período."""
    if not valores:
        return _COL_FIXAS
    max_j = _COL_FIXAS - 1
    for row in valores:
        for j in range(len(row) - 1, -1, -1):
            if j < len(row) and str(row[j]).strip():
                if j > max_j:
                    max_j = j
                break
    return max_j + 1


def _montar_row_dados(c, dias):
    """Monta [presencas, mark_dia1, mark_dia2, ...] para as colunas do período."""
    if not c:
        return [0] + [""] * len(dias)
    row = [c["presencas"]]
    for dia in dias:
        d = c["detalhes"].get(dia, {})
        if d.get("presente"):
            row.append(("A" if d.get("almoco") else "") + ("J" if d.get("janta") else ""))
        else:
            row.append("")
    return row


def _escrever_periodo_horizontal(aba, valores, contagem, alunos, dias, periodo, col_inicio):
    """
    Escreve (ou sobrescreve) as colunas de um período.
    Se a aba não tem linhas de alunos ainda, também inicializa Nº/Nome/Mat.
    """
    import gspread as _gs

    num_cols = 1 + len(dias)
    eh_nova = len(valores) < 3 or not any(
        str(r[0]).strip() for r in valores[2:] if r
    )
    aluno_map = {c["numero"]: c for c in contagem}
    updates = []

    # Row 1: marcador de período
    updates.append({
        "range": _gs.utils.rowcol_to_a1(1, col_inicio + 1),
        "values": [[_marcador_periodo(periodo)]],
    })

    if eh_nova:
        # Row 2: cabeçalhos fixos + sub-headers do período em uma única linha
        cabecalho = (
            ["Nº", "Nome", "Matrícula"]
            + [""] * (col_inicio - _COL_FIXAS)
            + ["Presenças"] + list(dias)
        )
        updates.append({"range": "A2", "values": [cabecalho]})

        # Rows 3+: dados completos de cada aluno
        for i, (nome, mat) in enumerate(alunos):
            num = i + 1
            row_data = _montar_row_dados(aluno_map.get(num), dias)
            sheet_row = num + 2
            linha = [num, nome, mat] + [""] * (col_inicio - _COL_FIXAS) + row_data
            updates.append({"range": f"A{sheet_row}", "values": [linha]})
    else:
        # Row 2: apenas sub-headers do novo período
        updates.append({
            "range": (
                f"{_gs.utils.rowcol_to_a1(2, col_inicio + 1)}"
                f":{_gs.utils.rowcol_to_a1(2, col_inicio + num_cols)}"
            ),
            "values": [["Presenças"] + list(dias)],
        })

        # Rows 3+: atualiza dados nas colunas do período para alunos existentes
        for row_idx in range(2, len(valores)):
            row = valores[row_idx]
            if not row or not str(row[0]).strip():
                continue
            try:
                num = int(str(row[0]).strip())
            except ValueError:
                continue
            row_data = _montar_row_dados(aluno_map.get(num), dias)
            sheet_row = row_idx + 1
            updates.append({
                "range": (
                    f"{_gs.utils.rowcol_to_a1(sheet_row, col_inicio + 1)}"
                    f":{_gs.utils.rowcol_to_a1(sheet_row, col_inicio + num_cols)}"
                ),
                "values": [row_data],
            })

    aba.batch_update(updates, value_input_option="USER_ENTERED")


def _aplicar_formatacao_horizontal(spreadsheet, aba, col_inicio, num_dias, num_alunos):
    """Formata as colunas do período recém-adicionado via Sheets API v4."""
    sheet_id = aba.id
    num_cols = 1 + num_dias

    r_per  = 0      # row 1 (0-indexed): período header
    r_cab  = 1      # row 2: sub-headers
    r_ini  = 2      # row 3: início dos dados
    r_fim  = r_ini + num_alunos

    c0 = col_inicio
    c1 = col_inicio + num_cols

    borda = {"style": "SOLID", "width": 1, "color": _COR_BORDA}

    def rng(r0, r1, c_ini=c0, c_fim=c1):
        return {
            "sheetId": sheet_id,
            "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c_ini, "endColumnIndex": c_fim,
        }

    requests = [
        {"mergeCells": {"range": rng(r_per, r_per + 1), "mergeType": "MERGE_ALL"}},
        {"repeatCell": {
            "range": rng(r_per, r_per + 1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _COR_TITULO,
                "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": _BRANCO},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }},
        {"repeatCell": {
            "range": rng(r_cab, r_cab + 1),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _COR_CABECALHO,
                "textFormat": {"bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }},
        {"repeatCell": {
            "range": rng(r_ini, r_fim),
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontSize": 10},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)",
        }},
        {"updateBorders": {
            "range": rng(r_per, r_fim),
            "top": borda, "bottom": borda, "left": borda, "right": borda,
            "innerHorizontal": borda, "innerVertical": borda,
        }},
        # Largura coluna Presenças
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": c0, "endIndex": c0 + 1},
            "properties": {"pixelSize": 75}, "fields": "pixelSize",
        }},
        # Altura row período
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": r_per, "endIndex": r_per + 1},
            "properties": {"pixelSize": 32}, "fields": "pixelSize",
        }},
        # Altura row sub-headers
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": r_cab, "endIndex": r_cab + 1},
            "properties": {"pixelSize": 28}, "fields": "pixelSize",
        }},
    ]

    for i in range(num_dias):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": c0 + 1 + i, "endIndex": c0 + 2 + i},
            "properties": {"pixelSize": 55}, "fields": "pixelSize",
        }})

    spreadsheet.batch_update({"requests": requests})


# Restaurantes FDS não têm planilha própria: usam a planilha do restaurante pai
# e mesclam os dados no bloco da semana já existente.
# FDS especial = feriado emendado com dias customizáveis (ex: Qui+Sex+Sab).
_RESTAURANTE_PAI = {
    "canela_fds":           "canela",
    "sao_lazaro_fds":       "sao_lazaro",
    "canela_fds_especial":  "canela",
    "sao_lazaro_fds_especial": "sao_lazaro",
}


def _mesclar_fds_no_bloco(aba, inicio_1idx, contagem_fds, dias_fds):
    """
    Adiciona dados do FDS ao bloco da semana já existente.

    Suporta múltiplos dias (ex: FDS especial = ["Quinta", "Sexta", "Sábado"]).
    - Dias já presentes no header (mesmo vindos do template de semana) são
      reutilizados; dias novos são adicionados após a última coluna.
    - Incrementa "Presenças" com o total de dias FDS presentes por aluno.
    """
    import gspread as _gs

    valores = aba.get_all_values()

    # header está no índice 0-based = inicio_1idx (marcador é inicio_1idx - 1)
    header_0idx = inicio_1idx
    header_row_1idx = inicio_1idx + 1
    data_start_0idx = inicio_1idx + 1

    if header_0idx >= len(valores):
        return

    header = valores[header_0idx]

    # Mapeia cada dia FDS para a coluna no header (existente ou nova)
    dia_cols = {}   # dia -> col_0idx
    novos_dias = [] # dias que precisam de nova coluna no cabeçalho

    ultimo_col_alocado = max((j for j, h in enumerate(header) if h.strip()), default=3)

    for dia in dias_fds:
        col = None
        for j, h in enumerate(header):
            if h.strip() == dia:
                col = j
                break
        if col is None:
            ultimo_col_alocado += 1
            col = ultimo_col_alocado
            novos_dias.append(dia)
        dia_cols[dia] = col

    col_presencas_0idx = 3
    fds_map = {c["numero"]: c for c in contagem_fds}
    updates = []

    for dia in novos_dias:
        updates.append({
            "range": _gs.utils.rowcol_to_a1(header_row_1idx, dia_cols[dia] + 1),
            "values": [[dia]],
        })

    for row_0idx in range(data_start_0idx, len(valores)):
        row = valores[row_0idx]
        if not any(cell.strip() for cell in row):
            break

        try:
            num = int(row[0])
        except (ValueError, IndexError):
            continue

        c = fds_map.get(num)
        if not c:
            continue

        sheet_row_1idx = row_0idx + 1

        try:
            presencas_atuais = (
                int(row[col_presencas_0idx])
                if col_presencas_0idx < len(row) and row[col_presencas_0idx].strip()
                else 0
            )
        except ValueError:
            presencas_atuais = 0

        updates.append({
            "range": _gs.utils.rowcol_to_a1(sheet_row_1idx, col_presencas_0idx + 1),
            "values": [[presencas_atuais + c["presencas"]]],
        })

        for dia in dias_fds:
            d = c["detalhes"].get(dia, {})
            marcas = ("A" if d.get("almoco") else "") + ("J" if d.get("janta") else "")
            updates.append({
                "range": _gs.utils.rowcol_to_a1(sheet_row_1idx, dia_cols[dia] + 1),
                "values": [[marcas]],
            })

    if updates:
        aba.batch_update(updates, value_input_option="USER_ENTERED")


def _desfazer_fds_do_bloco(aba, inicio_1idx, dias_fds):
    """
    Desfaz a mesclagem FDS de um bloco (para permitir re-exportação forçada).

    Suporta múltiplos dias: decrementa "Presenças" pelo número de dias FDS que
    tinham marcação e limpa as células correspondentes.
    """
    import gspread as _gs

    valores = aba.get_all_values()
    header_0idx = inicio_1idx
    data_start_0idx = inicio_1idx + 1

    if header_0idx >= len(valores):
        return

    header = valores[header_0idx]

    # Localiza colunas para cada dia FDS
    dia_cols = {}
    for dia in dias_fds:
        for j, h in enumerate(header):
            if h.strip() == dia:
                dia_cols[dia] = j
                break

    if not dia_cols:
        return

    col_presencas_0idx = 3
    updates = []

    for row_0idx in range(data_start_0idx, len(valores)):
        row = valores[row_0idx]
        if not any(cell.strip() for cell in row):
            break

        sheet_row_1idx = row_0idx + 1

        # Conta quantos dias FDS tinham marcação nesta linha
        dias_tinha = sum(
            1 for col in dia_cols.values()
            if col < len(row) and row[col].strip()
        )

        if dias_tinha == 0:
            continue

        try:
            presencas_atuais = (
                int(row[col_presencas_0idx])
                if col_presencas_0idx < len(row) and row[col_presencas_0idx].strip()
                else dias_tinha
            )
        except ValueError:
            presencas_atuais = dias_tinha

        updates.append({
            "range": _gs.utils.rowcol_to_a1(sheet_row_1idx, col_presencas_0idx + 1),
            "values": [[max(0, presencas_atuais - dias_tinha)]],
        })

        for col in dia_cols.values():
            if col < len(row) and row[col].strip():
                updates.append({
                    "range": _gs.utils.rowcol_to_a1(sheet_row_1idx, col + 1),
                    "values": [[""]],
                })

    if updates:
        aba.batch_update(updates, value_input_option="USER_ENTERED")


_DIA_SEMANA_PT = {
    "Segunda": 0,
    "Terça":   1,
    "Quarta":  2,
    "Quinta":  3,
    "Sexta":   4,
    "Sábado":  5,
    "Domingo": 6,
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

    Cada período vira um grupo de colunas lado a lado na aba mensal.
    Row 1 = marcadores de período; Row 2 = sub-headers; Rows 3+ = alunos.
    FDS usa a planilha do restaurante pai como bloco standalone.

    Returns:
        {"ok": True, "aba": "Maio 2026"}
        {"ok": True, "aba": "...", "aviso_formatacao": "..."}
        {"ok": False, "duplicado": True, "aba": "..."}
        {"ok": False, "erro": "mensagem"}
    """
    try:
        config = _carregar_config()

        # FDS usa a planilha do restaurante pai (ex: canela_fds → canela)
        restaurante_efetivo = _RESTAURANTE_PAI.get(restaurante_key, restaurante_key)

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

        col_existente = _encontrar_col_periodo(valores, periodo)

        if col_existente is not None and not forcar:
            return {"ok": False, "duplicado": True, "aba": nome_aba}

        eh_nova = len(valores) < 3 or not any(
            str(r[0]).strip() for r in valores[2:] if r
        )
        col_inicio = col_existente if col_existente is not None else _proxima_col_periodo(valores)

        _escrever_periodo_horizontal(aba, valores, contagem, alunos, dias, periodo, col_inicio)

        try:
            # Congela colunas fixas ANTES de criar mesclagens (requisito da API)
            if eh_nova:
                _formatar_colunas_fixas(spreadsheet, aba, len(alunos))
            _aplicar_formatacao_horizontal(spreadsheet, aba, col_inicio, len(dias), len(contagem))
        except Exception as e_fmt:
            return {"ok": True, "aba": nome_aba, "aviso_formatacao": str(e_fmt)}

        return {"ok": True, "aba": nome_aba}

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
