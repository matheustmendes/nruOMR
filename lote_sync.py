"""
lote_sync.py

Sincronização de lotes (`lotes/<id>.json`) entre máquinas via Google
Sheets — ver SINCRONIZACAO_LOTES.md para o problema que isso resolve.

Por que Sheets e não Drive API direto
--------------------------------------
A primeira tentativa foi Drive API (upload do .json/.pdf numa pasta
compartilhada). Falhou num jeito estrutural, não de configuração: criar um
arquivo *novo* via conta de serviço sempre tenta alocar a cota de
armazenamento da própria conta de serviço, e ela não tem nenhuma fora de um
Google Workspace — nem compartilhar a pasta como Editor contorna isso (erro
real recebido: "Service Accounts do not have storage quota. Leverage shared
drives, or use OAuth delegation instead." — as duas saídas exigem Workspace).

Escrever numa planilha que já existe não tem esse problema: a cota é de
quem é dono do arquivo (a conta humana que já compartilha os spreadsheets
de exportação), a conta de serviço só edita conteúdo. Por isso este módulo
reaproveita a mesma planilha do dashboard (`dashboard_spreadsheet_id`, já
compartilhada como Editor) numa aba nova — nenhum passo de configuração
adicional é necessário.

Limitação aceita: PDFs não sincronizam por aqui (não cabem bem numa célula
de planilha). Só o roster/geometria sincroniza — é isso que resolve o bug
de identidade; a reimpressão exata do PDF continua só na máquina que gerou
(mesma limitação que já existia antes desta sincronização).

Restaurante grande (Canela passa de 500 pessoas) gera um JSON maior que o
limite real do Sheets de 50.000 caracteres por célula — por isso o JSON
vai fatiado em colunas extras (`lote_json`, `lote_json`+1, ...) quando não
cabe numa célula só; a leitura junta todas de volta. A aba cresce (mais
colunas) sozinha quando precisa.

Reaproveita a autenticação e os helpers já existentes em google_sheets.py
(`_carregar_config`, `_obter_cliente`, `_com_retry`,
`_obter_ou_criar_aba_dashboard`, `_upsert_aba`) — mesmo domínio (Sheets),
sem duplicar o padrão de autenticação/retry num módulo à parte.
"""

import json

import google_sheets as gs

_ABA_PADRAO = "lotes_sync"

_CABECALHOS = [
    "lote_id", "restaurante_key", "restaurante_nome", "datas", "mes_ano",
    "dias", "total_alunos", "total_paginas", "origem", "criado_em",
    "processado", "lote_json",
]
_COL = {nome: i for i, nome in enumerate(_CABECALHOS)}

# Uma célula do Sheets aceita até 50.000 caracteres — fica-se com margem de
# segurança. `_MAX_PARTES` é o teto de colunas extras pro JSON fatiado:
# 11 colunas fixas + até 14 de JSON = 25, ainda dentro de colunas de uma
# letra só (A-Z), que é o que `google_sheets._upsert_aba` sabe endereçar.
_TAMANHO_CHUNK = 45000
_MAX_PARTES = 14


def _config():
    return gs._carregar_config()


def _spreadsheet_id(config) -> str:
    override = (config.get("lotes_sincronizacao") or {}).get("spreadsheet_id", "")
    if override and override.strip():
        return override.strip()
    return (config.get("dashboard_spreadsheet_id") or "").strip()


def disponivel() -> bool:
    """False quando gspread não está instalado ou não há planilha
    configurada (nem `lotes_sincronizacao.spreadsheet_id`, nem
    `dashboard_spreadsheet_id`) — nesses casos, todo o resto deste módulo
    vira no-op."""
    if not gs._DISPONIVEL:
        return False
    try:
        config = _config()
    except FileNotFoundError:
        return False
    return bool(_spreadsheet_id(config))


def _obter_aba(config):
    cliente = gs._obter_cliente(config)
    planilha = cliente.open_by_key(_spreadsheet_id(config))
    nome_aba = (config.get("lotes_sincronizacao") or {}).get("aba") or _ABA_PADRAO
    return gs._obter_ou_criar_aba_dashboard(planilha, nome_aba, _CABECALHOS)


def _linha(lote_id, partes_json, propriedades):
    p = propriedades or {}
    return [
        lote_id,
        p.get("restaurante_key", ""),
        p.get("restaurante_nome", ""),
        p.get("datas", ""),
        p.get("mes_ano", ""),
        p.get("dias", ""),
        str(p.get("total_alunos", 0)),
        str(p.get("total_paginas", 0)),
        p.get("origem", ""),
        p.get("criado_em", ""),
        "0",
        *partes_json,
    ]


