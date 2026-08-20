"""
ordenar_planilha.py

Coloca os períodos de uma aba em ordem cronológica e os dias de cada período
em ordem de semana (Segunda → Domingo), preservando todos os dados.

Serve para consertar abas gravadas fora de ordem sem precisar reescanear.
Exportações novas já fazem isso sozinhas — este script é só para o passivo.

ATENCAO: este script ESCREVE na planilha. Use --simular para ver o que ele
faria sem alterar nada.

Uso:
    python ordenar_planilha.py <restaurante> "<aba>" --simular
    python ordenar_planilha.py <restaurante> "<aba>"

    restaurante : canela | ondina | sao_lazaro
    aba         : ex "Julho 2026"  (padrao: mes atual)

Exemplos:
    python ordenar_planilha.py sao_lazaro "Julho 2026" --simular
    python ordenar_planilha.py sao_lazaro "Julho 2026"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gspread

from google_sheets import (
    _carregar_config, _obter_cliente, _nome_aba_mes, _ler_grupos, _ler_roster,
    _reordenar_grupos, _precisa_reordenar, _periodos_sem_data, _ordenar_dias,
    _formatar_colunas_fixas, _aplicar_formatacao_horizontal,
    _RESTAURANTE_PAI, _LINHA_DADOS,
)


def _col_letra(idx0):
    letra, n = "", idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        letra = chr(65 + r) + letra
    return letra


def _resumir(grupos, titulo):
    print(f"{titulo}:")
    for g in grupos:
        print(f"   {_col_letra(g['col']):>4}  {g['periodo']:<20} "
              f"{', '.join(g['dias']) or '(nenhum dia)'}")


def ordenar(restaurante_key, nome_aba, simular=False):
    config  = _carregar_config()
    efetivo = _RESTAURANTE_PAI.get(restaurante_key, restaurante_key)
    sid     = config.get("restaurantes", {}).get(efetivo, {}).get("spreadsheet_id", "").strip()

    if not sid:
        print(f"ERRO: spreadsheet_id nao configurado para '{efetivo}'")
        return False

    cliente  = _obter_cliente(config)
    planilha = cliente.open_by_key(sid)

    try:
        aba = planilha.worksheet(nome_aba)
    except gspread.WorksheetNotFound:
        print(f"ERRO: aba '{nome_aba}' nao existe em '{planilha.title}'.")
        print(f"Abas disponiveis: {[w.title for w in planilha.worksheets()]}")
        return False

    valores = aba.get_all_values()
    grupos  = _ler_grupos(valores)

    if not grupos:
        print("Nenhum grupo de periodo encontrado nessa aba (layout horizontal).")
        return False

    _resumir(grupos, "ANTES")
    print()

    ilegiveis = _periodos_sem_data(grupos)
    if ilegiveis:
        print("AVISO: nao consegui ler a data de: "
              + ", ".join(repr(p) for p in ilegiveis))
        print("       A ordem dos periodos sera mantida; so os dias serao ajeitados.")
        print()

    if not _precisa_reordenar(grupos):
        print("Ja esta em ordem — nada a fazer.")
        return True

    roster = _ler_roster(valores)
    if not roster:
        print("ERRO: nenhuma linha de aluno encontrada.")
        return False
    linha_fim = roster[-1]["linha"]

    # Previsão do resultado, sem tocar na planilha
    if ilegiveis:
        previsto = list(grupos)
    else:
        from google_sheets import _data_inicio_periodo
        previsto = sorted(grupos, key=lambda g: _data_inicio_periodo(g["periodo"]))

    col, preview = 3, []
    for g in previsto:
        dias = _ordenar_dias(g["dias"])
        preview.append({"col": col, "periodo": g["periodo"], "dias": dias})
        col += 1 + len(dias)

    _resumir(preview, "DEPOIS")
    print()

    if simular:
        print(f"(simulacao — {len(roster)} alunos, ate a linha {linha_fim}; "
              "nada foi alterado)")
        return True

    print(f"Reescrevendo {len(roster)} alunos ate a linha {linha_fim}...")
    novos = _reordenar_grupos(planilha, aba, valores, grupos, linha_fim)

    print("Reaplicando formatacao...")
    try:
        _formatar_colunas_fixas(planilha, aba, linha_fim)
        for g in novos:
            _aplicar_formatacao_horizontal(
                planilha, aba, g["col"], len(g["dias"]), linha_fim
            )
    except Exception as e:
        print(f"AVISO: dados ordenados, mas a formatacao falhou: {e}")

    print("\nConcluido.")
    return True


def main():
    args    = [a for a in sys.argv[1:] if not a.startswith("--")]
    simular = "--simular" in sys.argv

    if not args:
        print(__doc__)
        sys.exit(1)

    restaurante_key = args[0]
    nome_aba = args[1] if len(args) > 1 else _nome_aba_mes()

    print(f"Restaurante : {restaurante_key}")
    print(f"Aba         : {nome_aba}")
    print(f"Modo        : {'SIMULACAO (nao escreve)' if simular else 'ESCRITA'}")
    print("-" * 70)

    try:
        ok = ordenar(restaurante_key, nome_aba, simular)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nERRO: {e}")
        ok = False

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
