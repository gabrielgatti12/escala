# Escala da residência

Aplicação Django em português, com SQLite para desenvolvimento. Cada residente entra com uma conta individual, consulta a própria escala e as escalas dos colegas, sinaliza interesse em trocar e acompanha solicitações. O envio confirma a intenção do solicitante; somente o destinatário pode aceitar. A confirmação revalida titularidade, datas futuras e conflitos, atualiza ambos os plantões atomicamente e cancela pedidos concorrentes sobre os mesmos plantões.

## Desenvolvimento

```bash
cd /workspace/escala
python -m venv /workspace/.escala-venv
/workspace/.escala-venv/bin/pip install -r requirements.txt
/workspace/.escala-venv/bin/python manage.py migrate
/workspace/.escala-venv/bin/python manage.py test
/workspace/.escala-venv/bin/python manage.py createsuperuser
bash scripts/dev-start.sh
```

O servidor de desenvolvimento escuta a porta 8000 no loopback. O script também inicia a sincronização a cada 300 segundos e encerra os dois processos ao sair. Não é um servidor de produção.

No painel `/admin/`, o administrador pode habilitar o acesso dos residentes definindo senhas individuais. As identidades importadas são criadas com senha inutilizável; não há senha padrão, cadastro público nem conta administrativa automática. Não publique o banco de dados ou credenciais no Git. A recuperação de senha por email ainda não está configurada; contate o administrador.

## Google Sheets

```bash
/workspace/.escala-venv/bin/python manage.py sync_sheets ID_DA_PLANILHA
/workspace/.escala-venv/bin/python manage.py sync_sheets ID_1 ID_2 --interval 300
```

O ID é o trecho entre `/d/` e `/edit` no link. `SHEET_IDS` aceita IDs separados por espaços e `SYNC_INTERVAL` define o intervalo do script de desenvolvimento. Configure os IDs no ambiente; sem `SHEET_IDS`, o sincronizador usa as fontes já cadastradas no banco. O endereço da planilha não é incorporado ao código. O comando mantém as fontes em `SheetSource` e reúne todas as semanas cadastradas. Para descoberta das pastas mensais do Drive, veja `deploy/README.md`. A fonte precisa permitir leitura pelo link; planilhas privadas exigirão integração autenticada, ainda não implementada. O adaptador usa exportação HTTPS de leitura e nunca altera o Google Sheets.

- **Espelho Semanal**: lê datas da primeira linha, blocos de sete colunas por dia, horário explícito e nomes de residentes por atividade. Quando aparecem dois nomes na mesma linha (por exemplo F8/G8 e F191/G191), eles representam alternativas de alocação: a aplicação mostra a atividade como pendente, sem atribuí-la a ambos. Um dos residentes propõe em qual atividade cada um ficará; após aceite do colega, as duas atribuições entram no calendário final e podem participar de trocas futuras. Turnos que atravessam a meia-noite terminam no dia seguinte. Plantões sem horários explícitos não são inventados.
- **Fonte operacional**: somente a aba Espelho Semanal. As abas R1/C1a/etc. não são expandidas em atribuições diárias. Referências geradas por versões anteriores são removidas na próxima sincronização da fonte, sem apagar plantões ou decisões aceitas. Cabeçalhos de setor podem estar mesclados em B:G ou C:G (segunda), com blocos equivalentes nos demais dias. Cada cartão permite conferir a célula de origem.
- **Horários ausentes ou inválidos**: preservados como atividades incompletas no calendário semanal, sem inventar horários nem habilitar troca. Dois nomes na mesma atividade permanecem alternativas de uma dupla.
- **Conta administrativa**: na página inicial, use “Minha conta corresponde a qual residente?” para vincular sua conta ao nome importado. O vínculo não concede privilégios ao residente importado. Somente superusuários podem fazer esse vínculo pela página; outros vínculos de acesso podem ser administrados pelo modelo ResidentAccess. Não vincule uma conta ao nome de outra pessoa.
- Trocas aceitas no aplicativo não são sobrescritas por uma planilha desatualizada. Plantões removidos da origem são desativados; quando já foram trocados, ficam preservados e a sincronização sinaliza necessidade de conciliação. Mudanças no nome/data/local podem gerar uma nova identidade de plantão. O administrador deve conciliar esses casos antes de operar com dados alterados após uma troca.
- Cada fonte é sincronizada em uma transação. Falhas preservam a última importação; o trabalhador periódico registra a falha e tenta novamente no próximo intervalo. A interface mostra a última atualização bem-sucedida.
- As contas são identificadas por nome normalizado com sufixo de hash. Pessoas homônimas ou mudanças de nome exigem mapeamento administrativo; não use contas compartilhadas.

O calendário semanal mostra plantões definidos, atividades da dupla ainda pendentes e atividades sem horário. O calendário mensal e a exportação ICS contêm somente plantões definidos. Esta versão não interpreta automaticamente férias, feriados, exceções ou células sem horário como plantões trocáveis. A semana do espelho da planilha de exemplo é histórica; use os filtros para consultá-la. Para solicitar trocas são necessários plantões futuros.

## CSV / Excel tabular alternativo

```bash
/workspace/.escala-venv/bin/python manage.py import_schedule /caminho/escala.csv
```

Colunas: `id,residente,inicio,fim,setor`. `residente` é o nome de usuário de uma conta ativa já cadastrada; datas em ISO 8601 (por exemplo `2026-12-01T08:00:00-03:00`). XLSX usa a primeira aba e cabeçalhos na primeira linha. Sem fuso explícito, usa America/Sao_Paulo. A importação é transacional e repetível. Não remove automaticamente linhas ausentes. `--interval 300` relê um arquivo local atualizado por outro processo.

