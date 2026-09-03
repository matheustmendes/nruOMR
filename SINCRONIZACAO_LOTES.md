# Sincronização de lotes entre máquinas — problema em aberto

> Documento de continuidade. Escrito para retomar o desenvolvimento depois —
> não é um plano fechado, é o registro do problema, do porquê ele existe na
> arquitetura atual, e das opções para resolver. Nada aqui foi implementado.

## O problema, em uma frase

**A lista é gerada numa máquina, o lote nasce só nela; o scan é processado
noutra, que não tem esse lote.**

## Como isso quebra na prática hoje

`lote.py` grava cada lote em `lotes/<lote_id>.json` (+ o PDF em
`lotes/<lote_id>.pdf`), num caminho relativo à própria instalação do
sistema:

```python
LOTES_DIR = os.path.join(SCRIPT_DIR, "lotes")          # lote.py:41
PROCESSADOS_DIR = os.path.join(LOTES_DIR, "processados") # lote.py:42
```

`SCRIPT_DIR` é a pasta onde o `nruOMR-main` está instalado *naquela
máquina*. Não há nada de rede, nuvem ou banco de dados aqui — é disco local.
E `lotes/` está deliberadamente fora do git (`.gitignore:20`), porque tem
nome e matrícula de gente real; git nunca foi (nem deveria ser) o canal de
sincronização.

Fluxo que expõe o buraco:

1. Alguém gera o template numa máquina A — pela aba "Gerar template" do
   `web.py`, rota `/gerar_template` (web.py:383). Isso cria o lote *só no
   `lotes/` de A*.
2. O PDF resultante sai de A por algum canal manual (pendrive, e-mail,
   impressão direta) e vira folha de papel numa unidade.
3. A folha é preenchida, escaneada. O arquivo do scan chega à máquina B —
   a que centraliza o processamento — por outro canal manual qualquer.
4. B tenta processar. A rota `/lotes` (web.py:617) só lista o que existe no
   `lotes/` *local* de B. O lote de A não está lá.
5. Resultado: ou cai no modo legado (planilha atual decide a identidade —
   o bug original que este sistema inteiro existe para matar), ou precisa
   de `recuperar_lote.py` (OCR, impreciso, e só funciona se o PDF do
   template *também* tiver sido carregado manualmente pra B).

Ou seja: o sistema resolveu o problema de identidade *dentro de uma
máquina* e recriou o mesmo problema *entre máquinas*, porque a premissa
implícita de todo o design de lotes (construído nesta sessão) foi "tudo
roda num computador só". Essa premissa é falsa na operação real.

## Por que isso é mais urgente do que parece

O ciclo de vida do lote (`lote.py`, seção "Ciclo de vida") já foi desenhado
para não apagar nada sem confirmação de sincronização com o Sheets — mas
essa lógica só sabe se *o Sheets* está sincronizado, não sabe nada sobre
*outra máquina* ter ou não o lote. As duas sincronizações (lote entre
máquinas, resultado no Sheets) são problemas distintos e hoje só o segundo
está resolvido.

## O que já está pronto e pode ser reaproveitado

- **Credenciais Google já configuradas e funcionando** — `config_sheets.yaml`
  + `credentials_sheets.json`, conta de serviço com escopo
  `spreadsheets`. Qualquer solução em cima de Google (Sheets ou Drive) reusa
  essa mesma credencial; Drive só precisa adicionar o escopo
  `drive.file` na mesma conta de serviço.
- **O lote_id impresso no rodapé da folha** (`gerar_template.py`,
  `desenhar_rodape`) — já é um identificador estável, curto, que sobrevive
  ao papel. Qualquer solução de sincronização pode continuar usando esse
  mesmo ID como chave.
- **O lote já é JSON pequeno** (dezenas de KB) — nunca foi pensado como
  "arquivo grande", isso simplifica bastante as opções de transporte.
- **`lote.montar_lote`/`salvar_lote`/`carregar_lote`** já isolam toda leitura
  e escrita do armazenamento atrás de três funções — trocar *onde* o lote
  mora é, em princípio, localizado nessas funções (mais os poucos lugares
  que usam `LOTES_DIR`/`PROCESSADOS_DIR` diretamente para o PDF).

## Opções para a sincronização (nenhuma decidida)