def _particionar(texto: str) -> list:
    """Fatia o JSON em pedaços que cabem numa célula — uma lista com um só
    elemento quando ele já coube inteiro (caso comum)."""
    if not texto:
        return [""]
    return [texto[i:i + _TAMANHO_CHUNK] for i in range(0, len(texto), _TAMANHO_CHUNK)]


def _garantir_colunas(aba, minimo):
    """Expande a grade da aba se ela ainda não tiver colunas suficientes
    pro JSON fatiado — redimensionar não apaga o que já está gravado nas
    colunas existentes."""
    if aba.col_count < minimo:
        gs._com_retry(lambda: aba.resize(cols=minimo))


# --- API pública (chamada por lote.py) ---------------------------------------

def enviar(lote_id, caminho_json, caminho_pdf=None, propriedades=None) -> bool:
    """
    Grava (upsert) a linha do lote na aba compartilhada.

    `caminho_pdf` é aceito só por compatibilidade de assinatura com o que
    `lote.py` chama — este backend não sincroniza PDF, é ignorado.
    """
    config = _config()
    if not _spreadsheet_id(config):
        return False

    with open(caminho_json, "r", encoding="utf-8") as f:
        lote_dict = json.load(f)
    compacto = json.dumps(lote_dict, ensure_ascii=False, separators=(",", ":"))
    partes = _particionar(compacto)

    # Mesmo fatiado, tem um teto — sem essa checagem o erro que chega em
    # `lote.py` é o 400 cru da API, sem dizer o que realmente aconteceu.
    if len(partes) > _MAX_PARTES:
        raise ValueError(
            f"lote grande demais até fatiado em células do Sheets "
            f"({len(compacto)} caracteres, {len(lote_dict.get('alunos', []))} pessoas) "
            "— fica só nesta máquina, não sincroniza"
        )

    aba = _obter_aba(config)
    linha = _linha(lote_id, partes, propriedades)
    _garantir_colunas(aba, len(linha))
    gs._com_retry(lambda: gs._upsert_aba(aba, [linha], key_cols=(0,)))
    return True


def baixar_json(lote_id):
    """Devolve o dict do lote, ou None se a linha não existir na aba."""
    config = _config()
    if not _spreadsheet_id(config):
        return None

    aba = _obter_aba(config)
    valores = gs._com_retry(lambda: aba.get_all_values())
    for row in valores[1:]:
        if row and row[0] == lote_id:
            # O JSON pode estar fatiado em mais de uma coluna (lotes
            # grandes) — junta tudo da coluna do lote_json em diante.
            texto = "".join(row[_COL["lote_json"]:])
            return json.loads(texto) if texto else None
    return None


def baixar_pdf(lote_id, destino_caminho) -> bool:
    """PDFs não sincronizam por este backend — sempre False (ver docstring
    do módulo). Mantido para a mesma interface que `lote.py` chama."""
    return False


def listar(restaurante_key=None) -> list:
    """
    Lotes conhecidos pela aba compartilhada, no formato de resumo que
    `lote.listar_lotes` monta a partir do disco local.
    """
    config = _config()
    if not _spreadsheet_id(config):
        return []

    aba = _obter_aba(config)
    valores = gs._com_retry(lambda: aba.get_all_values())

    def _col(row, nome, padrao=""):
        i = _COL[nome]
        return row[i] if i < len(row) else padrao

    itens = []
    for row in valores[1:]:
        if not row or not row[0]:
            continue
        if restaurante_key and _col(row, "restaurante_key") != restaurante_key:
            continue
        dias = _col(row, "dias")
        itens.append({
            "lote_id": _col(row, "lote_id"),
            "restaurante_key": _col(row, "restaurante_key"),
            "restaurante_nome": _col(row, "restaurante_nome"),
            "criado_em": _col(row, "criado_em"),
            "datas": _col(row, "datas"),
            "mes_ano": _col(row, "mes_ano"),
            "dias": dias.split(",") if dias else [],
            "total_alunos": int(_col(row, "total_alunos") or 0),
            "total_paginas": int(_col(row, "total_paginas") or 0),
            "origem": _col(row, "origem"),
            "avisos": [],
            "notas": [],
            "processado": _col(row, "processado") == "1",
            "processamentos": [],
            "tem_pdf": False,
            "local": False,
        })
    return itens


def marcar_processado(lote_id, sincronizado_sheets) -> bool:
    config = _config()
    if not _spreadsheet_id(config):
        return False

    aba = _obter_aba(config)
    celula = gs._com_retry(lambda: aba.find(lote_id, in_column=1))
    if not celula:
        return False

    gs._com_retry(lambda: aba.update_cell(
        celula.row, _COL["processado"] + 1,
        "1" if sincronizado_sheets else "0",
    ))
    return True