## Limites e implantação

O ambiente preparado é de desenvolvimento. Antes de disponibilizar a residentes: confirmar restrições de troca entre grupos, provisionar contas, definir política de aprovação institucional e preparar hospedagem HTTPS, banco persistente, backups e servidor WSGI/ASGI de produção. O SQLite atende desenvolvimento; recomenda-se PostgreSQL para produção e validação adicional de concorrência. Não há escrita de volta na planilha nem notificações por email/WhatsApp nesta versão.

Para produção, `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` forte e `DJANGO_ALLOWED_HOSTS` são obrigatórios. `DATABASE_PATH` permite escolher o arquivo SQLite. Não exponha o servidor de desenvolvimento publicamente.

A dupla decide a divisão entre si, pelo fluxo de proposta e aceite no aplicativo. Não há decisão automática baseada na posição dos nomes. O calendário final é a visão `/calendario/` do aplicativo. A exportação `/calendario.ics` inclui somente plantões definidos do usuário conectado; ela é um arquivo estático, que deve ser reexportado após alterações. Não há sincronização de escrita com Google Calendar nem com Google Sheets.

## Interface e cadastro de novas semanas

A página inicial abre na semana do próximo plantão, mantendo a seleção explícita quando informada. A navegação separa Minha escala, Escala dos colegas, Trocas e divisões, Calendário mensal e Planilhas e fontes (administrador). Os cartões distinguem plantões definidos, duplas pendentes e horários não informados. O botão Solicitar troca permite escolher os dois plantões; o atalho no cartão preenche o plantão escolhido. A divisão filtra atividades da mesma dupla e data, com validação final no servidor.

Em Planilhas e fontes, o administrador pode cadastrar uma semana pelo link Google Sheets. O trabalhador importa no próximo ciclo e preserva as semanas anteriores. Novas fontes sem nome recebem um título derivado das datas reais do espelho. Os estilos e scripts estão em `static/schedule/`, com `STATICFILES_DIRS` configurado; `collectstatic` gera os arquivos versionados na imagem de produção.

## Mural de trocas

“Quero trocar” publica automaticamente uma intenção no mural autenticado, visível a todos os residentes. Cada publicação permite comentários de até 600 caracteres e uma proposta formal pelo botão Propor uma troca. Comentários não alteram a escala. A confirmação dos dois continua obrigatória; uma troca aceita encerra as publicações dos plantões envolvidos. O autor pode retirar a publicação, mantendo a conversa no histórico de Minhas publicações.

O mural mostra por padrão somente plantões futuros, ativos e ainda pertencentes ao autor. Publicações vencidas, retiradas ou com titularidade alterada não aceitam novos comentários. Uma nova publicação após mudança de titular cria uma conversa separada, preservando a anterior. A migração inclui intenções futuras que já estavam marcadas antes da introdução do mural. A autenticação e o vínculo do residente são usados no servidor para autoria e permissões; valores de autor enviados pelo navegador não são aceitos.

O percurso publicar → comentar → propor → aceitar → atualizar calendários foi validado no navegador com duas contas fictícias, removidas após o teste. Não foram criadas publicações de teste nas escalas reais.

## Atualização contínua

O trabalhador faz um ciclo de descoberta das pastas configuradas e importação das fontes ativas; ao terminar, aguarda `SYNC_INTERVAL` (300 segundos por padrão) antes do próximo ciclo. A duração de cada importação se soma ao intervalo. Na primeira execução, a descoberta registra os arquivos Google Sheets das subpastas mensais; novas semanas entram no cadastro nos próximos ciclos. Cada fonte é importada em uma transação, com falhas registradas no cadastro e dados anteriores preservados.

O navegador autenticado verifica a revisão dos dados a cada 45 segundos enquanto a aba está visível. Quando há novidades e a página está livre, ela recarrega mantendo a URL e os filtros. Se houver formulário alterado, campo em uso ou janela aberta, aparece um aviso para atualizar quando terminar; comentários em edição não são apagados automaticamente. O endpoint de status só fornece uma revisão, sem conteúdo das escalas.

Na Oracle, prefira `bash scripts/oracle-compose.sh ...`: o script inclui automaticamente a configuração de leitura do Drive quando o arquivo privado estiver instalado, evitando removê-la em atualizações futuras. As credenciais não são enviadas ao Git nem incluídas na imagem.

## Conferência de pendências e formatos de horário

Linhas sem nomes preenchidos não geram atividades nem pendências. A contagem é por ocorrência diária (linha e dia), somando os cinco blocos da semana. Pós-plantão explicitamente indicado no setor é exibido como descanso e contado separadamente das atividades sem horário válido. A lista administrativa de atividades incompletas permite filtrar por fonte/data e conferir setor, célula e valor original.

Intervalos como `12-24` terminam às 00:00 do dia seguinte. Horários inválidos em linhas com nomes são preservados para conferência, sem derrubar a semana inteira. Um marcador da coluna A não apaga o último cabeçalho explícito de setor acima dos nomes. A correção foi validada nas quatro planilhas que falharam (06/07, 07/09, 21/12 e 28/12/2026), com transações de diagnóstico revertidas após a conferência.

Para repetir somente algumas fontes, use `python manage.py sync_sheets ID_1 ID_2 --only`; essa opção não percorre a pasta nem reimporta as demais fontes. Na Oracle, pare o trabalhador periódico antes dessa execução e reative-o ao terminar, inclusive se houver falhas individuais.
