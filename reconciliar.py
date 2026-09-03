"""
reconciliar.py

Confere — e, se você mandar, corrige — os períodos que já foram lançados no
Google Sheets com a lista de referência errada.

A ideia central
---------------
Para saber se uma semana antiga saiu trocada, **não é preciso ter a planilha
de referência daquela época**. Ela era só um intermediário. As duas coisas que
importam continuam existindo:

    o scan (quem marcou o quê, por linha)  +  o lote (quem era cada linha)

Reprocessando as duas, chega-se ao resultado correto de forma independente. O
que está hoje no Sheets é comparado contra ele. A planilha perdida não faz
falta nenhuma nessa conta.

Três modos
----------

  --triagem [--restaurante canela]
      Varre o Google Sheets e aponta, sem precisar de scan nenhum, onde há
      rastro de lista trocada. Serve para decidir quais semanas merecem o
      trabalho de reprocessar. É rápido e não altera nada.

  --lote <id> --scan <scan.pdf> --periodo "05/05 a 09/05"
      Releitura completa: reprocessa o scan com a identidade do lote e mostra,
      pessoa a pessoa, onde o que está no Sheets difere do correto. Também não
      altera nada.

  ... --aplicar
      Reescreve o período no Sheets com o resultado correto.

Uso:
    python reconciliar.py --triagem
    python reconciliar.py --triagem --restaurante canela
    python reconciliar.py --lote canela_20260512-090000 --scan maio_semana2.pdf \\
        --periodo "12/05 a 16/05"
    python reconciliar.py --lote canela_20260512-090000 --scan maio_semana2.pdf \\
        --periodo "12/05 a 16/05" --aplicar
"""

import os
import sys
import csv

import lote as lote_mod
import google_sheets as gs

RESTAURANTES_PADRAO = ["canela", "ondina", "sao_lazaro"]


# --- Triagem: o que a própria planilha denuncia -----------------------------

def triar(restaurante_key) -> list:
    """
    Diagnostica todas as abas mensais de um restaurante.

    Returns:
        Lista de dicts por aba, com os achados já classificados entre prova e
        indício — a distinção importa: agir sobre indício custa reprocessar uma
        semana à toa, ignorar uma prova deixa presença errada no ar.
    """
    resultados = []

    for nome_aba in gs.listar_abas_mes(restaurante_key):
        diag = gs.diagnosticar_aba(restaurante_key, nome_aba)
        if not diag.get("ok") or not diag["periodos"]:
            continue

        provas, indicios = [], []

        if diag["fantasmas"]:
            linhas = ", ".join(str(f["linha"]) for f in diag["fantasmas"][:10])
            provas.append(
                f"{len(diag['fantasmas'])} linha(s) 'Aluno N' sem matrícula "
                f"(linhas {linhas}). Só são criadas quando o scan tinha MAIS "
                f"pessoas do que a lista de referência usada — prova de que as "
                f"duas não batiam."
            )

        for p in diag["periodos"]:
            if p["fantasmas_marcados"]:
                provas.append(
                    f"Período '{p['periodo']}': {p['fantasmas_marcados']} "
                    f"marcação(ões) caíram em linhas 'Aluno N'."
                )

        for d in diag["duplicadas"]:
            # Distinguir os dois casos muda muito o que fazer: a mesma pessoa
            # digitada duas vezes só desperdiça uma linha impressa; duas
            # pessoas diferentes com a mesma matrícula significa que uma delas
            # perde a frequência toda semana, em todos os períodos.
            a, b = d["nomes"]
            mesma_pessoa = lote_mod.norm_nome(a) == lote_mod.norm_nome(b)
            if mesma_pessoa:
                indicios.append(
                    f"Matrícula {d['matricula']} duplicada nas linhas "
                    f"{d['linhas'][0]} e {d['linhas'][1]}, mesmo nome ('{a}'). "
                    f"Provável duplicata na planilha de origem: desperdiça uma "
                    f"linha impressa, mas não perde frequência."
                )
            else:
                provas.append(
                    f"Matrícula {d['matricula']} está em DUAS PESSOAS diferentes "
                    f"— linha {d['linhas'][0]} '{a}' e linha {d['linhas'][1]} "
                    f"'{b}'. Toda semana as duas colidem na mesma linha e uma "
                    f"delas fica sem frequência."
                )

        # Cada semana lê a mesma folha impressa, então a última linha marcada
        # deveria ficar próxima entre os períodos do mês. Uma diferença grande
        # significa que a lista mudou de tamanho no meio do caminho.
        ultimas = [p["ultima_linha"] for p in diag["periodos"] if p["ultima_linha"]]
        if len(ultimas) > 1 and (max(ultimas) - min(ultimas)) > 2:
            detalhe = ", ".join(
                f"{p['periodo']}→{p['ultima_linha']}"
                for p in diag["periodos"] if p["ultima_linha"]
            )
            indicios.append(
                f"A última linha com marcação varia entre os períodos "
                f"({detalhe}). Indício de que a lista de referência mudou de "
                f"tamanho entre uma semana e outra."
            )

        resultados.append({
            "aba": nome_aba,
            "total_linhas": diag["total_linhas"],
            "periodos": diag["periodos"],
            "provas": provas,
            "indicios": indicios,
        })

    return resultados


