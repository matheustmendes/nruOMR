"""
dashboard.py

Dashboard de presenças irregulares do NRUOMR.

Lê diretamente das planilhas Google Sheets de cada restaurante e identifica
bolsistas com menos de 10 presenças mensais. Inclui módulo de justificativas
via respostas de formulário Google.

Uso:
    python dashboard.py
    (abre automaticamente em http://localhost:5001)
"""

import json
import os
import re
import sys
import time
import traceback
import webbrowser
import threading
from datetime import datetime
from io import BytesIO
from collections import defaultdict

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_file, render_template

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIMITE_IRREGULAR = 10  # < 10 presenças mensais = irregular

_NOMES_DIAS = {
    "Seg", "Ter", "Qua", "Qui", "Sex", "Sab",           # abreviados
    "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado",  # por extenso
}

# Mapeia nome por extenso → abreviação para exibição compacta
_DIA_ABREV = {
    "Segunda": "Seg", "Terça": "Ter", "Quarta": "Qua",
    "Quinta": "Qui", "Sexta": "Sex", "Sábado": "Sáb",
}

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

_UNIDADE_LEGIVEL = {
    "canela": "Canela",
    "ondina": "Ondina",
    "sao_lazaro": "São Lázaro",
}

app = Flask(__name__, template_folder=os.path.join(SCRIPT_DIR, "templates"))


# ─── Config & autenticação ────────────────────────────────────────────────────

def _config():
    p = os.path.join(SCRIPT_DIR, "config_sheets.yaml")
    if not os.path.exists(p):
        raise FileNotFoundError("config_sheets.yaml não encontrado.")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _cliente(cfg):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise ImportError("Execute: pip install gspread google-auth")

    creds_path = cfg.get("credentials", "credentials_sheets.json")
    if not os.path.isabs(creds_path):
        creds_path = os.path.join(SCRIPT_DIR, creds_path)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Credenciais não encontradas: {creds_path}")

    creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
    return gspread.authorize(creds)


# ─── Cache simples em memória ─────────────────────────────────────────────────

_cache: dict = {}
_CACHE_TTL = 300  # 5 minutos


# ─── Gerenciamento dinâmico de formulários ────────────────────────────────────

def _formularios_path():
    return os.path.join(SCRIPT_DIR, "formularios.json")


def _extrair_spreadsheet_id(url_ou_id: str):
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url_ou_id)
    if m:
        return m.group(1)
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', url_ou_id.strip()):
        return url_ou_id.strip()
    return None


def _ler_formularios() -> list:
    p = _formularios_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("formularios", [])
    except Exception:
        return []


def _salvar_formularios(lista: list):
    with open(_formularios_path(), "w", encoding="utf-8") as f:
        json.dump({"formularios": lista}, f, ensure_ascii=False, indent=2)


def _get_all_form_ids() -> list:
    """Agrega IDs do config estático + formularios.json, sem duplicatas."""
    ids: list = []
    try:
        cfg = _config()
        form_cfg = cfg.get("formulario_justificativas", {})
        static_id = form_cfg.get("spreadsheet_id", "").strip()
        static_ids = form_cfg.get("spreadsheet_ids", [])
        if static_id:
            ids.append(static_id)
        for sid in static_ids:
            if sid not in ids:
                ids.append(sid)
    except Exception:
        pass
    for f in _ler_formularios():
        fid = f.get("id", "").strip()
        if fid and fid not in ids:
            ids.append(fid)
    return ids


def _cached(key, fn, forcar=False):
    agora = time.time()
    if not forcar and key in _cache:
        val, ts = _cache[key]
        if (agora - ts) < _CACHE_TTL:
            return val
    val = fn()
    _cache[key] = (val, agora)
    return val


# ─── Leitura das planilhas de presença ───────────────────────────────────────

def _limpar_matricula(v):
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


_PREFIXO_PERIODO = "Período:"
_COL_FIXAS = 3  # Nº, Nome, Matrícula


