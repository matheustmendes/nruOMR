"""
diagnosticar_planilha.py

Mostra o que o código enxerga numa aba do Google Sheets: quais grupos de
período foram detectados, que data foi lida de cada um, se estão em ordem
cronológica e onde um novo período seria inserido.

SOMENTE LEITURA. O acesso é pedido com o escopo `spreadsheets.readonly`, então
a própria credencial usada aqui não tem permissão de escrever — mesmo que
houvesse um bug, o Google recusaria qualquer alteração.

Uso:
    python diagnosticar_planilha.py <restaurante>
    python diagnosticar_planilha.py <restaurante> "Agosto 2026"
    python diagnosticar_planilha.py <restaurante> "Agosto 2026" --periodo "03/08 a 07/08"

    restaurante : canela | ondina | sao_lazaro
    --periodo   : simula o que aconteceria ao exportar esse período
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gspread

# Só funções puras (operam sobre listas já lidas) — nenhuma delas escreve.
from google_sheets import (
    SCRIPT_DIR, _carregar_config, _nome_aba_mes, _ler_grupos,
    _data_inicio_periodo, _posicao_cronologica, _grupos_fora_de_ordem,
    _proxima_col_periodo, _localizar_grupo_semana_fds, _ler_roster,
    _RESTAURANTE_PAI, _PREFIXO_PERIODO,
)

# Escopo de leitura apenas: este script não pode alterar a planilha.
_SCOPES_LEITURA = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _cliente_somente_leitura(config):
    """Autentica com permissão de leitura — a API recusa qualquer escrita."""
    from google.oauth2.service_account import Credentials

    creds_path = config.get("credentials", "credentials_sheets.json")
    if not os.path.isabs(creds_path):
        creds_path = os.path.join(SCRIPT_DIR, creds_path)
    if not os.path.exists(creds_path):
        raise FileNotFoundError(f"Credenciais nao encontradas: {creds_path}")

    creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES_LEITURA)
    return gspread.authorize(creds)


def _col_letra(idx0):
    """0 -> A, 25 -> Z, 26 -> AA"""
    letra = ""
    n = idx0 + 1
    while n:
        n, r = divmod(n - 1, 26)
        letra = chr(65 + r) + letra
    return letra


def diagnosticar(restaurante_key, nome_aba, periodo_teste=None):
    config = _carregar_config()
    efetivo = _RESTAURANTE_PAI.get(restaurante_key, restaurante_key)
    rest = config.get("restaurantes", {}).get(efetivo, {})
    sid = rest.get("spreadsheet_id", "").strip()

    if not sid:
        print(f"ERRO: spreadsheet_id nao configurado para '{efetivo}'")
        return False

    cliente = _cliente_somente_leitura(config)
    planilha = cliente.open_by_key(sid)

    print("Modo        : SOMENTE LEITURA (escopo spreadsheets.readonly)")
    print(f"Planilha    : {planilha.title}")
    print(f"Abas        : {[w.title for w in planilha.worksheets()]}")
    print()

    try:
        aba = planilha.worksheet(nome_aba)
    except gspread.WorksheetNotFound:
        print(f"ERRO: aba '{nome_aba}' nao existe nessa planilha.")
        return False

    valores = aba.get_all_values()
    print(f"Aba         : {aba.title}")
    print(f"Grade       : {aba.row_count} linhas x {aba.col_count} colunas")
    print(f"Preenchido  : {len(valores)} linhas x "
          f"{max((len(r) for r in valores), default=0)} colunas")
    print()

    # --- Linhas 1 e 2 cruas ---
    for n, rotulo in ((0, "Linha 1 (titulos de periodo)"),
                      (1, "Linha 2 (cabecalhos)")):
        print(rotulo)
        if n >= len(valores):
            print("   (vazia)")
        else:
            for j, v in enumerate(valores[n]):
                if str(v).strip():
                    print(f"   {_col_letra(j):>3} = {v!r}")
        print()

    # --- Layout detectado ---
    marcador_col_a = bool(valores) and str(valores[0][0]).strip().startswith(_PREFIXO_PERIODO)
    grupos = _ler_grupos(valores)

    if marcador_col_a:
        print("LAYOUT      : VERTICAL (formato antigo, 'Periodo:' na coluna A)")
        print("              A exportacao vai criar o layout horizontal a partir")
        print("              da coluna D, ao lado dos dados antigos.")
        print()

    if not grupos:
        print("Nenhum grupo de periodo horizontal encontrado.")
        print("Se a aba deveria ter dados, confira se o titulo esta escrito")
        print(f"exatamente como {_PREFIXO_PERIODO!r} seguido do periodo, da coluna D em diante.")
        print()
    else:
        print(f"GRUPOS DETECTADOS ({len(grupos)}):")
        print(f"   {'col':>4}  {'periodo':<20} {'data lida':<12} {'larg':>4}  dias")
        for g in grupos:
            try:
                data = _data_inicio_periodo(g["periodo"]).strftime("%d/%m/%Y")
            except Exception as e:
                data = f"ILEGIVEL ({e})"
            print(f"   {_col_letra(g['col']):>4}  {g['periodo']:<20} {data:<12} "
                  f"{g['largura']:>4}  {', '.join(g['dias']) or '(nenhum)'}")
        print()

        datas_ok = True
        for g in grupos:
            try:
                _data_inicio_periodo(g["periodo"])
            except Exception:
                datas_ok = False

        if not datas_ok:
            print("ORDEM       : nao verificavel — algum periodo tem data ilegivel.")
            print("              A reordenacao automatica fica DESLIGADA nesse caso.")
        elif _grupos_fora_de_ordem(grupos):
            ordem = sorted(grupos, key=lambda g: _data_inicio_periodo(g["periodo"]))
            print("ORDEM       : FORA DE ORDEM")
            print("              A proxima exportacao nessa aba vai reordenar para:")
            print("              " + "  ->  ".join(g["periodo"] for g in ordem))
        else:
            print("ORDEM       : cronologica, OK")
        print()

    # --- Alunos ---
    roster = _ler_roster(valores)
    print(f"ALUNOS      : {len(roster)} linhas"
          + (f" (linhas {roster[0]['linha']} a {roster[-1]['linha']})" if roster else ""))
    if roster:
        print(f"              primeiro: {roster[0]['nome']!r} / {roster[0]['mat']!r}")
        print(f"              ultimo  : {roster[-1]['nome']!r} / {roster[-1]['mat']!r}")
    print()

    # --- Simulação de uma exportação ---
    if periodo_teste:
        print(f"SIMULACAO   : exportar o periodo {periodo_teste!r}")
        try:
            data = _data_inicio_periodo(periodo_teste)
            print(f"              data lida: {data.strftime('%d/%m/%Y')}")
        except Exception as e:
            print(f"              ERRO ao ler a data: {e}")
            print("              -> sem data, o periodo seria acrescentado no FIM")
            print("                 (sem ordenacao). Digite no formato '03/08 a 07/08'.")
            return True

        print(f"              aba de destino: {_nome_aba_mes(periodo_teste)!r}")

        existente = next((g for g in grupos if g["periodo"] == periodo_teste), None)
        if existente:
            print(f"              ja existe na coluna {_col_letra(existente['col'])} "
                  "-> seria DUPLICADO (a menos que force)")
            return True

        fds = _localizar_grupo_semana_fds(grupos, periodo_teste)
        if fds:
            print(f"              como FDS, entraria no grupo {fds['periodo']!r} "
                  f"(coluna {_col_letra(fds['col'])})")

        col = _posicao_cronologica(grupos, periodo_teste)
        if col is None:
            col = _proxima_col_periodo(valores)
            print(f"              e o periodo mais recente -> vai para o FIM, "
                  f"coluna {_col_letra(col)}")
        else:
            print(f"              seria INSERIDO na coluna {_col_letra(col)}, "
                  "empurrando os periodos seguintes para a direita")
        print()

    return True


def main():
    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print(__doc__)
        sys.exit(1)

    restaurante_key = args[0]
    nome_aba = args[1] if len(args) > 1 else _nome_aba_mes()

    periodo_teste = None
    for i, f in enumerate(sys.argv):
        if f == "--periodo" and i + 1 < len(sys.argv):
            periodo_teste = sys.argv[i + 1]
        elif f.startswith("--periodo="):
            periodo_teste = f.split("=", 1)[1]

    print(f"Restaurante : {restaurante_key}")
    print(f"Aba pedida  : {nome_aba}")
    print("-" * 70)

    try:
        ok = diagnosticar(restaurante_key, nome_aba, periodo_teste)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nERRO: {e}")
        ok = False

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
