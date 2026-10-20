"""
padronizar_planilhas.py

Leva as abas mensais de um restaurante ao layout de referência (planilha da
Ondina):

    A Nº | B Nome | C Matrícula | D Presenças no Mês | E Justificou Ausência? |
    F Justificativa | G Quantidade de dias justificados | H... períodos

- D, E e F viram fórmula em todas as linhas de aluno (E/F com limite de 10
  presenças: 10 é frequente, 9 não).
- Abas com menos colunas fixas ganham as que faltam; G (digitada à mão) e as
  marcações de presença não são tocadas — e as marcações são conferidas contra
  a leitura anterior; se algo mudar, a aba acusa erro.
- O conteúdo anterior das linhas de aluno é salvo em JSON antes de alterar
  (pasta backups_padronizacao/ por padrão).

Idempotente. Substitui o antigo migrar_colunas_extras.py.

Uso:
    python padronizar_planilhas.py <restaurante> [--dry-run] [--backup DIR]

    restaurante : ondina | sao_lazaro | canela
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from google_sheets import padronizar_planilhas

_RESTAURANTES_VALIDOS = ("canela", "ondina", "sao_lazaro")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in _RESTAURANTES_VALIDOS:
        print(__doc__)
        sys.exit(1)

    restaurante = args[0]
    dry_run = "--dry-run" in args[1:]
    dir_backup = "backups_padronizacao"
    if "--backup" in args:
        dir_backup = args[args.index("--backup") + 1]

    print(f"Padronizando '{restaurante}'{' (dry-run)' if dry_run else ''}...\n")
    resultados = padronizar_planilhas(
        restaurante, dry_run=dry_run, dir_backup=None if dry_run else dir_backup
    )

    for r in resultados:
        extra = r.get("motivo") or r.get("erro") or ""
        if not extra and "alunos" in r:
            extra = f"{r['alunos']} alunos, +{r['colunas_novas']} coluna(s)"
        print(f"  {r['aba']:22s} {r['status']}{' — ' + extra if extra else ''}")

    erros = [r for r in resultados if r["status"] == "erro"]
    if erros:
        print(f"\n{len(erros)} erro(s) — ver acima.")
        sys.exit(1)


if __name__ == "__main__":
    main()
