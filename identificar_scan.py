"""
identificar_scan.py

Descobre, a partir do próprio arquivo, de que unidade e de que semana é cada
scan — sem depender de nome de arquivo, data de modificação ou memória.

Como funciona
-------------
A informação está impressa em toda página da folha, porque o gerador de
template a coloca no cabeçalho:

    Pró-Reitoria de Assistência Estudantil
    RELAÇÃO DE BOLSISTAS ONDINA
    JUNHO 2026  |  29/06 a 04/07
    Página 1 de 26

Então o arquivo se identifica sozinho. Duas formas de ler, nesta ordem:

  1. Camada de texto. Scanners costumam gravar o OCR dentro do PDF. Quando
     existe, é grátis e boa o bastante — mesmo com os erros típicos de OCR de
     scanner ("RELAQAO", "PROAE"), as datas e o nome da unidade sobrevivem.

  2. OCR do cabeçalho. Se não houver camada de texto, a faixa superior da
     primeira página é recortada e passada pelo Tesseract. Não precisa de
     alinhamento por marcadores: o texto do cabeçalho é grande e a posição
     varia pouco.

O "Página X de Y" ainda dá um brinde importante: comparando Y com o número de
páginas do arquivo, dá para saber se o scan está **incompleto** — folha que
não passou no alimentador é exatamente o tipo de falha silenciosa que some
com 25 pessoas de uma vez.

Atenção ao mês
--------------
O nome do mês impresso no cabeçalho **não é confiável**: um bug antigo do
gerador imprimiu folhas de junho com "MAIO" no título. A data dd/mm sempre
esteve correta, e é só ela que este módulo usa para decidir qualquer coisa. O
mês fica registrado apenas como diagnóstico — quando ele discorda das datas, a
folha é sinalizada, o que de quebra mapeia quais impressões saíram com o
título errado.

Uso:
    python identificar_scan.py <pasta_ou_arquivo>
    python identificar_scan.py <pasta> --csv identificacao.csv
    python identificar_scan.py <pasta> --sem-ocr      # só camada de texto
"""

import os
import re
import sys
import csv
import glob

import lote as lote_mod

# Variantes que o OCR de scanner produz para os nomes das unidades.
_UNIDADES = {
    "ondina":     ["ONDINA"],
    "canela":     ["CANELA", "PDCA"],
    "sao_lazaro": ["SAO LAZARO", "S LAZARO", "SLAZARO", "PDSL", "LAZARO"],
}

_DIAS_CONHECIDOS = [
    "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo",
]

_RE_DATA = re.compile(r"\b(\d{2})/(\d{2})\b")

# O cabeçalho escreve o período como um intervalo ("29/06 a 04/07",
# "DATA: 29/06 - 04/07") ou como um dia só ("DATA: 09/05"). Procurar qualquer
# par de datas soltas no texto pega lixo do corpo da página e produz períodos
# impossíveis como "12/03 a 16/01" — visto de verdade nesta pasta.
_RE_INTERVALO = re.compile(
    r"(\d{2}/\d{2})\s*(?:a|A|-|–|—|至|至)\s*(\d{2}/\d{2})"
)
_RE_DIA_UNICO = re.compile(r"DATA\s*[:.]?\s*(\d{2}/\d{2})", re.IGNORECASE)
_RE_PAGINA = re.compile(r"P[AÁ]GINA\s+(\d+)\s+DE\s+(\d+)")
_RE_MES = re.compile(
    r"\b(JANEIRO|FEVEREIRO|MAR[CÇ]O|ABRIL|MAIO|JUNHO|JULHO|AGOSTO|"
    r"SETEMBRO|OUTUBRO|NOVEMBRO|DEZEMBRO)\b.{0,12}?(\d{4})"
)


# --- Extração do cabeçalho --------------------------------------------------