### A. Pasta sincronizada (Google Drive Desktop / OneDrive / Dropbox)

Trocar `LOTES_DIR` para apontar para dentro de uma pasta sincronizada por um
cliente de desktop já instalado nas máquinas envolvidas.

- **Prós:** zero código novo de transporte — é só mudar um caminho.
  Funciona offline (sincroniza quando volta a rede). O usuário *vê* os
  arquivos, o que ajuda a debugar.
- **Contras:** depende de instalar/configurar o cliente de sync em cada
  máquina (fricção operacional, não é algo o sistema controla). Sincronização
  não é instantânea nem garantida — se alguém processa o scan antes do lote
  chegar, volta ao problema. Sem controle de conflito real (dois lotes com
  mesmo nome de arquivo colidindo).

### B. Google Sheets como "banco" do lote

Uma aba dedicada (ou planilha separada) onde cada linha é um lote — o JSON
inteiro cabe numa célula (é pequeno). Grava com o mesmo `gspread` que já é
usado para os resultados.

- **Prós:** nenhuma infraestrutura nova — mesma credencial, mesma
  biblioteca, já teste do em produção nesta sessão inteira. Centralizado por
  natureza: todas as máquinas com internet enxergam a mesma fonte.
- **Contras:** Sheets tem cota de leitura/escrita por minuto — já esbarramos
  nisso nesta sessão (triagem precisou de retry com backoff). Usar como
  banco de dados de verdade é usar a ferramenta fora do que ela foi feita
  pra fazer; funciona em escala pequena (que é o caso aqui — uma pessoa
  processando por dia), mas não escala se o uso crescer.

### C. Google Drive API (pasta compartilhada via API, não cliente desktop)

Como a opção A, mas o próprio `web.py` faz upload/download via API do Drive
em vez de depender de um cliente de sincronização instalado.

- **Prós:** sem depender de instalar nada nas máquinas — só precisa de
  internet e da credencial (que já existe, só precisa do escopo extra).
  Transporte fica sob controle do próprio sistema (dá pra saber
  *programaticamente* se o lote já chegou, em vez de confiar num cliente de
  sync de terceiro).
- **Contras:** mais código que as opções A/B (chamadas de API de
  upload/download/list, tratamento de erro de rede). Precisa decidir a
  estrutura da pasta compartilhada e permissões.

### D. Banco de dados de verdade (hospedado)

Um Postgres/Firebase/Supabase gerenciado, com uma tabela `lotes`.

- **Prós:** é a ferramenta certa para o problema — concorrência, consulta,
  integridade, tudo de graça. Cresce sem dor se o sistema crescer (mais
  unidades, mais gente processando).
- **Contras:** infraestrutura nova de verdade — conta, custo (mesmo que
  pequeno/gratuito no tier free), mais uma coisa para manter no ar e
  autenticar. Provavelmente desproporcional ao volume atual (uma pessoa,
  poucas dezenas de lotes por mês).

### E. Servidorzinho central (Flask simples, hospedado em algum lugar)

Um serviço HTTP mínimo — `POST /lotes` grava, `GET /lotes/<id>` devolve —
rodando em algo tipo Render/Railway free tier, ou até um Raspberry Pi na
sala do NRU.

- **Prós:** desenhado exatamente pro formato de uso (todas as máquinas do
  sistema já falam HTTP entre si, via `web.py`). Dá pra reusar bastante
  código do próprio `lote.py`.
- **Contras:** é o item da lista que mais parece "projeto novo" — precisa de
  hospedagem, precisa ficar no ar, precisa de manutenção. Só compensa se as
  opções mais simples (B ou C) esbarrarem em alguma limitação real.

### Combinação pragmática — "o quick win antes da solução definitiva"

Isso não é uma das opções acima, é algo que dá pra fazer **hoje, sem
nenhuma infraestrutura nova**: o download do template já existe
(`/lote/<id>/pdf`, web.py:625) — bastaria um segundo botão "baixar lote
completo" que empacota `.json` + `.pdf` num `.zip`, e um upload
correspondente ("importar lote") na tela de processar, que só salva o
`.json` recebido em `LOTES_DIR` local antes de listar os lotes disponíveis.
Continua sendo transporte manual (pendrive, e-mail, WhatsApp Web — o que já
é usado hoje pra levar o PDF pra impressão), mas fecha o buraco sem esperar
a solução de sincronização "de verdade" ficar pronta. Vale considerar como
ponte, não como destino.

