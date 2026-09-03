# nruOMR — Sistema de Controle Automatizado de Frequência

**Unidade responsável:** Núcleo de Restaurantes Universitários (NRU) — PROAE/UFBA

**Unidades atendidas:** Restaurante Universitário de Ondina, Ponto de Distribuição do Canela (PDCA) e Ponto de Distribuição de São Lázaro (PDSL)

**Responsável técnico:** Matheus Torres

**Coautoria:** Lais Maranduba de Matos Pereira e Ana Livia de Abreu Macedo Santos

**Desenvolvimento iniciado em:** abril de 2026 · **Entrada em operação:** semestre 2026.2

---

## 1. Apresentação

O nruOMR é um sistema de leitura óptica que automatiza a apuração da frequência dos estudantes beneficiários nas unidades de alimentação do Núcleo de Restaurantes Universitários. A cada semana, as listas de presença preenchidas em papel nas três unidades são digitalizadas e processadas pelo sistema, que identifica as refeições registradas por cada estudante e consolida os resultados em planilhas mensais de acesso compartilhado. O sistema também mantém um painel de acompanhamento que sinaliza automaticamente os beneficiários com frequência abaixo do mínimo exigido, cruzando essa informação com as justificativas de ausência apresentadas pelos estudantes.

## 2. O problema que motivou o sistema

O controle de frequência dos beneficiários é uma obrigação permanente do Núcleo: é ele que sustenta a verificação do uso efetivo do benefício e embasa as decisões sobre manutenção ou suspensão. Até então, essa apuração era feita inteiramente à mão — a equipe conferia as listas folha a folha e transcrevia os resultados, nome por nome, para uma planilha no computador.

A escala torna o problema evidente. São **1.358 beneficiários** atendidos: 665 em Ondina, 565 no PDCA e 128 no PDSL. Como cada estudante dispõe de duas refeições diárias, uma semana comum reúne aproximadamente **16 mil marcações a conferir**. Esse volume se distribui em cinco listas semanais — uma em Ondina e duas em cada uma das outras duas unidades, contemplando dias úteis e fins de semana —, e cada lista consumia em média **1 hora e 20 minutos** de trabalho. O total chegava a **6 horas e 40 minutos por semana**, ou cerca de **27 horas mensais** dedicadas exclusivamente a transcrição.

Além do custo em horas de trabalho, o procedimento apresentava duas fragilidades. A transcrição manual de milhares de registros está sujeita a erros de leitura e digitação que, uma vez cometidos, dificilmente são detectados. E a apuração ficava defasada: quando os dados estavam consolidados, já haviam perdido parte de sua utilidade para a gestão do benefício.

## 3. Como funciona

O princípio é o mesmo empregado na correção de cartões-resposta de concursos e vestibulares: a folha preenchida é digitalizada e o computador identifica quais círculos foram marcados. O processo se organiza em sete etapas.

**1. Geração das listas.** A partir da relação de beneficiários, o próprio sistema gera as listas em PDF, já com os nomes impressos e os círculos posicionados. Isso garante que toda folha em circulação siga exatamente o formato que o sistema sabe ler.

**2. Preenchimento.** No atendimento, marca-se um círculo para o almoço e outro para o jantar, em cada dia da semana.

**3. Digitalização.** Ao final do período, as folhas são digitalizadas em um scanner comum e salvas em um único arquivo.

**4. Leitura automática.** O sistema localiza quatro marcas impressas nos cantos de cada página e as utiliza para corrigir eventual inclinação ou variação de escala introduzida pelo scanner. Só então examina cada círculo e determina se foi preenchido.

**5. Conferência humana.** Marcações ambíguas — círculos preenchidos pela metade, riscados ou apagados — não são decididas pelo sistema. Elas são separadas e apresentadas ampliadas na tela, para que o operador confirme ou corrija cada caso. A decisão final em situações duvidosas permanece sempre com a equipe.

**6. Registro.** Confirmada a leitura, os resultados são gravados na planilha mensal da unidade, organizada por semana e acessível a toda a equipe. O sistema mantém os períodos em ordem cronológica automaticamente, independentemente da ordem em que as listas forem processadas.

**7. Acompanhamento.** Um painel reúne os beneficiários com menos de dez presenças no mês, cruzando cada caso com as justificativas de ausência recebidas por formulário e indicando quais já foram analisadas e quais aguardam parecer.

