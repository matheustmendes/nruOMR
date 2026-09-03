"""
processar.py

Interface interativa de terminal para estagiários.
Não requer conhecimento técnico — menus numerados e suporte a arrastar arquivos.

Uso:
    python processar.py
"""

import sys
import os
import yaml


CONFIGS = {
    "1": ("configs/config_canela.yaml",    "CANELA IMPRESSÃO",      "canela"),
    "2": ("configs/config_ondina.yaml",    "ONDINA IMPRESSÃO",      "ondina"),
    "3": ("configs/config_sao_lazaro.yaml","SÃO LÁZARO IMPRESSÃO",  "sao_lazaro"),
}

ABAS_TEMPLATE = {
    "1": ("CANELA IMPRESSÃO",      ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]),
    "2": ("ONDINA IMPRESSÃO",      ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]),
    "3": ("SÃO LÁZARO IMPRESSÃO",  ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]),
}


def limpar(caminho):
    return caminho.strip().strip('"').strip("'")


def linha():
    print("─" * 50)


def cabecalho():
    os.system("cls" if os.name == "nt" else "clear")
    print()
    linha()
    print("  Sistema OMR — Controle de Presença")
    print("  PROAE / UFBA")
    linha()
    print()


def menu_principal():
    cabecalho()
    print("  O que deseja fazer?\n")
    print("  1. Gerar template semanal (PDF + config)")
    print("  2. Processar scan de presença (→ xlsx)")
    print("  0. Sair")
    print()
    return input("  Opção: ").strip()


def escolher_restaurante(mensagem="Restaurante"):
    print(f"\n  {mensagem}:")
    print("    1. Canela")
    print("    2. Ondina")
    print("    3. São Lázaro")
    return input("  Escolha (1-3): ").strip()


def pedir_arquivo(extensoes, descricao):
    while True:
        print(f"\n  {descricao}")
        print("  (arraste o arquivo aqui e pressione Enter)")
        caminho = limpar(input("  Arquivo: "))
        if not caminho:
            print("  Cancelado.")
            return None
        if not os.path.isfile(caminho):
            print(f"  ERRO: arquivo não encontrado: {caminho}")
            continuar = input("  Tentar de novo? (s/n): ").strip().lower()
            if continuar != "s":
                return None
            continue
        ext = os.path.splitext(caminho)[1].lower()
        if ext not in extensoes:
            print(f"  ERRO: extensão {ext} inválida. Esperado: {', '.join(extensoes)}")
            continuar = input("  Tentar de novo? (s/n): ").strip().lower()
            if continuar != "s":
                return None
            continue
        return caminho


def gerar_template():
    cabecalho()
    print("  GERAR TEMPLATE SEMANAL\n")

    op = escolher_restaurante()
    if op not in ABAS_TEMPLATE:
        print("  Opção inválida.")
        input("\n  [Enter para voltar]")
        return

    nome_aba, dias = ABAS_TEMPLATE[op]
    _, _, slug = CONFIGS[op]

    xlsx = pedir_arquivo([".xlsx"], "Planilha de alunos (.xlsx exportado do Google Sheets):")
    if not xlsx:
        return

    print("\n  Gerando template...")

    try:
        from gerar_template import ler_planilha, gerar_com_lote

        info = ler_planilha(xlsx, nome_aba)
        print(f"  {len(info['alunos'])} alunos encontrados.")

        os.makedirs("configs", exist_ok=True)
        nome_config = f"configs/config_{slug}.yaml"

        nomes_rest = {"1": "Canela", "2": "Ondina", "3": "São Lázaro"}
        novo_lote, caminho_pdf = gerar_com_lote(
            info, dias, slug, nomes_rest.get(op, slug), nome_aba, nome_config
        )

        print()
        linha()
        print(f"  PDF gerado    : {caminho_pdf}")
        print(f"  Config gerado : {nome_config}")
        print(f"  Lote          : {novo_lote['lote_id']}")
        print(f"  (guarde este código: e ele que liga a folha impressa aos nomes)")
        linha()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n  ERRO: {e}")

    input("\n  [Enter para voltar]")


