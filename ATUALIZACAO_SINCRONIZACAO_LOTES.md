# Sincronização de lotes + recuperação na UI — resumo da atualização (2026-09-10)

> Este documento existe pra dar contexto rápido em qualquer máquina, na
> hora do `git pull`. Detalhes de design e das opções descartadas estão em
> `SINCRONIZACAO_LOTES.md` — aqui é só o resumo prático: o que mudou, o que
> fazer, e o que ainda é limitação conhecida.

## O que mudou

1. **Lotes agora sincronizam entre máquinas**, automaticamente, via Google
   Sheets. Até aqui, o lote (roster + geometria congelados na impressão)
   só existia na máquina que gerou o template — a máquina que processava o
   scan noutra máquina não o enxergava, caindo no modo legado ou exigindo
   recuperação manual.
2. **Lotes grandes (Canela, 500+ pessoas) sincronizam também.** O JSON do
   lote passa do limite de 50.000 caracteres por célula do Sheets — agora
   é fatiado automaticamente em colunas extras e remontado na leitura.
3. **Nova aba "Recuperar lote" na interface web**, pra recuperar o lote de
   scans já impressos/preenchidos sem precisar de linha de comando.

## O que fazer depois do pull

**Nada de configuração nova.** Isso já vem pronto no `config_sheets.yaml`
commitado (aponta pro `dashboard_spreadsheet_id` que já existe). Só
precisa, como sempre precisou:

- `credentials_sheets.json` presente na pasta (mesmo arquivo de sempre —
  se essa máquina já exporta pro Sheets, já está tudo certo).
- `pip install -r requirements.txt` — não tem dependência nova além do
  que já era exigido pra exportação (gspread, google-auth). Só rode se
  essa máquina nunca teve o ambiente instalado.

A partir do próximo lote gerado (ou recuperado) em qualquer máquina com
esse pull, ele já aparece automaticamente nas outras — sem nenhum passo
manual.

## Onde os dados ficam

- **Roster/geometria de cada lote:** aba nova `lotes_sync`, dentro da
  mesma planilha do dashboard (a que já era usada pro Looker Studio) —
  não é uma planilha nova, é só uma aba a mais na que já existia.
- **PDF do lote NÃO sincroniza** — só existe na máquina onde foi gerado
  (célula de planilha não é lugar pra guardar PDF). Reimprimir a folha
  exata continua funcionando só ali; nas outras máquinas, o roster já
  chega — é o que resolve o bug de identidade, que é o problema original.

## Nova aba "Recuperar lote"

Pra scans já impressos e preenchidos que não têm lote (a sincronização só
vale daqui pra frente, não recria lotes que nunca existiram):

1. Abre a aba **Recuperar lote**.
2. Informa a pasta com os scans preenchidos (aceita subpastas).
3. Opcionalmente, a pasta com os templates originais em PDF (também aceita
   subpastas) — se tiver, a recuperação é exata; se não tiver, cai pra OCR
   do próprio scan.
4. Clica em "Buscar e recuperar". O sistema lê o cabeçalho impresso de
   cada folha (não o nome do arquivo) pra descobrir sozinho o restaurante
   e o período, casa cada scan com o template certo, e recupera o lote.
5. Depois é só ir na aba **Processar scan** — os lotes recuperados
   aparecem sozinhos na lista, sem link especial.

Rodar duas vezes não duplica — se o lote daquele período já existe, é
reaproveitado.

## Limitações conhecidas

- PDF não sincroniza entre máquinas (ver acima).
- Teto de segurança: um lote com JSON maior que ~630KB (bem acima de
  qualquer lote real visto até aqui — o maior testado foi Canela com
  539 pessoas / 83KB) para de tentar fatiar e avisa que ficou só local,
  em vez de travar.
- A recuperação em lote (aba nova) não escreve nada nas planilhas de
  presença — só cria o lote. O lançamento continua acontecendo depois,
  pelo fluxo normal de processamento, com os mesmos gates de segurança de
  sempre.

## Arquivos novos/alterados

- `lote_sync.py` — **novo**. Sincronização via Sheets (upload/download/
  listagem/marcação de processado).
- `lote.py` — chama `lote_sync` de forma best-effort em `salvar_lote`,
  `carregar_lote`, `listar_lotes`, `registrar_processamento`; nova função
  `garantir_pdf_local`.
- `web.py` — nova aba "Recuperar lote" + rota `POST /recuperar_lotes`.
- `config_sheets.yaml` — seção `lotes_sincronizacao` (opcional; vazio =
  reaproveita `dashboard_spreadsheet_id`).
- `tests/test_lote.py` — testes da sincronização com `lote_sync` mockado
  (nenhum toca rede).
- `SINCRONIZACAO_LOTES.md` / `todo.md` — atualizados com o design completo
  e o estado atual.
