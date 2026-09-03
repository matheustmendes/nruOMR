"""
corrigir_passivo.py

Automatiza a correção dos períodos já lançados com a lista de referência
errada, do diagnóstico até a regravação no Google Sheets.

Por que em três etapas
----------------------
A máquina consegue fazer quase tudo sozinha: descobrir quais períodos estão
errados, achar o template de cada um, reconstruir o lote, reprocessar o scan e
comparar com a planilha. Até de que semana é cada scan ela descobre sozinha,
lendo o cabeçalho impresso na folha — o nome do arquivo não importa.

O que sobra para a pessoa é decidir: conferir o que a ferramenta propõe, dar o
aval para gravar, e preencher à mão as poucas linhas que ficaram ambíguas.
Nada é gravado no Sheets sem um comando explícito.

    1. preparar   roda a triagem, monta plano.csv já preenchido no que dá
    2. conferir   executa o plano em modo seco: mostra o que mudaria
    3. aplicar    regrava os períodos no Sheets

Uso:
    python corrigir_passivo.py preparar --templates ~/Downloads --scans D:/scans
    python corrigir_passivo.py conferir
    python corrigir_passivo.py aplicar                    # todos os prontos
    python corrigir_passivo.py aplicar --periodo "29/06 a 04/07"

Colunas do plano.csv:
    restaurante, aba, periodo, marcacoes_perdidas   preenchidas pela triagem
    template_pdf                                    achado automaticamente
    scan_pdf                                        identificado pelo cabeçalho
    scan_paginas                                    lidas/total impresso
    pagina_inicial                                  1, salvo scan parcial
    lote_id, status, divergentes, csv               preenchidas na execução
"""

import os
import re
import sys
import csv
import glob

import lote as lote_mod
import reconciliar
import recuperar_lote
import identificar_scan

PLANO_PADRAO = "plano_correcao.csv"

# O scan de uma semana costuma ser vários arquivos: o maço é digitalizado
# em partes. A coluna guarda todos, separados por ";".
SEPARADOR_SCANS = ";"

COLUNAS = [
    "restaurante", "aba", "periodo", "marcacoes_perdidas",
    "template_pdf", "scan_pdf", "scan_paginas", "pagina_inicial",
    "lote_id", "status", "divergentes", "csv",
]

_UNIDADES = {
    "canela": ["CANELA"],
    "ondina": ["ONDINA"],
    "sao_lazaro": ["SAO LAZARO", "SÃO LÁZARO", "S. LAZARO", "SAOLAZARO", "SAO_LAZARO"],
}


# --- Casamento de arquivos com períodos -------------------------------------

def _datas_do_periodo(periodo: str):
    """
    Extrai os dd/mm de um rótulo de período.

    Os rótulos não são uniformes ("29/06 a 04/07", "DATA: 09/05", "20/06"),
    então a comparação é feita pelo conjunto de datas que aparecem, e não pelo
    texto — é o que sobrevive às variações de digitação.
    """
    return tuple(re.findall(r"\d{2}/\d{2}", periodo or ""))


# Assinatura textual do formulário gerado pelo sistema. Sem esse filtro,
# qualquer PDF da pasta que por acaso contenha duas datas vira candidato — num
# teste com a pasta real um "Parecer Técnico" casou com uma semana de Ondina, e
# usar o arquivo errado gravaria uma correção errada no Sheets.
_MARCAS_FORMULARIO = ("RELACAO DE BOLSISTAS", "PRO-REITORIA DE ASSISTENCIA ESTUDANTIL")


def _cabecalho_pdf(caminho: str):
    """
    Lê unidade e datas do cabeçalho da primeira página do PDF.

    Returns:
        (restaurante_key ou None, tupla de datas, eh_formulario)
    """
    from pypdf import PdfReader

    try:
        texto = (PdfReader(caminho).pages[0].extract_text() or "")
    except Exception:
        return None, (), False

    plano = lote_mod.norm_nome(texto.replace("\n", " "))
    eh_formulario = any(m in plano for m in _MARCAS_FORMULARIO)

    chave = None
    for key, apelidos in _UNIDADES.items():
        if any(lote_mod.norm_nome(a) in plano for a in apelidos):
            chave = key
            break

    return chave, tuple(re.findall(r"\d{2}/\d{2}", texto)), eh_formulario