def _parse_aba(rows):
    """
    Parseia as linhas de uma aba mensal (ex: 'Maio 2026') de um restaurante.

    Aceita os dois layouts:
      - horizontal (atual): períodos lado a lado, um grupo de colunas cada
      - vertical (legado):  períodos empilhados em blocos

    Retorna lista de {nome, matricula, presencas_semana, periodo, dias}.
    """
    if _eh_horizontal(rows):
        return _parse_aba_horizontal(rows)
    return _parse_aba_vertical(rows)


def _eh_horizontal(rows):
    """Layout horizontal: marcador de período na linha 1, à direita das colunas fixas."""
    if not rows:
        return False
    primeira = [str(c).strip() for c in rows[0]]
    return any(c.startswith(_PREFIXO_PERIODO) for c in primeira[_COL_FIXAS:])


def _parse_aba_horizontal(rows):
    """
    Layout horizontal:
        Linha 1:  (vazio A:C)       | Período: 05/05 a 09/05      | Período: 12/05 ...
        Linha 2:  Nº | Nome | Matr. | Presenças | Seg | ... | Sex | Presenças | ...
        Linha 3+: uma linha por aluno, permanente no mês

    Gera um registro por (aluno, período) — mesma forma do parser vertical.
    """
    if len(rows) < 3:
        return []

    row1 = [str(c).strip() for c in rows[0]]
    row2 = [str(c).strip() for c in rows[1]]

    inicios = [
        j for j, v in enumerate(row1)
        if j >= _COL_FIXAS and v.startswith(_PREFIXO_PERIODO)
    ]

    grupos = []  # (periodo, col_presencas, [(dia, col)])
    for k, col in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else max(len(row1), len(row2))
        periodo = row1[col].replace(_PREFIXO_PERIODO, "").strip()
        col_dias = [
            (row2[c], c)
            for c in range(col + 1, min(fim, len(row2)))
            if row2[c] in _NOMES_DIAS
        ]
        grupos.append((periodo, col, col_dias))

    resultado = []
    for row in rows[2:]:
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue

        nome      = cells[1] if len(cells) > 1 else ""
        matricula = _limpar_matricula(cells[2]) if len(cells) > 2 else ""
        if not nome and not matricula:
            continue

        semanas_do_aluno = 0
        for periodo, col_pres, col_dias in grupos:
            try:
                presencas = (
                    int(cells[col_pres])
                    if col_pres < len(cells) and cells[col_pres] else 0
                )
            except ValueError:
                presencas = 0

            dias = {
                _DIA_ABREV.get(dia, dia): (cells[idx] if idx < len(cells) else "")
                for dia, idx in col_dias
            }

            # Semana sem célula preenchida: o aluno não foi lido nesse período
            if (col_pres >= len(cells) or not cells[col_pres]) and not any(dias.values()):
                continue

            semanas_do_aluno += 1
            resultado.append({
                "nome":             nome,
                "matricula":        matricula,
                "presencas_semana": presencas,
                "periodo":          periodo,
                "dias":             dias,
            })

        # Aluno sem nenhuma semana lida ainda assim conta no mês (0 presenças)
        if not semanas_do_aluno:
            resultado.append({
                "nome":             nome,
                "matricula":        matricula,
                "presencas_semana": 0,
                "periodo":          "",
                "dias":             {},
            })

    return resultado


