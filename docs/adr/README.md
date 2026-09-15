# ADRs — Registro de decisões de arquitetura

Cada arquivo aqui documenta **uma** decisão não óbvia: o problema, o que foi
decidido, por que essa opção e não outra, e o que ela deixa sem resolver.
Serve pra não ter que reconstruir o raciocínio lendo commits ou perguntando
de novo — principalmente quando a decisão foi "mitigar" ou "aceitar um
limite" em vez de resolver de vez.

Não é lugar para o estado atual do projeto (isso é `todo.md`) nem para o
desenho detalhado de uma feature grande (isso continua em arquivos como
`SINCRONIZACAO_LOTES.md` ou `LOTES.md`, referenciados pelo ADR quando
existirem). Um ADR é curto — se está passando de uma página, provavelmente
parte do conteúdo é desenho, não decisão.

## Quando criar um

Quando duas ou mais opções razoáveis foram consideradas e a escolhida tem
um trade-off real (não resolve tudo, tem um custo, fecha uma porta). Bug
fix comum não vira ADR — só decisão que alguém, no futuro, poderia
questionar sem saber que já foi discutida.

## Template

```markdown
# NNNN — Título curto da decisão

Status: Aceito | Proposto | Substituído por NNNN

## Contexto

Qual problema motivou a decisão. O que estava acontecendo.

## Decisão

O que foi escolhido, em uma ou duas frases diretas.

## Alternativas consideradas

- Opção A — por que não
- Opção B — por que não

## Consequências

O que essa escolha resolve, o que ela **não** resolve, e o que fica em
aberto ou precisa de atenção manual por causa dela.
```

## Índice

- [0001](0001-aviso-matricula-duplicada.md) — Avisar sobre matrícula duplicada em vez de mudar a chave de identidade
