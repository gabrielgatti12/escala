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
