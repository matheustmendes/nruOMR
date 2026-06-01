"""
google_sheets.py

Exporta resultados de presença para Google Sheets.

Configuração em config_sheets.yaml:
    credentials: credentials_sheets.json
    restaurantes:
      canela:
        spreadsheet_id: "1abc..."
      ondina:
        spreadsheet_id: "1def..."
      sao_lazaro:
        spreadsheet_id: "1ghi..."

Estrutura escrita na planilha (aba mensal, ex: "Maio 2026"):
    Período: 05/05 a 09/05
    Nº | Nome | Matrícula | Presenças | Seg | Ter | Qua | Qui | Sex
    1  | João  | 123456   | 3         | AJ  |     | A   |     | J
    (linha vazia separando semanas)
"""

import os
import yaml
from datetime import datetime

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
        return spreadsheet.add_worksheet(title=nome_aba, rows=500, cols=20)


def _marcador_periodo(periodo):
    return f"Período: {periodo}"


def _localizar_bloco(aba, periodo):
    """
    Retorna (linha_inicio, linha_fim) 1-indexed se o período já existe,
    ou None. O range inclui a linha vazia separadora se presente.
    """
    marcador = _marcador_periodo(periodo)
    valores = aba.get_all_values()

    inicio = None
    for i, row in enumerate(valores):
        if row and row[0] == marcador:
            inicio = i + 1  # converte para 1-indexed
            break

    if inicio is None:
        return None

    # Encontra fim do bloco: próxima linha vazia ou fim da planilha
    fim = inicio
    for i in range(inicio, len(valores)):  # i é 0-indexed
        if not any(valores[i]):
            fim = i + 1  # inclui a linha vazia, 1-indexed
            break
        fim = i + 1

    return (inicio, fim)


def _localizar_bloco_semana_fds(aba, periodo_fds):
    """
    Para FDS: encontra o bloco de dias úteis da mesma semana, mesmo que o
    período FDS ("10/05 a 10/05") não coincida exatamente com o da semana
    ("05/05 a 09/05"). Aceita blocos cujo início seja 0-6 dias antes do FDS.
    """
    from datetime import datetime

    try:
        parte = periodo_fds.split(" a ")[0].strip()
        dd, mm = parte.split("/")
        hoje = datetime.now()
        ano = hoje.year if int(mm) <= hoje.month else hoje.year - 1
        data_fds = datetime(ano, int(mm), int(dd))
    except Exception:
        return None

    valores = aba.get_all_values()
    melhor = None  # (inicio_1idx, diff_dias)

    for i, row in enumerate(valores):
        if not (row and row[0].startswith("Período: ")):
            continue
        periodo_str = row[0][len("Período: "):]
        try:
            parte2 = periodo_str.split(" a ")[0].strip()
            dd2, mm2 = parte2.split("/")
            # Mesmo ano ou anterior se o mês do bloco for maior que o mês FDS
            ano2 = ano if int(mm2) <= int(mm) else ano - 1
            data_ini = datetime(ano2, int(mm2), int(dd2))
            diff = (data_fds - data_ini).days
            if 0 <= diff <= 6:
                if melhor is None or diff < melhor[1]:
                    melhor = (i + 1, diff)
        except Exception:
            continue

    if melhor is None:
        return None

    inicio_1idx = melhor[0]
    fim = inicio_1idx
    for i in range(inicio_1idx, len(valores)):
        if not any(valores[i]):
            fim = i + 1
            break
        fim = i + 1

    return (inicio_1idx, fim)


def _construir_bloco(contagem, alunos, dias, periodo):
    linhas = []
    linhas.append([_marcador_periodo(periodo)])
    linhas.append(["Nº", "Nome", "Matrícula", "Presenças"] + list(dias))

    for c in contagem:
        idx = c["numero"] - 1
        nome, matricula = (alunos[idx] if 0 <= idx < len(alunos)
                           else (f"Aluno {c['numero']}", ""))
        linha = [c["numero"], nome, matricula, c["presencas"]]
        for dia in dias:
            d = c["detalhes"][dia]
            if d["presente"]:
                marcas = ("A" if d["almoco"] else "") + ("J" if d["janta"] else "")
                linha.append(marcas)
            else:
                linha.append("")
        linhas.append(linha)

    linhas.append([])  # separador entre semanas
    return linhas


