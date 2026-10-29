# IPTU · Central de preparação

Aplicação Docker com painel em português para ler PDFs de um compartilhamento Windows, identificar páginas de requerimentos de isenção de IPTU e extrair conjuntos por processo. Preparada para GitHub + Coolify. O usuário publica o repositório; este projeto não cria nem publica recursos externos automaticamente.

## O que está implementado (v2)

- Configuração de servidor, compartilhamento, subpasta, domínio, usuário e senha pelo painel.
- Acesso SMB 2/3 direto em modo leitura: não requer mount CIFS, `privileged` ou `SYS_ADMIN`.
- Teste de acesso, inventário recursivo e lotes manuais de 1 a 500 processos.
- Um processo corresponde à primeira pasta abaixo da origem. Todos os PDFs dessa pasta e suas subpastas são considerados juntos. PDFs soltos na raiz formam o grupo `_raiz`.
- OCR leve em português para classificar início, continuação e encerramento, incluindo formulários de duas ou três páginas e páginas fora de ordem.
- Aproveitamento do texto nativo; verificação de orientação com Tesseract.
- Extração das páginas originais com PyMuPDF. Não rasteriza o PDF final nem mantém documentos pessoais de exemplo no código.
- Reunião de páginas entre PDFs do mesmo processo. Múltiplas frentes/continuações/encerramentos são encaminhadas para revisão, sem deduplicação silenciosa.
- Retomada após reinício; arquivos alterados voltam a pendentes após atualizar o inventário.
- PDF, manifesto JSON de páginas/evidências e relatório CSV. Candidatas incertas são preservadas ao final do PDF para conferência.
- Falhas de leitura de um arquivo não interrompem o lote; o processo passa para erro ou revisão.
- Autenticação HTTP Basic; proteção CSRF; senha SMB criptografada em disco usando chave derivada de `APP_SECRET`.

## Deploy no Coolify (Dockerfile)

