# TODO — nruOMR

> Revisitar sempre antes de iniciar novas sessões de desenvolvimento.

---

## ⚠ DIREÇÃO DO PROJETO — guinada anunciada (2026-09-16)

Decisão anunciada pelo usuário: abandonar a impressão das listas em papel e
migrar para **outro sistema, mais simples**, reaproveitando as regras de
negócio já validadas aqui (identidade por lote, detecção de matrícula
duplicada, cálculo de presença/período, estrutura da planilha) mas sem OMR,
sem scan, sem `lote_sync`/`recuperar_lote`/`corrigir_passivo` — todo esse
aparato existe só por causa do papel. Ainda não começou; nada foi decidido
sobre a nova arquitetura (app web direto? totem? app do bolsista?). Ao
planejar isso, tratar como projeto novo que consome as regras de negócio
deste, não como refactor incremental do pipeline de scan.

---

## ▶ PRÓXIMA SESSÃO — UX do fluxo de lote

Sincronização de lotes entre máquinas **implementada e ativa** (2026-09-10)
via Google Sheets — ver **`SINCRONIZACAO_LOTES.md`** para o design completo
e o porquê (a primeira tentativa foi Drive API e esbarrou num limite real
de cota de conta de serviço fora de Workspace; Sheets reaproveita a
planilha do dashboard que já funciona, sem configuração nova). Testado de
ponta a ponta contra a planilha real. Limitação aceita: PDF não sincroniza,
só o roster/geometria (o que resolve o bug de identidade) — reimpressão
exata continua só na máquina que gerou o lote.

**Lotes grandes (Canela, 500+ pessoas) — resolvido de vez.** O JSON do
lote passa dos 50.000 caracteres por célula que o Sheets aceita. Corrigido
fatiando automaticamente em colunas extras (`lote_sync._particionar` +
`_garantir_colunas`, que redimensiona a aba sozinha quando precisa) — a
leitura junta os pedaços de volta. Testado de ponta a ponta com um lote
real de 539 pessoas (83KB de JSON, 2 colunas): sobe, baixa, os dados batem
exatamente. Teto de segurança em 14 colunas de JSON (~630KB) antes de
desistir e avisar que ficou só local — bem acima de qualquer lote real
visto até aqui.

**Recuperação de lote acessível na UI** (2026-09-10) — nova aba "Recuperar
lote" em `web.py`: aponta pra uma pasta de scans (+ opcionalmente uma pasta
de templates), casa cada scan com seu template pelo período automaticamente
(reaproveita o casamento já testado de `corrigir_passivo.py`), recupera o
lote (exato via template, ou por OCR do scan quando não achar o template) e
lista o resultado em português simples — sem exigir linha de comando.
Rodar duas vezes não duplica (reaproveita o lote já criado pro mesmo
período). Não escreve no Sheets — só cria o lote; a presença é lançada
depois, normalmente, pela aba "Processar scan" já existente.

Próximo passo, ainda não atacado: deixar o resto do fluxo do lote
compreensível para quem não é técnico (`SINCRONIZACAO_LOTES.md` tem uma
seção mapeando o que já é amigável e o que não é).

### Estado do passivo (correção de listas trocadas)

**Praticamente fechado.** Dos 17 períodos identificados, 9 corrigidos e
confirmados nesta sessão, 3 já estavam certos (nada a corrigir), 5 seguem
bloqueados — de propósito, pela trava que impede gravar com ordem de página
não confirmada:

| Restaurante | Período | Motivo do bloqueio |
| --- | --- | --- |
| São Lázaro | `08/06 a 13/06` | 104 linhas sem match no OCR — corrigir manualmente em `lotes/*.json` (candidatos já sugeridos) |
| Ondina | `08/06 a 13/06` | ordem de página não reconstruível — arquivos sobrepostos no scan |
| São Lázaro | `06/07 a 10/07` | só 1 de 6 páginas com número legível — sinal insuficiente, provavelmente precisa reescanear |
| Ondina | `01/06 a 06/06` | ordem de página não reconstruível — 4 arquivos com sobreposição complexa (inclui leitura impossível "29" num lote de 27) |
| São Lázaro | `25/05 a 29/05` | idem a São Lázaro 06/07 — sinal insuficiente |

