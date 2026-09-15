"""
lote.py

Snapshot de lote de impressão — a memória do sistema sobre "quem estava em
qual linha de qual página" no momento exato em que o PDF foi gerado.

Motivo de existir
-----------------
Até aqui a identidade de cada linha lida no scan era resolvida indexando a
planilha de referência *no momento do processamento* (`alunos[numero - 1]`).
Como essa planilha muda de uma semana para outra (inclusões, exclusões,
reordenações), processar um scan antigo com a planilha atual desloca todo
mundo e atribui presenças à pessoa errada — silenciosamente.

O lote resolve isso congelando, junto com o PDF:
  - o roster na ordem impressa (número, página, linha, nome, matrícula);
  - a geometria completa do formulário (o mesmo dict do config_*.yaml).

A geometria vai junto porque `configs/config_<restaurante>.yaml` é sobrescrito
a cada nova geração de template: um lote impresso com 6 dias e reprocessado
depois de gerar um template de 5 dias leria as bolhas nas coordenadas erradas.

Estrutura em disco
------------------
    lotes/<lote_id>.json          lote ativo (ainda não confirmado no Sheets)
    lotes/<lote_id>.pdf           o PDF exatamente como foi impresso
    lotes/processados/<id>.json   histórico auditável (não é apagado no ato)

A limpeza definitiva é uma rotina separada e deliberada (`limpar_antigos`),
nunca um efeito colateral do processamento.
"""

import os
import json
import glob
import re
import unicodedata
from datetime import datetime, timedelta

import lote_sync

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOTES_DIR = os.path.join(SCRIPT_DIR, "lotes")
PROCESSADOS_DIR = os.path.join(LOTES_DIR, "processados")

VERSAO_LOTE = 1

# Origens possíveis de um lote — usadas para sinalizar confiabilidade na UI.
ORIGEM_GERACAO = "geracao"           # criado junto com o PDF: confiança total
ORIGEM_PDF = "recuperado_pdf"        # extraído da camada de texto do PDF impresso
ORIGEM_OCR = "recuperado_ocr"        # reconstruído por OCR do próprio scan
ORIGEM_XLSX = "legado_xlsx"          # planilha informada à mão no processamento


# --- Normalização ----------------------------------------------------------

