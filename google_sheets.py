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

        rest = config.get("restaurantes", {}).get(restaurante_key, {})
        spreadsheet_id = rest.get("spreadsheet_id", "").strip()
        if not spreadsheet_id:
            return {
                "ok": False,
                "erro": (
                    f"spreadsheet_id não configurado para '{restaurante_key}' "
                    "em config_sheets.yaml"
                ),
            }

        cliente = _obter_cliente(config)
        spreadsheet = cliente.open_by_key(spreadsheet_id)
        nome_aba = _nome_aba_mes(periodo)
        aba = _obter_ou_criar_aba(spreadsheet, nome_aba)

        bloco_existente = _localizar_bloco(aba, periodo)

        if bloco_existente and not forcar:
            return {"ok": False, "duplicado": True, "aba": nome_aba}

        if bloco_existente:
            inicio, fim = bloco_existente
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