Nenhum desses tem template do período disponível ainda — se aparecer,
`corrigir_passivo.py preparar` resolve a maioria sozinho (foi o que
resolveu Canela `01/06` e Canela `13/07` nesta sessão, via dois bugs de
verdade corrigidos em `exportar._inferir_numeros_paginas`, ver "Bugs
corrigidos" abaixo). Comando para retomar:

```bash
python corrigir_passivo.py preparar \
    --scans "C:/Users/matheus.torres/Documents/Smart Touch/i11xx/Output" \
    --templates <pasta com templates novos, se achar algum>
python corrigir_passivo.py conferir --periodo "<período>"
python corrigir_passivo.py aplicar --periodo "<período>"
```

As linhas `Aluno N` que sobraram na planilha (dos períodos já corrigidos)
continuam lá, agora vazias — apagar à mão só quando todos os períodos do mês
estiverem reconciliados, nunca no meio (desloca as linhas de baixo).

---

## Pendências operacionais

- [ ] **URGENTE — mesma matrícula em pessoas diferentes (Ondina).** Três casos em que duas pessoas distintas compartilham matrícula. Como o Sheets casa por matrícula e a primeira linha vence, uma delas fica sem frequência **toda semana** — problema independente da lista trocada, e continua acontecendo até a planilha de origem ser corrigida. **Mitigado em 2026-09-15:** `exportar_para_sheets` agora roda `_detectar_matriculas_duplicadas` a cada exportação e devolve `aviso_matricula_duplicada`, mostrado na tela (caixa de avisos) toda vez que acontece — não evita a perda daquela semana, só garante que fica visível em vez de passar batido:
  - `221118905` — Geovana Luiza da Silva / Geovana Luiza da Silva Batista
  - `225115016` — Alidey Godwill Uranof Kpoahoun / Kpoahoun Alidey Godwill Uranof (mesmo nome invertido — confirmar se são duas pessoas ou cadastro em duplicidade)
  - `225115165` — Ludimila Bauin Oliveira Sanca / Ludimila Buin Oliveira Sanca
- [ ] **Duplicatas do mesmo nome** (desperdiçam linha impressa, não perdem frequência): Canela 223217415, 225115012, 219118561, 224119511, 225119865, 221119022, 223119129; Ondina 225115010, 225115011, 224216326, 224120334; São Lázaro 222120254.
- [ ] **Definir o período de retenção** dos lotes processados (`python lote.py limpar <dias> --aplicar`). Sugestão: fim do semestre.

## Melhorias

- [x] **Fallback de matrícula não encontrada via planilha de bolsistas.** Implementado (2026-09-16): `recuperar_lote.carregar_cadastro_bolsistas()` lê a planilha oficial de bolsistas (spreadsheet_id em `config_sheets.yaml`, chave `bolsistas`), abas ONDINA/CANELA/SÃO LÁZARO (cabeçalho localizado dinamicamente pela célula "MATRÍCULA" — o layout varia entre abas). Entra em `cadastro_combinado()` por último e só com `setdefault` — preenche matrícula que planilha de impressão/histórico do Sheets não conhecem, nunca sobrepõe nome já resolvido. Testado contra a planilha real: 1312 matrículas carregadas das três abas. CLI: `--sem-bolsistas` desativa, espelhando `--sem-sheets`.
  - **Extensão (2026-09-16): busca por nome quando a matrícula não bate de jeito nenhum.** `extrair_do_scan` agora lê por OCR a coluna do nome (só quando a matrícula falhou tanto exata quanto por 1 dígito) e busca no cadastro combinado (que já inclui os bolsistas) pelo nome mais parecido via `_melhor_candidato_nome` — só resolve sozinho quando o melhor candidato se destaca claramente do segundo (mesmo critério de segurança usado para matrícula). Nova situação `"por_nome"` no JSON do lote, com `nome_ocr` e `candidatos_nome` para conferência manual quando não resolve. **Não testado contra scan real** (só a lógica de comparação de nomes, offline) — a geometria do recorte da coluna do nome (`geo["nome_x1"]/["nome_x2"]`) é estimada a partir da mesma fórmula de `gerar_template.calcular_posicoes_colunas`, sem validar contra uma imagem de verdade. Testar no próximo caso real de `sem_match` antes de confiar cegamente.
- [ ] **Trocar geração de xlsx temporário pela planilha existente no projeto** — exportação cria xlsx novo a cada processamento; substituir pelo arquivo já existente.
- [x] **Executável para iniciar o projeto inteiro** — sobe `web.py` (5000) e `dashboard.py` (5001) juntos. (`Iniciar Sistema Completo.bat`)
- [x] **Snapshot de lote de impressão** — PDF e roster nascem juntos em `lotes/`, com a geometria congelada. Código do lote impresso no rodapé da folha. Ver LOTES.md.
- [x] **Recuperação de lotes perdidos** — `recuperar_lote.py` reconstrói o snapshot do PDF do template (exato), do scan por OCR, ou de uma cópia da planilha.
- [x] **Triagem e reconciliação do passivo** — `reconciliar.py --triagem` acha os períodos errados sem precisar de scan; `reconciliar.py --lote ... --scan ...` confere e corrige.
- [x] **Identificação automática dos scans** — `identificar_scan.py` lê unidade, período e paginação do cabeçalho impresso; agrupa as partes de cada maço e confere a cobertura página a página.
- [x] **Automação de ponta a ponta** — `corrigir_passivo.py` (preparar → conferir → aplicar).

## Bugs corrigidos

- [x] **Identidade por posição na planilha viva.** A causa raiz de tudo: `alunos[numero - 1]` indexava a planilha de referência enviada no processamento, que muda toda semana. Corrigido pelo snapshot de lote (`lote.py`).
- [x] **CRÍTICO — OCR do número de página lia errado, não só falhava.** No maço de Ondina `29/06` a página 14 saiu como "4" (o traço do "1" se perdeu). Número errado é pior que ausente porque parece confiável, e colidia com a página 4 real de outro arquivo. `_inferir_numeros_paginas` agora procura a progressão que explica o maior número de leituras e descarta as discordantes.
- [x] **CRÍTICO — gate de segurança ausente: escrita com ordem de página não confirmada.** `corrigir_passivo.py`/`reconciliar.py` seguiam em frente e gravavam no Sheets mesmo quando `_inferir_numeros_paginas` desistia e a leitura caía pra ordem bruta do arquivo (essencialmente aleatória). Isso corrompeu de verdade 4 períodos nesta sessão antes de ser pego (2 foram recuperados depois, ver abaixo; 2 seguem pendentes — ver tabela do passivo). Corrigido com um bloqueio explícito (`resumo["ordem_confiavel"]`) que impede `aplicar` — `conferir` continua liberado para diagnóstico.
- [x] **Desempate errado na reconstrução de página: preferia a coincidência espalhada, não o cluster apertado.** Achado real (Canela `01/06`): um "14" mal lido e um "19" verdadeiro, a 5 posições de distância, empataram em votos com o par verdadeiro (20,19) adjacente — preferir o span *maior* no empate escolhia a leitura errada e travava a reconstrução inteira ("números repetidos"). Corrigido para preferir o span *menor*.
- [x] **Página inserida fora de ordem física não era resolvida por eliminação.** Achado real (Canela `13/07`): um arquivo de redigitalização trouxe as páginas na ordem 23,22,20,19,**21** — a última fora de sequência (não é erro de leitura, é a ordem real do arquivo). A extensão por vizinhança não alcançava essa posição. Corrigido: sobrando uma única posição vazia e um único número ainda não usado, atribui por eliminação (não há ambiguidade possível).
- [x] **`identificar_scan.py` extrapolava página em branco do duplex como se fosse página real.** Um arquivo com conteúdo real só nas páginas 18–23 (mais 2 costas em branco no início) tinha as brancas "preenchidas" como páginas 24 e 25 inexistentes, e a busca de cabeçalho (que só olha as 2 primeiras páginas do arquivo) caía nas brancas e nunca achava a unidade/período. Corrigido em `_paginas_do_texto` e `_texto_do_pdf`, pulando página sem conteúdo antes de contar. Não afetava as correções já aplicadas (o pipeline de leitura de bolhas já pulava branco corretamente); só o diagnóstico/agrupamento por `identificar_scan.py` estava errado.
- [x] **Recuperação por OCR usava só a primeira parte do maço e assumia ordem 1..N.** Com scans invertidos e fatiados, o roster saía embaralhado. Agora `extrair_do_scan` aceita o maço inteiro e tira a posição de cada linha do "Página X de Y" impresso.
- [x] **CRÍTICO — ordem das páginas invertida.** O scanner entrega o maço da última folha para a primeira, e o OCR do número de página falha em algumas folhas (3 de 11 num scan real de Ondina). A lógica era tudo-ou-nada: uma falha e o sistema usava a ordem do arquivo — invertida —, dando a presença de cada pessoa a outra. Corrigido com `exportar._inferir_numeros_paginas`, que reconstrói a numeração a partir das páginas legíveis. Provavelmente o mecanismo por trás das 259 marcações perdidas em Ondina `29/06`.
- [x] **Regravação não limpava o lixo.** `_montar_matriz_grupo` preservava as células de linhas que a nova fonte não cobre, então as marcações erradas em linhas `Aluno N` sobreviveriam à correção. Agora `limpar_ausentes` zera essas células e recalcula o total — só quando o scan cobre o lote inteiro.
- [x] **Ambíguos em massa (252 casos em scan de 25 nomes).** A medição do círculo era uma ROI quadrada que incluía a borda impressa (~29% da área): bolhas vazias liam 0,29 contra corte de 0,40. Corrigido em `ler_bolhas.ler_circulo`, medindo o interior (disco de 0,65·raio). Zona ambígua centralizada em `get_zona_ambigua`.
- [x] **Scans fatiados em partes.** Um maço sai em vários PDFs ("1 a 10", "11 a 23") com redigitalizações no meio. `identificar_scan.agrupar_por_periodo` junta as partes, escolhe o conjunto mínimo e confere a cobertura página a página.
- [x] **Casamento de arquivo por sobreposição parcial de data.** A folha de sábado `13/06` casava com a semana `08/06 a 13/06` — lotes diferentes, com outras colunas e outro roster. Agora exige conjunto de datas idêntico.
- [x] **Datas lidas do corpo da página.** Pegar qualquer par de datas do texto produzia períodos impossíveis ("12/03 a 16/01"). Agora exige o formato do cabeçalho (intervalo ou `DATA:`).
- [x] **Filtro de template frouxo.** Um "Parecer Técnico" casou com uma semana de Ondina só por conter duas datas. Agora exige a assinatura do formulário no cabeçalho.
- [x] **Revisão de ambíguos não chegava ao Sheets.** `rota_aplicar_correcoes` reexportava, mas engolia a exceção com `except: pass`. Agora reporta o resultado real na tela de revisão.
- [x] **Caminho do Tesseract fixo** no perfil `matheus.torres` — em qualquer outra máquina o OCR não rodava, sem erro visível. Agora em `utils.obter_pytesseract` (locais usuais + `TESSERACT_CMD`).
- [x] **`print` com `✓` derrubava a leitura de página** em console cp1252 (o caso do `.bat`): o `UnicodeEncodeError` caía no `except` do laço e a página aparecia como "ERRO na página N". Corrigido com `utils.stdout_tolerante()`.
- [x] **Mês errado no cabeçalho de algumas impressões** (bug antigo do gerador: folha de junho com título "MAIO"). Nada no sistema decide pelo nome do mês — período e aba do Sheets vêm sempre da data `dd/mm`. As folhas com título divergente são sinalizadas pelo `identificar_scan.py`.
- [x] **Cota da API do Sheets** estourava na varredura de todas as abas e o diagnóstico saía incompleto, parecendo que não havia nada a achar. Agora com retry e backoff.
- [x] **Leitura de página quando o scan começa na página 2** (campo "Página inicial do scan" na UI + `pagina_inicial`).
- [x] **Justificativas quebravam com mais de um documento.**