1. Publique **o conteúdo desta pasta** na raiz de um repositório GitHub. Não publique dados de produção, `.env`, backups ou PDFs pessoais.
2. No Coolify, crie uma aplicação a partir do repositório e escolha o build pack **Dockerfile**. Caminho: `/Dockerfile`. Porta interna: **8000**.
3. Configure as variáveis **de execução**:
   - `APP_SECRET`: segredo aleatório com ao menos 32 caracteres. Gere com `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - `ADMIN_USER`: por exemplo `admin`.
   - `ADMIN_PASSWORD`: senha forte com ao menos 12 caracteres, diferente da senha SMB.
   - `COOKIE_SECURE=true` para o domínio com HTTPS.
4. Adicione armazenamento persistente em **`/data`**. Use volume gerenciado pelo Docker/Coolify. Se usar bind mount, a pasta precisa ser gravável por UID/GID **10001**.
5. Configure domínio com HTTPS e health check **`/healthz`**. Faça o deploy. Execute **uma única réplica e um único processo Python**; não use Gunicorn com vários workers. A aplicação já inicia Waitress com um worker interno de extração e threads HTTP.
6. Abra a aplicação e informe usuário/senha administrativa na janela de autenticação do navegador.
7. Preencha a conexão Windows no painel, salve, teste o acesso e atualize o inventário. Comece com um lote de 3–10 processos.

**Guarde `APP_SECRET` com o backup.** Trocar a chave impede a leitura das credenciais SMB salvas. A criptografia protege o arquivo em repouso, mas não substitui controle de acesso ao servidor e aos backups. O volume contém PDFs e informações pessoais.

## Rede Windows

O servidor do Coolify precisa alcançar o Windows por **TCP 445**, normalmente pela rede privada ou VPN. Não exponha a porta SMB à internet. Se o Coolify estiver em uma VPS externa, configure a VPN/rota no host antes de testar o acesso. O painel configura o cliente SMB; ele não cria uma VPN.

Para `\\192.168.1.20\Documentos\Isencao_IPTU`, preencha:

- Servidor: `192.168.1.20`
- Compartilhamento: `Documentos`
- Subpasta: `Isencao_IPTU`
- Domínio: o domínio Windows, ou nome do servidor para uma conta local, quando necessário.
- Usuário e senha: uma conta com leitura/listagem nas permissões **do compartilhamento e NTFS**.

Não informe `Z:` nem um caminho `C:\...`: letras de unidade pertencem à sessão Windows. Pode usar `DOMINIO\usuario` ou `usuario@dominio` no campo usuário; nesse caso deixe domínio vazio. Nomes de compartilhamento com espaços e acentos são aceitos. Acesso SMB usa a negociação segura do servidor; não há opção SMB1.

## Operação e resultados

1. **Salvar conexão:** senha em branco mantém a anterior. Alterar servidor/compartilhamento/subpasta desativa o inventário anterior; os resultados continuam no volume, mas deixam de aparecer no painel.
2. **Testar acesso:** verifica listagem da pasta configurada. A permissão de leitura de cada PDF é verificada na extração.
3. **Atualizar inventário:** varre os PDFs e calcula assinatura com caminho/tamanho/data de alteração, sem baixar o conteúdo. Uma alteração que preserve esses metadados exige reprocessamento manual. Falha de inventário preserva o inventário anterior. Não há agendamento automático nesta versão.
4. **Iniciar lote:** processa até N pastas pendentes, sequencialmente. N é quantidade de processos, não de PDFs nem paralelismo. Cada PDF é copiado temporariamente para o disco local e removido ao concluir o processo. Reserve espaço para todos os PDFs do maior processo e para os resultados.
5. **Interromper:** cancela na próxima verificação entre arquivos/páginas. Um OCR pode levar até 90 segundos no reconhecimento leve e 180 segundos por página no OCR completo antes da verificação; a listagem SMB em andamento precisa terminar/expirar. O processo interrompido volta a pendente, os concluídos são preservados.
6. **Reprocessar:** recoloca o processo na fila. Depois inicie outro lote. O resultado atual é substituído na nova execução; versões com JSON/revisão são arquivadas em `history/`.
7. **Downloads:** use PDF e Manifesto por processo ou CSV do inventário. Em `/data/results/<id>/` ficam `formulario.pdf` e `manifesto.json`; `/data/state.sqlite3` mantém configuração criptografada, inventário e estado. Faça backup consistente do volume, preferencialmente com a aplicação parada.

As páginas registradas no manifesto começam em **1**, relativas ao PDF de origem. `file_index` começa em 0. `rotation` é a correção adicional de orientação. O texto completo do OCR preliminar não é salvo; o manifesto guarda expressões reconhecidas e origem.

### Situações

- `pendente`: aguarda lote.
- `processando`: em execução.
- `completo_provavel`: uma frente, uma continuação e um encerramento, em duas ou três páginas. **É uma hipótese por regras, não certificação de completude.**
- `incompleto`: partes identificadas sem todos os papéis esperados.
- `revisao`: múltiplos formulários, candidatos ambíguos ou falhas parciais.
- `nao_encontrado`: nenhuma página atingiu os critérios; pode ser ausência ou falha de reconhecimento.
- `erro`: falha impediu uma extração útil. Corrija a causa e recoloque na fila.

O campo de estabilidade (padrão 120 segundos) impede copiar PDFs recém-alterados. Tamanho/data são conferidos antes e depois da cópia. Processos com arquivos ainda instáveis não são automaticamente aprovados.

## Limites do piloto

O detector atual é baseado em expressões normalizadas e combinações de campos (`app/detector.py`), **não é um classificador visual treinado**. Não reconhece todas as variantes possíveis; manuscritos e digitalizações ruins podem falhar. Não há promessa de acurácia para 6.000 processos. Antes de produção, validar 50–100 processos variados, incluindo ausentes, formulários incompletos e múltiplas versões, medindo páginas perdidas e falsos positivos.

O pré-processamento usa OCR leve; a etapa adicional de dados usa OCR completo e extração local por rótulos/geometria. `formulario.pdf` mantém as páginas originais e `formulario_ocr.pdf` é uma versão rasterizada pesquisável. `manifesto.json` continua descrevendo apenas a etapa de localização (`definitive_ocr=false`); a execução do OCR completo consta em `dados.json` e na situação de dados do painel. Não há reconhecimento confiável de manuscritos, validação de assinatura ou decisão automática sobre concessão da isenção.

Processamento é sequencial para limitar CPU/memória. Um PDF com muitas páginas pode demorar; não existe limite fixo de páginas. Falhas e PDFs protegidos entram para revisão/erro. Páginas candidatas não são deduplicadas automaticamente. PDFs extraídos não preservam a validade de assinaturas digitais do documento de origem; conserve sempre os originais.

## Executar localmente com Docker

```bash
cp .env.example .env
# Edite .env, configure segredos reais e COOKIE_SECURE=false apenas para localhost HTTP.
docker compose up --build -d
```

Abra `http://localhost:8000`. No Coolify, prefira as variáveis do painel e o Dockerfile. `compose.yaml` é uma alternativa local.

## Desenvolvimento e testes