# --- Releitura: o resultado correto, obtido do scan + lote ------------------

def releitura_correta(lote: dict, caminhos_scan, pagina_inicial=1):
    """
    Reprocessa o scan usando a identidade e a geometria do lote.

    Returns:
        (contagem, roster, dias, resumo) — `resumo["ordem_confiavel"]` é False
        quando a numeração das páginas não pôde ser reconstruída com segurança
        (menos de duas páginas legíveis, números repetidos por sobreposição
        entre arquivos, etc.) e o processamento caiu de volta para a ordem
        bruta do maço. Nesse caso o `contagem` devolvido não é confiável —
        quem chama isso para escrever em planilha compartilhada deve tratar
        como bloqueio, do mesmo jeito que scan incompleto ou OCR sem match.
    """
    from exportar import carregar_todas_paginas, processar_pdf_completo, contar_presencas
    from ler_bolhas import get_zona_ambigua, _get_threshold

    config = lote["config"]
    dias = lote["dias"]

    paginas = carregar_todas_paginas(*caminhos_scan, dpi=config["scan"]["dpi"])
    diagnostico = {}
    resultados = processar_pdf_completo(
        paginas, config, pagina_inicial=pagina_inicial, diagnostico=diagnostico
    )

    # Mesma regra do fluxo normal: a dúvida conta como presença e vai para
    # conferência humana depois. Reconciliar com outro critério produziria
    # diferenças que não são erro de identidade.
    threshold = _get_threshold(config)
    amb_min, amb_max = get_zona_ambigua(config)
    ambiguos = 0
    for r in resultados:
        for dia in dias:
            for tipo in ("almoco", "janta"):
                pct = r["dias"][dia][f"{tipo}_pct"]
                if amb_min <= pct <= amb_max:
                    ambiguos += 1
                if amb_min <= pct < threshold:
                    r["dias"][dia][tipo] = True

    contagem = contar_presencas(resultados, dias)
    maior = max((c["numero"] for c in contagem), default=0)
    total_alunos = lote["total_alunos"]

    # Linhas além do fim do lote não têm identidade nenhuma para receber —
    # são padding da última página (o código sempre lê alunos_por_pagina
    # posições de bolha, mesmo quando a página real tem menos gente) ou folha
    # de outro lote no maço. Exportadas do jeito que estavam, viram uma linha
    # nova no Sheets com o nome literal "[linha N fora do lote]" — é assim que
    # nascem os fantasmas "Aluno N" que a triagem encontra depois. Por isso
    # ficam de fora do que é devolvido para exportação; o que elas leram
    # continua disponível em `resumo` para o operador decidir.
    excedentes_com_marca = [
        c for c in contagem if c["numero"] > total_alunos and c["presencas"] > 0
    ]
    contagem = [c for c in contagem if c["numero"] <= total_alunos]
    roster = lote_mod.roster_do_lote(lote)

    resumo = {
        "linhas_lidas": len(resultados),
        "ambiguos": ambiguos,
        "maior_numero": maior,
        "fora_do_lote": max(0, maior - total_alunos),
        "excedentes_com_marca": excedentes_com_marca,
        "ordem_confiavel": diagnostico.get("ordem_confiavel", False),
        "motivo_ordem": diagnostico.get("motivo_ordem", ""),
    }
    return contagem, roster, dias, resumo