def _parse_aba_vertical(rows):
    """
    Layout legado (blocos empilhados):
        Período: 05/05 a 09/05        ← cabeçalho de semana (merged)
        Nº | Nome | Matrícula | Presenças | Seg | ...
        1  | João  | 123456   | 3         | AJ  | ...
        (linha vazia)
    """
    resultado = []
    periodo = ""
    col_dias: list = []  # [(nome_dia, col_index), ...]

    for row in rows:
        cells = [str(c).strip() for c in row]

        if not any(cells):
            continue

        if cells[0].startswith("Período:"):
            periodo = cells[0].replace("Período:", "").strip()
            continue

        # Linha de cabeçalho — detecta posição de cada dia
        if cells[0] in ("Nº", "N°", "Nº"):
            col_dias = [
                (cells[j], j)
                for j in range(len(cells))
                if cells[j] in _NOMES_DIAS
            ]
            continue

        # Linha de dado: primeira célula é um número
        try:
            num = int(cells[0])
        except ValueError:
            continue
        if num <= 0:
            continue

        nome = cells[1] if len(cells) > 1 else ""
        matricula = _limpar_matricula(cells[2]) if len(cells) > 2 else ""

        try:
            presencas = int(cells[3]) if len(cells) > 3 and cells[3] else 0
        except ValueError:
            presencas = 0

        if not nome and not matricula:
            continue

        dias = {
            _DIA_ABREV.get(dia, dia): (cells[idx] if idx < len(cells) else "")
            for dia, idx in col_dias
        }

        resultado.append({
            "nome": nome,
            "matricula": matricula,
            "presencas_semana": presencas,
            "periodo": periodo,
            "dias": dias,
        })

    return resultado


def _ler_aba_restaurante(cliente, spreadsheet_id, nome_aba):
    try:
        import gspread
        sh = cliente.open_by_key(spreadsheet_id)
        aba = sh.worksheet(nome_aba)
        return _parse_aba(aba.get_all_values())
    except Exception as e:
        print(f"[dashboard] Erro ao ler aba '{nome_aba}' ({spreadsheet_id[:8]}...): {e}")
        return []


def _carregar_dados_mes(mes_str, unidade_filter="", forcar=False):
    """
    Lê e agrega presenças mensais de todos os restaurantes.

    Returns: lista de {matricula, nome, unidade, mes, presencas_mes, irregular}
    """
    def _load():
        cfg = _config()
        cl = _cliente(cfg)
        restaurantes_cfg = cfg.get("restaurantes", {})

        todos = []
        for key in ("canela", "ondina", "sao_lazaro"):
            if unidade_filter and key != unidade_filter:
                continue
            sid = restaurantes_cfg.get(key, {}).get("spreadsheet_id", "").strip()
            if not sid:
                continue
            linhas = _ler_aba_restaurante(cl, sid, mes_str)
            for linha in linhas:
                linha["unidade"] = key
            todos.extend(linhas)

        # Agrega: soma presencas_semana por (matricula, unidade); acumula semanas
        agg: dict = defaultdict(lambda: {
            "presencas_mes": 0, "nome": "", "unidade": "", "semanas": []
        })
        for linha in todos:
            chave = (linha["matricula"], linha["unidade"])
            agg[chave]["presencas_mes"] += linha["presencas_semana"]
            agg[chave]["nome"] = linha["nome"]
            agg[chave]["unidade"] = linha["unidade"]
            if linha.get("periodo"):
                agg[chave]["semanas"].append({
                    "periodo":   linha["periodo"],
                    "presencas": linha["presencas_semana"],
                    "dias":      linha.get("dias", {}),
                })

        resultado = []
        for (mat, unid), dados in agg.items():
            total = dados["presencas_mes"]
            resultado.append({
                "matricula":    mat,
                "nome":         dados["nome"],
                "unidade":      unid,
                "mes":          mes_str,
                "presencas_mes": total,
                "irregular":    total < LIMITE_IRREGULAR,
                "semanas":      dados["semanas"],
            })

        return resultado

    cache_key = f"dados_{mes_str}_{unidade_filter}"
    return _cached(cache_key, _load, forcar)


# ─── Justificativas (Google Forms) ────────────────────────────────────────────

# Status possíveis para um aluno irregular:
#   "irregular"  — sem entrada no formulário
#   "pendente"   — submeteu justificativa, aguardando parecer
#   "justificado"— Contabilizado = Sim (ou parecer positivo)
#   "negado"     — Contabilizado = Não / Parecer negado

