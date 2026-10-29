# IPTU — Central de preparação

Aplicação de upload, identificação de formulários de isenção de IPTU, OCR e extração de dados em JSON. Interface azul e branca em português. Os dados ficam no próprio servidor. Não há conexão direta com compartilhamentos Windows nem configuração de usuário/senha de rede.

## Operação com pastas grandes, incluindo acervos de 36 GB

1. Clique em **Adicionar pasta inteira**. A seleção inclui recursivamente as subpastas. Repita para acrescentar outras pastas. Somente nomes terminados em `.pdf` ou `.PDF` entram na fila; os demais são ignorados antes da leitura e do envio.
2. Confira a organização dos processos e o tamanho da rodada. O padrão é **5 PDFs por rodada**, enviados um por vez. Isso é independente do lote de processos para OCR.
3. Clique em enviar. Um Worker calcula SHA-256 em blocos de 4 MiB no navegador. O servidor consulta os registros persistentes e solicita somente os conteúdos que ainda faltam. A conferência funciona em HTTP e HTTPS.
4. A cada rodada, o servidor identifica e guarda as páginas candidatas a formulário e remove suas cópias dos PDFs completos. Quando todos os PDFs de um processo chegaram, libera esse processo para extração e, se marcado, OCR/JSON. Só então continua a próxima rodada.
5. Confira os resultados, baixe os PDFs/JSONs e faça a revisão dos campos sugeridos.

O limite padrão de uma seleção é **64 GiB**, até 20.000 PDFs, com **100 MiB por PDF**. Não é necessário enviar os 36 GB em uma única seleção: várias seleções menores usam o mesmo controle de conteúdo.

O navegador deve permanecer aberto e o computador acordado durante a conferência e o envio. Se a aba fechar, a rede cair ou o contêiner reiniciar, selecione novamente as mesmas pastas e envie. Os comprovantes de recebimento e o trabalho concluído permanecem no volume. A conferência pode reler os arquivos no computador, mas os PDFs reconhecidos não precisam ser transmitidos novamente. Um trabalho já iniciado no servidor continua após fechar a aba; novos envios dependem dela.

### Identidade e retomada

- A identidade do PDF usa SHA-256 dos bytes, e não somente nome/tamanho/data. Conteúdos idênticos podem reutilizar preparação e OCR mesmo em seleções diferentes.
- O processo é identificado pelo nome normalizado da pasta. Preserve os nomes das pastas de processo; processos distintos devem ter identificadores distintos.
- Na seleção de uma raiz como `Isencao_IPTU`, cada pasta de processo abaixo dela agrupa os seus PDFs, inclusive anexos em níveis mais profundos. Pastas SEI como `3516200.410.00010907-2026-51` também são reconhecidas quando selecionadas diretamente.
- PDFs avulsos são processos separados por nome. A opção de agrupamento permite tratar a pasta selecionada como um único processo.
- Uploads parciais acrescentam documentos ao processo existente. Arquivos ausentes na nova seleção não são excluídos. Um conteúdo alterado no mesmo caminho produz uma nova versão do resultado; resultados e revisões anteriores são preservados no histórico.
- PDFs visualmente iguais, mas com bytes diferentes, não são considerados duplicatas exatas.

### Espaço e exclusão dos originais

A aplicação apaga **somente as suas cópias de upload** após guardar e verificar os fragmentos com as páginas candidatas. Não apaga arquivos do computador nem da pasta de rede escolhida pelo operador. PDFs com falha de preparação são preservados para tentativa posterior; a rodada avisa e pausa o envio.

Permanecem no volume: formulários, OCRs, textos, JSONs, decisões por página, histórico e comprovantes SHA-256. Quando nenhum formulário é encontrado, permanecem os registros da análise. A classificação é um piloto e pode errar: mantenha os originais na origem para auditoria e eventual nova análise com outra versão do detector.

O processamento em rodadas evita acumular todo o acervo original no servidor. Ainda é preciso dimensionar espaço para formulários, OCRs, histórico, temporários e arquivos com falha. A quantidade de PDFs por rodada limita o recebimento normal de originais, não o tamanho total dos resultados.

## Publicar no Coolify

Use Docker Compose com `compose.yaml` na raiz do repositório. Porta interna: **8000**. Configure as variáveis abaixo no Coolify **antes do build**, pois Compose exige seus valores durante a interpolação:

- `APP_SECRET`: segredo aleatório com pelo menos 32 caracteres. Gere com `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `ADMIN_USER`: padrão `admin`.
- `ADMIN_PASSWORD`: senha do painel com pelo menos 12 caracteres.
- `COOKIE_SECURE=true` para HTTPS. Para o teste em endereço HTTP, use `false` e faça novo deploy.
- `UPLOAD_MAX_FILE_MB=100`, `UPLOAD_MAX_BATCH_MB=65536`, `UPLOAD_MAX_FILES=20000`: limites configuráveis; MB corresponde a 1024² bytes.

Preserve o volume `iptu-data` montado em `/data` entre os deploys. Faça backup dele: a base SQLite e os caches desse volume são necessários para manter a retomada, as revisões e o reconhecimento de arquivos já recebidos. O aplicativo usa um único processo e uma fila serial; não crie réplicas concorrentes sobre esse mesmo volume.

Configure o proxy para aceitar pelo menos o limite por PDF mais a pequena margem multipart, por exemplo **110 MB** com o padrão atual. Cada requisição transmite um único PDF; o proxy não precisa aceitar 36 GB em uma requisição. Um erro HTTP 413 normalmente indica limite do proxy ou do aplicativo.

A atualização de uma instalação antiga remove as configurações Windows salvas. Resultados locais anteriores são preservados; uma nova extração exige upload. OCR dos formulários já retidos continua disponível.

## Executar localmente

```sh
cp .env.example .env
# Edite os segredos e COOKIE_SECURE antes de iniciar.
docker compose up --build -d
```

Abra `http://localhost:8000` e entre com as credenciais do painel. Para HTTP local, configure `COOKIE_SECURE=false`.

Sem Docker, use Python 3.12+ com Tesseract 5 e os idiomas `por` e `eng` instalados:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data APP_SECRET='um-segredo-aleatorio-de-pelo-menos-32-caracteres'
export ADMIN_USER=admin ADMIN_PASSWORD='uma-senha-forte-para-o-painel' COOKIE_SECURE=false
python -m app
```

## Desempenho da identificação

`DETECTOR_WORKERS=2` é o padrão recomendado para um servidor com 4 vCPUs e 16 GB de RAM. A identificação analisa até duas páginas simultaneamente, em processos separados. Cada processo abre o seu próprio PDF; objetos PyMuPDF não são compartilhados entre threads. Os resultados são reunidos na ordem original das páginas.

A fila de processos continua serial. O paralelismo é interno à identificação dos PDFs com mais de duas páginas; o OCR definitivo dos formulários continua na etapa seguinte. Os trabalhadores são reutilizados entre PDFs e cada comando Tesseract usa um único thread de CPU. Não há fila ilimitada de páginas na memória. Documentos de até duas páginas são analisados sequencialmente para evitar o custo de iniciar processos auxiliares.

O ajuste aceita de 1 a 4 trabalhadores; comece com 2 e confira o limite de CPU/memória atribuído ao contêiner no Coolify, além da capacidade total do host. Use 1 para diagnóstico ou se houver disputa de recursos. A interrupção aguarda as páginas já em execução terminarem antes de remover os temporários.

O detector e sua versão de cache permanecem os mesmos nesta otimização. Formulários e análises já armazenados continuam reutilizáveis. As durações de identificação por PDF, número de páginas e trabalhadores usados ficam em `runtime.details.timings` na resposta de `/api/state` da operação atual. O ganho depende do tipo de página e dos recursos efetivamente disponíveis; não há promessa de reduzir pela metade o tempo total.

## Resultados e revisão

O detector usa texto existente e OCR de classificação para localizar frente, continuação e encerramento. Gera PDF somente com páginas candidatas e um manifesto com arquivo/página/SHA de origem. `Completo provável` ainda exige validação; `Não encontrado` exige amostragem. Os modelos de formulário variam e precisam ser validados com exemplos reais.

A etapa de OCR completo oferece 300 ou 400 dpi e gera PDF pesquisável, texto e JSON com 63 campos. Campos incertos ficam vazios; marcações de opções são sugestões para revisão. A revisão humana fica separada da extração automática e registra alterações. O cache reaproveita OCR por formulário e por página, inclusive ao acrescentar documentos a um processo.

Há exportação em CSV e JSONL, downloads por processo e uma tela de revisão dos dados. Não há envio de dados a modelos externos. Os exemplos do repositório são fictícios.

## Verificação

```sh
python -m pytest -q
node --test tests/*.test.cjs
```

Os testes cobrem upload recursivo lógico, rejeição de caminhos e conteúdos inválidos, autenticação/CSRF, retomada, reutilização por conteúdo, descarte seguro de cópias, preparação antes do término da seleção, extração/revisão e vetores SHA-256. O limite de 36 GiB é verificado por manifesto; os testes não transferem um acervo real desse tamanho.
