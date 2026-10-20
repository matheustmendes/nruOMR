"""
reparar_presencas_mes.py

Regrava a fórmula da coluna D ("Presenças no Mês") nas abas mensais em que ela
virou número fixo — efeito de uma reordenação de períodos feita antes da
correção em `_reordenar_grupos`.

Só mexe na coluna D, e só nas linhas cuja célula difere da fórmula esperada.
Marcações de presença não são tocadas. Idempotente: abas sem divergência
aparecem como "ok".

Uso:
    python reparar_presencas_mes.py <restaurante> [--dry-run]

    restaurante : ondina | sao_lazaro | canela
    --dry-run   : só mostra o que seria reparado, sem gravar nada

Exemplos:
    python reparar_presencas_mes.py ondina --dry-run
    python reparar_presencas_mes.py ondina
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google_sheets import reparar_presencas_mes

_RESTAURANTES_VALIDOS = ("canela", "ondina", "sao_lazaro")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in _RESTAURANTES_VALIDOS:
        print(__doc__)
        sys.exit(1)

    restaurante = args[0]
    dry_run = "--dry-run" in args[1:]

    print(f"Reparando '{restaurante}'{' (dry-run)' if dry_run else ''}...\n")
    resultados = reparar_presencas_mes(restaurante, dry_run=dry_run)

    for r in resultados:
        extra = r.get("motivo") or r.get("erro") or ""
        if not extra and "reparadas" in r:
            extra = f"{r['reparadas']} de {r['alunos']} linhas"
        print(f"  {r['aba']:22s} {r['status']}{' — ' + extra if extra else ''}")

    reparadas = sum(r.get("reparadas", 0) for r in resultados)
    erros = [r for r in resultados if r["status"] == "erro"]
    print(f"\n{reparadas} linha(s) {'a reparar' if dry_run else 'reparada(s)'}.")
    if erros:
        print(f"{len(erros)} erro(s) — ver acima.")
        sys.exit(1)


if __name__ == "__main__":
    main()
