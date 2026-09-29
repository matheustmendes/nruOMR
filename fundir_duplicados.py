"""
fundir_duplicados.py

Une duas listas escaneadas (PDFs de bolhas) que a empresa devolveu
duplicadas para o MESMO período/restaurante, mas com presenças diferentes
entre uma e outra (ex: um bolsista com 5 presenças numa lista e 0 na outra).

Como decide o resultado
------------------------
As duas listas usam o mesmo lote (mesmo template impresso, mesma ordem de
linha), então a linha N identifica a mesma pessoa nas duas — não é preciso
casar por matrícula/nome como o `reconciliar.py` faz (lá as fontes são
diferentes; aqui é a mesma lista, só duplicada).

Regra de união: presente se marcado em QUALQUER uma das duas listas, dia a
dia (almoço e janta também por OR). Perder uma presença legítima pesa mais
contra o bolsista do que sobrar uma marca a mais — mesmo critério que o
sistema já usa para bolha em zona de dúvida (ver `reconciliar.releitura_correta`).

Não escreve nada no Sheets sem `--aplicar`.

Uso:
    python fundir_duplicados.py --lote <id> --periodo "05/05 a 09/05" \\
        --scan-a lista1_pag1.pdf lista1_pag2.pdf \\
        --scan-b lista2_pag1.pdf lista2_pag2.pdf

    python fundir_duplicados.py --lote <id> --periodo "05/05 a 09/05" \\
        --scan-a lista1.pdf --scan-b lista2.pdf --aplicar

Opcionais:
    --restaurante <chave>       (padrão: restaurante do lote)
    --pagina-inicial-a <n>      (padrão: 1)
    --pagina-inicial-b <n>      (padrão: 1)
    --aplicar                   grava a união no Google Sheets
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lote as lote_mod
import google_sheets as gs
import reconciliar as rec


def unir_contagens(contagem_a, contagem_b, dias):
    """
    Une duas contagens da mesma pessoa (mesmo número de linha) por OR,
    dia a dia.

    Returns:
        (unida, diferencas)
            unida: contagem no mesmo formato de `exportar.contar_presencas`
            diferencas: [{"numero", "presencas_a", "presencas_b",
                          "presencas_unida"}] — só as linhas em que as duas
                         listas discordaram no total de presenças
    """
    por_numero_a = {c["numero"]: c for c in contagem_a}
    por_numero_b = {c["numero"]: c for c in contagem_b}
    numeros = sorted(set(por_numero_a) | set(por_numero_b))

    unida, diferencas = [], []
    for numero in numeros:
        ca = por_numero_a.get(numero)
        cb = por_numero_b.get(numero)

        detalhes = {}
        for dia in dias:
            da = ca["detalhes"].get(dia, {}) if ca else {}
            db = cb["detalhes"].get(dia, {}) if cb else {}
            almoco = bool(da.get("almoco")) or bool(db.get("almoco"))
            janta = bool(da.get("janta")) or bool(db.get("janta"))
            detalhes[dia] = {
                "presente": almoco or janta,
                "almoco": almoco,
                "janta": janta,
            }

        presencas = sum(1 for d in detalhes.values() if d["presente"])
        unida.append({"numero": numero, "presencas": presencas, "detalhes": detalhes})

        pa = ca["presencas"] if ca else 0
        pb = cb["presencas"] if cb else 0
        if pa != pb:
            diferencas.append({
                "numero": numero, "presencas_a": pa, "presencas_b": pb,
                "presencas_unida": presencas,
            })

    return unida, diferencas


def _relatar_resumo(nome, resumo, total_alunos):
    print(f"Lista {nome}     : {resumo['linhas_lidas']} linha(s) lidas, "
          f"{resumo['ambiguos']} marcação(ões) na zona de dúvida")
    if not resumo["ordem_confiavel"]:
        print(f"  ORDEM DA LISTA {nome} NÃO CONFIRMADA: {resumo['motivo_ordem']}")
    if resumo["fora_do_lote"]:
        print(f"  lista {nome} chegou à linha {resumo['maior_numero']}, além das "
              f"{total_alunos} do lote — {resumo['fora_do_lote']} linha(s) de padding.")
    if resumo["excedentes_com_marca"]:
        print(f"  ATENÇÃO: lista {nome} tem {len(resumo['excedentes_com_marca'])} linha(s) "
              f"fora do lote com marcação real — descartadas da união.")


def _modo_uniao():
    lote_id = rec._arg("--lote")
    periodo = rec._arg("--periodo")
    scans_a = rec._args_multiplos("--scan-a")
    scans_b = rec._args_multiplos("--scan-b")

    if not (lote_id and periodo and scans_a and scans_b):
        raise SystemExit(
            "Informe --lote, --periodo, --scan-a e --scan-b. Veja --help."
        )

    lote = lote_mod.carregar_lote(lote_id)
    restaurante_key = rec._arg("--restaurante") or lote["restaurante_key"]

    try:
        pag_a = max(1, int(rec._arg("--pagina-inicial-a", "1")))
        pag_b = max(1, int(rec._arg("--pagina-inicial-b", "1")))
    except ValueError:
        pag_a = pag_b = 1

    print(f"Lote        : {lote_id}  ({lote['restaurante_nome']}, origem "
          f"{lote['origem']}, {lote['total_alunos']} pessoas)")
    print(f"Período     : {periodo}")
    print(f"Lista A     : {', '.join(scans_a)}")
    print(f"Lista B     : {', '.join(scans_b)}")
    if lote["origem"] == lote_mod.ORIGEM_OCR:
        print("  ATENÇÃO: lote reconstruído por OCR. Confira antes as linhas "
              "marcadas como 'sem_match' no JSON do lote.")
    print()

    print("Lendo lista A...")
    contagem_a, roster, dias, resumo_a = rec.releitura_correta(lote, scans_a, pag_a)
    print("Lendo lista B...")
    contagem_b, _roster_b, _dias_b, resumo_b = rec.releitura_correta(lote, scans_b, pag_b)
    print()

    total_alunos = lote["total_alunos"]
    _relatar_resumo("A", resumo_a, total_alunos)
    _relatar_resumo("B", resumo_b, total_alunos)

    if not (resumo_a["ordem_confiavel"] and resumo_b["ordem_confiavel"]):
        raise SystemExit(
            "\nUNIÃO BLOQUEADA: a ordem das páginas não foi confirmada em pelo "
            "menos uma das duas listas (ver aviso acima). Unir aqui arrisca "
            "casar a presença de uma pessoa com a de outra — o mesmo risco que "
            "bloqueia o --aplicar em reconciliar.py. Ajuste o conjunto de "
            "arquivos de cada lista (remova redigitalizações sobrepostas) e "
            "rode de novo."
        )

    unida, diferencas = unir_contagens(contagem_a, contagem_b, dias)

    print(f"\nUnião       : {len(unida)} pessoa(s), {len(diferencas)} "
          f"com total de presenças diferente entre A e B")

    if diferencas:
        print("\nDiferenças resolvidas por união (A → B → total unido):")
        for d in diferencas[:30]:
            idx = d["numero"] - 1
            nome = roster[idx][0] if 0 <= idx < len(roster) else f"linha {d['numero']}"
            print(f"    nº {d['numero']:>4}  {nome[:34]:34s}  "
                  f"A={d['presencas_a']}  B={d['presencas_b']}  →  {d['presencas_unida']}")
        if len(diferencas) > 30:
            print(f"    ... e mais {len(diferencas) - 30}.")
    else:
        print("\nAs duas listas já batiam — a união não muda nada.")

    if "--aplicar" not in sys.argv:
        print("\nNada foi gravado. Para exportar a união ao Sheets, repita o "
              "comando com --aplicar.")
        return

    # Só é seguro apagar marcas de linhas ausentes quando a UNIÃO cobre o lote
    # inteiro — do contrário, gente que nenhuma das duas listas leu perderia
    # o que já estava certo no Sheets.
    cobertura_total = len(unida) >= total_alunos
    if not cobertura_total:
        print(f"\n  AVISO: a união cobriu {len(unida)} das {total_alunos} linhas "
              f"do lote. Marcações erradas que sobraram em linhas fantasma NÃO "
              f"serão apagadas.")

    print(f"\nExportando união do período '{periodo}' para '{restaurante_key}'...")
    resultado = gs.exportar_para_sheets(
        unida, roster, dias, restaurante_key, periodo, forcar=True,
        limpar_ausentes=cobertura_total,
    )
    if resultado.get("ok"):
        lote_mod.registrar_processamento(
            lote_id, periodo, sincronizado_sheets=True,
            detalhe=f"união de listas duplicadas: {len(diferencas)} linha(s) resolvidas",
        )
        print(f"  Período gravado em '{resultado.get('aba', '')}'.")
    else:
        print(f"  ERRO ao gravar: {resultado.get('erro')}")
        print("  O lote continua ativo — dá para tentar de novo sem reescanear.")


def main():
    from utils import stdout_tolerante

    stdout_tolerante()

    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    _modo_uniao()


if __name__ == "__main__":
    main()
