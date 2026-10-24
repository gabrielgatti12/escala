# Implantação na instância Oracle

Arquivos preparados; a imagem de produção ainda precisa ser construída e validada na instância. Docker Engine com Compose é pré-requisito. A aplicação escuta apenas em loopback no host; não abra a porta 8000 no firewall público.

1. Copie o projeto para uma pasta da instância (não copie o banco de desenvolvimento com dados reais sem definir o procedimento de migração).
2. Crie `deploy/.env`, com permissão `600`, e os campos abaixo. Gere `DJANGO_SECRET_KEY` aleatoriamente na instância, sem colocá-la em comandos registrados, Git ou chat. Não use valores de exemplo como credenciais.

```
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<gerar-na-instancia>
DJANGO_ALLOWED_HOSTS=<IP-ou-dominio>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<IP-ou-dominio>
TRUST_PROXY_HTTPS=1
DJANGO_SSL_REDIRECT=1
SHEET_IDS=<ID_DA_PLANILHA>
SYNC_INTERVAL=300
```

3. Execute a partir da raiz do projeto:

```bash
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml run --rm web python manage.py migrate --noinput
docker compose -f deploy/compose.yaml run --rm web python manage.py test
docker compose -f deploy/compose.yaml run --rm web python manage.py createsuperuser
docker compose -f deploy/compose.yaml up -d
```

4. Configure um proxy reverso HTTPS confiável apontando para `127.0.0.1:8000`. Ele deve substituir o cabeçalho `X-Forwarded-Proto`, nunca encaminhar um valor arbitrário vindo do cliente. Libere somente as portas necessárias no firewall da instância e no grupo/lista de segurança da Oracle. Não altere regras existentes sem inspecioná-las.
5. Valide login, acesso autenticado ao painel, arquivos estáticos do administrador, sincronização, divisão da dupla, troca e exportação do calendário. Não considere apenas o contêiner rodando como validação.
6. Provisione as senhas dos residentes individualmente pelo administrador e configure backups protegidos do volume `data`. Evite copiar um arquivo SQLite enquanto há gravações; use a API de backup do SQLite ou pare os dois serviços para fazer um backup consistente. O volume contém dados pessoais.

Não é necessário escolher domínio antes de preparar a instância. Para acesso público com credenciais, é necessário HTTPS com certificado válido para o endereço escolhido. As etapas de TLS, DNS, acesso SSH e implantação ainda dependem dos dados da instância.

Esta configuração usa SQLite com apenas um processo web, adequada para avaliação inicial. Antes de ampliar o número de usuários/processos, migrar para PostgreSQL e validar concorrência dos fluxos de confirmação.

## Reunir semanas de pastas mensais do Google Drive

A sincronização mantém um cadastro de fontes (`SheetSource`) e importa todas as fontes ativas para o mesmo calendário. Novas semanas são adicionadas sem apagar as semanas anteriores. Fontes desativadas pelo administrador não são reativadas pela descoberta. Exclusões ou mudanças de acesso no Drive não apagam automaticamente o histórico local.

Para descobrir automaticamente a pasta principal e suas subpastas mensais:

1. Habilite a Google Drive API em um projeto Google Cloud e crie uma conta de serviço. Compartilhe **somente a pasta de escalas** com o e-mail dessa conta, como **Leitor**. A integração usa o escopo `drive.readonly`.
2. Guarde a credencial JSON na instância como `deploy/google-service-account.json`, fora do Git e da imagem Docker. Não envie seu conteúdo ao chat. Configure permissão de leitura apenas para o operador e o UID 10001 do contêiner (por exemplo, proprietário root, grupo 10001 e modo 640). O arquivo é montado somente no trabalhador de sincronização, como secret de leitura.
3. Cadastre o ID da pasta principal em **Drive folders**, no administrador do aplicativo. Os IDs das planilhas semanais serão descobertos recursivamente. Não precisa cadastrar cada mês ou semana.
4. Use o complemento de configuração:

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.drive.yaml up -d
```

A pasta será reexaminada a cada intervalo; todas as páginas da listagem são percorridas, com limites explícitos de 1000 pastas e 10000 planilhas. A descoberta só é salva após terminar sem erros. Somente arquivos nativos Google Sheets são descobertos; backups XLSX não são importados em duplicidade.

Sem credencial Drive, planilhas públicas cadastradas individualmente por ID continuam funcionando. O link de uma pasta, sozinho, não autentica a API de listagem. A descoberta real deve ser validada após configurar a credencial na instância; testes locais simulam a paginação e as respostas da API, sem provar acesso à pasta real.


## Operação diária e atualizações

Use `bash scripts/oracle-compose.sh` no lugar do prefixo `docker compose -f deploy/compose.yaml`. O script acrescenta o complemento Drive automaticamente se a credencial privada estiver presente. Exemplos:

```bash
bash scripts/oracle-compose.sh build
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput
bash scripts/oracle-compose.sh up -d
bash scripts/oracle-compose.sh logs --tail=30 sync
```

O serviço `sync` possui política de reinício `unless-stopped`. Docker precisa estar habilitado na inicialização, como já foi configurado na Oracle. O intervalo é contado após cada ciclo completo; a primeira importação de todas as pastas pode demorar mais. Para uma execução inicial com o acesso de leitura configurado, use `bash scripts/oracle-compose.sh run --rm sync python manage.py sync_sheets --registered`; não deixe um segundo sincronizador periódico rodando junto do serviço normal. Configure somente a pasta principal; a descoberta percorre as subpastas.

O painel verifica novidades a cada 45 segundos e se atualiza quando o usuário não estiver escrevendo ou com uma janela de troca aberta. Essa consulta do navegador não inicia importações no Google e não substitui o serviço de sincronização.