def _ultima_linha_com_dados(aba):
    """Retorna índice 1-indexed da última linha não vazia, ou 0 se planilha vazia."""
    valores = aba.get_all_values()
    for i in range(len(valores) - 1, -1, -1):
        if any(cell.strip() for cell in valores[i]):
            return i + 1
    return 0


def _aplicar_formatacao(spreadsheet, aba, linha_inicio_1idx, num_alunos, num_dias):
    """Aplica formatação profissional ao bloco recém-inserido via Sheets API v4."""
    sheet_id = aba.id
    num_colunas = 4 + num_dias

    # Índices de linha em 0-indexed para a API
    r_periodo    = linha_inicio_1idx - 1
    r_cabecalho  = r_periodo + 1
    r_dados_ini  = r_cabecalho + 1
    r_dados_fim  = r_dados_ini + num_alunos  # exclusive

    borda = {"style": "SOLID", "width": 1, "color": _COR_BORDA}

    def rng(r_ini, r_fim, c_ini=0, c_fim=None):
        return {
            "sheetId": sheet_id,
            "startRowIndex": r_ini,
            "endRowIndex": r_fim,
            "startColumnIndex": c_ini,
            "endColumnIndex": c_fim if c_fim is not None else num_colunas,
        }

    requests = [
        # 1. Mesclar linha do período em todas as colunas
        {
            "mergeCells": {
                "range": rng(r_periodo, r_periodo + 1),
                "mergeType": "MERGE_ALL",
            }
        },
        # 2. Estilo da linha do período (título azul escuro)
        {
            "repeatCell": {
                "range": rng(r_periodo, r_periodo + 1),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _COR_TITULO,
                    "textFormat": {
                        "bold": True,
                        "fontSize": 11,
                        "foregroundColor": _BRANCO,
                    },
                    "horizontalAlignment": "LEFT",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "OVERFLOW_CELL",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        # 3. Estilo da linha de cabeçalho (azul claro, negrito)
        {
            "repeatCell": {
                "range": rng(r_cabecalho, r_cabecalho + 1),
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
        # 4. Estilo geral das linhas de dados
        {
            "repeatCell": {
                "range": rng(r_dados_ini, r_dados_fim),
                "cell": {"userEnteredFormat": {
                    "textFormat": {"fontSize": 10},
                    "verticalAlignment": "MIDDLE",
                    "horizontalAlignment": "CENTER",
                    "wrapStrategy": "CLIP",
                }},
                "fields": "userEnteredFormat(textFormat,verticalAlignment,horizontalAlignment,wrapStrategy)",
            }
        },
        # 5. Coluna de nomes: alinhado à esquerda
        {
            "repeatCell": {
                "range": rng(r_dados_ini, r_dados_fim, c_ini=1, c_fim=2),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                "fields": "userEnteredFormat(horizontalAlignment)",
            }
        },
        # 6. Bordas em todo o bloco (período + cabeçalho + dados)
        {
            "updateBorders": {
                "range": rng(r_periodo, r_dados_fim),
                "top": borda, "bottom": borda,
                "left": borda, "right": borda,
                "innerHorizontal": borda, "innerVertical": borda,
            }
        },
        # 7. Larguras das colunas fixas
        *[
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            }
            for col, px in [(0, 45), (1, 220), (2, 105), (3, 80)]
        ],
        # 8. Altura da linha do período
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": r_periodo, "endIndex": r_periodo + 1},
                "properties": {"pixelSize": 32},
                "fields": "pixelSize",
            }
        },
        # 9. Altura da linha de cabeçalho
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": r_cabecalho, "endIndex": r_cabecalho + 1},
                "properties": {"pixelSize": 28},
                "fields": "pixelSize",
            }
        },
    ]

    # 10. Largura das colunas de dias (55 px cada)
    for i in range(num_dias):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": 4 + i, "endIndex": 5 + i},
                "properties": {"pixelSize": 55},
                "fields": "pixelSize",
            }
        })

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


