"""
popular_dashboard.py

Lê os dados históricos das planilhas existentes de cada restaurante
(canela, ondina, sao_lazaro) e os envia para a planilha do dashboard
analítico (Looker Studio).

Os dados de FDS já estão mesclados nos blocos de semana das planilhas,
então cada bloco é enviado como restaurante regular — sem separação.

Uso:
    python popular_dashboard.py
    python popular_dashboard.py --restaurante canela
    python popular_dashboard.py --restaurante canela --periodo "05/05 a 09/05"
    python popular_dashboard.py --dry-run
"""

import sys
import time
import argparse

_RESTAURANTES = ["canela", "ondina", "sao_lazaro"]


def _parsear_bloco(valores, inicio_0idx):
    """
    Extrai (periodo, dias, contagem, alunos) de um bloco de semana.

    Formato esperado na planilha:
        linha inicio_0idx  : "Período: DD/MM a DD/MM"
        linha inicio_0idx+1: "Nº | Nome | Matrícula | Presenças | Dia1 | Dia2 ..."
        linhas seguintes   : dados dos alunos
        linha vazia        : fim do bloco

    Retorna None se o bloco for inválido ou não tiver alunos.
    """
    periodo_row = valores[inicio_0idx]
    if not periodo_row or not periodo_row[0].startswith("Período: "):
        return None

    periodo = periodo_row[0][len("Período: "):]

    if inicio_0idx + 1 >= len(valores):
        return None

    header = valores[inicio_0idx + 1]
    dias = [h.strip() for h in header[4:] if h.strip()]
    if not dias:
        return None

    contagem = []
    alunos_map = {}

    for row in valores[inicio_0idx + 2:]:
        if not any(cell.strip() for cell in row):
            break

        try:
            numero = int(row[0])
        except (ValueError, IndexError):
            continue

        nome      = row[1].strip() if len(row) > 1 else ""
        matricula = row[2].strip() if len(row) > 2 else ""

        try:
            presencas = int(row[3]) if len(row) > 3 and row[3].strip() else 0
        except ValueError:
            presencas = 0

        detalhes = {}
        for i, dia in enumerate(dias):
            col = 4 + i
            val = row[col].strip() if col < len(row) else ""
            detalhes[dia] = {
                "presente": bool(val),
                "almoco":   "A" in val,
                "janta":    "J" in val,
            }

        contagem.append({"numero": numero, "presencas": presencas, "detalhes": detalhes})
        alunos_map[numero] = (nome, matricula)

    if not contagem:
        return None

    max_num = max(c["numero"] for c in contagem)
    alunos  = [alunos_map.get(i + 1, ("", "")) for i in range(max_num)]

    return periodo, dias, contagem, alunos


def _blocos_de_aba(valores):
    """Gera o índice 0-based de cada linha 'Período:' em uma aba."""
    for i, row in enumerate(valores):
        if row and row[0].startswith("Período: "):
            yield i


def _parsear_grupos_horizontais(valores):
    """
    Extrai [(periodo, dias, contagem, alunos)] do layout horizontal.

        Linha 1:  (vazio A:C)       | Período: 05/05 a 09/05      | Período: ...
        Linha 2:  Nº | Nome | Matr. | Presenças | Seg | ... | Sex | Presenças | ...
        Linha 3+: uma linha por aluno
    """
    if len(valores) < 3:
        return []

    row1 = [str(c).strip() for c in valores[0]]
    row2 = [str(c).strip() for c in valores[1]]

    # A coluna A com "Período:" é do layout vertical antigo, não um grupo
    inicios = [j for j, v in enumerate(row1) if j >= 3 and v.startswith("Período: ")]
    if not inicios:
        return []

    alunos = []
    linhas_alunos = []
    for row in valores[2:]:
        nome = str(row[1]).strip() if len(row) > 1 else ""
        mat  = str(row[2]).strip() if len(row) > 2 else ""
        if not nome and not mat:
            break
        alunos.append((nome, mat))
        linhas_alunos.append(row)

    if not alunos:
        return []

    grupos = []
    for k, col in enumerate(inicios):
        fim = inicios[k + 1] if k + 1 < len(inicios) else max(len(row1), len(row2))
        periodo = row1[col][len("Período: "):].strip()
        cols_dias = [
            (row2[c], c) for c in range(col + 1, min(fim, len(row2))) if row2[c]
        ]
        dias = [d for d, _ in cols_dias]

        contagem = []
        for i, row in enumerate(linhas_alunos):
            valor_pres = str(row[col]).strip() if col < len(row) else ""
            marcas = {
                dia: (str(row[c]).strip() if c < len(row) else "")
                for dia, c in cols_dias
            }
            if not valor_pres and not any(marcas.values()):
                continue  # aluno não lido nesse período

            try:
                presencas = int(valor_pres) if valor_pres else 0
            except ValueError:
                presencas = 0

            contagem.append({
                "numero":    i + 1,
                "presencas": presencas,
                "detalhes": {
                    dia: {
                        "presente": bool(val),
                        "almoco":   "A" in val,
                        "janta":    "J" in val,
                    }
                    for dia, val in marcas.items()
                },
            })

        if contagem:
            grupos.append((periodo, dias, contagem, alunos))

    return grupos


