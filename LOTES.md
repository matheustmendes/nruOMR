# Lotes de impressão — como o sistema sabe de quem é cada linha

## O problema que isso resolve

Até agora, a identidade de cada linha lida no scan era resolvida assim:

```python
nome, matricula = alunos[numero - 1]   # alunos = planilha enviada AGORA
```

Ou seja: quem estava na 5ª linha da folha era, por definição, a 5ª pessoa da
planilha no momento do **processamento**. Só que a folha foi impressa a partir
da planilha de **outro dia** — e entre uma coisa e outra entram e saem
beneficiários, o que empurra todo mundo para cima ou para baixo.

Uma única pessoa removida no topo da lista desloca as ~1.300 linhas seguintes
em uma posição. Cada presença passa a ser lançada no vizinho de baixo. E não há
nenhum sinal de erro: o sistema termina, exporta e sincroniza normalmente.

## A solução: o lote

Quando o PDF é gerado, o sistema grava junto um **snapshot do lote**:

```
lotes/canela_20260827-114816.json    quem estava em cada linha + a geometria
lotes/canela_20260827-114816.pdf     o PDF exatamente como foi impresso
```

O snapshot nasce no mesmo instante em que o papel é impresso — por isso ele
nunca fica desatualizado. Não importa o que aconteça com a planilha depois.

Ele guarda duas coisas:

1. **O roster na ordem impressa** — número, página, linha, nome e matrícula.
2. **A geometria completa do formulário** — a mesma que vai para
   `configs/config_*.yaml`.

A geometria vai junto por um motivo concreto: `configs/config_canela.yaml` é
**sobrescrito** a cada novo template gerado. Um lote impresso com 6 dias,
reprocessado depois de se gerar um template de 5 dias, leria as bolhas nas
coordenadas erradas. Congelando a geometria no lote, isso deixa de ser
possível.

O código do lote sai **impresso no rodapé de cada folha**. Uma folha solta
encontrada meses depois é rastreável sem depender da memória de ninguém.

## Fluxo de trabalho

### Gerar as listas

Igual a antes: aba **Gerar template**, escolhe o restaurante, envia a planilha.
A diferença é que o resultado agora mostra o código do lote, e o botão de
download entrega o PDF guardado em `lotes/` (não um temporário).

Nesse momento o sistema também confere a planilha de origem e avisa sobre:

- matrículas duplicadas (a mesma matrícula em duas linhas);
- nomes repetidos;
- linhas da planilha puladas por não terem nome.

Esses avisos **não bloqueiam a impressão** — a identidade do lote é a linha
impressa, não a matrícula, então um erro de digitação na origem não quebra
nada. Mas aparecem em destaque para serem corrigidos na planilha.

Matrícula em branco não é aviso: acontece normalmente e o sistema lida com
isso. Fica só registrada como nota, porque tem uma consequência prática — essas
linhas não podem ser conferidas por matrícula depois, só por nome e posição.

### Processar o scan

Aba **Processar presenças**: escolhe o restaurante, envia o PDF do scan e
**seleciona o lote** que originou aquele papel. O código está no rodapé da
folha; a lista mostra também a data e o número de pessoas de cada lote.

A planilha `.xlsx` deixou de ser obrigatória. Se você enviar uma mesmo assim,
ela é usada **apenas** para mostrar o que mudou desde a impressão — nunca para
decidir quem é quem.

### Ciclo de vida

- O lote fica em `lotes/` enquanto não houver confirmação de que os dados
  chegaram ao Google Sheets.
- Confirmada a sincronização, ele vai para `lotes/processados/` com o registro
  de quando e com qual período — histórico auditável.
- **Se o envio falhar, o lote continua ativo de propósito**: é ele que permite
  reprocessar sem reescanear a folha.
- A limpeza definitiva é uma rotina separada e deliberada, nunca automática:

```bash
python lote.py limpar 180            # lista o que seria apagado
python lote.py limpar 180 --aplicar  # apaga de fato
```

### Outros comandos

