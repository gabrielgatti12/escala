# Implantação na instância Oracle

## Atualização de 12/11/2026: navegação do calendário

Esta atualização requer a migração **0017**, que cria índices para a navegação. A janela de troca passa a consultar opções sob demanda no servidor e os títulos do calendário exportado usam atividade antes do local.

```bash
cd /opt/escala-app &&
git pull --ff-only origin codex/escala-inicial &&
bash scripts/oracle-compose.sh build &&
bash scripts/oracle-compose.sh stop web sync &&
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput &&
bash scripts/oracle-compose.sh up -d &&
bash scripts/oracle-compose.sh exec -T web python manage.py sync_status
```

Não reinicie os serviços se a migração falhar; confira o erro antes de retomá-los. Preserve o volume `data` e os arquivos privados. O wrapper preserva os complementos de credenciais do Drive. O intervalo diário configurado continua em uso.

Depois de aplicar, recarregue a página para obter o JavaScript novo. Confira a navegação entre semanas, a busca ao abrir Solicitar troca e o envio/aceite com contas distintas. A busca mostra até 50 opções; nome, setor, data e horário localizam os demais plantões. Um erro de rede bloqueia o envio e oferece nova tentativa. A assinatura Google depende da próxima releitura pelo próprio Google.

As [medições e validações locais](../docs/desempenho-calendario-2026-11-12.md) não comprovam a implantação ou o tempo de resposta na Oracle.

