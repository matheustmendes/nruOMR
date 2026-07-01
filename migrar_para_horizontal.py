"""
migrar_para_horizontal.py

Migra uma aba existente do layout vertical (períodos empilhados) para o layout
horizontal (períodos como colunas lado a lado), preservando todos os dados.

Uso:
    python migrar_para_horizontal.py <restaurante> [nome_aba]
    python migrar_para_horizontal.py <restaurante> [nome_aba] --reformatar

    restaurante : canela | ondina | sao_lazaro
    nome_aba    : ex "Maio 2026"  (padrão: mês atual)
    --reformatar: re-aplica só a formatação (usa quando dados já estão horizontais)

Exemplos:
    python migrar_para_horizontal.py ondina "Maio 2026"
    python migrar_para_horizontal.py ondina "Maio 2026" --reformatar
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gspread
from google_sheets import (
    _carregar_config, _obter_cliente, _nome_aba_mes, _marcador_periodo,
    _COL_FIXAS, _aplicar_formatacao_horizontal, _formatar_colunas_fixas,
    _RESTAURANTE_PAI,
)


def _conectar(restaurante_key, nome_aba):
    config = _carregar_config()
    restaurante_efetivo = _RESTAURANTE_PAI.get(restaurante_key, restaurante_key)
    rest = config.get("restaurantes", {}).get(restaurante_efetivo, {})
    spreadsheet_id = rest.get("spreadsheet_id", "").strip()
    if not spreadsheet_id:
        raise ValueError(f"spreadsheet_id nao configurado para '{restaurante_efetivo}'")
    cliente = _obter_cliente(config)
    spreadsheet = cliente.open_by_key(spreadsheet_id)
    try:
        aba = spreadsheet.worksheet(nome_aba)
    except gspread.WorksheetNotFound:
        raise ValueError(f"Aba '{nome_aba}' nao encontrada.")
    return spreadsheet, aba


def _unmerge_all(spreadsheet, aba):
    """Remove todas as mesclagens da aba para evitar conflitos."""
    try:
        spreadsheet.batch_update({"requests": [{"unmergeCells": {"range": {
            "sheetId": aba.id,
            "startRowIndex": 0, "endRowIndex": 10000,
            "startColumnIndex": 0, "endColumnIndex": 500,
        }}}]})
    except Exception as e:
        print(f"  Aviso unmerge: {e}")


def _parsear_blocos_verticais(valores):
    """
    Lê a estrutura vertical e retorna lista de blocos.
    Cada bloco: (periodo, dias, {num: {nome, mat, presencas, marcas}})
    """
    blocos = []
    i = 0
    while i < len(valores):
        row = valores[i]
        cell0 = str(row[0]).strip() if row else ""

        if not cell0.startswith("Período: "):
            i += 1
            continue

        periodo = cell0[len("Período: "):]
        i += 1
        if i >= len(valores):
            break

        header = valores[i]
        dias = [str(h).strip() for h in header[4:] if str(h).strip()]
        i += 1

        bloco_data = {}
        while i < len(valores):
            drow = valores[i]
            if not drow or not any(str(c).strip() for c in drow):
                i += 1
                break

            try:
                num = int(str(drow[0]).strip())
            except (ValueError, IndexError):
                i += 1
                continue

            nome = str(drow[1]).strip() if len(drow) > 1 else ""
            mat  = str(drow[2]).strip() if len(drow) > 2 else ""
            try:
                presencas = int(str(drow[3]).strip()) if len(drow) > 3 and str(drow[3]).strip() else 0
            except ValueError:
                presencas = 0

            marcas = {}
            for k, dia in enumerate(dias):
                j = 4 + k
                marcas[dia] = str(drow[j]).strip() if j < len(drow) else ""

            bloco_data[num] = {
                "nome": nome, "mat": mat,
                "presencas": presencas, "marcas": marcas,
            }
            i += 1

        blocos.append((periodo, dias, bloco_data))

    return blocos


def _construir_linhas_horizontais(blocos):
    """Converte blocos verticais em linhas do formato horizontal."""
    if not blocos:
        return [], [], {}, 0

    alunos_map = {}
    for _, _, bloco_data in blocos:
        for num, data in bloco_data.items():
            if num not in alunos_map:
                alunos_map[num] = (data["nome"], data["mat"])

    nums = sorted(alunos_map.keys())
    periodos = [p for p, _, _ in blocos]
    dias_por_periodo = {p: d for p, d, _ in blocos}
    bloco_por_periodo = {p: bd for p, _, bd in blocos}

    row1 = ["", "", ""]
    row2 = ["Nº", "Nome", "Matrícula"]
    for periodo in periodos:
        dias = dias_por_periodo[periodo]
        row1.append(_marcador_periodo(periodo))
        row1.extend([""] * len(dias))
        row2.append("Presenças")
        row2.extend(dias)

    linhas_dados = []
    for num in nums:
        nome, mat = alunos_map[num]
        linha = [num, nome, mat]
        for periodo in periodos:
            dias = dias_por_periodo[periodo]
            data = bloco_por_periodo[periodo].get(num, {})
            linha.append(data.get("presencas", 0))
            marcas = data.get("marcas", {})
            for dia in dias:
                linha.append(marcas.get(dia, ""))
        linhas_dados.append(linha)

    return [row1, row2] + linhas_dados, periodos, dias_por_periodo, len(nums)


def _parsear_horizontal(valores):
    """
    Lê o layout horizontal e retorna [(periodo, col_inicio, dias)].
    Usado pelo --reformatar.
    """
    if not valores or len(valores) < 2:
        return []
    row1 = valores[0]
    row2 = valores[1]
    periodos = []
    for j, v in enumerate(row1):
        if not str(v).strip().startswith("Período: "):
            continue
        periodo = str(v).strip()[len("Período: "):]
        col_inicio = j
        dias = []
        for k in range(col_inicio + 1, len(row1)):
            if str(row1[k]).strip().startswith("Período: "):
                break
            h = str(row2[k]).strip() if k < len(row2) else ""
            if h:
                dias.append(h)
        periodos.append((periodo, col_inicio, dias))
    return periodos


def _aplicar_toda_formatacao(spreadsheet, aba, periodos_info, num_alunos):
    """Aplica formatação completa dado [(periodo, col_inicio, dias)]."""
    print("Removendo mesclagens antigas...")
    _unmerge_all(spreadsheet, aba)

    # Congela colunas/linhas fixas ANTES de criar mesclagens (requisito da API)
    print("Formatando colunas fixas (freeze)...")
    try:
        _formatar_colunas_fixas(spreadsheet, aba, num_alunos)
    except Exception as e:
        print(f"  Aviso colunas fixas: {e}")

    print("Aplicando formatacao por periodo...")
    for periodo, col_inicio, dias in periodos_info:
        try:
            _aplicar_formatacao_horizontal(spreadsheet, aba, col_inicio, len(dias), num_alunos)
            print(f"  {periodo}  OK")
        except Exception as e:
            print(f"  {periodo}  AVISO: {e}")


def reformatar(restaurante_key, nome_aba):
    """Re-aplica formatação a uma aba já no layout horizontal."""
    spreadsheet, aba = _conectar(restaurante_key, nome_aba)

    print(f"Lendo '{nome_aba}'...")
    valores = aba.get_all_values()

    periodos_info = _parsear_horizontal(valores)
    if not periodos_info:
        print("Nenhum periodo encontrado no layout horizontal.")
        return False

    num_alunos = sum(
        1 for r in valores[2:] if r and str(r[0]).strip()
    )

    print(f"Periodos: {[p for p, _, _ in periodos_info]}")
    print(f"Alunos  : {num_alunos}")
    print()

    _aplicar_toda_formatacao(spreadsheet, aba, periodos_info, num_alunos)
    print("\nFormatacao concluida.")
    return True


def migrar(restaurante_key, nome_aba):
    """Converte aba de layout vertical para horizontal."""
    spreadsheet, aba = _conectar(restaurante_key, nome_aba)

    print(f"Lendo '{nome_aba}'...")
    valores = aba.get_all_values()

    if not valores or not any(any(str(c).strip() for c in r) for r in valores):
        print("A aba esta vazia.")
        return False

    row1_0 = str(valores[0][0]).strip() if valores[0] else ""
    if not row1_0.startswith("Período: "):
        if any(str(v).strip().startswith("Período: ") for v in valores[0][3:]):
            print("A aba ja esta no formato horizontal.")
            print("Use --reformatar para reaplicar a formatacao.")
            return False
        print(f"ERRO: formato nao reconhecido (coluna A linha 1 = '{row1_0}')")
        return False

    blocos = _parsear_blocos_verticais(valores)
    if not blocos:
        print("Nenhum bloco vertical encontrado.")
        return False

    print(f"Periodos encontrados ({len(blocos)}):")
    for p, dias, bloco_data in blocos:
        print(f"  {p}  |  {len(bloco_data)} alunos  |  dias: {dias or ['(nenhum)']}")

    todas_linhas, periodos, dias_por_periodo, num_alunos = _construir_linhas_horizontais(blocos)
    print(f"\n{num_alunos} alunos, {len(periodos)} periodos -> layout horizontal")

    print("Limpando aba...")
    aba.clear()

    print("Escrevendo dados...")
    aba.update(values=todas_linhas, range_name="A1", value_input_option="USER_ENTERED")

    periodos_info = [
        (p, _COL_FIXAS + sum(1 + len(dias_por_periodo[periodos[k]]) for k in range(i)), dias_por_periodo[p])
        for i, p in enumerate(periodos)
    ]

    _aplicar_toda_formatacao(spreadsheet, aba, periodos_info, num_alunos)
    print(f"\nMigracao concluida.")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]

    restaurante_key = args[0] if args else None
    nome_aba = args[1] if len(args) > 1 else _nome_aba_mes()
    modo_reformatar = "--reformatar" in flags

    if not restaurante_key:
        print("Uso: python migrar_para_horizontal.py <restaurante> [nome_aba] [--reformatar]")
        sys.exit(1)

    print(f"Restaurante : {restaurante_key}")
    print(f"Aba         : {nome_aba}")
    print(f"Modo        : {'reformatar' if modo_reformatar else 'migrar'}")
    print()

    try:
        if modo_reformatar:
            ok = reformatar(restaurante_key, nome_aba)
        else:
            ok = migrar(restaurante_key, nome_aba)
    except ValueError as e:
        print(f"ERRO: {e}")
        ok = False

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