```bash
python lote.py listar             # todos os lotes
python lote.py listar canela      # só os do Canela
python lote.py ver <lote_id>      # detalhes, avisos e histórico
```

---

## Recuperar lotes perdidos

Para as folhas impressas antes disso existir — ou cuja planilha de referência
se perdeu — o `recuperar_lote.py` reconstrói o snapshot. Três caminhos, do mais
confiável ao menos:

### 1. A partir do PDF do template (exato)

O PDF gerado pelo sistema tem camada de texto real do ReportLab. Nome e
matrícula saem exatos, sem OCR:

```bash
python recuperar_lote.py --pdf template_canela.pdf --restaurante canela
```

Validado contra um template real de 539 alunos: **539/539 registros com
número, página, linha e matrícula idênticos ao original.** Se o arquivo do
template ainda existir em algum lugar, é sempre esta a opção.

### 2. A partir do próprio scan (OCR)

Os nomes estão impressos na folha escaneada. O OCR lê a coluna de matrícula
(só dígitos, que é o que o Tesseract reconhece de forma estável) e cruza com um
cadastro mestre para resolver os nomes:

```bash
python recuperar_lote.py --scan scan.pdf --restaurante canela \
    --cadastro impressao.xlsx
```

Medido num scan real de 50 linhas: **48 acertos, 4 correções automáticas de um
dígito (todas certas), 2 marcados como `sem_match`.** Nenhum chute silencioso —
quando o OCR não decide com segurança, a linha é marcada e o JSON traz os cinco
candidatos mais próximos do cadastro, com a distância de cada um, para a
conferência manual ser rápida:

```json
{
  "numero": 3, "situacao": "sem_match", "ocr_bruto": "522247772",
  "candidatos": [
    {"matricula": "222217772", "nome": "Adalberto Mario Bispo Dos Santos", "distancia": 2},
    {"matricula": "222217172", "nome": "Juliana De Jesus Silva Santos",   "distancia": 3}
  ]
}
```

Corrija o `matricula` e o `nome` dessas linhas direto no JSON antes de usar o
lote.

### 3. A partir de uma cópia da planilha da época

```bash
python recuperar_lote.py --planilha backup_maio.xlsx --restaurante canela
```

Última opção. Só vale se houver **certeza** de que é a versão que gerou a
impressão — e é justamente essa certeza que costuma faltar.

---

## Triagem: descobrir quais semanas estão erradas

**Não é preciso ter a planilha de referência da época.** Ela era só um
intermediário: o que importa é o scan (quem marcou o quê, por linha) e o lote
(quem era cada linha). Com os dois, o resultado correto é obtido de forma
independente.

Mas antes de reprocessar qualquer coisa, vale rodar a triagem — ela lê o
Google Sheets e aponta onde há rastro de lista trocada, **sem precisar de scan
nenhum e sem alterar nada**:

```bash
python reconciliar.py --triagem                      # os três restaurantes
python reconciliar.py --triagem --restaurante canela
```

Ela separa o que encontra em duas categorias, e a distinção importa: agir
sobre indício custa reprocessar uma semana à toa; ignorar uma prova deixa
presença errada no ar.

**`[PROVA]` — linhas `Aluno N`.** `exportar_para_sheets` só cria uma linha
chamada "Aluno N", sem matrícula, quando o scan leu **mais** linhas do que a
lista de referência tinha. A existência dessas linhas é prova de que as duas
não batiam. Quando um período tem marcações caindo nelas, aquelas presenças
foram para o vazio.

**`[PROVA]` — mesma matrícula em duas pessoas diferentes.** O `_sincronizar_
roster` casa por matrícula e a primeira linha vence, então as duas colidem na
mesma linha do Sheets e uma delas fica sem frequência — em **todos** os
períodos, não só nos afetados pela lista trocada.

**`[indício]` — a última linha marcada varia entre os períodos do mês.** Todas
as semanas leem a mesma folha impressa, então esse número deveria ser estável.
Uma variação grande sugere que a lista mudou de tamanho no meio do caminho.

**`[indício]` — matrícula duplicada com o mesmo nome.** Provável duplicata na
planilha de origem: desperdiça uma linha impressa, mas ninguém perde
frequência.