def _datas_do_nome(caminho: str):
    """Datas dd/mm deduzidas do nome do arquivo (dd-mm, dd_mm, ddmm)."""
    nome = os.path.basename(caminho)
    achados = set()
    for d, m in re.findall(r"(\d{2})[-_./](\d{2})", nome):
        if 1 <= int(d) <= 31 and 1 <= int(m) <= 12:
            achados.add(f"{d}/{m}")
    return tuple(sorted(achados))


def _agrupar_scans(pasta: str):
    """
    Cataloga os scans lendo o cabeçalho impresso em cada folha e junta as
    partes de um mesmo maço.

    O nome do arquivo é a pior fonte possível de informação — scanners salvam
    como "Digitalizar0007.pdf" e a data de modificação muda ao copiar a pasta.
    A folha traz a unidade, o período e o "Página X de Y" impressos em todas
    as páginas: é de lá que tudo sai.

    E o scan de uma semana quase nunca é um arquivo só: o maço é digitalizado
    em partes, e quando o alimentador pula uma folha a parte é refeita. Por
    isso o que interessa é o grupo, não o arquivo.

    Returns:
        {(restaurante, periodo): grupo} — ver identificar_scan.agrupar_por_periodo
    """
    if not pasta or not os.path.isdir(pasta):
        return {}

    identificacoes = identificar_scan.identificar_pasta(pasta, progresso=False)
    return identificar_scan.agrupar_por_periodo(identificacoes)


def _grupo_do_periodo(grupos, restaurante_key, periodo):
    """
    Acha o grupo de scans de um período comparando o conjunto de datas.

    Os rótulos variam entre a planilha e o cabeçalho ("29/06 a 04/07" x
    "DATA: 29/06 - 04/07"), mas as datas são as mesmas. No empate não escolhe:
    a linha fica em branco para decisão humana.
    """
    alvo = set(_datas_do_periodo(periodo))
    if not alvo:
        return None, None

    candidatos = []
    for (rest, per), grupo in grupos.items():
        if rest != restaurante_key:
            continue
        if alvo & set(_datas_do_periodo(per)) == alvo:
            candidatos.append(((rest, per), grupo))

    if len(candidatos) == 1:
        return candidatos[0]
    return None, None


def _indexar_pdfs(pasta: str, exigir_formulario: bool):
    """
    Cataloga os PDFs de uma pasta com a unidade e as datas de cada um.

    Args:
        exigir_formulario: para templates (que têm camada de texto), descarta
            tudo que não for o formulário do sistema. Para scans não se aplica:
            são imagem, sem texto para conferir — ali a verificação vem depois,
            no alinhamento pelos marcadores, que falha se o PDF não for uma
            folha de presença.

    Returns:
        [{"caminho", "restaurante", "datas"}]
    """
    itens = []
    if not pasta or not os.path.isdir(pasta):
        return itens

    # Recursivo: nada obriga o operador a jogar tudo solto na pasta — aqui
    # mesmo os templates vieram organizados em subpastas por restaurante.
    for caminho in sorted(glob.glob(os.path.join(pasta, "**", "*.pdf"), recursive=True)):
        restaurante, datas = None, ()

        if exigir_formulario:
            restaurante, datas, eh_formulario = _cabecalho_pdf(caminho)
            if not eh_formulario:
                continue

        if not datas:
            datas = _datas_do_nome(caminho)
        if not restaurante:
            plano = lote_mod.norm_nome(os.path.basename(caminho)).replace(" ", "")
            for key, apelidos in _UNIDADES.items():
                if any(lote_mod.norm_nome(a).replace(" ", "") in plano
                       for a in apelidos + [key]):
                    restaurante = key
                    break

        itens.append({"caminho": caminho, "restaurante": restaurante, "datas": datas})
    return itens