Requer Python 3.12+ e Tesseract com idiomas `por` e `osd` instalados. No macOS: `brew install tesseract tesseract-lang`.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
# Defina APP_SECRET, ADMIN_PASSWORD e DATA_DIR antes de executar:
python -m app
```

Dependências Python usam faixas de versão para receber atualizações de correção. Para builds rigorosamente reproduzíveis em produção, gere e revise um lockfile após validar no seu ambiente.

Referências: [Coolify / Docker e configuração](https://coolify.io/docs/applications/how-applications-work), [armazenamento persistente](https://coolify.io/docs/applications/configuration/persistent-storage), [cliente smbprotocol](https://github.com/jborean93/smbprotocol).

### Conferir os exemplos locais

Copie `tests/amostras.example.json` para um arquivo fora do repositório, ajuste os caminhos e as páginas esperadas (na ordem final) e execute:

```bash
python -m app.validate_samples /caminho/amostras.json
```

A rotina analisa todas as páginas, imprime o resultado de cada amostra e retorna código 1 se alguma seleção divergir. Não altera os PDFs nem envia dados a serviços externos. Preserve exemplos de diferentes modelos e acrescente processos sem formulário para medir falsos positivos.

Validação nesta entrega: 17 testes automatizados aprovados (incluindo OCR/JSON/revisão); detector executado nas 35 páginas dos três PDFs fornecidos, selecionando as 7 páginas esperadas na ordem correta (2 + 2 + 3). Essa amostra pequena não mede acurácia do acervo. Interface conferida em largura móvel e desktop. Build Docker e conexão SMB real não executados neste Mac: Docker não está instalado e as credenciais/rede do servidor Windows não foram fornecidas. O workflow de GitHub executa os testes, build Docker e smoke test após publicação.


## Extração estruturada para JSON

No painel, a opção **Gerar OCR completo e JSON após extrair cada formulário** vem marcada. Via API de pré-processamento, use `extract_data=true`; o padrão da API continua sendo false para compatibilidade. A execução automática usa 300 dpi.

Para formulários já extraídos, use **Gerar JSON dos próximos processos**: usa o tamanho do lote selecionado e a resolução 300/400 dpi, sem reconectar ao SMB. Processa somente os que ainda não têm JSON. Falhas podem ser tentadas novamente pelo mesmo botão. JSONs existentes e revisões não são sobrescritos por esse lote. **Reprocessar** na lista refaz o pré-processamento e arquiva os resultados anteriores em `results/<id>/history/` quando já há JSON/revisão, antes de refazer a extração. Esses arquivos ficam preservados no volume; a interface apresenta somente a versão atual.

### Arquivos adicionais por processo

- `dados.json`: resultado original do OCR, schema 2.0, preservado durante revisões humanas.
- `dados.revisado.json`: versão atual com correções e histórico (quando houver).
- `ocr.txt`: texto reconhecido de todas as páginas selecionadas, separado por quebra de página.
- `formulario_ocr.pdf`: versão pesquisável produzida por Tesseract, separada do PDF de páginas originais.

O JSON define **63 campos** agrupados em requerente, endereço, imóvel, benefícios, atividade, cônjuge, dependente e formulário. Isso é o catálogo de campos suportados para extração/revisão, **não uma garantia de extração automática de todos eles**. Campos sem reconhecimento permanecem null. O catálogo inclui observações para preenchimento manual; não há leitura automática confiável desse campo ou de manuscritos.

A seção `dados` contém os valores normalizados para integração. A seção `campos` contém valor, situação, tipo, evidências, página original/extraída, coordenadas em pixels do OCR e confiança do texto. `resumo` registra preenchimento e conflitos. O schema de integração está em `app/schema.json` e pode ser obtido em `GET /api/schema.json` com autenticação.

### Regras e limites

- CPF: somente dígitos, com validação de tamanho e dígitos verificadores. Um CPF inválido fica null; a leitura bruta continua na evidência. CPF válido não confirma identidade.
- Datas: formato ISO `AAAA-MM-DD`, validadas no calendário; datas ilegíveis ficam null.
- Dinheiro: string decimal em reais (`"1621.00"`), sem arredondamentos de ponto flutuante. Ausência de centavos não é completada automaticamente.
- CEP: oito dígitos; telefone: dez ou onze dígitos. Não há consulta externa.
- E-mail: validação básica de formato; ausência explicitamente reconhecida pode ser `nao_aplicavel_declarado`.
- Valores conflitantes: ficam null e recebem `conflito`; evidências são preservadas.
- Campos textuais ainda podem conter erros de OCR. A confiança informada é da linha de texto, **não uma probabilidade calibrada de acerto do campo**.
- Caixas de seleção: OpenCV detecta quadrados e densidade de marcação e associa palavras/perguntas próximas. Essas associações são experimentais: aparecem em `sugestoes` e na tela de revisão, sem preencher automaticamente o valor definitivo do campo. Uma caixa vazia nunca vira resposta “não”.
- Todos os dados começam com `revisao_necessaria`. Não usar para deferir/indeferir pedidos sem revisão competente.

### Conferência humana

Clique em **N campos · revisar** na linha do processo. A página original fica ao lado dos valores e evidências. Você pode editar um valor, usar uma sugestão após conferir ou confirmar um campo vazio. Somente campos alterados ou marcados como conferidos são salvos; os demais continuam pendentes.

O histórico registra usuário administrativo, data e alterações. Até todos os campos serem conferidos o documento permanece `revisao_parcial`; ao conferir todos, passa a `revisado`. Esse controle indica revisão dos campos no aplicativo, não validação jurídica do requerimento. O download **JSON** devolve a versão revisada mais recente, quando existir. **JSON original do OCR** permanece disponível na revisão.

**Exportar todos os JSONs** gera JSONL, um documento por linha, com a versão mais recente de cada processo que tiver dados. Registros revisados e não revisados podem coexistir: consumidores precisam verificar `status` e `revisao_humana`.

### API (mesma autenticação e CSRF do painel)

- `POST /api/ocr` com `{"limit":10,"dpi":300}` inicia o OCR de formulários já extraídos. `process_id` opcional restringe a um processo; JSON existente não é refeito.
- `GET /api/data/<id>` lê os dados atuais.
- `POST /api/data/<id>/review` com `{"changes":{"requerente.nome":"NOME CONFERIDO"}}` salva correções. Texto ou null; datas e valores precisam estar normalizados. Não enviar campos desconhecidos.
- `GET /api/export.jsonl` exporta as versões atuais.
- `GET /results/<id>/dados`, `/bruto`, `/txt` e `/ocr` baixam JSON atual, JSON original, texto e PDF pesquisável.
- `GET /api/page/<id>/<n>.png` mostra a página extraída para revisão. Páginas começam em 1.

A execução é inteiramente local. Nenhum PDF, imagem ou dado é enviado a APIs de IA. Não há custo por página de serviços externos. Recomenda-se começar com 2 CPUs e 4 GB RAM, ajustando após medir os PDFs reais; o processamento segue sequencial. O campo de resolução refere-se à rasterização: não recupera detalhes ausentes em imagens de baixa resolução.

A nova etapa foi testada também nas sete imagens de conferência dos três exemplos (resolução reduzida, pois o disco externo estava desconectado). Esses testes verificam execução e geração de artefatos; **não comprovam a exatidão de todos os valores extraídos**. Validar em PDFs originais e em uma amostra rotulada antes de ampliar o lote.

Exemplo completo com dados fictícios: `examples/dados-ficticios.json`. Ele demonstra o formato e valores null; não é um formulário de produção.


## Erro de sessão em endereço HTTP

Se o painel abrir em `http://...` com `COOKIE_SECURE=true`, o navegador não envia o cookie de sessão e as ações POST são bloqueadas antes da conexão SMB. O painel agora informa esse caso e as APIs distinguem autenticação do painel (401) de sessão/CSRF (403).

