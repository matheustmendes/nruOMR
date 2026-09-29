"""
migrar_colunas_extras.py

Adiciona, em todas as abas mensais já existentes de um restaurante, as duas
colunas fixas novas:

    D — "Presenças no Mês"      : fórmula, soma automática de todos os
                                   períodos já lançados naquele mês
    E — "Justificou Ausência?"  : dropdown (Sim / Não)

Não mexe em nenhum dado de presença já lançado — só insere as 2 colunas e
preenche a fórmula/cabeçalho. Idempotente: pode rodar de novo sem duplicar
nada (abas já migradas são puladas).

Uso:
    python migrar_colunas_extras.py <restaurante> [--dry-run]

    restaurante : ondina | sao_lazaro | canela
    --dry-run   : só mostra o que seria feito, sem gravar nada

Exemplos:
    python migrar_colunas_extras.py ondina --dry-run
    python migrar_colunas_extras.py ondina
    python migrar_colunas_extras.py sao_lazaro
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google_sheets import migrar_colunas_extras

_RESTAURANTES_VALIDOS = ("canela", "ondina", "sao_lazaro")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in _RESTAURANTES_VALIDOS:
        print(__doc__)
        sys.exit(1)

    restaurante = args[0]
    dry_run = "--dry-run" in args[1:]

    print(f"Migrando '{restaurante}'{' (dry-run)' if dry_run else ''}...\n")
    resultados = migrar_colunas_extras(restaurante, dry_run=dry_run)

    for r in resultados:
        extra = r.get("motivo") or r.get("erro") or ""
        alunos = f", {r['alunos']} alunos" if "alunos" in r else ""
        sufixo = f" — {extra}" if extra else alunos
        print(f"  {r['aba']:22s} {r['status']}{sufixo}")

    migradas = sum(1 for r in resultados if r["status"] in ("migrada", "seria migrada"))
    erros = [r for r in resultados if r["status"] == "erro"]
    print(f"\n{migradas} aba(s) migrada(s) de {len(resultados)}.")
    if erros:
        print(f"{len(erros)} erro(s) — ver acima.")
        sys.exit(1)


if __name__ == "__main__":
    main()
