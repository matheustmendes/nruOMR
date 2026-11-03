"""
buscar_alunos.py

Busca os estudantes de cada restaurante direto na planilha "SERVIÇO
ALIMENTAÇÃO" (a mesma configurada em `bolsistas` no config_sheets.yaml),
descarta duplicados e gera a lista pronta para o gerar_template.py — no lugar
do copia-e-cola manual entre planilhas.

Cada aba (ONDINA, CANELA, SÃO LÁZARO) tem uma lista grande no topo e, mais
abaixo, blocos separados por linhas de título ("SERVIÇO ALIMENTAÇÃO
TEMPORÁRIO", "PEC_G", "SAT Extraordinário"...). Esses blocos não importam
aqui: só os nomes. Então toda linha com nome vira estudante, e as linhas de
título são reconhecidas e descartadas.

Uso:
    python buscar_alunos.py ondina
    python buscar_alunos.py canela --saida lista_canela.xlsx
    python buscar_alunos.py todos
"""

import argparse
import os
import re
import unicodedata

from openpyxl import Workbook

import google_sheets as gs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# restaurante_key -> (abas da planilha, nome no cabeçalho da lista)
# Ondina tem duas abas oficiais ("ONDINA" e "Lista Ondina - NOVA"): a lista é a
# soma das duas, sem repetidos. A ordem importa: na repetição vale a primeira.
RESTAURANTES = {
    "ondina": (["ONDINA", "Lista Ondina - NOVA"], "ONDINA"),
    "canela": (["CANELA"], "CANELA"),
    "sao_lazaro": (["SÃO LÁZARO"], "SÃO LÁZARO"),
}

# Linha só com nome (sem matrícula/curso/auxílio/local) que casa com isto é
# separador de bloco, não estudante.
_TITULO = re.compile(
    r"servico alimentacao|^sat\b|\bpec\b|pec[-_ ]?(g|ple)|convenio|gcub"
    r"|manter no|extraordinario|^-+$"
)


def _norm_texto(valor):
    txt = unicodedata.normalize("NFKD", str(valor or ""))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", txt).strip().lower()


def _norm_mat(valor):
    """Alfanumérica em maiúsculas: estrangeiros têm matrícula tipo 'A78950824'."""
    return re.sub(r"[^0-9A-Za-z]", "", str(valor or "")).upper()


def _achar_cabecalho(valores):
    """(linha, col_nome, col_mat, col_curso, col_local) pelo rótulo MATRÍCULA."""
    for i, linha in enumerate(valores[:10]):
        rotulos = [_norm_texto(c) for c in linha]
        if "matricula" in rotulos and "nome" in rotulos:
            return (
                i,
                rotulos.index("nome"),
                rotulos.index("matricula"),
                rotulos.index("curso") if "curso" in rotulos else None,
                rotulos.index("local") if "local" in rotulos else None,
            )
    return None


def _celula(linha, col):
    return linha[col].strip() if col is not None and col < len(linha) else ""


def ler_aba(valores, origem=""):
    """
    Devolve (alunos, titulos_ignorados), sem deduplicar.
    alunos: lista de dicts {nome, matricula, curso, local, linha, aba}.
    """
    cab = _achar_cabecalho(valores)
    if cab is None:
        raise ValueError("cabeçalho NOME/MATRÍCULA não encontrado nas 10 primeiras linhas")
    i_cab, c_nome, c_mat, c_curso, c_local = cab

    alunos, titulos = [], []
    for i in range(i_cab + 1, len(valores)):
        linha = valores[i]
        nome = _celula(linha, c_nome)
        if not nome:
            continue
        mat = _celula(linha, c_mat)
        curso = _celula(linha, c_curso)
        local = _celula(linha, c_local)

        if not (mat or curso or local) and _TITULO.search(_norm_texto(nome)):
            titulos.append((i + 1, nome))
            continue

        alunos.append({
            "nome": re.sub(r"\s+", " ", nome),
            "matricula": mat,
            "curso": curso,
            "local": local,
            "linha": i + 1,
            "aba": origem,
        })
    return alunos, titulos


def deduplicar(alunos):
    """
    Remove repetidos preservando a ordem da planilha.

    Chave principal é a matrícula. Quem não tem matrícula (PEC-PLE, alguns
    cadastros incompletos) cai para o nome normalizado — e também some se o
    mesmo nome já apareceu com matrícula, pra não listar a pessoa duas vezes.
    """
    por_mat, nomes_com_mat = {}, set()
    for a in alunos:
        if _norm_mat(a["matricula"]):
            nomes_com_mat.add(_norm_texto(a["nome"]))

    unicos, duplicados, vistos_nome = [], [], set()
    for a in alunos:
        mat = _norm_mat(a["matricula"])
        nome = _norm_texto(a["nome"])
        if mat:
            if mat in por_mat:
                duplicados.append((a, por_mat[mat]))
                continue
            por_mat[mat] = a
        else:
            if nome in nomes_com_mat or nome in vistos_nome:
                duplicados.append((a, None))
                continue
            vistos_nome.add(nome)
        unicos.append(a)
    return unicos, duplicados


