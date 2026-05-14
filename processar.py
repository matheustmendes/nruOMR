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
        from gerar_template import ler_planilha, gerar_template as _gerar, gerar_config, CONFIG_ABAS
        import unicodedata

        info = ler_planilha(xlsx, nome_aba)
        print(f"  {len(info['alunos'])} alunos encontrados.")

        nome_pdf = f"template_{slug}.pdf"
        os.makedirs("configs", exist_ok=True)
        nome_config = f"configs/config_{slug}.yaml"

        resultado = _gerar(info, dias, nome_pdf)
        gerar_config(info, dias, resultado, nome_config)

        print()
        linha()
        print(f"  PDF gerado    : {nome_pdf}")
        print(f"  Config gerado : {nome_config}")
        linha()

    except Exception as e:
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

    if not os.path.isfile(config_path):
        print(f"\n  ERRO: Config não encontrado: {config_path}")
        print("  Gere o template primeiro (opção 1).")
        input("\n  [Enter para voltar]")
        return

    # Carrega config
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

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

    # Planilha
    xlsx = pedir_arquivo([".xlsx"], "Planilha de alunos (.xlsx exportado do Google Sheets):")
    if not xlsx:
        return

    # Página de início
    pagina_inicio = 1
    print(f"\n  Página de início do formulário? (Enter = 1)")
    print("  Use outro valor se o scan não começa pela primeira página.")
    pi = input("  Página de início: ").strip()
    if pi.isdigit() and int(pi) >= 1:
        pagina_inicio = int(pi)

    print("\n  Processando...")
    try:
        from exportar import (
            carregar_todas_paginas, processar_pdf_completo,
            contar_presencas, ler_nomes_alunos, exportar_xlsx,
        )

        dias = config["layout"]["dias"]
        dpi = config["scan"]["dpi"]

        paginas = carregar_todas_paginas(*pdfs, dpi=dpi)
        resultados = processar_pdf_completo(paginas, config, pagina_inicio=pagina_inicio)

        alunos = ler_nomes_alunos(xlsx, nome_aba)
        contagem = contar_presencas(resultados, dias)

        arquivo_saida = f"presencas_{slug}.xlsx"
        exportar_xlsx(contagem, alunos, dias, arquivo_saida)

        com_presenca = sum(1 for c in contagem if c["presencas"] > 0)
        _thr = config.get("scan", {}).get("threshold", 0.40)
        _mb = config.get("scan", {}).get("ambiguo_margem_abaixo", 0.04)
        _ma = config.get("scan", {}).get("ambiguo_margem_acima", 0.10)
        ambiguos = sum(
            1
            for r in resultados
            for dia in dias
            for tipo in ["almoco", "janta"]
            if (_thr - _mb) <= r["dias"][dia][f"{tipo}_pct"] <= (_thr + _ma)
        )

        print()
        linha()
        print(f"  Planilha salva  : {arquivo_saida}")
        print(f"  Alunos lidos    : {len(resultados)}")
        print(f"  Com presença    : {com_presenca}")
        if ambiguos:
            print(f"  Casos ambíguos  : {ambiguos}  ← verifique com debug.py")
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
    main()