def _marca(detalhe):
    if not detalhe.get("presente"):
        return ""
    return ("A" if detalhe.get("almoco") else "") + ("J" if detalhe.get("janta") else "")


def comparar_com_sheets(lote, contagem, roster, dias, restaurante_key, periodo):
    """
    Confronta o resultado correto com o que está gravado na planilha.

    O casamento é feito por matrícula e, para quem não tem matrícula, por nome
    normalizado — nunca por posição, que é justamente o que estava errado.

    Returns:
        {"ok", "aba", "divergentes": [...], "nao_encontrados": [...],
         "conferidos": int, "iguais": int}
    """
    gravado = gs.ler_periodo(restaurante_key, periodo)
    if not gravado.get("ok"):
        return {"ok": False, "erro": gravado.get("erro", "falha ao ler o período")}

    por_mat, por_nome = {}, {}
    for linha in gravado["linhas"]:
        if linha["matricula"]:
            por_mat.setdefault(linha["matricula"], linha)
        chave = lote_mod.norm_nome(linha["nome"])
        if chave:
            por_nome.setdefault(chave, linha)

    divergentes, nao_encontrados = [], []
    iguais = 0

    for c in contagem:
        idx = c["numero"] - 1
        if not (0 <= idx < len(roster)):
            continue
        nome, matricula = roster[idx]

        linha = por_mat.get(matricula) if matricula else None
        if linha is None:
            linha = por_nome.get(lote_mod.norm_nome(nome))

        correto = {d: _marca(c["detalhes"].get(d, {})) for d in dias}

        if linha is None:
            if any(correto.values()):
                nao_encontrados.append({
                    "numero": c["numero"], "nome": nome,
                    "matricula": matricula, "correto": correto,
                })
            continue

        atual = {d: linha["marcas"].get(d, "") for d in dias}
        difs = {d: (atual[d], correto[d]) for d in dias if atual[d] != correto[d]}

        if difs:
            divergentes.append({
                "numero": c["numero"],
                "linha_sheets": linha["linha"],
                "nome": nome,
                "nome_sheets": linha["nome"],
                "matricula": matricula,
                "diferencas": difs,
                "presencas_sheets": linha["presencas"],
                "presencas_correto": c["presencas"],
            })
        else:
            iguais += 1

    return {
        "ok": True,
        "aba": gravado["aba"],
        "divergentes": divergentes,
        "nao_encontrados": nao_encontrados,
        "conferidos": len(contagem),
        "iguais": iguais,
    }


def salvar_csv(comparacao, dias, caminho):
    campos = ["numero", "linha_sheets", "matricula", "nome_correto", "nome_sheets",
              "presencas_sheets", "presencas_correto"]
    campos += [f"{d}_sheets" for d in dias] + [f"{d}_correto" for d in dias]

    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=campos)
        escritor.writeheader()
        for d in comparacao["divergentes"]:
            linha = {
                "numero": d["numero"],
                "linha_sheets": d["linha_sheets"],
                "matricula": d["matricula"],
                "nome_correto": d["nome"],
                "nome_sheets": d["nome_sheets"],
                "presencas_sheets": d["presencas_sheets"],
                "presencas_correto": d["presencas_correto"],
            }
            for dia in dias:
                atual, correto = d["diferencas"].get(dia, ("", ""))
                linha[f"{dia}_sheets"] = atual
                linha[f"{dia}_correto"] = correto
            escritor.writerow(linha)


# --- CLI --------------------------------------------------------------------

def _arg(nome, padrao=None):
    if nome in sys.argv:
        idx = sys.argv.index(nome)
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
            return sys.argv[idx + 1]
    return padrao


def _args_multiplos(nome):
    if nome not in sys.argv:
        return []
    valores = []
    for arg in sys.argv[sys.argv.index(nome) + 1:]:
        if arg.startswith("--"):
            break
        valores.append(arg)
    return valores