## Instalação inicial

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
SYNC_INTERVAL=86400
```

3. Execute a partir da raiz do projeto:

```bash
bash scripts/oracle-compose.sh build
bash scripts/oracle-compose.sh stop web sync
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput
bash scripts/oracle-compose.sh run --rm web python manage.py test
bash scripts/oracle-compose.sh run --rm web python manage.py createsuperuser
bash scripts/oracle-compose.sh up -d
```

4. Configure um proxy reverso HTTPS confiável apontando para `127.0.0.1:8000`. Ele deve substituir o cabeçalho `X-Forwarded-Proto`, nunca encaminhar um valor arbitrário vindo do cliente. Libere somente as portas necessárias no firewall da instância e no grupo/lista de segurança da Oracle. Não altere regras existentes sem inspecioná-las.
5. Valide login, acesso autenticado ao painel, arquivos estáticos do administrador, sincronização, divisão da dupla, troca e exportação do calendário. Não considere apenas o contêiner rodando como validação.
6. Provisione as senhas dos residentes individualmente pelo administrador e configure backups protegidos do volume `data`. Evite copiar um arquivo SQLite enquanto há gravações; use a API de backup do SQLite ou pare os dois serviços para fazer um backup consistente. O volume contém dados pessoais.

Não é necessário escolher domínio antes de preparar a instância. Para acesso público com credenciais, é necessário HTTPS com certificado válido para o endereço escolhido. As etapas de TLS, DNS, acesso SSH e implantação ainda dependem dos dados da instância.

Esta configuração usa SQLite com apenas um processo web, adequada para avaliação inicial. Antes de ampliar o número de usuários/processos, migrar para PostgreSQL e validar concorrência dos fluxos de confirmação.

## Reunir semanas de pastas mensais do Google Drive

A sincronização mantém um cadastro de fontes (`SheetSource`) e importa todas as fontes ativas para o mesmo calendário. Novas semanas são adicionadas sem apagar as semanas anteriores. Fontes desativadas pelo administrador não são reativadas pela descoberta. Exclusões ou mudanças de acesso no Drive não apagam automaticamente o histórico local.

Para uma pasta pública que também abre sem login, use uma chave de API restrita, sem conta de serviço:

1. Ative a Google Drive API, crie uma chave em APIs e serviços → Credenciais e restrinja-a à Google Drive API e ao IP público de saída da instância Oracle.
2. Guarde somente o valor da chave em `deploy/google-drive-api-key.txt` na instância. Não envie o valor ao chat, não registre em comandos com eco e não inclua no Git. Configure proprietário root, grupo numérico 10001 e modo 640 para o trabalhador conseguir ler o secret.
3. Cadastre o ID da pasta principal em Drive folders, no administrador. Use `bash scripts/oracle-compose.sh ...`; o script inclui automaticamente `deploy/compose.drive-key.yaml` quando esse arquivo privado está presente. A chave é montada apenas no trabalhador.
4. A listagem usa a API; as planilhas públicas usam a exportação HTTPS de leitura já validada. A listagem real via API precisa ser validada após fornecer a chave, pois os testes locais usam uma chave fictícia e respostas simuladas.

Para pastas privadas compartilhadas explicitamente com uma conta de serviço, a alternativa é:

1. Habilite a Google Drive API em um projeto Google Cloud e crie uma conta de serviço. Compartilhe **somente a pasta de escalas** com o e-mail dessa conta, como **Leitor**. A integração usa o escopo `drive.readonly`.
2. Guarde a credencial JSON na instância como `deploy/google-service-account.json`, fora do Git e da imagem Docker. Não envie seu conteúdo ao chat. Configure permissão de leitura apenas para o operador e o UID 10001 do contêiner (por exemplo, proprietário root, grupo 10001 e modo 640). O arquivo é montado somente no trabalhador de sincronização, como secret de leitura.
3. Cadastre o ID da pasta principal em **Drive folders**, no administrador do aplicativo. Os IDs das planilhas semanais serão descobertos recursivamente. Não precisa cadastrar cada mês ou semana.
4. Use o complemento de configuração:

```bash
bash scripts/oracle-compose.sh up -d
```

A pasta será reexaminada a cada intervalo diário; todas as páginas da listagem são percorridas, com limites explícitos de 1000 pastas e 10000 planilhas. A descoberta só é salva após terminar sem erros. Somente arquivos nativos Google Sheets são descobertos; backups XLSX não são importados em duplicidade.

Sem credencial Drive, planilhas públicas cadastradas individualmente por ID continuam funcionando. A API de listagem exige uma chave de API para a pasta pública ou uma credencial autenticada para a pasta privada. A descoberta real deve ser validada após configurar a credencial na instância; testes locais simulam a paginação e as respostas da API, sem provar acesso à pasta real.


## Operação diária e atualizações

Use `bash scripts/oracle-compose.sh` no lugar do prefixo `docker compose -f deploy/compose.yaml`. O script acrescenta o complemento correspondente automaticamente se a chave de API ou a credencial de conta de serviço estiver presente. Exemplos:

```bash
bash scripts/oracle-compose.sh build
bash scripts/oracle-compose.sh stop web sync
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput
bash scripts/oracle-compose.sh up -d
bash scripts/oracle-compose.sh logs --tail=30 sync
```

O serviço `sync` possui política de reinício `unless-stopped`. Docker precisa estar habilitado na inicialização, como já foi configurado na Oracle. O intervalo é contado após cada ciclo completo; a primeira importação de todas as pastas pode demorar mais. Para uma execução inicial com o acesso de leitura configurado, use `bash scripts/oracle-compose.sh run --rm sync python manage.py sync_sheets --registered`; não deixe um segundo sincronizador periódico rodando junto do serviço normal. Configure somente a pasta principal; a descoberta percorre as subpastas.

O painel verifica novidades a cada 45 segundos e se atualiza quando o usuário não estiver escrevendo ou com uma janela de troca aberta. Essa consulta do navegador não inicia importações no Google e não substitui o serviço de sincronização.

## Atualização com leitura diária, avisos e assinatura de calendário

As migrações 0011–0013 acrescentaram os avisos, a assinatura e o controle de leitura diária; a atualização atual inclui também a 0016, que registra a fonte mensal de plantões automaticamente. O código continua com leitura do Google; não grava trocas nas planilhas originais. O banco local mantém as decisões aceitas. A escrita depende de permissão de editor e de uma credencial autorizada para escrita, ainda não configuradas.

```bash
cd /opt/escala-app &&
git pull --ff-only origin codex/escala-inicial &&
bash scripts/set-sync-interval.sh 86400 &&
bash scripts/oracle-compose.sh build &&
bash scripts/oracle-compose.sh stop web sync &&
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput &&
bash scripts/oracle-compose.sh up -d &&
bash scripts/oracle-compose.sh exec -T web python manage.py sync_status
```

Se a migração falhar, mantenha web e sync parados até resolver o erro. Não remova o volume `data`. Depois de corrigir a migração, `up -d` retoma os serviços. A alteração de intervalo preserva os demais campos e as permissões de `deploy/.env`, sem mostrar credenciais.

O serviço lê todas as fontes no início, primeiro as semanais e depois as mensais de plantões, e espera 86400 segundos depois do ciclo. A pausa tem consultas curtas apenas ao SQLite para o pedido manual, contato operacional e lembretes. O botão Atualizar agora antecipa a próxima leitura sem iniciar outro trabalhador concorrente. O contato continua sendo registrado durante a pausa; a interface sinaliza se ficar ausente por mais de 10 minutos.

No Google Calendário, cada residente gera sua própria assinatura em Google Calendário → Gerar meu link privado. Adicionar em Outros calendários → Por URL no Google web cria uma assinatura, enquanto importar um arquivo ICS cria apenas uma cópia. O Google controla o intervalo de releitura e pode levar horas; o feed já expõe a escala atual assim que a troca é aceita. Revogar/renovar links interrompe novas consultas pelo link antigo e não apaga cópias previamente baixadas.

Os avisos de importação começam a partir da próxima mudança efetiva em fontes já importadas. Importações iniciais e dados históricos não disparam avisos em massa. Solicitações são avisadas no envio; o lembrete é verificado localmente a cada cinco minutos depois de completar 24 horas sem resposta.

### URLs privadas no proxy

O Gunicorn usa `config.access_logging.SafeAccessLogger` para ocultar tokens das assinaturas e links de ativação nos logs. No Nginx existente, inclua a configuração abaixo **dentro do bloco `server` HTTPS da aplicação**, antes de usar assinaturas reais:

```nginx
include /opt/escala-app/deploy/nginx-private-calendars.conf;
```

Ela preserva o proxy e os cabeçalhos de HTTPS, desabilitando logs apenas nas rotas que carregam tokens privados. Depois, valide com `nginx -t` e aplique com `systemctl reload nginx`. Mantenha links externos em HTTPS. Se quiser evitar o registro de acessos acidentais por HTTP, desabilite também o access log do bloco HTTP que apenas redireciona para HTTPS. Não publique logs antigos com links de ativação ou assinatura; gere outro link se ele tiver sido exposto.

## Importação automática de todos os plantões mensais

A migração **0016** registra ou atualiza automaticamente a fonte de ID `1t1pAxNbg1PdAxY4mhsW8JcVPKFnu-RIri-IoS8O-5zE`, com nome Plantões — cinderelas, noturnos e fins de semana e formato **Plantões completos**. Ela habilita essa fonte e enfileira uma atualização para o serviço `sync`. Não é necessário adicionar a URL na interface, configurar esse ID em `SHEET_IDS` ou executar um importador por mês. A migração cadastra a fonte; não acessa o Google nem comprova que a importação terminou.

Para uma instalação existente, preserve os arquivos privados e o volume `data` e execute:

```bash
cd /opt/escala-app &&
git pull --ff-only origin codex/escala-inicial &&
bash scripts/set-sync-interval.sh 86400 &&
bash scripts/oracle-compose.sh build &&
bash scripts/oracle-compose.sh stop web sync &&
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput &&
bash scripts/oracle-compose.sh up -d &&
bash scripts/oracle-compose.sh exec -T web python manage.py sync_status
```

O wrapper mantém os complementos de leitura do Drive já instalados. A sequência só reinicia os serviços se a migração terminar com sucesso. Se houver erro na migração, deixe web e sync parados, corrija o erro e então execute `bash scripts/oracle-compose.sh up -d`. Não apague o volume nem substitua o banco por uma cópia de desenvolvimento.

Ao iniciar, o trabalhador importa primeiro as fontes semanais, que fornecem os perfis usados para associar os nomes. Em seguida lê a grade mensal final: cinderelas **18–24**, noturnos como **18–08**, plantões **08–08** de 24 horas e diurnos de fins de semana e feriados, incluindo **08–20**, **09–21**, **12–24**, **10–18** e **10–16**. O horário explícito é considerado também em feriados de dias úteis. Formulários, histórico de trocas e marcadores COB UNEX/x não viram plantões nem novas contas.

O aplicativo consulta o cache SQLite; após concluir o ciclo completo, o trabalhador aguarda **86400 segundos** até a próxima leitura. Durante a pausa só há manutenção local de contato, pedidos e lembretes. **Atualizar agora** em Planilhas e fontes antecipa a leitura pelo mesmo trabalhador. A duração da importação soma-se à pausa; não há promessa de execução em um horário fixo do dia.

Confira o resultado em Planilhas e fontes e nos registros do serviço:

```bash
bash scripts/oracle-compose.sh exec -T web python manage.py sync_status
bash scripts/oracle-compose.sh logs --timestamps --tail=60 sync
```

`sync_status` consulta somente o banco local e informa contato, progresso, falhas e próxima tentativa; um contato recente não prova que todas as fontes foram importadas sem erro. Consulte também o resumo e os avisos do cartão mensal. Na conferência local, MAIO2026!Q63 tem uma data inválida com plantões preenchidos e SETEMBRO2026!R74 tem um nome sem associação segura. Esses dois meses históricos ficam preservados para conferência; os outros dez meses foram importados localmente. Não se trata de sucesso integral dos doze meses, e essa conferência não comprova a importação na Oracle. Trocas aceitas e IDs das cinderelas já existentes são preservados.

A opção restrita **Cinderelas 18–24** permanece disponível para outras fontes. A planilha acima usa **Plantões completos** automaticamente; não a recadastre nem altere o formato de uma fonte semanal para reutilizá-la. Nenhuma dessas leituras grava nas planilhas originais. Os passos acima ainda precisam ser executados e verificados na Oracle; este documento não confirma uma implantação remota.