Para um teste em HTTP, defina `COOKIE_SECURE=false` no Coolify, salve e faça novo deploy. Depois recarregue a página. Para uso com HTTPS, mantenha `COOKIE_SECURE=true`. A aplicação não reduz essa proteção automaticamente. Não troque `APP_SECRET` para resolver esse erro.

Exemplo de conexão para `\\servidor\Informatica\Arquivos\Isencao_IPTU`: servidor `servidor` (ou IP), compartilhamento `Informatica`, subpasta `Arquivos/Isencao_IPTU`. Não cole o caminho UNC completo no campo Servidor ou IP.


## Diagnóstico de conexão SMB

O botão Testar acesso agora mostra as etapas separadas: (1) resolução/conexão TCP 445 a partir do contêiner, (2) negociação SMB/autenticação e (3) compartilhamento/subpasta/permissão de listagem. As falhas exibem código próprio, como `tcp445_timeout`, `network_unreachable`, `smb_auth_failed`, `smb_share_not_found` ou `smb_access_denied`. Logs de falhas incluem operação/código/classe da exceção, sem imprimir senha ou mensagem bruta da biblioteca.

Se o Windows tem IP privado, o host do Coolify precisa estar na mesma rede ou ter VPN/rota. Confirme também se a rede Docker não se sobrepõe à rede interna Windows. Porta 445 acessível não comprova que a conta/pasta está autorizada; as etapas seguintes verificam isso.

## Lembrar o formulário de conexão

Além da configuração salva de forma criptografada no servidor, o navegador lembra os campos não secretos enquanto você digita: servidor, compartilhamento, subpasta, domínio, usuário e tempo de estabilidade. Isso permite retomar um preenchimento ainda não salvo. **A senha nunca é incluída no armazenamento do navegador.**

Um link `/#conexao?server=SERVIDOR&share=PASTA&subpath=SUBPASTA&domain=DOMINIO` pode preencher os campos de conexão. O perfil é aplicado após carregar a configuração salva, mantém o usuário existente e remove os parâmetros do endereço. Ele não salva senha nem inicia autenticação automaticamente: confira o usuário, informe a senha e clique em Salvar conexão. Valores de rede específicos da instituição não precisam ficar no código público do repositório.