def _achar_aba(abas, nome):
    return next((a for a in abas if _norm_texto(a.title) == _norm_texto(nome)), None)


def buscar(restaurante_key, spreadsheet_id=None, verbose=True):
    """Lê as abas do restaurante e devolve {alunos, duplicados, titulos, ...}."""
    nomes_abas, rotulo = RESTAURANTES[restaurante_key]
    config = gs._carregar_config()
    spreadsheet_id = spreadsheet_id or config.get("bolsistas", {}).get("spreadsheet_id", "").strip()
    if not spreadsheet_id:
        raise ValueError("`bolsistas.spreadsheet_id` não configurado em config_sheets.yaml")

    cliente = gs._obter_cliente(config)
    planilha = gs._com_retry(lambda: cliente.open_by_key(spreadsheet_id))
    abas = gs._com_retry(planilha.worksheets)

    brutos, titulos, por_aba = [], [], {}
    for nome_aba in nomes_abas:
        aba = _achar_aba(abas, nome_aba)
        if aba is None:
            raise ValueError(f"Aba '{nome_aba}' não encontrada na planilha '{planilha.title}'")
        valores = gs._com_retry(aba.get_all_values)
        da_aba, tit = ler_aba(valores, origem=aba.title.strip())
        brutos += da_aba
        titulos += tit
        por_aba[aba.title.strip()] = len(da_aba)

    unicos, duplicados = deduplicar(brutos)
    # A planilha vem em blocos (lista principal, SAT, PEC...), mas a lista
    # impressa é uma só, em ordem alfabética (sem acento, sem diferença de caixa).
    unicos.sort(key=lambda a: (_norm_texto(a["nome"]), _norm_mat(a["matricula"])))

    sem_mat = [a for a in unicos if not _norm_mat(a["matricula"])]
    outro_local = [
        a for a in unicos
        if a["local"] and _norm_texto(a["local"]) != _norm_texto(rotulo)
    ]

    if verbose:
        detalhe = ", ".join(f"'{n}': {q}" for n, q in por_aba.items())
        print(f"[{restaurante_key}] {len(brutos)} linhas com nome ({detalhe}), "
              f"{len(duplicados)} duplicadas, {len(unicos)} estudantes únicos")
        print(f"  {len(titulos)} linha(s) de título ignoradas; "
              f"{len(sem_mat)} sem matrícula; "
              f"{len(outro_local)} com coluna LOCAL diferente da aba")
        for dup, orig in duplicados[:10]:
            ref = f" (já em '{orig['aba']}' linha {orig['linha']})" if orig else ""
            print(f"    duplicado: '{dup['aba']}' linha {dup['linha']} {dup['nome']} {dup['matricula']}{ref}")
        if len(duplicados) > 10:
            print(f"    ... e mais {len(duplicados) - 10}")

    return {
        "restaurante": restaurante_key,
        "alunos": unicos,
        "duplicados": duplicados,
        "titulos": titulos,
        "sem_matricula": sem_mat,
        "outro_local": outro_local,
        "por_aba": por_aba,
    }


def para_tuplas(resultado):
    """Formato (nome, matricula) que o gerar_template.ler_planilha devolve."""
    return [(a["nome"], a["matricula"]) for a in resultado["alunos"]]


def salvar_xlsx(resultado, caminho):
    """
    Grava no layout que gerar_template.ler_planilha lê: título na linha 1,
    estudantes a partir da linha 8, nome na coluna B e matrícula na C.
    """
    _, rotulo = RESTAURANTES[resultado["restaurante"]]
    wb = Workbook()
    ws = wb.active
    ws.title = f"{rotulo} IMPRESSÃO"
    ws["A1"] = f"RELAÇÃO DE BOLSISTAS {rotulo}"
    ws["A7"] = "Nº"
    ws["B7"] = "NOME"
    ws["C7"] = "MATRÍCULA"
    for n, a in enumerate(resultado["alunos"], start=1):
        linha = 7 + n
        ws.cell(linha, 1, n)
        ws.cell(linha, 2, a["nome"])
        ws.cell(linha, 3, a["matricula"]).number_format = "@"
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 16
    wb.save(caminho)
    return caminho


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("restaurante", choices=[*RESTAURANTES, "todos"])
    ap.add_argument("--saida", help="caminho do .xlsx (padrão: lista_<restaurante>.xlsx)")
    args = ap.parse_args()

    for key in (RESTAURANTES if args.restaurante == "todos" else [args.restaurante]):
        res = buscar(key)
        saida = args.saida if args.restaurante != "todos" and args.saida else \
            os.path.join(SCRIPT_DIR, f"lista_{key}.xlsx")
        print(f"  -> {salvar_xlsx(res, saida)}")


if __name__ == "__main__":
    main()