def _melhor_arquivo(itens, restaurante_key, periodo):
    """
    Escolhe o template que corresponde ao período.

    Exige que o conjunto de datas seja **idêntico**, não apenas sobreposto.
    Sobreposição parcial casa coisas diferentes: a folha de sábado 13/06 tem
    header "13/06" e compartilha uma data com a semana "08/06 a 13/06", mas é
    outro lote, com outras colunas e outro roster. Isso aconteceu de verdade
    aqui — `template_sao_lazaro_fds (3).pdf` foi oferecido para a semana
    errada.

    E se dois arquivos empatam, devolve None: a linha fica em branco para
    decisão humana. Errar o template grava uma correção errada no Sheets, o
    que é bem pior do que uma lacuna.
    """
    alvo = set(_datas_do_periodo(periodo))
    if not alvo:
        return None

    candidatos = [
        item for item in itens
        if (not item["restaurante"] or item["restaurante"] == restaurante_key)
        and set(item["datas"]) == alvo
    ]
    caminhos = {item["caminho"] for item in candidatos}
    return candidatos[0]["caminho"] if len(caminhos) == 1 else None


# --- Etapa 1: preparar ------------------------------------------------------

def preparar(pasta_templates, pasta_scans, saida, restaurante_filtro=None):
    chaves = [restaurante_filtro] if restaurante_filtro else reconciliar.RESTAURANTES_PADRAO

    print("Lendo o Google Sheets para achar os períodos afetados...")
    print("(uma varredura completa leva alguns minutos por causa da cota da API)\n")

    afetados = []
    for chave in chaves:
        try:
            abas = reconciliar.triar(chave)
        except Exception as e:
            print(f"  {chave}: não foi possível ler — {e}")
            continue

        for info in abas:
            for p in info["periodos"]:
                if p["fantasmas_marcados"]:
                    afetados.append({
                        "restaurante": chave,
                        "aba": info["aba"],
                        "periodo": p["periodo"],
                        "marcacoes_perdidas": p["fantasmas_marcados"],
                    })
        print(f"  {chave}: {len(abas)} aba(s) lidas")

    if not afetados:
        print("\nNenhum período com prova de lista trocada. Nada a preparar.")
        print("Lembre: um deslocamento sem sobra de linhas não deixa rastro —")
        print("a conferência definitiva continua sendo a releitura do scan.")
        return

    print("\nCatalogando os templates...")
    templates = _indexar_pdfs(pasta_templates, exigir_formulario=True)
    print(f"  {len(templates)} template(s) em {pasta_templates or '(não informado)'}")

    print("Lendo o cabeçalho de cada scan e juntando as partes de cada maço...")
    grupos = _agrupar_scans(pasta_scans)
    print(f"  {len(grupos)} semana(s) reconhecida(s) em "
          f"{pasta_scans or '(não informado)'}")

    afetados.sort(key=lambda x: -x["marcacoes_perdidas"])

    linhas, problemas = [], []
    for a in afetados:
        _, grupo = _grupo_do_periodo(grupos, a["restaurante"], a["periodo"])

        arquivos, paginas, status = "", "", "pendente"
        if grupo:
            arquivos = SEPARADOR_SCANS.join(grupo["arquivos"])
            paginas = (f"{len(grupo['paginas_cobertas'])}/{grupo['total_paginas']}"
                       if grupo["total_paginas"]
                       else str(len(grupo["paginas_cobertas"])))

            if grupo["paginas_faltando"]:
                # Corrigir a partir de um maço furado zeraria a presença de
                # quem estava nas páginas que faltam.
                status = f"faltam as páginas {list(grupo['paginas_faltando'])}"
                problemas.append((a["periodo"], status))
            elif not grupo["verificavel"]:
                status = "não deu para verificar se o maço está completo"
                problemas.append((a["periodo"], status))

        linhas.append({
            **a,
            "template_pdf": _melhor_arquivo(templates, a["restaurante"], a["periodo"]) or "",
            "scan_pdf": arquivos,
            "scan_paginas": paginas,
            "pagina_inicial": 1,
            "lote_id": "", "status": status, "divergentes": "", "csv": "",
        })

    _salvar_plano(linhas, saida)

    faltando_scan = sum(1 for l in linhas if not l["scan_pdf"])
    faltando_tpl = sum(1 for l in linhas if not l["template_pdf"])

    print(f"\nPlano gravado: {saida}")
    print(f"  {len(linhas)} período(s) a corrigir")
    print(f"  {len(linhas) - faltando_scan} com scan identificado automaticamente")
    print(f"  {len(linhas) - faltando_tpl} com template localizado automaticamente")

    if problemas:
        print(f"\n  >> {len(problemas)} período(s) com o maço em dúvida:")
        for periodo, motivo in problemas:
            print(f"     {periodo}: {motivo}")
        print("     Serão pulados no conferir/aplicar. Reescaneie as folhas que")
        print("     faltam: corrigir com maço furado zeraria a presença de quem")
        print("     estava nelas.")

    if faltando_scan:
        print(f"\n  >> {faltando_scan} período(s) sem scan identificado. Preencha a")
        print("     coluna 'scan_pdf' à mão, ou rode para ver o que há na pasta:")
        print(f"       python identificar_scan.py {pasta_scans or '<pasta>'}")
    if faltando_tpl:
        print(f"\n  >> {faltando_tpl} linha(s) sem template. Tudo bem: nesses casos o")
        print("     lote é reconstruído por OCR do próprio scan (menos exato,")
        print("     as linhas duvidosas ficam marcadas para conferência).")

    print(f"\nDepois: python corrigir_passivo.py conferir")