> **Limite honesto da triagem:** um deslocamento em que a lista de referência
> tinha *exatamente o mesmo tamanho* da folha impressa (uma pessoa entrou e
> outra saiu na mesma semana) **não deixa rastro nenhum** na planilha. A
> triagem serve para priorizar, não para absolver. A conferência definitiva é
> sempre a releitura do scan.

## Conferir e corrigir um período

```bash
# só mostra as diferenças, não altera nada
python reconciliar.py --lote <lote_id> --scan scan.pdf --periodo "12/05 a 16/05"

# regrava o período com o resultado correto
python reconciliar.py --lote <lote_id> --scan scan.pdf --periodo "12/05 a 16/05" --aplicar
```

Saída:

```
Conferidos  : 539  |  iguais: 524  |  divergentes: 15

Divergências (o que está lá → o que deveria estar):
    linha  181  Beltrano Silva                      Segunda: 'AJ' -> '', Terça: '' -> 'A'
    ...

Relatório completo: reconciliacao_canela_20260512-090000_12-05 a 16-05.csv
```

O casamento é feito por **matrícula** (e por nome para quem não tem matrícula)
— nunca por posição, que é exatamente o que estava errado. A ordem das linhas
no Sheets é irrelevante.

Com `--aplicar`, o período é regravado e o lote recebe o registro da
reconciliação no histórico. **Guarde o CSV**: é ele que sustenta a decisão
caso um beneficiário conteste.

**A regravação também apaga o lixo.** As marcações que o lançamento errado
deixou em linhas fantasma ("Aluno N") ou em pessoas que já saíram da lista não
seriam sobrescritas pela correção — essas linhas não existem no roster correto,
então nunca receberiam dado novo. A reconciliação zera essas células e
recalcula o total. Isso só acontece quando o scan cobriu o lote inteiro: num
scan parcial, zerar as linhas não lidas apagaria presença legítima, e nesse
caso a ferramenta avisa e não limpa.

As linhas "Aluno N" em si continuam existindo na planilha, agora vazias. Podem
ser apagadas à mão depois que todos os períodos do mês estiverem reconciliados
— apagá-las automaticamente deslocaria as linhas de baixo no meio do processo.

## Auditar o lote contra uma planilha

Depois de recuperar o lote, este comando diz, linha a linha, quem o
processamento antigo teria atribuído e quem realmente estava ali:

```bash
python recuperar_lote.py --auditar <lote_id> --contra impressao.xlsx
```

Saída no terminal e um CSV completo:

```
Lote auditado   : canela_20260512-090000  (539 linhas)
Mudanças        : 12 pessoa(s) mudaram de linha; 3 saíram da planilha
Linhas trocadas : 15 de 539

  linha  180 (pág 8): a presença foi para 'Fulano de Tal' mas era de 'Beltrano Silva'
  ...
```

Cada linha marcada como `TROCADO` é uma presença lançada na pessoa errada — em
duas direções ao mesmo tempo: alguém recebeu presença que não era sua e alguém
perdeu a sua.

### Descobrir de quando é cada scan

Se a pasta de scans está com nomes tipo `Digitalizar0007.pdf` e ninguém lembra
de que semana é cada um, não tem problema: **a informação está impressa na
própria folha**, em todas as páginas.

```bash
python identificar_scan.py <pasta_de_scans>
python identificar_scan.py <pasta> --csv identificacao.csv
```

```
ARQUIVO                          UNIDADE     PERÍODO             PÁGS  FONTE
scan0007.pdf                     ondina      29/06 a 04/07       26/26 texto
scan0008.pdf                     canela      06/07 a 10/07       20/22 texto
    ! SCAN INCOMPLETO: o cabeçalho diz 22 páginas e o arquivo tem 20.
      Faltam 2 — cada página perdida são 25 pessoas sem registro.
```

Duas formas de ler, nesta ordem: a **camada de texto** que o próprio scanner
grava no PDF (grátis, e os erros típicos de OCR de scanner — "RELAQAO",
"PROAE" — não atrapalham porque as datas e o nome da unidade sobrevivem); e,
quando não houver, **OCR da faixa superior** da primeira página.