def _texto_do_pdf(caminho, max_paginas=2):
    """
    Camada de texto das primeiras páginas COM conteúdo, se houver.

    Pula o verso em branco do duplex ao contar `max_paginas` — um arquivo de
    redigitalização que começa com 1-2 costas em branco (comum: a página
    ímpar da frente foi escaneada, a par de trás ficou em branco) faria a
    janela `[:max_paginas]` cair inteira nelas, e o cabeçalho (nome da
    unidade, período) nunca seria encontrado mesmo estando presente páginas
    à frente. Achado com um arquivo real de correção do Canela.
    """
    from pypdf import PdfReader

    try:
        leitor = PdfReader(caminho)
    except Exception:
        return "", 0

    partes = []
    for pagina in leitor.pages:
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            texto = ""
        if not texto.strip():
            continue
        partes.append(texto)
        if len(partes) >= max_paginas:
            break
    return "\n".join(partes), len(leitor.pages)


def _texto_por_ocr(caminho, dpi=150):
    """
    OCR da faixa superior da primeira página.

    Recorta os 30% de cima: é onde mora todo o cabeçalho, e cortar o resto
    evita que o Tesseract se perca nas centenas de nomes e círculos abaixo.
    """
    import cv2
    import numpy as np
    from utils import converter_pdf, obter_pytesseract

    pytesseract = obter_pytesseract()
    if pytesseract is None:
        return ""

    try:
        paginas = converter_pdf(caminho, dpi=dpi)
    except Exception:
        return ""
    if not paginas:
        return ""

    img = cv2.cvtColor(np.array(paginas[0]), cv2.COLOR_RGB2BGR)
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faixa = cinza[: int(cinza.shape[0] * 0.30), :]

    _, binario = cv2.threshold(faixa, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    try:
        return pytesseract.image_to_string(binario, config="--psm 6 --oem 3")
    except Exception:
        return ""


def _data_valida(valor):
    try:
        dia, mes = valor.split("/")
        return 1 <= int(dia) <= 31 and 1 <= int(mes) <= 12
    except (ValueError, AttributeError):
        return False


def _extrair_datas(texto):
    """
    Datas do período, na ordem de confiança do formato encontrado.

    Um intervalo explícito é o que o cabeçalho imprime, então vence. Depois,
    "DATA: dd/mm" de folha de um dia só. Datas soltas no texto são o último
    recurso e só entram quando não há mais nada — no corpo da página aparecem
    números que o OCR confunde com data.
    """
    for a, b in _RE_INTERVALO.findall(texto):
        if _data_valida(a) and _data_valida(b):
            return [a, b] if a != b else [a]

    unicas = [d for d in _RE_DIA_UNICO.findall(texto) if _data_valida(d)]
    if unicas:
        return [unicas[0]]

    soltas = []
    for dia, mes in _RE_DATA.findall(texto):
        valor = f"{dia}/{mes}"
        if _data_valida(valor) and valor not in soltas:
            soltas.append(valor)
    return soltas[:2]


def _paginas_do_texto(caminho):
    """
    Números de página do lote presentes no arquivo, lidos da camada de texto.

    Devolve a lista na ordem física do arquivo, restrita às páginas com
    conteúdo — o verso em branco do duplex é descartado, nunca vira um `None`
    a ser preenchido por extrapolação. Sem esse filtro, `_inferir_numeros_
    paginas` trata a folha em branco como "número ilegível" e estica a
    sequência pra ela: um arquivo com páginas reais 18–23 mais duas costas em
    branco virava, na extrapolação, "18–25" — duas páginas que não existem.
    O mesmo raciocínio que `exportar.eh_pagina_branca` já aplica no
    processamento de verdade, aqui feito sobre texto em vez de imagem.

    Returns:
        (numeros, total, paginas_com_conteudo) — `numeros` tem um item por
        página NÃO em branco (None onde o número não foi lido); `total` é o
        "de Y" do rodapé, quando encontrado; `paginas_com_conteudo` é a
        contagem dessas páginas — a base certa para julgar se a cobertura
        ficou completa, não o total bruto do PDF (que inclui as brancas).
    """
    from pypdf import PdfReader

    try:
        leitor = PdfReader(caminho)
    except Exception:
        return [], None, 0

    numeros, total = [], None
    for pagina in leitor.pages:
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            texto = ""
        if not texto.strip():
            continue

        m = _RE_PAGINA.search(lote_mod.norm_nome(texto))
        if m:
            numeros.append(int(m.group(1)))
            total = total or int(m.group(2))
        else:
            numeros.append(None)
    return numeros, total, len(numeros)


def _interpretar(texto):
    """Extrai unidade, datas, mês/ano, dias e paginação de um texto de cabeçalho."""
    plano = lote_mod.norm_nome(texto.replace("\n", " "))

    restaurante = None
    for chave, apelidos in _UNIDADES.items():
        if any(a in plano for a in apelidos):
            restaurante = chave
            break

    datas = _extrair_datas(texto)

    # Diagnóstico apenas: o mês impresso pode estar errado (ver docstring do
    # módulo). Nada aqui decide com base nele.
    mes_ano = ""
    m = _RE_MES.search(plano)
    if m:
        mes_ano = f"{m.group(1).capitalize()} {m.group(2)}"

    total_lote = None
    m = _RE_PAGINA.search(plano)
    if m:
        total_lote = int(m.group(2))

    dias = [d for d in _DIAS_CONHECIDOS if lote_mod.norm_nome(d) in plano]

    return {
        "restaurante": restaurante,
        "datas": datas,
        "mes_ano": mes_ano,
        "total_paginas_lote": total_lote,
        "dias": dias,
    }


_MESES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def _mes_divergente(mes_ano, datas):
    """
    Nome do mês correto segundo as datas, quando ele discorda do título.

    Devolve None quando batem (ou quando não há como comparar). Serve só para
    sinalizar as folhas impressas com o título errado — nenhuma decisão do
    sistema depende disso.
    """
    if not mes_ano or not datas:
        return None
    try:
        mes_das_datas = int(datas[0].split("/")[1])
    except (ValueError, IndexError):
        return None

    correto = _MESES_PT[mes_das_datas - 1]
    impresso = lote_mod.norm_nome(mes_ano.split()[0])
    return None if lote_mod.norm_nome(correto) == impresso else correto


def identificar(caminho, usar_ocr=True):
    """
    Identifica um único scan.

    Returns:
        dict com arquivo, restaurante, datas, periodo_sugerido, mes_ano, dias,
        paginas_pdf, total_paginas_lote, paginas_faltando, fonte e avisos.

        `periodo_sugerido` vem sempre das datas; `mes_ano` é informativo e pode
        estar errado na folha impressa.
    """
    texto, paginas_pdf = _texto_do_pdf(caminho)
    dados = _interpretar(texto)
    fonte = "texto"

    # Que páginas do lote este arquivo contém. Um maço é digitalizado em
    # partes ("1 a 10", "11 a 23"), então nenhum arquivo sozinho costuma ser o
    # scan completo — a cobertura só faz sentido somada entre os arquivos da
    # mesma semana.
    numeros_pagina, total_pelo_texto, paginas_com_conteudo = _paginas_do_texto(caminho)
    if total_pelo_texto and not dados["total_paginas_lote"]:
        dados["total_paginas_lote"] = total_pelo_texto

    from exportar import _inferir_numeros_paginas

    inferidos, _ = _inferir_numeros_paginas(numeros_pagina)
    cobertas = inferidos if inferidos else [n for n in numeros_pagina if n]
    paginas_lote = tuple(sorted(set(cobertas)))
    # A comparação é contra as páginas COM conteúdo, não contra `paginas_pdf`
    # (que inclui o verso em branco do duplex) — senão um arquivo perfeitamente
    # coberto nunca bateria a igualdade só por ter costas em branco no meio.
    cobertura_confiavel = bool(inferidos) and len(paginas_lote) == paginas_com_conteudo

    # Sem unidade ou sem data, a camada de texto não serviu — tenta o OCR.
    if usar_ocr and not (dados["restaurante"] and dados["datas"]):
        texto_ocr = _texto_por_ocr(caminho)
        if texto_ocr:
            dados_ocr = _interpretar(texto_ocr)
            # Aproveita de cada fonte o que ela conseguiu ler.
            for campo in ("restaurante", "mes_ano", "total_paginas_lote"):
                dados[campo] = dados[campo] or dados_ocr[campo]
            dados["datas"] = dados["datas"] or dados_ocr["datas"]
            dados["dias"] = dados["dias"] or dados_ocr["dias"]
            fonte = "texto+ocr" if texto.strip() else "ocr"

    avisos = []
    if not dados["restaurante"]:
        avisos.append("unidade não identificada")
    if not dados["datas"]:
        avisos.append("nenhuma data legível no cabeçalho")

    mes_divergente = _mes_divergente(dados["mes_ano"], dados["datas"])
    if mes_divergente:
        avisos.append(
            f"O título diz '{dados['mes_ano']}' mas as datas são de "
            f"{mes_divergente}. A data manda — o mês impresso saiu errado "
            f"nesta folha. Só afeta a leitura humana da folha, não o "
            f"processamento."
        )

    faltando = None
    if dados["total_paginas_lote"]:
        faltando = dados["total_paginas_lote"] - len(paginas_lote)
        if faltando < 0:
            avisos.append(
                f"O arquivo cobre {len(paginas_lote)} páginas, mais que as "
                f"{dados['total_paginas_lote']} do lote. Há folha de outro lote "
                f"no maço, ou páginas duplicadas."
            )
    if not cobertura_confiavel and paginas_pdf:
        avisos.append(
            "não foi possível determinar com segurança quais páginas do lote "
            "este arquivo contém"
        )

    # Rótulo no formato usado nas abas do Sheets ("29/06 a 04/07").
    datas = dados["datas"]
    if len(datas) >= 2:
        periodo = f"{datas[0]} a {datas[1]}"
    elif len(datas) == 1:
        periodo = datas[0]
    else:
        periodo = ""

    return {
        "arquivo": caminho,
        "restaurante": dados["restaurante"],
        "paginas_lote": paginas_lote,
        "cobertura_confiavel": cobertura_confiavel,
        "datas": tuple(datas),
        "periodo_sugerido": periodo,
        "mes_ano": dados["mes_ano"],
        "dias": dados["dias"],
        "paginas_pdf": paginas_pdf,
        "total_paginas_lote": dados["total_paginas_lote"],
        "paginas_faltando": faltando,
        "fonte": fonte,
        "avisos": avisos,
    }


def identificar_pasta(pasta, usar_ocr=True, progresso=True):
    """Identifica todos os PDFs de uma pasta (ou o arquivo único informado)."""
    if os.path.isfile(pasta):
        caminhos = [pasta]
    else:
        caminhos = sorted(glob.glob(os.path.join(pasta, "**", "*.pdf"), recursive=True))

    resultados = []
    for i, caminho in enumerate(caminhos, start=1):
        if progresso:
            print(f"  [{i}/{len(caminhos)}] {os.path.basename(caminho)[:60]}")
        try:
            resultados.append(identificar(caminho, usar_ocr))
        except Exception as e:
            resultados.append({
                "arquivo": caminho, "restaurante": None, "datas": (),
                "paginas_lote": (), "cobertura_confiavel": False,
                "periodo_sugerido": "", "mes_ano": "", "dias": [],
                "paginas_pdf": 0, "total_paginas_lote": None,
                "paginas_faltando": None, "fonte": "erro",
                "avisos": [f"falha ao ler: {e}"],
            })
    return resultados


# --- Agrupamento: juntar as partes de um mesmo maço -------------------------

def agrupar_por_periodo(identificacoes):
    """
    Junta os arquivos de uma mesma semana e diz se, somados, cobrem o lote.

    Um maço não é digitalizado de uma vez: sai em partes ("1 a 10", "11 a 23"),
    e quando o alimentador pula uma folha a parte é refeita. O resultado é uma
    pasta com pedaços que se sobrepõem. Nenhum arquivo sozinho é o scan da
    semana — o scan é a união dos pedaços certos.

    A escolha é gulosa pelo que cada arquivo acrescenta de página nova; empate
    fica com o mais recente, que é a redigitalização feita justamente para
    corrigir a anterior. Arquivos que não acrescentam nada saem como
    redundantes.

    Returns:
        {(restaurante, periodo): {
            "total_paginas", "arquivos", "paginas_cobertas", "paginas_faltando",
            "redundantes", "completo", "avisos"
        }}
    """
    grupos = {}
    for ident in identificacoes:
        if not (ident["restaurante"] and ident["periodo_sugerido"]):
            continue
        chave = (ident["restaurante"], ident["periodo_sugerido"])
        grupos.setdefault(chave, []).append(ident)

    resultado = {}
    for chave, itens in grupos.items():
        totais = {i["total_paginas_lote"] for i in itens if i["total_paginas_lote"]}
        total = max(totais) if totais else None

        if len(totais) > 1:
            aviso_total = [
                f"Os arquivos discordam sobre o tamanho do lote ({sorted(totais)}). "
                f"Provavelmente há folha de outra semana no meio."
            ]
        else:
            aviso_total = []

        # Ordem de preferência: primeiro os arquivos cujas páginas foram
        # mapeadas com segurança, depois os mais recentes (uma
        # redigitalização existe porque a anterior saiu ruim). Assim um
        # arquivo de paginação duvidosa só é escolhido se ele for o único a
        # trazer alguma página — e é só nesse caso que o maço fica sem como
        # verificar.
        def _prioridade(item):
            try:
                quando = os.path.getmtime(item["arquivo"])
            except OSError:
                quando = 0
            return (0 if item["cobertura_confiavel"] else 1, -quando)

        restantes = sorted(itens, key=_prioridade)
        escolhidos, cobertas, redundantes = [], set(), []

        while restantes:
            melhor, ganho_max = None, 0
            for item in restantes:
                ganho = len(set(item["paginas_lote"]) - cobertas)
                if ganho > ganho_max:
                    melhor, ganho_max = item, ganho
            if melhor is None:
                redundantes.extend(restantes)
                break
            escolhidos.append(melhor)
            cobertas |= set(melhor["paginas_lote"])
            restantes.remove(melhor)

        if total:
            faltando = tuple(sorted(set(range(1, total + 1)) - cobertas))
        else:
            faltando = ()

        avisos = list(aviso_total)
        # Duas situações bem diferentes: o maço está furado, ou o maço pode
        # estar inteiro e faltou informação para conferir. Misturar as duas
        # faria reescanear folha que está boa e, pior, daria por verificado o
        # que não foi.
        verificavel = bool(total) and all(
            i["cobertura_confiavel"] for i in escolhidos
        )
        if faltando and verificavel:
            avisos.append(
                f"FALTAM as páginas {list(faltando)} do lote — "
                f"{len(faltando) * 25} pessoas ficariam sem registro. "
                f"Reescaneie essas folhas antes de corrigir o período."
            )
        elif not verificavel:
            avisos.append(
                "NÃO VERIFICADO: os arquivos não trazem 'Página X de Y' legível, "
                "então não dá para saber se o maço está inteiro. Pode estar "
                "completo — confira à mão antes de corrigir este período."
            )

        resultado[chave] = {
            "total_paginas": total,
            "arquivos": [i["arquivo"] for i in escolhidos],
            "paginas_cobertas": tuple(sorted(cobertas)),
            "paginas_faltando": faltando,
            "redundantes": [i["arquivo"] for i in redundantes],
            "completo": verificavel and not faltando,
            "verificavel": verificavel,
            "avisos": avisos,
        }

    return resultado


# --- Casamento com os períodos que estão no Sheets --------------------------

def casar_com_periodos(identificacoes, periodos):
    """
    Liga cada scan a um período já lançado no Sheets.

    Args:
        periodos: [{"restaurante", "periodo"}] — a saída da triagem.

    O casamento é pelo conjunto de datas, não pelo texto: os rótulos variam
    ("DATA: 29/06 - 04/07", "29/06 a 04/07") mas as datas são as mesmas.
    Quando dois scans disputam o mesmo período, nenhum é escolhido — duplicata
    de digitalização é comum e escolher a errada grava correção errada.
    """
    def _datas(texto):
        return {f"{d}/{m}" for d, m in _RE_DATA.findall(texto or "")}

    candidatos = {}
    for p in periodos:
        alvo = _datas(p["periodo"])
        if not alvo:
            continue
        for ident in identificacoes:
            if ident["restaurante"] and ident["restaurante"] != p["restaurante"]:
                continue
            if not alvo & set(ident["datas"]):
                continue
            chave = (p["restaurante"], p["periodo"])
            candidatos.setdefault(chave, []).append(ident)

    casados, ambiguos = {}, {}
    for chave, itens in candidatos.items():
        # Empate técnico só entre arquivos diferentes; o mesmo caminho repetido
        # não é ambiguidade.
        unicos = {i["arquivo"]: i for i in itens}
        if len(unicos) == 1:
            casados[chave] = next(iter(unicos.values()))
        else:
            ambiguos[chave] = list(unicos.values())

    return casados, ambiguos


# --- CLI --------------------------------------------------------------------

def _arg(nome, padrao=None):
    if nome in sys.argv:
        idx = sys.argv.index(nome)
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
            return sys.argv[idx + 1]
    return padrao


def main():
    from utils import stdout_tolerante

    stdout_tolerante()

    if len(sys.argv) < 2 or "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    alvo = sys.argv[1]
    if not os.path.exists(alvo):
        raise SystemExit(f"Não encontrado: {alvo}")

    print(f"Lendo os PDFs de {alvo}...\n")
    resultados = identificar_pasta(alvo, usar_ocr="--sem-ocr" not in sys.argv)

    identificados = sum(
        1 for r in resultados if r["restaurante"] and r["periodo_sugerido"]
    )
    grupos = agrupar_por_periodo(resultados)

    print(f"\n{identificados} de {len(resultados)} arquivo(s) identificados, "
          f"em {len(grupos)} semana(s).\n")

    completos, incompletos = [], []
    for chave in sorted(grupos):
        unidade, periodo = chave
        g = grupos[chave]
        marca = "OK " if g["completo"] else ("?? " if not g["verificavel"] else "!! ")
        cobertura = (f"{len(g['paginas_cobertas'])}/{g['total_paginas']}"
                     if g["total_paginas"] else f"{len(g['paginas_cobertas'])}/?")

        print(f"{marca}{unidade:11} {periodo:18} páginas {cobertura:>8}  "
              f"({len(g['arquivos'])} arquivo(s))")
        for caminho in g["arquivos"]:
            print(f"      + {os.path.basename(caminho)}")
        for caminho in g["redundantes"]:
            print(f"      - {os.path.basename(caminho)}  (não acrescenta páginas)")
        for aviso in g["avisos"]:
            print(f"      ! {aviso}")

        (completos if g["completo"] else incompletos).append(chave)

    nao_identificados = [
        r for r in resultados if not (r["restaurante"] and r["periodo_sugerido"])
    ]
    if nao_identificados:
        print(f"\n{len(nao_identificados)} arquivo(s) não identificados:")
        for r in nao_identificados:
            print(f"  {os.path.basename(r['arquivo'])}: {'; '.join(r['avisos'])}")

    furados = [c for c in incompletos if grupos[c]["verificavel"]]
    duvidosos = [c for c in incompletos if not grupos[c]["verificavel"]]

    print(f"\n{len(completos)} semana(s) com o maço completo (OK), "
          f"{len(furados)} com página faltando (!!), "
          f"{len(duvidosos)} sem como verificar (??).")

    saida = _arg("--csv")
    if saida:
        with open(saida, "w", encoding="utf-8-sig", newline="") as f:
            campos = ["arquivo", "restaurante", "periodo_sugerido", "mes_ano",
                      "paginas_pdf", "total_paginas_lote", "paginas_faltando",
                      "dias", "fonte", "avisos"]
            escritor = csv.DictWriter(f, fieldnames=campos)
            escritor.writeheader()
            for r in resultados:
                escritor.writerow({
                    **{c: r.get(c, "") for c in campos},
                    "dias": ", ".join(r["dias"]),
                    "avisos": " | ".join(r["avisos"]),
                })
        print(f"\nCSV: {saida}")

    print("\nPara usar na correção:")
    print("  python corrigir_passivo.py preparar --scans <esta pasta> "
          "--templates <pasta de templates>")


if __name__ == "__main__":
    main()