def _carregar_justificativas(forcar=False):
    """
    Lê respostas do formulário de justificativas (Google Forms → Google Sheets).

    Estrutura esperada das colunas (configurável em config_sheets.yaml):
        A  Carimbo de data/hora
        B  Nome completo
        C  Matrícula
        D  Local de Refeição
        E  Datas de ausência
        F  Comprovante de Ausência
        G  Parecer
        I  Comentário (Se negado )
    """
    
    def _load():
        cfg = _config()
        form_cfg = cfg.get("formulario_justificativas", {})
        all_ids = _get_all_form_ids()

        if not all_ids:
            return []

        try:
            cl = _cliente(cfg)
        except Exception as e:
            print(f"[dashboard] Erro ao autenticar para justificativas: {e}")
            return []

        c_ts    = form_cfg.get("coluna_timestamp",    "Carimbo de data/hora")
        c_nome  = form_cfg.get("coluna_nome",         "Nome completo")
        c_mat   = form_cfg.get("coluna_matricula",    "Matrícula")
        c_local = form_cfg.get("coluna_local",        "Local de Refeição")
        c_datas = form_cfg.get("coluna_datas",        "Datas de ausência")
        c_comp  = form_cfg.get("coluna_comprovante",  "Comprovante de Ausência")
        c_par   = form_cfg.get("coluna_parecer",      "Parecer")
        c_comt  = form_cfg.get("coluna_comentario",   "Comentário (Se negado )")
        
        def _normalizar_links(valor: str) -> list[str]:
            if not valor:
                    return []
                # Aceita separador por vírgula OU quebra de linha (cobre os dois formatos)
            partes = re.split(r'[,\n\r]+', valor)
            return [p.strip() for p in partes if p.strip()]
    
    
        def _get(row_norm, key):
            key_norm = key.strip().lower()
            for k, v in row_norm.items():
                if k.strip().lower() == key_norm:
                    return str(v).strip()
            return ""

        result = []
        for form_id in all_ids:
            try:
                sh = cl.open_by_key(form_id)
                aba = sh.get_worksheet(0)
                rows = aba.get_all_records()
            except Exception as e:
                print(f"[dashboard] Erro ao ler formulário {form_id[:8]}...: {e}")
                continue
            for row in rows:
                result.append({
                    "timestamp":     _get(row, c_ts),
                    "nome":          _get(row, c_nome),
                    "matricula":     _limpar_matricula(_get(row, c_mat)),
                    "local":         _get(row, c_local),
                    "datas":         _get(row, c_datas),
                    "comprovante":   _normalizar_links(_get(row, c_comp)),
                    "parecer":       _get(row, c_par),
                    "comentario":    _get(row, c_comt),
                })
        return result

    return _cached("justificativas", _load, forcar)


def _status_justificativa(j):
    """
    Determina o status com base nas colunas Contabilizado e Parecer.
    """
    par  = j.get("parecer", "").strip().lower()

    if "sim" in par or any(w in par for w in ("aprovad", "aceit", "deferido")):
        return "justificado"
    if "não" in par or "nao" in par or any(w in par for w in ("negad", "recusad", "indeferido")):
        return "negado"
    # Tem entrada mas nenhuma decisão ainda
    return "pendente"


