# 0001 — Avisar sobre matrícula duplicada em vez de mudar a chave de identidade

Status: Aceito

## Contexto

Três casos em Ondina (e possivelmente outros ainda não achados) têm a
mesma matrícula associada a duas pessoas diferentes na planilha de origem
(fora do nosso controle). `_sincronizar_roster` (`google_sheets.py`) casa
alunos com linhas já existentes na aba por matrícula primeiro, nome como
fallback — `por_mat[chave] = r` só grava a primeira ocorrência, então a
matrícula fica permanentemente ligada à linha da primeira pessoa vista.
Toda semana em que a segunda pessoa aparece no scan, sua presença cai
nessa mesma linha e sobrescreve a da primeira: uma das duas fica sem
frequência lançada, sempre a mesma, sem erro visível.

## Decisão

Detectar o caso (`_detectar_matriculas_duplicadas`, rodando a cada
`exportar_para_sheets`) e expor um aviso (`aviso_matricula_duplicada`) na
UI toda vez que ele ocorrer, em vez de mudar a chave de identidade do
sistema para incluir o nome.

## Alternativas consideradas

- **Incluir o nome normalizado na chave de upsert** (`presencas` e
  `_sincronizar_roster` passariam a casar por matrícula+nome, não só
  matrícula). Resolveria de fato — cada pessoa ganharia sua própria linha
  mesmo com matrícula repetida. Rejeitada: muda uma premissa central do
  sistema (identidade = matrícula) por causa de um punhado de casos
  conhecidos, com o risco de fragmentar o histórico de alguém se o nome
  vier grafado de forma diferente entre semanas (typo, acento, abreviação)
  na planilha de origem. Trade-off julgado pelo usuário como não
  compensando o ganho.
- **Bloquear a exportação** daquela semana até alguém resolver manualmente
  (mesmo padrão do gate de `ordem_confiavel` em `corrigir_passivo.py`).
  Rejeitada por ora: pararia o fluxo semanal inteiro por um problema que
  não tem solução no código — o bloqueio faria sentido se houvesse uma ação
  corretiva possível do lado de cá, que não há.

## Consequências

Resolve o "descobrir tarde" — antes o problema só era percebido quando
alguém notava a ausência recorrente ou rodava `reconciliar.py --triagem`
manualmente; agora aparece na tela a cada exportação que envolve uma das
matrículas duplicadas.

Não resolve a perda de frequência em si: a pessoa que perde a linha
continua perdendo, semana a semana, até a planilha de origem ser
corrigida (fora do nosso controle) ou até esta decisão ser revisitada.
Fica em aberto como pendência operacional urgente em `todo.md`.