def _salvar_plano(linhas, caminho):
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUNAS)
        escritor.writeheader()
        for l in linhas:
            escritor.writerow({c: l.get(c, "") for c in COLUNAS})


def _carregar_plano(caminho):
    if not os.path.isfile(caminho):
        raise SystemExit(
            f"Plano não encontrado: {caminho}\n"
            "Rode primeiro: python corrigir_passivo.py preparar"
        )
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# --- Obtenção do lote de um período -----------------------------------------

def _scans_da_linha(linha):
    """Caminhos de scan de uma linha do plano (pode haver vários)."""
    bruto = (linha.get("scan_pdf") or "").strip()
    return [c.strip() for c in bruto.split(SEPARADOR_SCANS) if c.strip()]


def _mes_do_periodo(periodo):
    """Rótulo do mês derivado das datas — a mesma regra que nomeia a aba."""
    import google_sheets as gs

    try:
        return gs._nome_aba_mes(periodo)
    except Exception:
        return ""



def _obter_lote(linha, cadastro_path):
    """
    Devolve o lote_id do período, reaproveitando o que já existir.

    Ordem de preferência: lote já criado > template PDF (exato) > OCR do scan.
    """
    if linha.get("lote_id"):
        try:
            lote_mod.carregar_lote(linha["lote_id"])
            return linha["lote_id"], "reaproveitado"
        except FileNotFoundError:
            pass

    restaurante = linha["restaurante"]
    nome_rest, aba = recuperar_lote.RESTAURANTES[restaurante]
    config = recuperar_lote._carregar_config(None, restaurante)

    sufixo = re.sub(r"[^0-9]", "", linha["periodo"]) or "sem-data"
    lote_id = f"{restaurante}_rec-{sufixo}"

    if linha.get("template_pdf"):
        dados = recuperar_lote.extrair_do_pdf(linha["template_pdf"])
        origem = lote_mod.ORIGEM_PDF
        fonte = linha["template_pdf"]
    elif _scans_da_linha(linha):
        # Planilha atual + histórico do Sheets: é o histórico que resolve
        # quem já saiu do benefício, e é justamente essa gente que aparece nos
        # scans antigos.
        cadastro = recuperar_lote.cadastro_combinado(cadastro_path)
        # O maço inteiro, não só a primeira parte: cada arquivo traz páginas
        # diferentes do lote, e a posição de cada linha vem do "Página X de Y"
        # impresso na folha, nunca da ordem do arquivo.
        dados = recuperar_lote.extrair_do_scan(
            _scans_da_linha(linha), config, cadastro
        )
        origem = lote_mod.ORIGEM_OCR
        fonte = linha["scan_pdf"]
    else:
        raise RuntimeError("sem template e sem scan — impossível reconstruir o lote")

    avisos = list(dados.get("avisos", []))
    avisos.append(f"Reconstruído para a correção do período {linha['periodo']} "
                  f"a partir de {os.path.basename(fonte)}.")

    novo = lote_mod.montar_lote(
        lote_id=lote_id,
        restaurante_key=restaurante,
        restaurante_nome=nome_rest,
        aba=aba,
        dias=config["layout"]["dias"],
        paginacao=dados["paginacao"],
        config=config,
        # O mês vem da data do período, nunca do título impresso: um bug
        # antigo do gerador imprimiu folhas de junho com "MAIO" no cabeçalho.
        # Assim o rótulo do lote bate com a aba onde os dados realmente estão.
        info={"datas": linha["periodo"], "mes_ano": _mes_do_periodo(linha["periodo"])},
        origem=origem,
        avisos=avisos,
    )
    for destino, bruto in zip(novo["alunos"], dados["paginacao"]):
        for extra in ("ocr_bruto", "situacao", "candidatos"):
            if extra in bruto:
                destino[extra] = bruto[extra]

    lote_mod.salvar_lote(novo)
    return lote_id, origem