def _modo_triagem():
    alvo = _arg("--restaurante")
    chaves = [alvo] if alvo else RESTAURANTES_PADRAO

    total_provas = 0
    for chave in chaves:
        print(f"\n{'=' * 66}")
        print(f"  {chave.upper()}")
        print("=" * 66)
        try:
            abas = triar(chave)
        except Exception as e:
            print(f"  Não foi possível ler: {e}")
            continue

        if not abas:
            print("  Nenhuma aba mensal com períodos lançados.")
            continue

        for info in abas:
            periodos = ", ".join(p["periodo"] for p in info["periodos"])
            print(f"\n  {info['aba']}  —  {info['total_linhas']} linhas  |  {periodos}")

            for prova in info["provas"]:
                total_provas += 1
                print(f"    [PROVA]   {prova}")
            for indicio in info["indicios"]:
                print(f"    [indício] {indicio}")
            if not info["provas"] and not info["indicios"]:
                print("    nada suspeito nesta aba")

    print(f"\n{'=' * 66}")
    if total_provas:
        print(f"  {total_provas} achado(s) com prova de lista trocada.")
        print("  Para cada semana afetada, recupere o lote e reprocesse:")
        print("    python recuperar_lote.py --pdf <template.pdf> --restaurante <rest>")
        print("    python reconciliar.py --lote <id> --scan <scan.pdf> --periodo \"<per>\"")
    else:
        print("  Nenhuma prova de lista trocada encontrada.")
        print("  Isso não garante que esteja tudo certo: um deslocamento em que")
        print("  a lista tinha o MESMO tamanho da folha não deixa rastro na")
        print("  planilha. A conferência definitiva é a releitura do scan.")
    print("=" * 66)


