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

O ID é o trecho entre `/d/` e `/edit` no link. `SHEET_IDS` aceita IDs separados por espaços e `SYNC_INTERVAL` define o intervalo do script de desenvolvimento. Configure os IDs no ambiente; sem `SHEET_IDS`, o script de desenvolvimento inicia somente o servidor, sem sincronização. O endereço da planilha não é incorporado ao código. A fonte precisa permitir leitura pelo link; planilhas privadas exigirão integração autenticada, ainda não implementada. O adaptador usa exportação HTTPS de leitura e nunca altera o Google Sheets.

- **Espelho Semanal**: lê datas da primeira linha, blocos de sete colunas por dia, horário explícito e nomes de residentes por atividade. Quando aparecem dois nomes na mesma linha (por exemplo F8/G8 e F191/G191), eles representam alternativas de alocação: a aplicação mostra a atividade como pendente, sem atribuí-la a ambos. Um dos residentes propõe em qual atividade cada um ficará; após aceite do colega, as duas atribuições entram no calendário final e podem participar de trocas futuras. Turnos que atravessam a meia-noite terminam no dia seguinte. Plantões sem horários explícitos não são inventados.
- **R1/C1a/C1b/R2/C2a/C2b/R3/C3a/C3b**: expande as duas linhas de atividades nos dias úteis do período indicado em cada aba. As linhas aparecem como “Linha 1” e “Linha 2” até confirmação de seus horários e significado. Elas são consultáveis, mas ainda não participam das trocas.
- Trocas aceitas no aplicativo não são sobrescritas por uma planilha desatualizada. Plantões removidos da origem são desativados; quando já foram trocados, ficam preservados e a sincronização sinaliza necessidade de conciliação. Mudanças no nome/data/local podem gerar uma nova identidade de plantão. O administrador deve conciliar esses casos antes de operar com dados alterados após uma troca.
- Cada fonte é sincronizada em uma transação. Falhas preservam a última importação; o trabalhador periódico registra a falha e tenta novamente no próximo intervalo. A interface mostra a última atualização bem-sucedida.
- As contas são identificadas por nome normalizado com sufixo de hash. Pessoas homônimas ou mudanças de nome exigem mapeamento administrativo; não use contas compartilhadas.

A aba Espelho Semanal e as abas de rotação apresentam visões diferentes e são mostradas separadamente. Esta versão não interpreta automaticamente férias, feriados, exceções ou células sem horário como plantões trocáveis. A semana do espelho da planilha de exemplo é histórica; use os filtros para consultá-la. Para solicitar trocas são necessários plantões futuros.

## CSV / Excel tabular alternativo

```bash
/workspace/.escala-venv/bin/python manage.py import_schedule /caminho/escala.csv
```

Colunas: `id,residente,inicio,fim,setor`. `residente` é o nome de usuário de uma conta ativa já cadastrada; datas em ISO 8601 (por exemplo `2026-12-01T08:00:00-03:00`). XLSX usa a primeira aba e cabeçalhos na primeira linha. Sem fuso explícito, usa America/Sao_Paulo. A importação é transacional e repetível. Não remove automaticamente linhas ausentes. `--interval 300` relê um arquivo local atualizado por outro processo.

## Limites e implantação

O ambiente preparado é de desenvolvimento. Antes de disponibilizar a residentes: confirmar interpretação das rotações e restrições de troca entre grupos, provisionar contas, definir política de aprovação institucional e preparar hospedagem HTTPS, banco persistente, backups e servidor WSGI/ASGI de produção. O SQLite atende desenvolvimento; recomenda-se PostgreSQL para produção e validação adicional de concorrência. Não há escrita de volta na planilha nem notificações por email/WhatsApp nesta versão.

Para produção, `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` forte e `DJANGO_ALLOWED_HOSTS` são obrigatórios. `DATABASE_PATH` permite escolher o arquivo SQLite. Não exponha o servidor de desenvolvimento publicamente.

A dupla decide a divisão entre si, pelo fluxo de proposta e aceite no aplicativo. Não há decisão automática baseada na posição dos nomes. O calendário final é a visão `/calendario/` do aplicativo. A exportação `/calendario.ics` inclui somente plantões definidos do usuário conectado; ela é um arquivo estático, que deve ser reexportado após alterações. Não há sincronização de escrita com Google Calendar nem com Google Sheets.
