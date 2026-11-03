"""
cruzar_justificativas.py

Cruza as justificativas respondidas pelos alunos (planilha da coordenação, com
as colunas APROVADO / REPROVADO) com as planilhas de contabilização de presença
e preenche a coluna "Justificativa aprovada?" da aba do mês. A coluna
"Justificativa" (fórmula) passa a mostrar "Justificado" para quem foi aprovado,
e a coordenadora filtra por ela (Dados → Visões de filtro → "Justificados" /
"Não justificados").

Regras:
- Cruza por matrícula; se a matrícula não bate, pelo nome normalizado.
- Várias respostas da mesma pessoa: vale a decisão (aprovada/reprovada) mais
  recente; sem nenhuma decisão, fica "Pendente". Marcada como aprovada E
  reprovada ao mesmo tempo, fica "Pendente" (ambíguo) e aparece no aviso.
- Quem não respondeu o formulário não é tocado (a célula fica como está).
- Célula já preenchida com outro valor (ex.: a coordenadora digitou) não é
  sobrescrita, a menos que se passe --sobrescrever; vira "conflito" no resumo.
- Por padrão só simula. Grava com --aplicar.

Uso:
    python cruzar_justificativas.py                      # simula, todos os restaurantes
    python cruzar_justificativas.py --aba "Setembro 2026" --aplicar
    python cruzar_justificativas.py --rest ondina canela --aplicar
"""

import argparse
import re
import unicodedata
from datetime import datetime

import google_sheets as gs

RESTAURANTES = ("canela", "ondina", "sao_lazaro")
_MESES = gs._MESES


def _nn(valor):
    """Minúsculas, sem acento e sem espaço duplicado: para casar nomes e rótulos."""
    txt = unicodedata.normalize("NFKD", str(valor or ""))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.lower().split())


def _norm_rotulo(v):
    return _nn(re.sub(r"[^\w ]", "", str(v or "")))


def _verdadeiro(v):
    return str(v).strip().upper() in ("TRUE", "VERDADEIRO", "SIM", "1")


def _data(v):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt)
        except ValueError:
            pass
    return datetime.min


def ler_justificativas(config=None, cliente=None):
    """Lista de respostas: {linha, ts, nome, mat, aprovado, reprovado, motivo}."""
    config = config or gs._carregar_config()
    cliente = cliente or gs._obter_cliente(config)
    cfg = config.get("justificativas_aprovadas", {})
    sid = cfg.get("spreadsheet_id", "").strip()
    if not sid:
        raise RuntimeError("`justificativas_aprovadas.spreadsheet_id` não configurado em config_sheets.yaml")
    planilha = gs._com_retry(lambda: cliente.open_by_key(sid))
    aba = planilha.worksheet(cfg["aba"]) if cfg.get("aba") else planilha.get_worksheet(0)
    valores = gs._com_retry(aba.get_all_values)

    cab = [_norm_rotulo(c) for c in valores[0]]

    def col(*nomes):
        for n in nomes:
            if n in cab:
                return cab.index(n)
        raise RuntimeError(f"coluna '{nomes[0]}' não encontrada na planilha de justificativas")

    c_ts, c_nome = col("carimbo de datahora"), col("nome completo")
    c_mat, c_apr, c_rep = col("matricula"), col("aprovado"), col("reprovado")
    c_mot = col("motivo do reprovado")

    def cel(l, c):
        return l[c].strip() if c < len(l) else ""

    respostas = []
    for i, l in enumerate(valores[1:], start=2):
        if not cel(l, c_nome) and not cel(l, c_mat):
            continue
        respostas.append({
            "linha": i, "ts": _data(cel(l, c_ts)), "nome": cel(l, c_nome),
            "mat": gs._norm_mat(cel(l, c_mat)),
            "aprovado": _verdadeiro(cel(l, c_apr)),
            "reprovado": _verdadeiro(cel(l, c_rep)),
            "motivo": cel(l, c_mot),
        })
    return respostas


def decidir(respostas):
    """
    {chave: {status, nome, mat, ambiguo}} com uma decisão por pessoa.
    Chave = matrícula, ou o nome normalizado quando não há matrícula.
    """
    por_pessoa = {}
    for r in respostas:
        chave = r["mat"] or ("N:" + _nn(r["nome"]))
        por_pessoa.setdefault(chave, []).append(r)

    out = {}
    for chave, rs in por_pessoa.items():
        rs.sort(key=lambda r: r["ts"])
        ambiguo = any(r["aprovado"] and r["reprovado"] for r in rs)
        decididas = [r for r in rs if r["aprovado"] != r["reprovado"]]
        if decididas:
            status = gs.APROVADA_SIM if decididas[-1]["aprovado"] else gs.APROVADA_NAO
        else:
            status = gs.APROVADA_PENDENTE
        if ambiguo and not decididas:
            status = gs.APROVADA_PENDENTE
        out[chave] = {"status": status, "nome": rs[-1]["nome"], "mat": rs[-1]["mat"],
                      "ambiguo": ambiguo, "respostas": len(rs)}
    return out


def aba_padrao(respostas):
    """Mês anterior ao da resposta mais recente (respondem em outubro sobre setembro)."""
    ult = max((r["ts"] for r in respostas), default=datetime.now())
    mes, ano = ult.month - 1, ult.year
    if mes == 0:
        mes, ano = 12, ano - 1
    return f"{_MESES[mes - 1]} {ano}"


def _num(v):
    try:
        return int(float(str(v).replace(",", ".")))
    except ValueError:
        return 0