def processar_scan():
    cabecalho()
    print("  PROCESSAR SCAN DE PRESENÇA\n")

    op = escolher_restaurante()
    if op not in CONFIGS:
        print("  Opção inválida.")
        input("\n  [Enter para voltar]")
        return

    config_path, nome_aba, slug = CONFIGS[op]

    import lote as lote_mod

    lotes = lote_mod.listar_lotes(slug, limite=15)
    if not lotes:
        print("\n  ERRO: nenhum lote impresso registrado para este restaurante.")
        print("  Gere o template pela opção 1 — é ela que cria o lote.")
        print("  Para folhas impressas antes disso, use: python recuperar_lote.py")
        input("\n  [Enter para voltar]")
        return

    print("\n  Lote impresso que originou este scan:")
    for i, l in enumerate(lotes, start=1):
        estado = " (já processado)" if l["processado"] else ""
        print(f"    {i}. {l['datas'] or l['criado_em'][:10]}  "
              f"{l['total_alunos']} alunos  {l['lote_id']}{estado}")
    escolha = input(f"  Escolha (1-{len(lotes)}): ").strip()
    if not escolha.isdigit() or not (1 <= int(escolha) <= len(lotes)):
        print("  Opção inválida.")
        input("\n  [Enter para voltar]")
        return

    lote_escolhido = lote_mod.carregar_lote(lotes[int(escolha) - 1]["lote_id"])

    # A geometria vem do lote, não do config em disco: o arquivo em configs/ é
    # sobrescrito a cada novo template e pode já não descrever esta folha.
    config = lote_escolhido["config"]

    # Scan principal
    scan = pedir_arquivo([".pdf"], "Arquivo PDF do scan escaneado:")
    if not scan:
        return
    pdfs = [scan]

    # Merge
    print("\n  Há mais PDFs para mesclar? (s/n):")
    if input("  ").strip().lower() == "s":
        print("  Arraste os demais PDFs um por vez. Pressione Enter em branco para terminar.")
        while True:
            extra = limpar(input("  PDF: "))
            if not extra:
                break
            if os.path.isfile(extra) and extra.lower().endswith(".pdf"):
                pdfs.append(extra)
                print(f"    Adicionado: {os.path.basename(extra)}")
            else:
                print(f"    Ignorado (não encontrado ou não é PDF): {extra}")

    print("\n  Processando...")
    try:
        from exportar import (
            carregar_todas_paginas, processar_pdf_completo,
            contar_presencas, exportar_xlsx,
        )
        from ler_bolhas import get_zona_ambigua

        dias = lote_escolhido["dias"]
        dpi = config["scan"]["dpi"]

        paginas = carregar_todas_paginas(*pdfs, dpi=dpi)
        resultados = processar_pdf_completo(paginas, config)

        # Identidade congelada na impressão — nunca a planilha de hoje.
        alunos = lote_mod.roster_do_lote(lote_escolhido)
        contagem = contar_presencas(resultados, dias)

        arquivo_saida = f"presencas_{slug}.xlsx"
        exportar_xlsx(contagem, alunos, dias, arquivo_saida)

        com_presenca = sum(1 for c in contagem if c["presencas"] > 0)
        amb_min, amb_max = get_zona_ambigua(config)
        ambiguos = sum(
            1
            for r in resultados
            for dia in dias
            for tipo in ["almoco", "janta"]
            if amb_min <= r["dias"][dia][f"{tipo}_pct"] <= amb_max
        )

        print()
        linha()
        print(f"  Planilha salva  : {arquivo_saida}")
        print(f"  Lote            : {lote_escolhido['lote_id']}")
        print(f"  Alunos lidos    : {len(resultados)}")
        print(f"  Com presença    : {com_presenca}")
        if ambiguos:
            print(f"  Casos ambíguos  : {ambiguos}  <- revise pela interface web")
        linha()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n  ERRO: {e}")

    input("\n  [Enter para voltar]")


def main():
    while True:
        op = menu_principal()
        if op == "0":
            break
        elif op == "1":
            gerar_template()
        elif op == "2":
            processar_scan()
        else:
            print("  Opção inválida.")
            input("  [Enter para continuar]")

    print("\n  Até logo!\n")


if __name__ == "__main__":
    from utils import stdout_tolerante

    stdout_tolerante()
    main()
