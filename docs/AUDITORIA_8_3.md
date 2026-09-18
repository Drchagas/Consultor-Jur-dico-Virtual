# JARBAS Jurídico Enterprise 8.3.1 — Auditoria do Motor de PDFs

## Objetivo
Eliminar a dependência de um único extrator de PDF e garantir que o Copiloto consiga trabalhar com autos nativos, PDFs com camada textual defeituosa e documentos digitalizados.

## Pipeline de leitura
1. Validação do arquivo PDF nos primeiros 1024 bytes.
2. Extração página a página por **PyMuPDF**, **pypdf** e **pdfplumber**.
3. Comparação automática de qualidade e escolha do melhor texto por página.
4. OCR local por **Tesseract + PyMuPDF**, quando Tesseract estiver instalado.
5. Se a extração local for parcial/insuficiente e a OpenAI estiver conectada, o Copiloto consulta também o **PDF original diretamente** pela Responses API.
6. Se a chamada direta ao PDF falhar, o sistema retorna ao índice textual local quando houver conteúdo útil.

## Integração com o Copiloto
A leitura do PDF original passa a ser utilizada, quando necessária, por:
- Pergunte aos Autos;
- Análise Estratégica;
- Hard Truth;
- Geração de Minuta;
- Central IA vinculada ao processo;
- Intake Inteligente.

## Migração
O instalador 8.3.1 reprocessa automaticamente documentos provenientes de extratores antigos e documentos com status `uploaded`, `needs_ocr`, `partial_ocr`, `error` ou `encrypted` quando houver possibilidade de nova leitura.

## Diagnóstico e recuperação
- cada documento exibe status, páginas, caracteres extraídos e nota de extração;
- existe reprocessamento individual e reprocessamento de todos os PDFs do processo;
- `REPROCESSAR_PDFS.cmd` reindexa os PDFs da instalação;
- `DIAGNOSTICO_JARBAS.cmd` testa caminhos físicos, extratores, OCR local e configuração OpenAI.

## Segurança técnica
O JARBAS não deve afirmar que leu páginas sem conteúdo extraído. PDFs digitalizados sem OCR local são marcados como `needs_ocr`. Se a OpenAI estiver conectada, a análise jurídica recebe o PDF original; sem OpenAI e sem OCR local, a interface informa claramente que aquelas páginas ainda não podem ser analisadas com segurança.

## Testes 8.3.1
Foram testados:
- PDF textual nativo;
- PDF somente imagem;
- OCR local quando Tesseract está disponível;
- upload, armazenamento e visualização;
- reindexação individual e integral;
- caminho legado de arquivo;
- Intake local;
- Intake com PDF direto à IA por simulação controlada;
- Pergunte aos Autos com PDF direto por simulação;
- Análise Estratégica com PDF direto por simulação;
- Hard Truth com PDF direto por simulação;
- Minuta com PDF direto por simulação;
- fallback da IA para texto local;
- servidor real, health-check e autenticação.

## Limite objetivo
OCR local depende de um mecanismo OCR instalado na máquina. Nesta versão, o JARBAS detecta Tesseract automaticamente, inclusive em pastas usuais do Windows. Quando Tesseract não estiver presente, a leitura de PDFs somente-imagem depende da OpenAI configurada ou de instalação posterior de OCR local. PDFs com texto nativo continuam sendo extraídos localmente sem OpenAI.