def exportar_para_sheets(contagem, alunos, dias, restaurante_key, periodo, forcar=False):
    """
    Exporta dados de presença para Google Sheets.

    Args:
        contagem: lista de dicts com presenças (saída de contar_presencas)
        alunos: lista de (nome, matricula)
        dias: lista de nomes dos dias
        restaurante_key: "canela", "ondina" ou "sao_lazaro"
        periodo: string do período da semana, ex: "05/05 a 09/05"
        forcar: se True, sobrescreve o bloco existente sem perguntar

    Returns:
        dict:
            {"ok": True, "aba": "Maio 2026"}                          — sucesso
            {"ok": True, "aba": "...", "aviso_formatacao": "..."}     — dados salvos, formatação falhou
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

        bloco_existente = _localizar_bloco(aba, periodo)

        if eh_fds:
            # Tenta localizar o bloco da semana mesmo quando o período FDS difere
            # (ex: "10/05 a 10/05" vs "05/05 a 09/05")
            if bloco_existente is None:
                bloco_existente = _localizar_bloco_semana_fds(aba, periodo)

            if bloco_existente:
                inicio, fim = bloco_existente
                valores = aba.get_all_values()
                header = valores[inicio] if inicio < len(valores) else []
                dia_fds_set = set(dias)

                # Verifica dados reais nas colunas FDS — não basta o header existir,
                # pois no FDS especial (ex: Qui+Sex+Sab) Qui/Sex podem estar no
                # template de semana mas vazios (feriado).
                ja_mesclado = False
                for j, h in enumerate(header):
                    if h.strip() not in dia_fds_set:
                        continue
                    for row_0idx in range(inicio + 1, len(valores)):
                        row = valores[row_0idx]
                        if not any(cell.strip() for cell in row):
                            break
                        if j < len(row) and row[j].strip():
                            ja_mesclado = True
                            break
                    if ja_mesclado:
                        break

                if ja_mesclado and not forcar:
                    return {"ok": False, "duplicado": True, "aba": nome_aba}

                if ja_mesclado and forcar:
                    _desfazer_fds_do_bloco(aba, inicio, dias)

                _mesclar_fds_no_bloco(aba, inicio, contagem, dias)
            else:
                # Bloco da semana ainda não existe: cria um bloco FDS standalone
                bloco = _construir_bloco(contagem, alunos, dias, periodo)
                linha_inicio = _ultima_linha_com_dados(aba) + 1
                aba.append_rows(bloco, value_input_option="USER_ENTERED")
                try:
                    _aplicar_formatacao(spreadsheet, aba, linha_inicio, len(contagem), len(dias))
                except Exception as e_fmt:
                    return {"ok": True, "aba": nome_aba, "aviso_formatacao": str(e_fmt)}

            return {"ok": True, "aba": nome_aba}

        # --- Fluxo normal (restaurante de semana) ---
        if bloco_existente and not forcar:
            return {"ok": False, "duplicado": True, "aba": nome_aba}

        if bloco_existente:
            inicio, fim = bloco_existente
            total_linhas = len(aba.get_all_values())
            if inicio == 1 and fim >= total_linhas:
                aba.clear()
            else:
                aba.delete_rows(inicio, fim)

        bloco = _construir_bloco(contagem, alunos, dias, periodo)
        linha_inicio = _ultima_linha_com_dados(aba) + 1
        aba.append_rows(bloco, value_input_option="USER_ENTERED")

        try:
            _aplicar_formatacao(spreadsheet, aba, linha_inicio, len(contagem), len(dias))
        except Exception as e_fmt:
            return {"ok": True, "aba": nome_aba, "aviso_formatacao": str(e_fmt)}

        return {"ok": True, "aba": nome_aba}

    except Exception as e:
        return {"ok": False, "erro": str(e)}