## Perguntas para decidir antes de implementar

Ficam em aberto — são do usuário, não algo pra eu resolver sozinho:

1. **Quantas máquinas realmente entram nisso, e com que frequência têm
   internet?** Muda o peso relativo de A/B/C (todas exigem rede; se alguma
   máquina gera lote offline com frequência, isso pesa a favor de manter um
   transporte manual/pendrive como fallback sempre).
2. **Existe já um Google Workspace institucional (da PROAE/UFBA) por trás
   disso, ou é conta pessoal?** Lote carrega nome e matrícula de
   beneficiários — dado pessoal. Onde ele pode legitimamente morar importa
   tanto quanto a engenharia.
3. **Sincronização em tempo real é necessária, ou dá para ser periódica /
   sob demanda?** Se o fluxo real é "gera lista segunda de manhã, processa
   scan só sexta", nem precisa ser instantâneo — simplifica muito as
   opções.
4. **Vale a pena resolver isso antes ou depois de terminar a limpeza do
   passivo?** (ver `todo.md` — ainda há 5 períodos pendentes do passivo,
   nenhum bloqueado por este problema de sincronização especificamente.)

## Sobre "entender melhor o fluxo do lote e deixar mais user-friendly"

Problema relacionado mas distinto do de sincronização — mesmo com os
arquivos no lugar certo, a experiência de quem não é técnico hoje é áspera
em vários pontos. Mapeando o que existe:

- **O que já é amigável:** a aba "Processar presenças" do `web.py` já tem
  um seletor de lote com data/quantidade de gente visível, e pré-seleciona
  sozinho quando há só um lote ativo (`web.py`, função `carregarLotes` no
  JS). O lote_id impresso no rodapé da folha é o elo físico — sem isso a
  pessoa nem saberia que "lote" existe.
- **O que não é amigável:**
  - `recuperar_lote.py` e `corrigir_passivo.py` são só linha de comando,
    com jargão técnico direto na tela (OCR, distância de edição,
    "sem_match", "cobertura_confiavel"). Ninguém leigo mexe nisso sem
    alguém técnico do lado.
  - O modo legado (sem lote) ainda existe no `/processar` — um estagiário
    apressado pode simplesmente não perceber o aviso e processar do jeito
    errado, porque a UI permite as duas coisas sem deixar claro *por que*
    uma é preferível.
  - Quando o lote não aparece na lista (justamente o cenário deste
    documento — máquina errada), a mensagem que a pessoa vê é genérica
    ("nenhum lote impresso para este restaurante") — não orienta o que
    fazer a seguir.
  - Nada na interface explica, numa frase, *o que é* um lote e *por que*
    ele importa — pra quem nunca leu este repositório, é só mais um menu.

Vale desenhar isso como um fluxo próprio (provavelmente depois de resolver
a sincronização, já que boa parte da fricção de UX hoje é sintoma direto do
buraco de sincronização — "cadê meu lote" é a pergunta que mais vai
aparecer). Ideias soltas para quando chegar a hora, sem compromisso:

- Um texto fixo curto na aba de processar explicando lote em uma frase
  ("é o que garante que a presença vá pra pessoa certa, mesmo se a lista
  mudou depois da impressão").
- Se nenhum lote bater, a UI already tem esse gancho — melhorar a mensagem
  pra ser um passo a passo ("não achou o lote? confira se está processando
  na mesma máquina onde a lista foi gerada, ou peça pra alguém te mandar o
  arquivo") em vez de um erro seco.
- Botão de import de lote (ver "quick win" acima) direto na tela de
  processar, não escondido em CLI.

## Onde estava o trabalho quando este documento foi escrito

A limpeza do passivo de listas trocadas está praticamente fechada — ver
`todo.md`. 9 de 17 períodos corrigidos e confirmados nesta sessão, 3 já
estavam certos, 5 seguem bloqueados por razões específicas documentadas lá
(sinal de OCR insuficiente em 2, ordem de página não confirmável em 2 por
excesso de arquivos sobrepostos, 1 com matrículas OCR não resolvidas). Esse
trabalho é independente do problema de sincronização descrito aqui — pode
retomar qualquer um dos dois primeiro.