O `Página X de Y` dá um brinde: comparado com o número de páginas do arquivo,
denuncia **scan incompleto**. Folha que não passou no alimentador são 25
pessoas sem registro, sem nenhum sinal de erro — e corrigir um período a
partir de um scan parcial zeraria a presença justamente delas. Por isso a
automação se recusa a usar scan incompleto.

> **O mês impresso não vale, a data vale.** Um bug antigo do gerador imprimiu
> folhas de junho com "MAIO" no título. Nada no sistema decide pelo nome do
> mês: o período vem sempre das datas `dd/mm`, e a aba do Sheets também
> (`_nome_aba_mes` lê o mês da data, não do cabeçalho). Quando o título
> discorda das datas, a folha é sinalizada — o que de quebra mapeia quais
> impressões saíram com o rótulo errado.

### Um maço não é um arquivo

O scan de uma semana quase nunca é um PDF só. O maço sai em partes ("1 a 10",
"11 a 23"), e quando o alimentador pula uma folha a parte é refeita — a pasta
acaba com pedaços que se sobrepõem. Então o `identificar_scan.py` agrupa por
semana e trata a **união** dos pedaços:

```
OK ondina      29/06 a 04/07     páginas   27/27  (3 arquivo(s))
      + 2026-08-13 (43).pdf
      + 2026-08-13 (44).pdf
      + 2026-08-13 (45).pdf
      - 2026-08-13 (46).pdf  (não acrescenta páginas)
!! canela      22/04 a 25/04     páginas    5/22  (1 arquivo(s))
      ! FALTAM as páginas [6..22] do lote — 425 pessoas ficariam sem registro.
?? ondina      08/06 a 13/06     páginas   27/27  (4 arquivo(s))
      ! NÃO VERIFICADO: os arquivos não trazem 'Página X de Y' legível.
```

Cada página traz `Página X de Y`, então dá para saber exatamente quais páginas
do lote cada arquivo contém e quais faltam no conjunto. A escolha dos arquivos
é gulosa pelo que cada um acrescenta de página nova, preferindo os de
paginação confiável e, em seguida, os mais recentes — uma redigitalização
existe porque a anterior saiu ruim.

As três marcas significam coisas diferentes, e a distinção evita retrabalho:

- **OK** — o maço está completo, verificado página a página.
- **!!** — falta página comprovadamente. Reescaneie antes de corrigir; o
  `aplicar` e o `conferir` pulam esses períodos.
- **??** — pode estar completo, mas os arquivos não trazem paginação legível
  para conferir. O `conferir` roda assim mesmo (não grava nada) e mostra a
  cobertura real da releitura; só o `aplicar` fica bloqueado.

### Fluxo automatizado — `corrigir_passivo.py`

Para não repetir os passos manualmente em cada semana, o `corrigir_passivo.py`
encadeia tudo. Ele é dividido em três comandos por um motivo: a máquina
descobre sozinha quais períodos estão errados, acha o template de cada um,
reconstrói o lote, reprocessa o scan e compara com a planilha — mas **não tem
como adivinhar qual arquivo de scan corresponde a qual semana**. Isso depende
de como a equipe nomeou e guardou os arquivos.

```bash
# 1. monta o plano, já preenchido no que der para descobrir
python corrigir_passivo.py preparar --templates ~/Downloads --scans D:/scans

# 2. abra plano_correcao.csv e preencha a coluna scan_pdf das linhas em branco

# 3. executa em modo seco: mostra o que mudaria, não grava nada
python corrigir_passivo.py conferir

# 4. grava as correções
python corrigir_passivo.py aplicar
python corrigir_passivo.py aplicar --periodo "29/06 a 04/07"   # uma só
```

O plano é um CSV com uma linha por período afetado, ordenado da maior perda
para a menor:

| coluna | de onde vem |
| --- | --- |
| `restaurante`, `aba`, `periodo`, `marcacoes_perdidas` | triagem |
| `template_pdf` | achado automaticamente na pasta de templates |
| `scan_pdf` | as partes do maço, separadas por `;` |
| `scan_paginas` | páginas lidas / total que a folha diz ter |
| `pagina_inicial` | 1, salvo se o scan começa no meio do lote |
| `lote_id`, `status`, `divergentes`, `csv` | preenchidas na execução |

Cada execução regrava o plano com o que aconteceu, então dá para rodar em
etapas: o que ficou pendente por falta de scan continua marcado como
`falta scan` e o que já foi corrigido não é refeito.

Scans incompletos entram no plano já com o status `scan incompleto` e são
pulados no `conferir` e no `aplicar`.

**Como o casamento automático evita errar de arquivo.** Um template só é
candidato se o cabeçalho contiver a assinatura do formulário
("RELAÇÃO DE BOLSISTAS"). Sem esse filtro, qualquer PDF da pasta que por acaso
tivesse duas datas viraria candidato — num teste com a pasta real de downloads,
um "Parecer Técnico referente à reabertura do PDV" chegou a casar com uma
semana de Ondina. E quando dois arquivos empatam, a ferramenta **não escolhe**:
deixa a linha em branco. Errar o arquivo grava uma correção errada no Sheets,
que é pior do que uma lacuna.

Os scans não passam por esse filtro porque são imagem, sem texto para
conferir. A verificação deles acontece depois: se o PDF não for uma folha de
presença, o alinhamento pelos marcadores falha e o período é reportado com
erro em vez de corrigido.

### Procedimento para o passivo

1. **Triar** — `python reconciliar.py --triagem`. Sai a lista de abas e
   períodos com rastro de lista trocada. Não precisa de scan nem altera nada.
2. **Corrigir as matrículas repetidas em pessoas diferentes** na planilha de
   origem. Esse problema não depende de lista trocada: acontece toda semana e
   continua acontecendo até ser corrigido.
3. **Levantar o material** dos períodos apontados: o PDF do template
   (recuperação exata) ou, na falta dele, o scan.
4. **Recuperar o lote** de cada um com `recuperar_lote.py`.
5. **Conferir** com `reconciliar.py --lote ... --scan ... --periodo ...`.
   Zero divergências significa que aquele período está correto — não precisa
   de nada.
6. **Aplicar** só onde houve divergência, com `--aplicar`.
7. **Guardar os CSVs** como registro do que foi corrigido e por quê.

O CSV traz as colunas `numero, pagina, correto_nome, correto_matricula,
atribuido_nome, atribuido_matricula, situacao` — dá para abrir no Sheets e
filtrar por `TROCADO`.

---

## Nota sobre a leitura das bolhas

Junto com os lotes foi corrigido o cálculo de preenchimento dos círculos, que
é a origem dos "252 casos ambíguos num scan de 25 nomes".

A medição anterior usava uma região **quadrada** de lado 2·raio ao redor do
centro, que inclui a borda impressa do círculo inteira. Essa borda sozinha
responde por cerca de 29% da área medida: num scan real, círculos **vazios**
liam 0,26–0,30 contra um corte de 0,40. A folga era de apenas 0,04 até a zona
de dúvida — bastava um scan um pouco mais escuro para *todos* os círculos
caírem nela de uma vez.

Agora a medição usa um disco de 0,65·raio, que exclui a borda. No mesmo scan:

| | vazios | preenchidos | folga até a zona de dúvida |
|---|---|---|---|
| antes (quadrado) | ~0,29 | 0,44–0,80 | 1,24× de escurecimento |
| agora (interior) | ~0,02 | 0,27–0,93 | ~5× de escurecimento |

O corte padrão acompanha o modo de medição (0,20 no interior, 0,40 no
quadrado), e nesse scan **nenhuma decisão de presença mudou** — as 115
marcações são exatamente as mesmas. O que muda é a margem: o sistema deixa de
quebrar quando a digitalização não sai perfeita.

Para reprocessar algo calibrado no método antigo, basta pôr no config:

```yaml
scan:
  medicao: quadrado
```
