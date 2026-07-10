# TODO — nruOMR

> Revisitar sempre antes de iniciar novas sessões de desenvolvimento.

---

## Bugs

- [x] Lógica de leitura de página incorreta: quando começa na página 2, lê os alunos da página 1 (xlsx) e interpreta como se fossem os da página 2. O certo é identificar a página e em qual aluno ela começa. (campo "Página inicial do scan" na UI + parâmetro `pagina_inicial` em `processar_pdf_completo`)
- [ ] UI de casos ambíguos com contagem errada: aparece 252 casos em scan com 25 nomes. Problema na lógica do ambíguo.
- [x] Documentos vindo das justificativa quebram quando há mais de um documento. 


## Melhorias

- [ ] **Estrutura da planilha de exportação no Sheets** — períodos atualmente empilhados verticalmente (um abaixo do outro); devem ficar lado a lado (colunas horizontais por período).
- [ ] **Trocar geração de xlsx temporário pela planilha existente no projeto** — exportação cria xlsx novo a cada processamento; substituir pelo arquivo já existente.
- [x] **Executável para iniciar o projeto inteiro** — um único clique/comando que sobe `web.py` (porta 5000) e `dashboard.py` (porta 5001) simultaneamente. (`Iniciar Sistema Completo.bat`)