def _cruzar(alunos, justificativas, mes):
    """
    Adiciona campos de justificativa a cada aluno irregular.
    Cruza por matrícula (primário) ou nome completo normalizado (fallback).
    Filtra por mês quando as datas de ausência informadas estão preenchidas.
    """
    by_mat:  dict = defaultdict(list)
    by_nome: dict = defaultdict(list)
    mes_nome = mes.lower().split()[0] if mes else ""  # "maio", "junho"…

    for j in justificativas:
        # Não filtra por mês: o campo "Datas de ausência" é texto livre do aluno,
        # não um campo de data estruturado. Aceita todas as respostas e cruza só por identidade.
        if j["matricula"]:
            by_mat[j["matricula"]].append(j)
        if j["nome"]:
            by_nome[j["nome"].lower()].append(j)

    _vazio = {
        "status_just": "irregular",
        "justificado": False,
        "datas":        "",
        "comprovante":  [],
        "parecer":      "",
        "comentario":   "",
    }

    for aluno in alunos:
        mat  = aluno.get("matricula", "")
        nome = aluno.get("nome", "").lower()

        candidatos = by_mat.get(mat) or by_nome.get(nome) or []

        if not candidatos:
            aluno.update(_vazio)
            continue

        # Mais recente primeiro; preferir os já com decisão
        candidatos_ord = sorted(
            candidatos,
            key=lambda x:  (x["parecer"] != "", x["timestamp"]),
            reverse=True,
        )
        melhor = candidatos_ord[0]
        status = _status_justificativa(melhor)

        aluno["status_just"]    = status
        aluno["justificado"]    = status == "justificado"
        aluno["datas"]          = melhor["datas"]
        aluno["comprovante"]    = melhor["comprovante"]
        aluno["parecer"]        = melhor["parecer"]
        aluno["comentario"]     = melhor["comentario"]

    return alunos


# ─── Exportação XLSX ──────────────────────────────────────────────────────────

_COR_STATUS = {
    "justificado": "E8F5E9",
    "pendente":    "FFFDE7",
    "negado":      "FFF0E0",
    "irregular":   "FFE8E8",
}

_LABEL_STATUS = {
    "justificado": "Justificado",
    "pendente":    "Pendente",
    "negado":      "Negado",
    "irregular":   "Irregular",
}


