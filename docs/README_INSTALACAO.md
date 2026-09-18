# JARBAS Jurídico Enterprise 8.3.1 — PDF Engine

Versão consolidada focada na leitura efetiva dos autos e integração do Copiloto com o PDF original.

## Instalação
1. Extraia completamente o ZIP.
2. Execute `VERIFICAR_PACOTE.cmd`.
3. Execute `INSTALAR_AGORA.cmd`.
4. O instalador preserva e faz backup dos dados anteriores, consolida a instalação em `%LOCALAPPDATA%\JARBAS_Enterprise`, instala as dependências, migra o banco e reprocessa PDFs antigos.
5. Defina a senha administrativa solicitada.
6. Configure a OpenAI durante a instalação ou depois com `CONFIGURAR_OPENAI.cmd`.

## Motor de PDF 8.3
A leitura local utiliza PyMuPDF, pypdf e pdfplumber em paralelo. A melhor extração é escolhida página a página. Se Tesseract estiver instalado, páginas digitalizadas também podem receber OCR local. Quando a OpenAI estiver conectada, PDFs com extração local incompleta são enviados diretamente ao modelo nas operações do Copiloto, Hard Truth, análise estratégica, Intake e minuta.

## PDFs já existentes
O instalador reprocessa automaticamente documentos produzidos por extratores antigos. Depois da instalação também existe `REPROCESSAR_PDFS.cmd`, além do botão **Reprocessar todos os PDFs** dentro de cada processo.

## Diagnóstico
Execute `DIAGNOSTICO_JARBAS.cmd`. O relatório mostra:
- caminho físico de cada PDF;
- estado do arquivo;
- páginas e caracteres extraídos;
- motores de extração;
- OCR local detectado;
- configuração OpenAI;
- health-check e logs.

## Importante
PDF textual funciona localmente, sem IA externa. PDF somente imagem requer OCR local ou OpenAI conectada. O sistema não mascara esse estado: documentos sem leitura suficiente são marcados e o advogado recebe orientação explícita.

## Hotfix 8.3.1 — leitura de PDFs

A versão 8.3.1 corrige um problema em que warnings do `pdfminer/pdfplumber` (especialmente `FontBBox`) podiam interromper a instalação/reindexação no Windows mesmo quando o PDF era legível por PyMuPDF. O pipeline agora usa fallback preguiçoso por página e o self-test isola stderr de warnings não fatais.

A instalação força a reindexação integral dos PDFs existentes e o diagnóstico passa a mostrar a cobertura real do índice, não apenas uma pequena amostra.

## Diagnóstico específico do caso 8.3.0
Se a versão 8.3.0 mostrava PDFs como `indexed`, mas o Copiloto parecia não considerar todo o processo, a 8.3.1 força a reindexação integral de todos os PDFs. O instalador também separa stdout/stderr dos extratores para que avisos não fatais do pdfminer, como `FontBBox`, não interrompam a instalação. Upload repetido do mesmo PDF passa a ser tratado como aviso de duplicidade, não como erro de leitura.
