# Plano de Correção — Sistema de Leitura Automatizada de Frequência

## Contexto

Sistema tipo "leitor de gabarito" (bolinhas marcadas) para folhas de frequência
física, alimentado por uma planilha/lista de pessoas que sofre inserções e
remoções ao longo do tempo. A folha impressa (PDF gerado via ReportLab) já traz
**Nº, Nome e Matrícula** impressos, e colunas de bolinhas para presença (ex:
Quinta A/J, Sábado A/J).

## Bugs reportados originalmente

- [ ] **UI de casos ambíguos com contagem errada** — 252 casos em scan com
  apenas 25 nomes.
- [ ] **Revisão de ambíguos não sincroniza com o Google Sheets** — botão
  "processar presença" envia dados desatualizados.
- [ ] **Leitura frágil da planilha de referência** — qualquer diferença entre
  a planilha atual e a esperada faz o sistema ignorar colunas ou pessoas.

## Causa raiz comum

O sistema identifica pessoas **pela posição da linha**, comparando com a
planilha de referência **no momento do processamento** — e essa planilha muda
com o tempo (inserções/remoções). Não existe um identificador estável
amarrando cada linha da folha física à pessoa correta.

---

## Descobertas ao inspecionar a folha real (`template_sao_lazaro_fds_especial.pdf`)

Rodando `pdffonts` e `pdfplumber` no PDF:

- O PDF **não é escaneado** — tem camada de texto real (Producer: ReportLab).
  Isso significa que **Nome e Matrícula podem ser extraídos com 100% de
  precisão diretamente do PDF, sem OCR**, antes mesmo de imprimir.
- Extraindo os 128 registros (6 páginas), apareceram problemas reais nos
  dados:
  - **Matrícula duplicada**: linhas 53 e 54, mesma pessoa
    ("Hellen Barbosa dos Santos", matrícula `222120254`) — confirmado como
    **erro de digitação** na planilha de origem.
  - **Matrículas com tamanhos diferentes** (8, 9 e 10 dígitos) — isso é
    **esperado e correto**: refletem categorias diferentes (graduação,
    mestrado, aluno estrangeiro). **Não é bug.**

### Lição prática
A matrícula deve ser tratada **sempre como string/texto**, nunca como
número, e **nunca validada por quantidade fixa de dígitos** — isso quebraria
silenciosamente para mestrado/estrangeiro.

---

## Roteiro de correção (ordem recomendada)

### 1. Criar um identificador estável por pessoa
Usar a **matrícula (como string)** como chave única em todo o pipeline —
leitura, ambíguos e exportação. Resolve a base dos bugs 1 e 3.

### 2. Mapear colunas por cabeçalho, não por posição
No parser, buscar a coluna pelo texto do cabeçalho (`Nome`, `Matrícula`,
`Quinta`, `Sábado`...) com tolerância a variação de espaço/maiúsculas, em vez
de índice fixo (`coluna[3]`). Se uma coluna esperada não for encontrada,
avisar explicitamente em vez de pular silenciosamente.

### 3. Refatorar a lógica de ambíguos para agrupar por (pessoa, dia)
O contador de 252 casos com 25 nomes sugere contagem por célula ou por
combinação, em vez de por pessoa. Corrigir para acumular ambiguidades por
`(matricula, dia)`. O total nunca deve passar de `pessoas × dias avaliados`.

### 4. Fazer da revisão manual a única fonte de verdade
Toda edição de um caso ambíguo deve escrever de volta na **mesma estrutura**
(indexada por matrícula) que alimenta a exportação para o Google Sheets. A
revisão não pode viver só no estado da UI.

### 5. Adicionar validação e log de divergências
Antes de processar: comparar pessoas detectadas vs. lista de referência,
apontando quem sumiu, quem é novo, e quais colunas não bateram.

---

## Como resolver a leitura da matrícula (solução específica)

Como o PDF é **gerado pelo próprio sistema**, a matrícula não precisa ser
"lida" do scan de jeito nenhum — ela já é conhecida no momento da geração.

### Fluxo recomendado

```
1. Ao gerar o PDF do lote:
   → validar duplicidade de matrícula (script abaixo)
   → salvar snapshot.json: { linha: N, pagina: P, nome, matricula }

2. Imprimir → aplicar → escanear a folha preenchida
   (só as bolinhas de presença mudam; nome/matrícula continuam os mesmos)

3. No processamento do scan:
   → ler bolinhas nas coordenadas fixas do template (já existe)
   → mapear linha/página → matrícula usando snapshot.json
     (NUNCA usando a planilha de referência "atual")
   → (opcional, camada extra de segurança) OCR da coluna Matrícula
     no scan, comparando com o snapshot esperado, para detectar
     folha fora de ordem/lote errado
```

### Por que isso funciona
O snapshot é gerado **no mesmo instante** em que a folha é impressa — ele
nunca fica desatualizado, porque não depende de nada que mude depois. Mesmo
que pessoas sejam inseridas/removidas da planilha viva, a folha já impressa
continua batendo certo.

### Validação de duplicidade antes de gerar o PDF

```python
from collections import Counter

def validar_lote(lista_pessoas):
    matriculas = [p["matricula"] for p in lista_pessoas]  # sempre string
    contagem = Counter(matriculas)
    duplicadas = {m: c for m, c in contagem.items() if c > 1}

    if duplicadas:
        print("ATENÇÃO: matrículas duplicadas antes de gerar o PDF:")
        for m, c in duplicadas.items():
            nomes = [p["nome"] for p in lista_pessoas if p["matricula"] == m]
            print(f"  {m} aparece {c}x: {nomes}")
        raise ValueError("Corrija as duplicidades na planilha de origem.")

    return True
```

