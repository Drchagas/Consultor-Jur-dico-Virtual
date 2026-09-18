# JARBAS Jurídico Enterprise 8.3.1 — Auditoria crítica do motor de PDF

## Diagnóstico corrigido

A leitura dos PDFs existentes estava parcialmente funcional, mas a instalação 8.3.0 podia abortar antes da reindexação integral por causa de um **warning do pdfminer/pdfplumber** (`FontBBox`) escrito em stderr. Esse warning não significa PDF ilegível. Como o Windows PowerShell do instalador estava em modo estrito, o aviso podia ser tratado como falha da etapa de self-test.

Também havia dois problemas de experiência/diagnóstico:

1. o diagnóstico amostrava poucas páginas e podia mostrar 100% de cobertura da amostra mesmo quando o índice antigo ainda tinha pouco texto no conjunto completo;
2. reenviar o mesmo PDF retornava `error=1` por duplicidade, fazendo parecer que a extração havia falhado.

## Correções 8.3.1

- PyMuPDF tornou-se o motor primário e suficiente quando a página já foi bem extraída.
- pypdf só é usado nas páginas em que o PyMuPDF retorna texto fraco.
- pdfplumber/pdfminer virou fallback de último recurso e seus warnings de fonte são isolados/suprimidos.
- OCR Tesseract continua sendo tentado apenas em páginas sem texto, quando estiver disponível.
- uma falha de um motor em uma página não invalida o PDF inteiro.
- reindexação 8.3.1 força a reconstrução integral de todos os PDFs existentes.
- diagnóstico passa a informar **cobertura real do índice do banco**, páginas indexadas, páginas com texto, caracteres e chunks, além de uma amostra de leitura ao vivo.
- upload duplicado deixa de ser apresentado como falha de extração e passa a informar que o PDF já está disponível ao Copiloto.
- erros reais de upload/extração aparecem detalhados na tela.
- self-test do instalador executa com stdout/stderr isolados; warnings não fatais não interrompem a instalação.
- a integração OpenAI permanece como segunda camada: com `JARBAS_AI_PDF_ALWAYS=1`, Copiloto, Hard Truth, análise e minuta tentam ler também o PDF original diretamente pela Responses API.

## Critério de leitura

O JARBAS considera um documento localmente indexado quando a maioria das páginas processadas possui conteúdo textual útil. Páginas vazias/digitalizadas ficam marcadas como `partial_ocr` ou `needs_ocr`; com OpenAI conectada, o PDF original continua disponível para análise direta.

## Verificações executadas

- extração nativa PyMuPDF;
- fallback forçado PyMuPDF → pypdf;
- fallback forçado PyMuPDF → pypdf → pdfplumber;
- PDF de 426 páginas extraído integralmente;
- rota HTTP de upload e indexação;
- reenvio de PDF duplicado sem falso erro de extração;
- inicialização FastAPI/Uvicorn e `/health` 200;
- self-test integral do instalador.