def _modo_comparacao():
    lote_id = _arg("--lote")
    periodo = _arg("--periodo")
    scans = _args_multiplos("--scan")

    if not (lote_id and periodo and scans):
        raise SystemExit("Informe --lote, --scan e --periodo. Veja --help.")

    alvo = lote_mod.carregar_lote(lote_id)
    restaurante_key = _arg("--restaurante") or alvo["restaurante_key"]

    try:
        pagina_inicial = max(1, int(_arg("--pagina-inicial", "1")))
    except ValueError:
        pagina_inicial = 1

    print(f"Lote        : {lote_id}  ({alvo['restaurante_nome']}, origem "
          f"{alvo['origem']}, {alvo['total_alunos']} pessoas)")
    print(f"Período     : {periodo}")
    print(f"Scan        : {', '.join(scans)}")
    if alvo["origem"] == lote_mod.ORIGEM_OCR:
        print("  ATENÇÃO: lote reconstruído por OCR. Confira antes as linhas")
        print("           marcadas como 'sem_match' no JSON do lote.")
    print()

    contagem, roster, dias, resumo = releitura_correta(alvo, scans, pagina_inicial)

    print()
    print(f"Releitura   : {resumo['linhas_lidas']} linhas, "
          f"{resumo['ambiguos']} marcação(ões) na zona de dúvida")
    if not resumo["ordem_confiavel"]:
        print(f"  ORDEM DAS PÁGINAS NÃO CONFIRMADA: {resumo['motivo_ordem']}")
        print(f"  Provável causa: os arquivos do maço se sobrepõem (uma "
              f"redigitalização recobriu páginas já lidas). O restante desta "
              f"comparação usa a ordem bruta do arquivo e NÃO é confiável.")
    if resumo["fora_do_lote"]:
        print(f"  o scan chegou à linha {resumo['maior_numero']}, além das "
              f"{alvo['total_alunos']} do lote — {resumo['fora_do_lote']} linha(s) "
              f"de padding, sem identidade. Não entram na exportação.")
    if resumo["excedentes_com_marca"]:
        print(f"  ATENÇÃO: {len(resumo['excedentes_com_marca'])} dessas linhas têm "
              f"marcação real (não são padding em branco):")
        for c in resumo["excedentes_com_marca"][:10]:
            print(f"    linha {c['numero']} — {c['presencas']} dia(s) marcado(s)")
        print(f"  Foram DESCARTADAS da exportação — gravá-las criaria uma pessoa")
        print(f"  fantasma no Sheets. Confira o scan: pode ser rasura/mancha na")
        print(f"  borda, folha de outro lote no maço, ou o lote está incompleto.")

    comparacao = comparar_com_sheets(
        alvo, contagem, roster, dias, restaurante_key, periodo
    )
    if not comparacao.get("ok"):
        raise SystemExit(f"\nERRO: {comparacao['erro']}")

    divergentes = comparacao["divergentes"]
    print(f"\nNo Sheets   : aba '{comparacao['aba']}'")
    print(f"Conferidos  : {comparacao['conferidos']}  |  iguais: {comparacao['iguais']}"
          f"  |  divergentes: {len(divergentes)}")

    if comparacao["nao_encontrados"]:
        print(f"\n{len(comparacao['nao_encontrados'])} pessoa(s) com presença na folha "
              f"não têm linha nesta aba do Sheets:")
        for n in comparacao["nao_encontrados"][:10]:
            print(f"    nº {n['numero']:>4}  {n['nome']}  ({n['matricula'] or 'sem matrícula'})")

    if divergentes:
        print(f"\nDivergências (o que está lá → o que deveria estar):")
        for d in divergentes[:30]:
            difs = ", ".join(
                f"{dia}: '{atual}' -> '{correto}'"
                for dia, (atual, correto) in d["diferencas"].items()
            )
            print(f"    linha {d['linha_sheets']:>4}  {d['nome'][:34]:34s}  {difs}")
        if len(divergentes) > 30:
            print(f"    ... e mais {len(divergentes) - 30}.")

        csv_saida = _arg("--csv", f"reconciliacao_{lote_id}_{periodo.replace('/', '-')}.csv")
        salvar_csv(comparacao, dias, csv_saida)
        print(f"\nRelatório completo: {csv_saida}")
    else:
        print("\nNada a corrigir: o que está no Sheets bate com a releitura do scan.")

    if "--aplicar" not in sys.argv:
        if divergentes:
            print("\nNada foi alterado. Para gravar a correção, repita o comando com "
                  "--aplicar.")
        return

    if not divergentes:
        print("\nNada a aplicar.")
        return

    if not resumo["ordem_confiavel"]:
        raise SystemExit(
            "\nAPLICAÇÃO BLOQUEADA: a ordem das páginas não foi confirmada "
            "(ver aviso acima). Gravar aqui arrisca atribuir a presença de "
            "cada pessoa a outra — exatamente o bug que este processo existe "
            "para corrigir. Ajuste o conjunto de arquivos do scan (remova "
            "redigitalizações sobrepostas) e rode de novo."
        )

    # Só é seguro apagar as marcas das linhas ausentes quando o scan cobriu o
    # lote inteiro. Num scan parcial, as linhas não lidas seriam zeradas.
    cobertura_total = resumo["linhas_lidas"] >= alvo["total_alunos"]
    if not cobertura_total:
        print(f"\n  AVISO: o scan cobriu {resumo['linhas_lidas']} das "
              f"{alvo['total_alunos']} linhas do lote. As marcações erradas que"
              f" ficaram em linhas fantasma NÃO serão apagadas — reprocesse com"
              f" o scan completo para limpá-las.")

    print(f"\nRegravando o período '{periodo}' na aba '{comparacao['aba']}'...")
    resultado = gs.exportar_para_sheets(
        contagem, roster, dias, restaurante_key, periodo, forcar=True,
        limpar_ausentes=cobertura_total,
    )
    if resultado.get("ok"):
        lote_mod.registrar_processamento(
            lote_id, periodo, sincronizado_sheets=True,
            detalhe=f"reconciliação: {len(divergentes)} linha(s) corrigidas",
        )
        print(f"  Período regravado em '{resultado.get('aba', '')}'.")
        print("  Guarde o CSV acima como registro do que mudou e por quê.")
    else:
        print(f"  ERRO ao regravar: {resultado.get('erro')}")
        print("  O lote continua ativo — dá para tentar de novo sem reescanear.")


def main():
    from utils import stdout_tolerante

    stdout_tolerante()

    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    if "--triagem" in sys.argv:
        _modo_triagem()
    else:
        _modo_comparacao()


if __name__ == "__main__":
    main()