### Extração do snapshot a partir de um PDF já pronto (fallback)

Se por algum motivo não for possível gerar o snapshot no momento da criação
do PDF (ex: template vem pronto de outro processo), dá pra extrair depois via
`pdfplumber`, já que o PDF tem camada de texto real:

```python
import pdfplumber, re, json

padrao = re.compile(r'^(\d+)\s+(.+?)\s+(\d{6,12})$')
registros = []

with pdfplumber.open("template.pdf") as pdf:
    for pagina in pdf.pages:
        for linha in pagina.extract_text().split("\n"):
            m = padrao.match(linha.strip())
            if m:
                num, nome, matricula = m.groups()
                registros.append({
                    "linha": int(num), "nome": nome, "matricula": matricula
                })

with open("snapshot_lote.json", "w", encoding="utf-8") as f:
    json.dump(registros, f, ensure_ascii=False, indent=2)
```

---

## Sobre banco de dados

**Decisão: não usar banco de dados** — apenas uma pessoa processa presenças
por dia, então a complexidade de um SGBD não se justifica.

Alternativa adotada: **arquivos estruturados (JSON/CSV)**:
- Cadastro mestre (matrícula → nome → categoria)
- Snapshots por lote de impressão (`snapshots/{lote_id}.json`)
- Resultado da revisão manual gravado no mesmo objeto usado para exportar ao
  Google Sheets (resolve o bug 2 de sincronização)

Se no futuro o volume ou o número de pessoas processando crescer, reavaliar
SQLite como camada intermediária (sem precisar de servidor).

---

## Ciclo de vida do arquivo snapshot (gerenciamento automático)

O `snapshot.json` deve ser criado e gerenciado pelo próprio sistema — sem
trabalho manual. Porém **não deve ser apagado imediatamente** após o
processamento das presenças.

### Por que não apagar na hora

- Se o envio ao Google Sheets falhar no meio do caminho (rede, erro de API),
  você perde a referência que permitiria reprocessar sem reescanear a folha.
- Se surgir uma contestação depois ("fui marcado ausente errado"), não há
  como auditar qual matrícula estava em qual linha daquela folha.
- O arquivo é minúsculo (dezenas/centenas de registros = poucos KB) — não há
  ganho real de espaço em apagar cedo.

### Estratégia recomendada: arquivar, não deletar na hora

Mover o snapshot para uma subpasta de "processados" com metadados de
controle, e só apagar de fato depois de um período de retenção (ex: fim do
semestre) **e** com a confirmação de que a sincronização com o Google Sheets
realmente deu certo (não apenas que o botão foi clicado).

```python
import json, os
from datetime import datetime

SNAPSHOTS_DIR = "snapshots"
PROCESSADOS_DIR = "snapshots/processados"

def marcar_processado(lote_id, sincronizado_com_sheets: bool):
    caminho = f"{SNAPSHOTS_DIR}/{lote_id}.json"
    with open(caminho, "r", encoding="utf-8") as f:
        dados = json.load(f)

    # metadados de controle, não apagam o conteúdo original
    dados_meta = {
        "lote_id": lote_id,
        "processado_em": datetime.now().isoformat(),
        "sincronizado_com_sheets": sincronizado_com_sheets,
        "registros": dados
    }

    os.makedirs(PROCESSADOS_DIR, exist_ok=True)
    destino = f"{PROCESSADOS_DIR}/{lote_id}.json"

    with open(destino, "w", encoding="utf-8") as f:
        json.dump(dados_meta, f, ensure_ascii=False, indent=2)

    if sincronizado_com_sheets:
        os.remove(caminho)  # sai da pasta "ativa", não desaparece de vez
```

### Estrutura de pastas resultante

- **`snapshots/`** → lotes ainda não confirmados como sincronizados (o
  sistema sabe que precisa cuidar deles).
- **`snapshots/processados/`** → histórico auditável, com registro de quando
  e se sincronizou de fato.
- **Limpeza definitiva** (deleção real) vira uma rotina separada e
  deliberada — ex: "apagar processados com mais de 90 dias" — rodada
  manualmente ou agendada, nunca automática no exato momento do
  processamento.

---

## Checklist de implementação

- [ ] Trocar toda comparação por posição por comparação por matrícula (string)
- [ ] Remover qualquer validação de tamanho fixo de dígitos na matrícula
- [ ] Implementar `validar_lote()` (duplicidade) antes de gerar cada PDF
- [ ] Gerar `snapshot_{lote_id}.json` junto com cada PDF impresso
- [ ] Reescrever leitura de bolinhas para usar `snapshot.json` como
      referência de identidade (não a planilha "atual")
- [ ] Refatorar contagem de ambíguos para agrupar por `(matricula, dia)`
- [ ] Garantir que revisão manual escreve na mesma estrutura usada na
      exportação ao Google Sheets
- [ ] Adicionar log de divergências (pessoas novas/removidas, colunas não
      encontradas) antes do processamento
- [ ] (Opcional) OCR da coluna Matrícula no scan como checagem cruzada,
      restrito a dígitos (`tessedit_char_whitelist=0123456789`)
- [ ] Implementar arquivamento de snapshots processados (não deleção
      imediata) com confirmação real de sincronização com o Google Sheets
- [ ] Definir rotina separada de limpeza definitiva por período de retenção

---

## Perguntas em aberto para fechar o design

- O PDF é gerado por um script próprio (dá pra inserir a geração do
  snapshot no mesmo processo) ou vem pronto de outra ferramenta?
- Onde fica o cadastro mestre hoje (planilha única, arquivo local)?
- Qual formato de saída o Google Sheets espera exatamente (colunas,
  ordenação)?