# --- Etapas 2 e 3: conferir e aplicar ---------------------------------------

def executar(caminho_plano, aplicar, periodo_filtro=None, cadastro=None):
    linhas = _carregar_plano(caminho_plano)
    alvos = [
        l for l in linhas
        if not periodo_filtro or l["periodo"] == periodo_filtro
    ]
    if periodo_filtro and not alvos:
        raise SystemExit(f"Período '{periodo_filtro}' não está no plano.")

    modo = "APLICANDO (grava no Google Sheets)" if aplicar else "CONFERINDO (não grava nada)"
    print(f"{'=' * 70}\n  {modo}\n{'=' * 70}")

    corrigidos = pulados = falhas = 0

    for linha in alvos:
        rotulo = f"{linha['restaurante']} · {linha['periodo']}"
        print(f"\n{'-' * 70}\n{rotulo}   ({linha['marcacoes_perdidas']} marcações perdidas)")

        scans = _scans_da_linha(linha)
        if not scans:
            print("  PULADO: coluna 'scan_pdf' vazia. Sem o scan não há leitura correta.")
            linha["status"] = "falta scan"
            pulados += 1
            continue

        ausentes = [c for c in scans if not os.path.isfile(c)]
        if ausentes:
            print(f"  PULADO: {len(ausentes)} arquivo(s) não encontrado(s):")
            for c in ausentes[:3]:
                print(f"          {c}")
            linha["status"] = "scan inexistente"
            pulados += 1
            continue

        status_atual = str(linha.get("status", ""))

        # Página comprovadamente ausente: não há o que discutir, corrigir a
        # partir daí zeraria a presença de quem estava nela.
        if status_atual.startswith("faltam as páginas"):
            print(f"  PULADO: {status_atual}.")
            print("          Reescaneie essas folhas antes de corrigir.")
            pulados += 1
            continue

        # Já "não deu para verificar" é outra coisa: o maço pode estar inteiro,
        # só faltou informação na conferência prévia. A releitura conta as
        # linhas de verdade, então vale deixar rodar no modo seco — é assim que
        # se descobre se está tudo lá. Só o gravar fica bloqueado.
        incerto = status_atual.startswith("não deu")
        if incerto and aplicar:
            print(f"  PULADO: {status_atual}.")
            print("          Rode 'conferir' para ver a cobertura real da releitura")
            print("          e libere a linha no plano se estiver completa.")
            pulados += 1
            continue
        if incerto:
            print(f"  ATENÇÃO: {status_atual} — conferindo assim mesmo (nada será gravado).")

        print(f"  Scan: {len(scans)} arquivo(s), páginas {linha.get('scan_paginas') or '?'}")

        try:
            lote_id, origem = _obter_lote(linha, cadastro)
            linha["lote_id"] = lote_id
            print(f"  Lote: {lote_id} ({origem})")

            alvo = lote_mod.carregar_lote(lote_id)

            # Linha de OCR não resolvida não é só "menos precisa": gravar com
            # ela ativamente estraga dois registros. A matrícula lida errado
            # não casa com ninguém no Sheets, então a pessoa certa não recebe
            # a marcação — e, com a limpeza de ausentes, ainda perde a que
            # tinha — enquanto uma linha nova de lixo é criada no fim da aba.
            # Por isso o `aplicar` para aqui. Conferir segue liberado: é assim
            # que se vê o tamanho do problema antes de decidir.
            duvidosos = [a for a in alvo["alunos"] if a.get("situacao") == "sem_match"]
            if duvidosos:
                print(f"  {len(duvidosos)} linha(s) do lote não foram resolvidas pelo OCR:")
                for a in duvidosos[:5]:
                    candidatos = a.get("candidatos") or []
                    sugestao = (f" — mais provável: {candidatos[0]['matricula']} "
                                f"{candidatos[0]['nome'][:32]}") if candidatos else ""
                    print(f"     linha {a['numero']:>4}  OCR leu '{a.get('ocr_bruto', '?')}'{sugestao}")
                if len(duvidosos) > 5:
                    print(f"     ... e mais {len(duvidosos) - 5}.")

                if aplicar:
                    print(f"  PULADO: corrija o nome e a matrícula dessas linhas em")
                    print(f"          lotes/{lote_id}.json (os candidatos estão lá) e")
                    print(f"          rode de novo. Gravar assim faria a pessoa certa")
                    print(f"          perder a presença e criaria linha de lixo no fim")
                    print(f"          da aba. Com o template do período isso não acontece.")
                    linha["status"] = f"OCR não resolveu {len(duvidosos)} linha(s)"
                    pulados += 1
                    continue

            try:
                pagina_inicial = max(1, int(linha.get("pagina_inicial") or 1))
            except ValueError:
                pagina_inicial = 1

            contagem, roster, dias, resumo = reconciliar.releitura_correta(
                alvo, scans, pagina_inicial
            )
            print(f"  Releitura: {resumo['linhas_lidas']} linhas, "
                  f"{resumo['ambiguos']} na zona de dúvida")

            # A ordem das páginas é a base de tudo: sem ela cada presença pode
            # ir para a pessoa errada, do mesmo jeito que o bug original. Um
            # maço com arquivos sobrepostos (redigitalização que reescaneou
            # páginas já cobertas) já produziu esse sintoma de verdade nesta
            # ferramenta — sem este bloqueio, a comparação seguiria e escreveria
            # no Sheets como se a ordem estivesse certa.
            if not resumo["ordem_confiavel"]:
                print(f"  ORDEM DAS PÁGINAS NÃO CONFIRMADA: {resumo['motivo_ordem']}")
                print(f"  Provável causa: os arquivos do maço se sobrepõem (uma")
                print(f"  redigitalização recobriu páginas já lidas). Confira")
                print(f"  scan_pdf no plano e remova o arquivo redundante, ou")
                print(f"  rode 'python identificar_scan.py <pasta>' para ver a")
                print(f"  cobertura de cada arquivo.")
                if aplicar:
                    linha["status"] = "ordem das páginas não confirmada"
                    pulados += 1
                    continue

            if resumo["fora_do_lote"]:
                print(f"  o scan chegou até a linha {resumo['maior_numero']}, além das "
                      f"{alvo['total_alunos']} do lote — padding sem identidade, "
                      f"fora da exportação.")
            if resumo["excedentes_com_marca"]:
                print(f"  ATENÇÃO: {len(resumo['excedentes_com_marca'])} dessas linhas "
                      f"têm marcação real e foram descartadas (senão viraria pessoa")
                print(f"           fantasma no Sheets). Confira o scan antes de confiar")
                print(f"           neste período.")

            comparacao = reconciliar.comparar_com_sheets(
                alvo, contagem, roster, dias, linha["restaurante"], linha["periodo"]
            )
            if not comparacao.get("ok"):
                print(f"  FALHA: {comparacao['erro']}")
                linha["status"] = "erro na leitura do Sheets"
                falhas += 1
                continue

            if comparacao["nao_encontrados"]:
                marcados = [n for n in comparacao["nao_encontrados"]
                           if not n["nome"].startswith("[linha")]
                print(f"  {len(marcados)} pessoa(s) com presença na folha não têm "
                      f"linha nesta aba — serão criadas como novas ao gravar:")
                for n in marcados[:5]:
                    print(f"    nº {n['numero']:>4}  {n['nome']}  "
                          f"({n['matricula'] or 'sem matrícula'})")
                if len(marcados) > 5:
                    print(f"    ... e mais {len(marcados) - 5}.")

            divergentes = comparacao["divergentes"]
            linha["divergentes"] = len(divergentes)
            print(f"  Comparação: {comparacao['iguais']} iguais, "
                  f"{len(divergentes)} divergentes")

            if not divergentes:
                print("  Nada a corrigir neste período.")
                linha["status"] = "ok"
                continue

            csv_saida = f"reconciliacao_{linha['restaurante']}_{re.sub(r'[^0-9]', '-', linha['periodo'])}.csv"
            reconciliar.salvar_csv(comparacao, dias, csv_saida)
            linha["csv"] = csv_saida
            print(f"  Relatório: {csv_saida}")

            for d in divergentes[:5]:
                difs = ", ".join(
                    f"{dia}: '{a}' -> '{c}'" for dia, (a, c) in d["diferencas"].items()
                )
                print(f"    linha {d['linha_sheets']:>4}  {d['nome'][:30]:30s} {difs}")
            if len(divergentes) > 5:
                print(f"    ... e mais {len(divergentes) - 5} (veja o CSV).")

            if not aplicar:
                linha["status"] = "a corrigir"
                continue

            import google_sheets as gs

            cobertura_total = resumo["linhas_lidas"] >= alvo["total_alunos"]
            if not cobertura_total:
                print(f"  AVISO: scan cobriu {resumo['linhas_lidas']} de "
                      f"{alvo['total_alunos']} linhas — as marcações erradas em"
                      f" linhas fantasma não serão apagadas.")

            resultado = gs.exportar_para_sheets(
                contagem, roster, dias, linha["restaurante"], linha["periodo"],
                forcar=True, limpar_ausentes=cobertura_total,
            )
            if resultado.get("ok"):
                lote_mod.registrar_processamento(
                    lote_id, linha["periodo"], sincronizado_sheets=True,
                    detalhe=f"correção do passivo: {len(divergentes)} linha(s)",
                )
                linha["status"] = "corrigido"
                corrigidos += 1
                print(f"  CORRIGIDO em '{resultado.get('aba', '')}'.")
            else:
                linha["status"] = f"erro ao gravar: {resultado.get('erro', '')}"
                falhas += 1
                print(f"  FALHA ao gravar: {resultado.get('erro')}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            linha["status"] = f"erro: {e}"
            falhas += 1

    _salvar_plano(linhas, caminho_plano)

    print(f"\n{'=' * 70}")
    if aplicar:
        print(f"  {corrigidos} período(s) corrigidos, {pulados} pulados, {falhas} com erro.")
        print("  Guarde os CSVs de reconciliação: são o registro do que mudou.")
    else:
        a_corrigir = sum(1 for l in alvos if l["status"] == "a corrigir")
        print(f"  {a_corrigir} período(s) com divergência, {pulados} pulados, "
              f"{falhas} com erro.")
        print("  Nada foi gravado. Confira os CSVs e então rode:")
        print("    python corrigir_passivo.py aplicar")
    print(f"  Plano atualizado: {caminho_plano}")
    print("=" * 70)


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

    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando not in ("preparar", "conferir", "aplicar") or "--help" in sys.argv:
        print(__doc__)
        return

    plano = _arg("--plano", PLANO_PADRAO)

    if comando == "preparar":
        preparar(
            _arg("--templates"),
            _arg("--scans"),
            plano,
            _arg("--restaurante"),
        )
    else:
        executar(
            plano,
            aplicar=(comando == "aplicar"),
            periodo_filtro=_arg("--periodo"),
            cadastro=_arg("--cadastro", "impressao.xlsx"),
        )


if __name__ == "__main__":
    main()