## 4. Benefícios

- **Redução do tempo de apuração.** As 27 horas mensais de transcrição passam a ser minutos de processamento, somados ao tempo de conferência dos casos duvidosos.
- **Maior confiabilidade.** A eliminação da transcrição manual remove a principal fonte de erro do processo anterior.
- **Informação tempestiva.** Os dados ficam disponíveis no mesmo dia da digitalização, permitindo acompanhamento ao longo do mês.
- **Padronização entre as unidades.** As três unidades passam a operar com o mesmo formato de lista, o mesmo critério de apuração e a mesma estrutura de planilha.
- **Identificação ativa de casos.** O painel sinaliza automaticamente quem está abaixo do mínimo, em vez de exigir busca manual na planilha.
- **Custo nulo de licenciamento.** O sistema é construído sobre software livre e opera em um computador comum, sem aquisição de equipamento ou licenças.

## 5. Situação atual e próximos passos

O sistema está em desenvolvimento desde abril de 2026 e teve suas funcionalidades validadas com listas reais das três unidades. A entrada em operação regular está prevista para o semestre 2026.2.

Há aperfeiçoamentos já mapeados para as próximas etapas, entre eles a propagação automática das correções feitas na tela de conferência para a planilha compartilhada e o refinamento do critério de identificação de marcações ambíguas. Cabe registrar que a qualidade da leitura depende da digitalização: folhas amassadas, dobradas ou digitalizadas em baixa resolução aumentam o número de casos encaminhados à conferência humana — sem, contudo, comprometer o resultado final, já que nenhum caso duvidoso é decidido automaticamente.

## 6. Necessidades para a operação

A digitalização é o único ponto do fluxo que depende de equipamento físico, e é também o que determina a qualidade de tudo o que vem depois. O scanner atualmente disponível é um **Kodak ScanMate i1150**, modelo lançado em 2014 e há anos fora de linha. Sua substituição é a principal necessidade material do sistema, por três razões.

**Risco de descontinuidade.** Scanners de produção dependem de peças de consumo que se desgastam pelo uso — roletes de alimentação e módulos de separação, substituídos periodicamente. Para um equipamento fora de linha, esses insumos tornam-se progressivamente escassos e caros, assim como o suporte do fabricante e a compatibilidade de drivers com versões atuais do sistema operacional. Uma falha sem reposição disponível interrompe a apuração de frequência das três unidades por tempo indeterminado.

**Ponto único de falha.** Todo o fluxo depende desse equipamento. Sem ele, não há alternativa senão o retorno ao procedimento manual, com o custo de aproximadamente 27 horas mensais de trabalho descrito na seção 2.

**Impacto sobre a qualidade da leitura.** O desgaste dos mecanismos de alimentação produz inclinação e arraste irregular das folhas. Ainda que o sistema corrija automaticamente parte dessas distorções, o efeito prático é o aumento do número de marcações classificadas como duvidosas — que passam a exigir conferência manual e reintroduzem, em escala menor, justamente o retrabalho que o sistema se propõe a eliminar. Mais crítico: a alimentação de duas folhas simultâneas em equipamento desgastado faz uma página inteira deixar de ser digitalizada, o que significa 25 estudantes sem registro de frequência naquele período, sem qualquer sinalização de erro.

### Especificação recomendada para a substituição

O volume processado é modesto — cerca de 85 páginas semanais —, de modo que o critério não é velocidade, mas confiabilidade e integridade da captura.

| Requisito | Justificativa |
| --- | --- |
| Detecção ultrassônica de dupla alimentação | Impede a perda silenciosa de páginas inteiras |
| Alimentador automático para no mínimo 50 folhas | Permite processar uma lista sem intervenção |
| Resolução óptica mínima de 300 DPI | O sistema opera a 200 DPI; a margem preserva a leitura |
| Digitalização frente e verso em passagem única | Reduz manuseio e risco de erro de ordenação |
| Correção automática de inclinação e saída em PDF | Compatibilidade direta com o fluxo do sistema |
| Linha atual, com garantia e suprimentos disponíveis | Evita repetir a situação presente em poucos anos |

Trata-se de equipamento de categoria departamental, amplamente disponível no mercado e de custo compatível com o porte da operação.