def cruzar_restaurante(restaurante_key, nome_aba, decisoes, config, cliente,
                       aplicar=False, sobrescrever=False):
    """Cruza as decisões com a aba do mês de um restaurante."""
    sid = config["restaurantes"][restaurante_key]["spreadsheet_id"].strip()
    planilha = gs._com_retry(lambda: cliente.open_by_key(sid))
    aba = next((a for a in gs._com_retry(planilha.worksheets) if a.title.strip() == nome_aba), None)
    res = {"restaurante": restaurante_key, "aba": nome_aba, "encontrados": set(),
           "escrever": [], "conflitos": [], "viram_justificado": 0, "erro": None}
    if aba is None:
        res["erro"] = f"aba '{nome_aba}' não existe"
        return res

    valores = gs._com_retry(aba.get_all_values)
    cab = valores[1] if len(valores) > 1 else []
    layout_novo = len(cab) > gs._COL_APROVADA and \
        str(cab[gs._COL_APROVADA]).strip() == gs._CABECALHO_FIXO[gs._COL_APROVADA]
    res["layout_novo"] = layout_novo
    if aplicar and not layout_novo:
        res["erro"] = ("a aba ainda não tem a coluna \"Justificativa aprovada?\" — "
                       f"rode: python padronizar_planilhas.py {restaurante_key}")
        return res

    por_mat = {k: v for k, v in decisoes.items() if not k.startswith("N:")}
    por_nome = {_nn(v["nome"]): v for v in decisoes.values()}

    for r in gs._ler_roster(valores):
        linha = valores[r["linha"] - 1]
        dec = por_mat.get(gs._norm_mat(r["mat"])) or por_nome.get(_nn(r["nome"]))
        if not dec:
            continue
        res["encontrados"].add(dec["mat"] or "N:" + _nn(dec["nome"]))
        atual = linha[gs._COL_APROVADA].strip() if layout_novo and len(linha) > gs._COL_APROVADA else ""
        novo = dec["status"]
        if atual == novo:
            continue
        item = {"linha": r["linha"], "nome": r["nome"], "mat": r["mat"],
                "atual": atual, "novo": novo}
        if atual and not sobrescrever:
            res["conflitos"].append(item)
            continue
        res["escrever"].append(item)
        pres = _num(linha[gs._COL_PRESENCAS_MES]) if len(linha) > gs._COL_PRESENCAS_MES else 0
        dias = _num(linha[gs._COL_DIAS_JUSTIFICADOS]) if len(linha) > gs._COL_DIAS_JUSTIFICADOS else 0
        if novo == gs.APROVADA_SIM and pres < gs._LIMITE_FREQUENCIA and \
                pres + dias < gs._LIMITE_FREQUENCIA:
            res["viram_justificado"] += 1

    if aplicar and res["escrever"]:
        col = gs._col_letra(gs._COL_APROVADA)
        gs._com_retry(lambda: aba.batch_update(
            [{"range": f"{col}{i['linha']}", "values": [[i["novo"]]]} for i in res["escrever"]],
            value_input_option="USER_ENTERED"))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aba", help='aba do mês (padrão: mês anterior à resposta mais recente), ex.: "Setembro 2026"')
    ap.add_argument("--rest", nargs="+", choices=RESTAURANTES, default=list(RESTAURANTES))
    ap.add_argument("--aplicar", action="store_true", help="grava na planilha (sem isso, só simula)")
    ap.add_argument("--sobrescrever", action="store_true", help="troca também células já preenchidas")
    args = ap.parse_args()

    config = gs._carregar_config()
    cliente = gs._obter_cliente(config)
    respostas = ler_justificativas(config, cliente)
    decisoes = decidir(respostas)
    nome_aba = args.aba or aba_padrao(respostas)

    cont = {}
    for d in decisoes.values():
        cont[d["status"]] = cont.get(d["status"], 0) + 1
    print(f"{len(respostas)} respostas, {len(decisoes)} pessoas: " +
          ", ".join(f"{k} {v}" for k, v in sorted(cont.items())))
    for d in decisoes.values():
        if d["ambiguo"]:
            print(f"  AVISO: {d['nome']} ({d['mat']}) marcada como aprovada E reprovada")
    print(f"Aba: {nome_aba} | {'GRAVANDO' if args.aplicar else 'simulação (nada é gravado)'}\n")

    achados, erro = set(), False
    for rk in args.rest:
        r = cruzar_restaurante(rk, nome_aba, decisoes, config, cliente,
                               aplicar=args.aplicar, sobrescrever=args.sobrescrever)
        achados |= r["encontrados"]
        if r["erro"]:
            erro = True
            print(f"[{rk}] ERRO: {r['erro']}")
            continue
        aviso = "" if r["layout_novo"] else "  (aba sem a coluna nova ainda: só simulação)"
        print(f"[{rk}] {len(r['encontrados'])} pessoas na aba | {len(r['escrever'])} "
              f"{'gravadas' if args.aplicar else 'a gravar'} | {len(r['conflitos'])} conflito(s) | "
              f"{r['viram_justificado']} passam a \"Justificado\"{aviso}")
        for c in r["conflitos"]:
            print(f"    conflito linha {c['linha']} {c['nome']}: planilha \"{c['atual']}\", formulário \"{c['novo']}\"")

    fora = [d for k, d in decisoes.items() if k not in achados]
    if fora:
        print(f"\n{len(fora)} pessoa(s) do formulário não encontradas em nenhuma aba '{nome_aba}':")
        for d in fora:
            print(f"    {d['nome']} ({d['mat'] or 'sem matrícula'}) — {d['status']}")
    if erro:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