def _periodos_da_aba(valores):
    """Extrai [(periodo, dias, contagem, alunos)] de qualquer um dos dois layouts."""
    if valores and any(
        str(c).strip().startswith("Período: ") for c in valores[0][3:]
    ):
        return _parsear_grupos_horizontais(valores)

    periodos = []
    for inicio_0idx in _blocos_de_aba(valores):
        parsed = _parsear_bloco(valores, inicio_0idx)
        if parsed is not None:
            periodos.append(parsed)
    return periodos


def popular_restaurante(restaurante_key, filtro_periodo=None, dry_run=False):
    from google_sheets import _carregar_config, _obter_cliente, exportar_para_dashboard

    config        = _carregar_config()
    rest          = config.get("restaurantes", {}).get(restaurante_key, {})
    spreadsheet_id = rest.get("spreadsheet_id", "").strip()

    if not spreadsheet_id:
        print(f"  [SKIP] spreadsheet_id não configurado para '{restaurante_key}'")
        return 0, 0

    cliente     = _obter_cliente(config)
    spreadsheet = cliente.open_by_key(spreadsheet_id)

    ok = erro = 0

    for aba in spreadsheet.worksheets():
        valores = aba.get_all_values()

        for periodo, dias, contagem, alunos in _periodos_da_aba(valores):
            if filtro_periodo and periodo != filtro_periodo:
                continue

            if dry_run:
                print(f"  DRY [{aba.title}] {periodo}  ({len(contagem)} alunos, dias: {', '.join(dias)})")
                ok += 1
                continue

            r = exportar_para_dashboard(contagem, alunos, dias, restaurante_key, periodo)
            if r.get("ok"):
                print(f"  OK  [{aba.title}] {periodo}")
                ok += 1
            else:
                print(f"  ERR [{aba.title}] {periodo} — {r.get('erro')}")
                erro += 1

            time.sleep(1)  # evita rate limit da API (~300 req/min)

    return ok, erro


def main():
    parser = argparse.ArgumentParser(
        description="Popula o dashboard Looker Studio com dados históricos das planilhas."
    )
    parser.add_argument(
        "--restaurante", choices=_RESTAURANTES,
        help="Processa apenas este restaurante (padrão: todos)"
    )
    parser.add_argument(
        "--periodo",
        help='Filtra por período específico, ex: "05/05 a 09/05"'
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Lista os blocos que seriam exportados sem gravar nada"
    )
    args = parser.parse_args()

    restaurantes = [args.restaurante] if args.restaurante else _RESTAURANTES
    total_ok = total_erro = 0

    for r in restaurantes:
        print(f"\n=== {r} ===")
        ok, erro = popular_restaurante(r, filtro_periodo=args.periodo, dry_run=args.dry_run)
        total_ok  += ok
        total_erro += erro

    sufixo = " (dry run)" if args.dry_run else ""
    print(f"\nTotal{sufixo}: {total_ok} exportados, {total_erro} erros")


if __name__ == "__main__":
    main()