def _exportar_xlsx(irregulares, mes):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Irregulares"

    thin  = Side(style="thin", color="AAAAAA")
    borda = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Título
    n_cols = 10
    ws.merge_cells(f"A1:{get_column_letter(n_cols)}1")
    c = ws["A1"]
    c.value = f"Presencas Irregulares - {mes}"
    c.font = Font(bold=True, size=13, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="1E6BA5")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    # Subtítulo
    ws.merge_cells(f"A2:{get_column_letter(n_cols)}2")
    c = ws["A2"]
    c.value = f"Criterio: menos de {LIMITE_IRREGULAR} presencas no mes"
    c.font = Font(italic=True, size=9, color="555555")
    c.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 16

    # Cabeçalho
    cabecalhos = [
        "Nr", "Matricula", "Nome", "Unidade",
        "Presencas no Mes", "Datas de Ausencia",
        "Comprovante", "Parecer", "Status",
    ]
    for col, h in enumerate(cabecalhos, 1):
        c = ws.cell(row=3, column=col, value=h)
        c.font = Font(bold=True, size=10)
        c.fill = PatternFill("solid", fgColor="D6E8F7")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = borda
    ws.row_dimensions[3].height = 20

    ordenados = sorted(
        irregulares, key=lambda x: (x.get("unidade", ""), x.get("nome", ""))
    )

    for i, aluno in enumerate(ordenados, 1):
        row = 3 + i
        st  = aluno.get("status_just", "irregular")
        cor = _COR_STATUS.get(st, "FFE8E8")
        comps = aluno.get("comprovante", [])
        if isinstance(comps, list):
            comps_str = ", ".join(comps)

        valores = [
            i,
            aluno.get("matricula", ""),
            aluno.get("nome", ""),
            _UNIDADE_LEGIVEL.get(aluno.get("unidade", ""), aluno.get("unidade", "")),
            aluno.get("presencas_mes", 0),
            aluno.get("datas", ""),
            comps_str,
            aluno.get("parecer", ""),
            _LABEL_STATUS.get(st, st.capitalize()),
        ]
        colunas_esquerda = {3, 6, 7}
        for col, val in enumerate(valores, 1):
            c = ws.cell(row=row, column=col, value=val)
            c.alignment = Alignment(
                horizontal="left" if col in colunas_esquerda else "center",
                vertical="center",
                wrap_text=True,
            )
            c.border = borda
            c.fill = PatternFill("solid", fgColor=cor)
        ws.row_dimensions[row].height = 20

    # Larguras das colunas (A→J)
    for col, w in zip(range(1, n_cols + 1), [5, 14, 35, 12, 14, 28, 35, 14, 14, 14]):
        ws.column_dimensions[get_column_letter(col)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ─── Rotas ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html", limite=LIMITE_IRREGULAR)


@app.route("/api/dados")
def api_dados():
    mes     = request.args.get("mes", "").strip()
    unidade = request.args.get("unidade", "").strip()

    if not mes:
        return jsonify({"ok": False, "erro": "Parâmetro 'mes' é obrigatório"}), 400

    try:
        dados       = _carregar_dados_mes(mes, unidade)
        irregulares = [a for a in dados if a.get("irregular")]

        justificativas = _carregar_justificativas()
        irregulares    = _cruzar(irregulares, justificativas, mes)
        irregulares.sort(key=lambda x: (x.get("unidade", ""), x.get("nome", "")))

        por_status = defaultdict(int)
        for a in irregulares:
            por_status[a.get("status_just", "irregular")] += 1

        return jsonify({
            "ok": True,
            "stats": {
                "total_alunos":      len(dados),
                "total_irregulares": len(irregulares),
                "justificados":      por_status["justificado"],
                "pendentes":         por_status["pendente"],
                "negados":           por_status["negado"],
                "sem_justificativa": por_status["irregular"],
            },
            "irregulares": irregulares,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/exportar")
def exportar():
    mes = request.args.get("mes", "").strip()
    unidade = request.args.get("unidade", "").strip()

    if not mes:
        return jsonify({"erro": "Parâmetro 'mes' é obrigatório"}), 400

    try:
        dados = _carregar_dados_mes(mes, unidade)
        irregulares = [a for a in dados if a.get("irregular")]
        justificativas = _carregar_justificativas()
        irregulares = _cruzar(irregulares, justificativas, mes)

        buf = _exportar_xlsx(irregulares, mes)
        nome_arquivo = f"irregulares_{mes.replace(' ', '_')}.xlsx"
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=nome_arquivo,
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"erro": str(e)}), 500


@app.route("/api/recarregar", methods=["POST"])
def recarregar():
    _cache.clear()
    return jsonify({"ok": True})


# ─── Rotas de gerenciamento de formulários ────────────────────────────────────

@app.route("/api/config/formularios", methods=["GET"])
def listar_formularios():
    return jsonify({"ok": True, "formularios": _ler_formularios()})


@app.route("/api/config/formularios", methods=["POST"])
def adicionar_formulario():
    body   = request.get_json(silent=True) or {}
    url    = (body.get("url") or "").strip()
    label  = (body.get("label") or "").strip()

    form_id = _extrair_spreadsheet_id(url)
    if not form_id:
        return jsonify({"ok": False, "erro": "URL ou ID inválido."}), 400

    lista = _ler_formularios()
    if any(f["id"] == form_id for f in lista):
        return jsonify({"ok": False, "erro": "Este formulário já está na lista."}), 409

    lista.append({
        "id":    form_id,
        "label": label or f"Adicionado em {datetime.now().strftime('%d/%m/%Y')}",
    })
    _salvar_formularios(lista)
    _cache.pop("justificativas", None)
    return jsonify({"ok": True, "id": form_id})


@app.route("/api/config/formularios/<form_id>", methods=["DELETE"])
def remover_formulario(form_id):
    lista = _ler_formularios()
    nova  = [f for f in lista if f["id"] != form_id]
    if len(nova) == len(lista):
        return jsonify({"ok": False, "erro": "ID não encontrado."}), 404
    _salvar_formularios(nova)
    _cache.pop("justificativas", None)
    return jsonify({"ok": True})


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 5001
    url = f"http://localhost:{port}"

    def _abrir():
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_abrir, daemon=True).start()
    print(f"\nDashboard de Presencas: {url}\n")
    app.run(port=port, debug=False)