def norm_mat(valor) -> str:
    """
    Matrícula sempre como texto, sem zeros à esquerda perdidos por conversão
    numérica e sem validação de tamanho: 8, 9 e 10 dígitos são todos legítimos
    (graduação, pós e estrangeiros).
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return re.sub(r"\D", "", str(valor).strip())


def norm_nome(valor) -> str:
    """Nome sem acento, sem caixa e sem espaço duplicado — só para comparação."""
    if not valor:
        return ""
    txt = unicodedata.normalize("NFKD", str(valor))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", txt).strip().upper()


# --- Validação do roster antes de imprimir ---------------------------------

def validar_roster(alunos) -> dict:
    """
    Checa problemas no roster antes de gerar o PDF.

    Não levanta exceção: um lote com matrícula duplicada continua imprimível e
    continua rastreável (a identidade do lote é a linha impressa, não a
    matrícula). Bloquear a impressão por um erro de digitação na planilha de
    origem pararia a operação sem necessidade — o certo é avisar alto.

    Args:
        alunos: lista de (nome, matricula)

    Matrícula em branco é situação normal e não entra em `avisos` — só fica
    registrada em `sem_matricula` e em `notas`, porque tem uma consequência
    prática: essas linhas não podem ser conferidas por matrícula depois, só
    por nome e posição.

    Returns:
        {"duplicadas": [...], "sem_matricula": [...], "nomes_repetidos": [...],
         "avisos": ["problema a corrigir", ...], "notas": ["informação", ...]}
    """
    vistos_mat, vistos_nome = {}, {}
    duplicadas, sem_matricula, nomes_repetidos = [], [], []

    for i, (nome, matricula) in enumerate(alunos):
        numero = i + 1
        mat = norm_mat(matricula)

        if not mat:
            sem_matricula.append({"numero": numero, "nome": nome})
        elif mat in vistos_mat:
            duplicadas.append({
                "matricula": mat,
                "numeros": [vistos_mat[mat], numero],
                "nomes": [alunos[vistos_mat[mat] - 1][0], nome],
            })
        else:
            vistos_mat[mat] = numero

        chave_nome = norm_nome(nome)
        if chave_nome and chave_nome in vistos_nome:
            nomes_repetidos.append({
                "nome": nome,
                "numeros": [vistos_nome[chave_nome], numero],
            })
        elif chave_nome:
            vistos_nome[chave_nome] = numero

    avisos, notas = [], []
    for d in duplicadas:
        avisos.append(
            f"Matrícula {d['matricula']} aparece nas linhas "
            f"{d['numeros'][0]} e {d['numeros'][1]} ({' / '.join(d['nomes'])})."
        )
    for n in nomes_repetidos:
        avisos.append(
            f"Nome repetido em linhas {n['numeros'][0]} e {n['numeros'][1]}: {n['nome']}."
        )
    if sem_matricula:
        notas.append(
            f"{len(sem_matricula)} pessoa(s) sem matrícula — normal; essas "
            "linhas só podem ser conferidas por nome e posição."
        )

    return {
        "duplicadas": duplicadas,
        "sem_matricula": sem_matricula,
        "nomes_repetidos": nomes_repetidos,
        "avisos": avisos,
        "notas": notas,
    }


# --- Criação e persistência ------------------------------------------------

def novo_lote_id(restaurante_key: str, quando: datetime = None) -> str:
    """
    Identificador legível, ordenável e curto o bastante para caber impresso no
    rodapé da folha — é ele que amarra o papel de volta ao snapshot.
    """
    quando = quando or datetime.now()
    return f"{restaurante_key}_{quando.strftime('%Y%m%d-%H%M%S')}"


def montar_lote(lote_id, restaurante_key, restaurante_nome, aba, dias,
                paginacao, config, info, origem=ORIGEM_GERACAO, avisos=None,
                notas=None):
    """
    Monta o dict do lote.

    Args:
        paginacao: lista de dicts vinda de gerar_template.gerar_template(),
            um por aluno impresso: numero, pagina, linha_pagina, nome,
            nome_impresso, matricula.
        config: dict de geometria (idêntico ao gravado em config_*.yaml).
        info: dict de cabeçalho do template (restaurante, mes_ano, datas).
    """
    alunos = [
        {
            "numero": p["numero"],
            "pagina": p["pagina"],
            "linha_pagina": p["linha_pagina"],
            "nome": p["nome"],
            "nome_impresso": p.get("nome_impresso", p["nome"]),
            "matricula": norm_mat(p.get("matricula")),
        }
        for p in paginacao
    ]

    return {
        "versao": VERSAO_LOTE,
        "lote_id": lote_id,
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "origem": origem,
        "restaurante_key": restaurante_key,
        "restaurante_nome": restaurante_nome,
        "aba": aba,
        "mes_ano": (info or {}).get("mes_ano", ""),
        "datas": (info or {}).get("datas", ""),
        "dias": list(dias),
        "alunos_por_pagina": config["layout"]["alunos_por_pagina"],
        "total_paginas": max((a["pagina"] for a in alunos), default=0),
        "total_alunos": len(alunos),
        "config": config,
        "avisos": list(avisos or []),
        "notas": list(notas or []),
        "alunos": alunos,
        "processamentos": [],
    }


def salvar_lote(lote: dict) -> str:
    os.makedirs(LOTES_DIR, exist_ok=True)
    caminho = os.path.join(LOTES_DIR, f"{lote['lote_id']}.json")
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(lote, f, ensure_ascii=False, indent=2)

    _sincronizar(lote, caminho)

    return caminho


def _sincronizar(lote: dict, caminho_json: str):
    """
    Envia o lote para a planilha compartilhada (Sheets), se configurado —
    ver SINCRONIZACAO_LOTES.md. PDF não sincroniza por esse caminho (não
    cabe bem numa célula); só roster/geometria, que é o que resolve o bug
    de identidade entre máquinas.

    Best-effort de propósito: uma máquina sem internet no momento continua
    gerando e salvando localmente sem travar; a falha só fica registrada no
    log. Enquanto não sincronizar, o lote só existe nesta máquina — mesma
    situação de hoje.
    """
    try:
        if not lote_sync.disponivel():
            return
        lote_sync.enviar(
            lote["lote_id"],
            caminho_json,
            propriedades={
                "restaurante_key": lote.get("restaurante_key", ""),
                "restaurante_nome": lote.get("restaurante_nome", ""),
                "datas": lote.get("datas", ""),
                "mes_ano": lote.get("mes_ano", ""),
                "dias": ",".join(lote.get("dias", [])),
                "total_alunos": lote.get("total_alunos", 0),
                "total_paginas": lote.get("total_paginas", 0),
                "origem": lote.get("origem", ORIGEM_GERACAO),
                "criado_em": lote.get("criado_em", ""),
                "processado": "0",
            },
        )
    except Exception as e:
        print(f"  AVISO: não foi possível sincronizar o lote "
              f"'{lote['lote_id']}': {e}")


def caminho_pdf(lote_id: str) -> str:
    return os.path.join(LOTES_DIR, f"{lote_id}.pdf")


def garantir_pdf_local(lote_id: str):
    """
    Devolve o caminho do PDF se ele existir localmente. PDF não sincroniza
    entre máquinas (ver `_sincronizar`) — só existe na máquina onde o lote
    foi gerado; reimprimir de outra máquina não é possível hoje.

    Returns:
        O caminho local se o PDF existir, ou None caso contrário.
    """
    caminho = caminho_pdf(lote_id)
    return caminho if os.path.isfile(caminho) else None


def carregar_lote(lote_id: str) -> dict:
    """
    Procura primeiro entre os ativos, depois no histórico de processados e,
    por fim, na planilha compartilhada (Sheets) — o caso do lote gerado em
    outra máquina (ver SINCRONIZACAO_LOTES.md). Quando vem de lá, grava uma
    cópia local em LOTES_DIR antes de devolver, para que o resto do ciclo de
    vida (registrar_processamento etc.) funcione como se o lote sempre
    tivesse existido aqui.
    """
    for pasta in (LOTES_DIR, PROCESSADOS_DIR):
        caminho = os.path.join(pasta, f"{lote_id}.json")
        if os.path.isfile(caminho):
            with open(caminho, "r", encoding="utf-8") as f:
                return json.load(f)

    remoto = None
    try:
        if lote_sync.disponivel():
            remoto = lote_sync.baixar_json(lote_id)
    except Exception as e:
        print(f"  AVISO: falha ao buscar o lote '{lote_id}' na planilha compartilhada: {e}")

    if remoto is not None:
        os.makedirs(LOTES_DIR, exist_ok=True)
        with open(os.path.join(LOTES_DIR, f"{lote_id}.json"), "w", encoding="utf-8") as f:
            json.dump(remoto, f, ensure_ascii=False, indent=2)
        return remoto

    raise FileNotFoundError(
        f"Lote '{lote_id}' não encontrado em {LOTES_DIR}, {PROCESSADOS_DIR} "
        "nem na planilha compartilhada."
    )


def status_sincronizacao() -> dict:
    """Diagnóstico da sincronização entre máquinas, pra expor na UI (rota
    `/lotes`) — ver SINCRONIZACAO_LOTES.md. Mesmo padrão best-effort do
    resto deste módulo: uma falha aqui nunca derruba a listagem de lotes,
    só faz a UI mostrar "não sei dizer" em vez do motivo real."""
    try:
        return lote_sync.status()
    except Exception as e:
        return {"ok": False, "motivo": f"Falha ao checar sincronização: {e}"}


def listar_lotes(restaurante_key=None, incluir_processados=True, limite=None) -> list:
    """
    Lista os lotes conhecidos, mais recentes primeiro.

    Returns:
        Lista de dicts resumidos (sem o roster completo), com "processado".
    """
    itens = []
    vistos = set()
    pastas = [(LOTES_DIR, False)]
    if incluir_processados:
        pastas.append((PROCESSADOS_DIR, True))

    for pasta, processado in pastas:
        for caminho in glob.glob(os.path.join(pasta, "*.json")):
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    lote = json.load(f)
            except Exception:
                continue
            if restaurante_key and lote.get("restaurante_key") != restaurante_key:
                continue
            lote_id = lote.get("lote_id", "")
            vistos.add(lote_id)
            itens.append({
                "lote_id": lote_id,
                "restaurante_key": lote.get("restaurante_key", ""),
                "restaurante_nome": lote.get("restaurante_nome", ""),
                "criado_em": lote.get("criado_em", ""),
                "datas": lote.get("datas", ""),
                "mes_ano": lote.get("mes_ano", ""),
                "dias": lote.get("dias", []),
                "total_alunos": lote.get("total_alunos", 0),
                "total_paginas": lote.get("total_paginas", 0),
                "origem": lote.get("origem", ORIGEM_GERACAO),
                "avisos": lote.get("avisos", []),
                "notas": lote.get("notas", []),
                "processado": processado,
                "processamentos": lote.get("processamentos", []),
                "tem_pdf": os.path.isfile(caminho_pdf(lote_id)),
                "local": True,
            })

    # Lotes que só existem na planilha compartilhada — gerados em outra
    # máquina, ainda não baixados nesta (ver SINCRONIZACAO_LOTES.md). O
    # local, quando existe, é sempre a versão que vale: só ele sabe se já
    # foi processado *nesta* máquina antes de isso voltar pra planilha.
    try:
        if lote_sync.disponivel():
            for remoto in lote_sync.listar(restaurante_key):
                if remoto["lote_id"] in vistos:
                    continue
                if not incluir_processados and remoto.get("processado"):
                    continue
                itens.append(remoto)
    except Exception as e:
        print(f"  AVISO: falha ao listar lotes da planilha compartilhada: {e}")

    itens.sort(key=lambda x: x["criado_em"], reverse=True)
    return itens[:limite] if limite else itens


# --- Roster na ordem impressa ---------------------------------------------

def roster_do_lote(lote: dict, ate_numero: int = 0) -> list:
    """
    Converte o lote na lista `[(nome, matricula), ...]` indexada por
    `numero - 1`, que é o formato que exportar/revisar/google_sheets já
    consomem. Buracos (linha impressa sem registro) viram placeholders para
    que a posição nunca deslize.

    Args:
        ate_numero: garante que a lista tenha ao menos esse tamanho.
    """
    por_numero = {a["numero"]: a for a in lote.get("alunos", [])}
    maior = max([*por_numero.keys(), ate_numero], default=0)

    roster = []
    for numero in range(1, maior + 1):
        a = por_numero.get(numero)
        if a:
            roster.append((a["nome"], a["matricula"]))
        else:
            roster.append((f"[linha {numero} fora do lote]", ""))
    return roster


def aluno_do_lote(lote: dict, numero: int):
    for a in lote.get("alunos", []):
        if a["numero"] == numero:
            return a
    return None


# --- Divergência entre o lote e uma planilha atual -------------------------

def comparar_com_planilha(lote: dict, alunos_planilha) -> dict:
    """
    Diz o que mudou entre o roster impresso e a planilha de hoje.

    Serve para o operador entender por que o antigo fluxo errava — e para
    confirmar que a folha em mãos é mesmo desse lote.

    Args:
        alunos_planilha: lista de (nome, matricula) da planilha atual.

    Returns:
        {"removidos": [...], "novos": [...], "deslocados": [...],
         "iguais": int, "resumo": "texto"}
    """
    # Matrícula duplicada não identifica ninguém: com duas linhas iguais não há
    # como dizer qual virou qual. Elas saem da comparação e são reportadas à
    # parte, em vez de aparecerem como "mudou de linha" sem ter mudado.
    ambiguas = {m for m, n in _contar_matriculas(lote["alunos"]).items() if n > 1}
    ambiguas |= {
        m for m, n in _contar_matriculas(
            [{"matricula": norm_mat(mat)} for _, mat in alunos_planilha]
        ).items() if n > 1
    }

    lote_por_mat = {
        a["matricula"]: a for a in lote["alunos"]
        if a["matricula"] and a["matricula"] not in ambiguas
    }
    plan_por_mat = {}
    for i, (nome, mat) in enumerate(alunos_planilha):
        m = norm_mat(mat)
        if m and m not in ambiguas and m not in plan_por_mat:
            plan_por_mat[m] = {"numero": i + 1, "nome": nome, "matricula": m}

    removidos = [a for m, a in lote_por_mat.items() if m not in plan_por_mat]
    novos = [a for m, a in plan_por_mat.items() if m not in lote_por_mat]

    deslocados, iguais = [], 0
    for m, a in lote_por_mat.items():
        p = plan_por_mat.get(m)
        if not p:
            continue
        if p["numero"] != a["numero"]:
            deslocados.append({
                "matricula": m,
                "nome": a["nome"],
                "numero_lote": a["numero"],
                "numero_planilha": p["numero"],
            })
        else:
            iguais += 1

    partes = []
    if deslocados:
        partes.append(f"{len(deslocados)} pessoa(s) mudaram de linha")
    if removidos:
        partes.append(f"{len(removidos)} saíram da planilha")
    if novos:
        partes.append(f"{len(novos)} entraram depois da impressão")
    if ambiguas:
        partes.append(f"{len(ambiguas)} matrícula(s) duplicadas ficaram de fora da comparação")
    resumo = "; ".join(partes) if partes else "planilha idêntica ao lote impresso"

    return {
        "removidos": removidos,
        "novos": novos,
        "deslocados": deslocados,
        "ambiguas": sorted(ambiguas),
        "iguais": iguais,
        "resumo": resumo,
    }


def _contar_matriculas(registros):
    contagem = {}
    for r in registros:
        mat = r.get("matricula")
        if mat:
            contagem[mat] = contagem.get(mat, 0) + 1
    return contagem


# --- Ciclo de vida ---------------------------------------------------------

def registrar_processamento(lote_id, periodo, sincronizado_sheets,
                            detalhe="", arquivar=None):
    """
    Anota no lote que ele foi processado — e só o move para `processados/`
    quando a sincronização com o Sheets realmente confirmou.

    Um lote cujo envio falhou continua na pasta ativa de propósito: é ele que
    permite reprocessar sem reescanear a folha.

    Args:
        arquivar: força (True) ou impede (False) o arquivamento. Por padrão,
            arquiva se e somente se `sincronizado_sheets` for verdadeiro.
    """
    origem = os.path.join(LOTES_DIR, f"{lote_id}.json")
    destino_hist = os.path.join(PROCESSADOS_DIR, f"{lote_id}.json")

    caminho = origem if os.path.isfile(origem) else destino_hist
    if not os.path.isfile(caminho):
        return None

    with open(caminho, "r", encoding="utf-8") as f:
        lote = json.load(f)

    lote.setdefault("processamentos", []).append({
        "em": datetime.now().isoformat(timespec="seconds"),
        "periodo": periodo,
        "sincronizado_com_sheets": bool(sincronizado_sheets),
        "detalhe": detalhe,
    })

    if arquivar is None:
        arquivar = bool(sincronizado_sheets)

    if arquivar:
        os.makedirs(PROCESSADOS_DIR, exist_ok=True)
        with open(destino_hist, "w", encoding="utf-8") as f:
            json.dump(lote, f, ensure_ascii=False, indent=2)
        if caminho == origem and os.path.abspath(origem) != os.path.abspath(destino_hist):
            try:
                os.remove(origem)
            except OSError:
                pass
        resultado = destino_hist
    else:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(lote, f, ensure_ascii=False, indent=2)
        resultado = caminho

    # Marca o lote como processado na planilha compartilhada também — sem
    # isso, outras máquinas continuariam oferecendo-o como pendente na
    # próxima listagem (ver SINCRONIZACAO_LOTES.md). Best-effort de
    # propósito, mesmo motivo de `_sincronizar`.
    try:
        if lote_sync.disponivel():
            lote_sync.marcar_processado(lote_id, bool(sincronizado_sheets))
    except Exception as e:
        print(f"  AVISO: falha ao marcar o lote '{lote_id}' como "
              f"processado na planilha compartilhada: {e}")

    return resultado


def _candidatos_limpeza(dias_retencao):
    """
    Lotes arquivados, já sincronizados com o Sheets, cujo último
    processamento é mais antigo que `dias_retencao`.

    Returns:
        [(caminho_json, lote_dict, data_ultimo_processamento), ...]
    """
    limite = datetime.now() - timedelta(days=dias_retencao)
    candidatos = []

    for caminho in glob.glob(os.path.join(PROCESSADOS_DIR, "*.json")):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                lote = json.load(f)
        except Exception:
            continue

        procs = lote.get("processamentos", [])
        if not procs or not any(p.get("sincronizado_com_sheets") for p in procs):
            continue

        try:
            ultimo = max(datetime.fromisoformat(p["em"]) for p in procs)
        except Exception:
            continue

        if ultimo < limite:
            candidatos.append((caminho, lote, ultimo))

    return candidatos


def detalhar_candidatos_limpeza(dias_retencao=180) -> list:
    """
    Como `limpar_antigos`, mas devolve o suficiente pra mostrar uma lista de
    conferência antes de apagar (usado pela limpeza manual na UI).

    Returns:
        [{"lote_id", "restaurante", "periodo", "datas", "total_alunos",
          "ultimo_processamento"}], mais recente primeiro.
    """
    itens = [
        {
            "lote_id": lote["lote_id"],
            "restaurante": lote.get("restaurante_nome", lote.get("restaurante_key", "")),
            "periodo": max(
                (p.get("periodo", "") for p in lote.get("processamentos", [])),
                default="",
            ),
            "datas": lote.get("datas", ""),
            "total_alunos": lote.get("total_alunos", 0),
            "ultimo_processamento": ultimo.isoformat(timespec="seconds"),
        }
        for _, lote, ultimo in _candidatos_limpeza(dias_retencao)
    ]
    itens.sort(key=lambda i: i["ultimo_processamento"], reverse=True)
    return itens


def limpar_antigos(dias_retencao=180, aplicar=False) -> list:
    """
    Rotina de limpeza definitiva — separada e deliberada, nunca automática no
    momento do processamento.

    Args:
        dias_retencao: idade mínima (pela data de processamento) para apagar.
        aplicar: se False (padrão), apenas lista o que seria apagado.

    Returns:
        Lista de lote_ids candidatos/apagados.
    """
    alvos = []
    for caminho, lote, _ in _candidatos_limpeza(dias_retencao):
        alvos.append(lote["lote_id"])
        if aplicar:
            for p in (caminho, caminho_pdf(lote["lote_id"])):
                try:
                    os.remove(p)
                except OSError:
                    pass

    return alvos


# --- CLI -------------------------------------------------------------------

def _cli():
    import sys

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Uso:")
        print("  python lote.py listar [restaurante]")
        print("  python lote.py ver <lote_id>")
        print("  python lote.py limpar [dias] [--aplicar]")
        return

    cmd = sys.argv[1]

    if cmd == "listar":
        rest = sys.argv[2] if len(sys.argv) > 2 else None
        itens = listar_lotes(rest)
        if not itens:
            print("Nenhum lote registrado.")
            return
        print(f"{'LOTE':32} {'RESTAURANTE':20} {'PERÍODO':18} {'ALUNOS':>6}  ESTADO")
        for it in itens:
            estado = "arquivado" if it["processado"] else "ativo"
            if it["origem"] != ORIGEM_GERACAO:
                estado += f" ({it['origem']})"
            print(f"{it['lote_id']:32} {it['restaurante_nome']:20} "
                  f"{it['datas']:18} {it['total_alunos']:>6}  {estado}")

    elif cmd == "ver":
        lote = carregar_lote(sys.argv[2])
        print(f"Lote        : {lote['lote_id']}")
        print(f"Restaurante : {lote['restaurante_nome']} ({lote['restaurante_key']})")
        print(f"Criado em   : {lote['criado_em']}  origem={lote['origem']}")
        print(f"Período     : {lote.get('datas','')}  {lote.get('mes_ano','')}")
        print(f"Dias        : {', '.join(lote['dias'])}")
        print(f"Alunos      : {lote['total_alunos']} em {lote['total_paginas']} página(s)"
              f" ({lote['alunos_por_pagina']}/página)")
        for aviso in lote.get("avisos", []):
            print(f"  AVISO: {aviso}")
        for nota in lote.get("notas", []):
            print(f"  nota : {nota}")
        for p in lote.get("processamentos", []):
            sync = "sim" if p["sincronizado_com_sheets"] else "NÃO"
            print(f"  processado {p['em']} período={p['periodo']} sheets={sync}")

    elif cmd == "limpar":
        dias = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 180
        aplicar = "--aplicar" in sys.argv
        alvos = limpar_antigos(dias, aplicar)
        if not alvos:
            print(f"Nenhum lote processado com mais de {dias} dias.")
        elif aplicar:
            print(f"{len(alvos)} lote(s) apagados definitivamente.")
        else:
            print(f"{len(alvos)} lote(s) seriam apagados (rode com --aplicar):")
            for a in alvos:
                print(f"  {a}")
    else:
        print(f"Comando desconhecido: {cmd}")


if __name__ == "__main__":
    _cli()
