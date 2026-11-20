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

O servidor de desenvolvimento escuta a porta 8000 no loopback. O script também inicia a sincronização diária (86400 segundos) e encerra os dois processos ao sair. Não é um servidor de produção.

No painel `/admin/`, o administrador pode habilitar o acesso dos residentes definindo senhas individuais. As identidades importadas são criadas com senha inutilizável; não há senha padrão, cadastro público nem conta administrativa automática. Não publique o banco de dados ou credenciais no Git. A recuperação de senha por email ainda não está configurada; contate o administrador.

## Google Sheets

```bash
/workspace/.escala-venv/bin/python manage.py sync_sheets ID_DA_PLANILHA
/workspace/.escala-venv/bin/python manage.py sync_sheets ID_1 ID_2 --interval 86400
```

O ID é o trecho entre `/d/` e `/edit` no link. `SHEET_IDS` aceita IDs separados por espaços e `SYNC_INTERVAL` define o intervalo do script de desenvolvimento. Configure os IDs no ambiente; sem `SHEET_IDS`, o sincronizador usa as fontes já cadastradas no banco. A planilha de plantões já fornecida é registrada automaticamente ao aplicar a migração 0016; não é necessário cadastrar seu link novamente. IDs de fontes adicionais podem ser configurados no ambiente ou na página de fontes. O comando mantém as fontes em `SheetSource` e reúne todas as semanas cadastradas. Para descoberta das pastas mensais do Drive, veja `deploy/README.md`. A exportação direta exige leitura pelo link; pastas privadas podem usar a integração de conta de serviço descrita em `deploy/README.md`. O adaptador usa exportação HTTPS de leitura e nunca altera o Google Sheets.

- **Espelho Semanal**: lê datas da primeira linha, blocos de sete colunas por dia, horário explícito e nomes de residentes por atividade. Quando aparecem dois nomes distintos na mesma linha, a aplicação mantém a atividade pendente de definição, sem atribuí-la a ambos nem inferir a regra de cobertura. A divisão exige outra atividade da mesma dupla no mesmo dia com horários sobrepostos. Um dos residentes propõe em qual atividade cada um ficará; após aceite do colega, as duas atribuições entram no calendário final e podem participar de trocas futuras. Turnos que atravessam a meia-noite terminam no dia seguinte. Plantões sem horários explícitos não são inventados.
- **Fonte operacional semanal**: somente a aba Espelho Semanal. As abas R1/C1a/etc. não são expandidas em atribuições diárias. Referências geradas por versões anteriores são removidas na próxima sincronização da fonte, sem apagar plantões ou decisões aceitas. Cabeçalhos de setor podem estar mesclados em B:G ou C:G (segunda), com blocos equivalentes nos demais dias. Cada cartão permite conferir a célula de origem.
- **Horários ausentes ou inválidos**: preservados como atividades incompletas no calendário semanal, sem inventar horários nem habilitar troca. Dois nomes na mesma atividade permanecem alternativas de uma dupla.
- **Conta administrativa**: na página inicial, use “Minha conta corresponde a qual residente?” para vincular sua conta ao nome importado. O vínculo não concede privilégios ao residente importado. Somente superusuários podem fazer esse vínculo pela página; outros vínculos de acesso podem ser administrados pelo modelo ResidentAccess. Não vincule uma conta ao nome de outra pessoa.
- Trocas aceitas no aplicativo não são sobrescritas por uma planilha desatualizada. Plantões removidos da origem são desativados; quando já foram trocados, ficam preservados e a sincronização sinaliza necessidade de conciliação. Mudanças no nome/data/local podem gerar uma nova identidade de plantão. O administrador deve conciliar esses casos antes de operar com dados alterados após uma troca.
- Cada fonte é sincronizada em uma transação. Falhas preservam a última importação; o trabalhador periódico registra a falha e tenta novamente no próximo intervalo. A interface mostra a última atualização bem-sucedida.
- As contas são identificadas por nome normalizado com sufixo de hash. Pessoas homônimas ou mudanças de nome exigem mapeamento administrativo; não use contas compartilhadas.

O calendário semanal mostra plantões definidos, atividades da dupla ainda pendentes e atividades sem horário. O calendário mensal, a exportação ICS e a assinatura privada contêm somente plantões definidos. Férias, feriados ou exceções não geram plantões por inferência. A fonte mensal inclui plantões diurnos de fins de semana e feriados quando a grade informa explicitamente data, horário e residente; células sem horário não viram plantões trocáveis. A semana do espelho da planilha de exemplo é histórica; use os filtros para consultá-la. Para solicitar trocas são necessários plantões futuros.

## CSV / Excel tabular alternativo

```bash
/workspace/.escala-venv/bin/python manage.py import_schedule /caminho/escala.csv
```

Colunas: `id,residente,inicio,fim,setor`. `residente` é o nome de usuário de uma conta ativa já cadastrada; datas em ISO 8601 (por exemplo `2026-12-01T08:00:00-03:00`). XLSX usa a primeira aba e cabeçalhos na primeira linha. Sem fuso explícito, usa America/Sao_Paulo. A importação é transacional e repetível. Não remove automaticamente linhas ausentes. `--interval 86400` relê um arquivo local atualizado por outro processo.

## Limites e implantação

O ambiente preparado é de desenvolvimento. Antes de disponibilizar a residentes: confirmar restrições de troca entre grupos, provisionar contas, definir política de aprovação institucional e preparar hospedagem HTTPS, banco persistente, backups e servidor WSGI/ASGI de produção. O SQLite atende desenvolvimento; recomenda-se PostgreSQL para produção e validação adicional de concorrência. A escrita em Sheets está restrita ao piloto administrativo descrito abaixo; não há notificações por email/WhatsApp.

Para produção, `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` forte e `DJANGO_ALLOWED_HOSTS` são obrigatórios. `DATABASE_PATH` permite escolher o arquivo SQLite. Não exponha o servidor de desenvolvimento publicamente.

A dupla decide a divisão entre si, pelo fluxo de proposta e aceite no aplicativo. Não há decisão automática baseada na posição dos nomes. O calendário final é a visão `/calendario/` do aplicativo. A exportação `/calendario.ics` é um arquivo estático. Em Google Calendário, o usuário pode gerar uma assinatura privada que reflete os plantões definidos e pode ser revogada. O Google decide quando reler o link; o painel continua sendo a referência imediata. O piloto de escrita de trocas aceitas na semana de 23–29/11/2026 exige conexão Google e conferência administrativa; as demais fontes continuam somente leitura.

## Interface e cadastro de novas semanas

A página inicial abre na semana do próximo plantão, mantendo a seleção explícita quando informada. A navegação separa Minha escala, Escala dos colegas, Trocas e divisões, Calendário mensal e Planilhas e fontes (administrador). Os cartões distinguem plantões definidos, duplas pendentes e horários não informados. O botão Solicitar troca permite escolher os dois plantões; o atalho no cartão preenche o plantão escolhido. A divisão filtra atividades da mesma dupla e data, com validação final no servidor.

Em Planilhas e fontes, o administrador pode cadastrar uma semana pelo link Google Sheets. O trabalhador importa no próximo ciclo e preserva as semanas anteriores. Novas fontes sem nome recebem um título derivado das datas reais do espelho. Os estilos e scripts estão em `static/schedule/`, com `STATICFILES_DIRS` configurado; `collectstatic` gera os arquivos versionados na imagem de produção.

## Mural de trocas

“Quero trocar” publica automaticamente uma intenção no mural autenticado, visível a todos os residentes. Cada publicação permite comentários de até 600 caracteres e uma proposta formal pelo botão Propor uma troca. Comentários não alteram a escala. A confirmação dos dois continua obrigatória; uma troca aceita encerra as publicações dos plantões envolvidos. O autor pode retirar a publicação, mantendo a conversa no histórico de Minhas publicações.

O mural mostra por padrão somente plantões futuros, ativos e ainda pertencentes ao autor. Publicações vencidas, retiradas ou com titularidade alterada não aceitam novos comentários. Uma nova publicação após mudança de titular cria uma conversa separada, preservando a anterior. A migração inclui intenções futuras que já estavam marcadas antes da introdução do mural. A autenticação e o vínculo do residente são usados no servidor para autoria e permissões; valores de autor enviados pelo navegador não são aceitos.

O percurso publicar → comentar → propor → aceitar → atualizar calendários foi validado no navegador com duas contas fictícias, removidas após o teste. Não foram criadas publicações de teste nas escalas reais.

## Atualização contínua

O trabalhador faz um ciclo de descoberta das pastas configuradas e importação das fontes ativas, lendo primeiro as escalas semanais e depois as fontes mensais de plantões; ao terminar, aguarda `SYNC_INTERVAL` (86400 segundos, ou 24 horas, por padrão) antes do próximo ciclo. A duração de cada importação se soma ao intervalo. Na primeira execução, a descoberta registra os arquivos Google Sheets das subpastas mensais; novas semanas entram no cadastro nos próximos ciclos. Cada fonte é importada em uma transação, com falhas registradas no cadastro e dados anteriores preservados.

O navegador autenticado verifica a revisão dos dados a cada 45 segundos enquanto a aba está visível. Quando há novidades e a página está livre, ela recarrega mantendo a URL e os filtros. Se houver formulário alterado, campo em uso ou janela aberta, aparece um aviso para atualizar quando terminar; comentários em edição não são apagados automaticamente. O endpoint de status só fornece uma revisão, sem conteúdo das escalas.

Na Oracle, prefira `bash scripts/oracle-compose.sh ...`: o script inclui automaticamente a configuração de leitura do Drive quando o arquivo privado estiver instalado, evitando removê-la em atualizações futuras. As credenciais não são enviadas ao Git nem incluídas na imagem.

## Conferência de pendências e formatos de horário

Linhas sem nomes preenchidos não geram atividades nem pendências. A contagem é por ocorrência diária (linha e dia), somando os cinco blocos da semana. Pós-plantão explicitamente indicado no setor é exibido como descanso e contado separadamente das atividades sem horário válido. A lista administrativa de atividades incompletas permite filtrar por fonte/data e conferir setor, célula e valor original.

Intervalos como `12-24` terminam às 00:00 do dia seguinte. Horários inválidos em linhas com nomes são preservados para conferência, sem derrubar a semana inteira. Um marcador da coluna A não apaga o último cabeçalho explícito de setor acima dos nomes. A correção foi validada nas quatro planilhas que falharam (06/07, 07/09, 21/12 e 28/12/2026), com transações de diagnóstico revertidas após a conferência.

Para repetir somente algumas fontes, use `python manage.py sync_sheets ID_1 ID_2 --only`; essa opção não percorre a pasta nem reimporta as demais fontes. Na Oracle, pare o trabalhador periódico antes dessa execução e reative-o ao terminar, inclusive se houver falhas individuais.

Atividades em sequência (por exemplo, Sala Dr. Pedro 08–12 e Patologia 13–18) não são alternativas de divisão. O seletor e o servidor exigem mesma dupla, mesma data e sobreposição de horários; a confirmação revalida essas condições. Repetir um único nome em linhas diferentes mantém seus plantões separados. Linhas com dois nomes sem outra atividade compatível são mostradas como “Dois nomes na origem”, aguardando interpretação, sem atribuição automática ou alteração das decisões existentes.

## Setores, logins e solicitações

O calendário semanal abre em Por dia. Por setor continua disponível, com setores nas linhas e dias nas colunas, seguindo a ordem das células de origem. As atividades semanais usam as cores de fundo e de texto do cabeçalho do setor na planilha; observações na coluna à direita dos nomes aparecem nos cartões. Registros sem cores importadas mantêm a paleta anterior. Os rótulos continuam indicando pendências e interesse em troca.

Solicitações separa Recebidas, Enviadas e Histórico. A confirmação ou recusa é feita pelo destinatário autenticado dentro do aplicativo. Enviar confirma a parte do solicitante; somente o aceite do destinatário altera as duas titularidades. Solicitações enviadas podem ser canceladas enquanto pendentes. O endereço antigo `?tab=exchanges` continua funcionando.

Em Contas e acessos (somente administrador), selecione o residente importado e crie usuário e senha individuais. O login é ligado ao registro importado por ResidentAccess, preservando o usuário técnico de importação e a continuidade da sincronização. O aplicativo valida senha e unicidade e impede criar outra conta para um residente já vinculado ou com senha própria. A lista mostra vínculos e acessos diretos existentes. Senhas nunca são exibidas; entregar as credenciais diretamente ao residente. O próprio usuário pode alterar a senha pela sua conta. Contas novas não recebem privilégios administrativos.

A interpretação da cobertura do pós-plantão depende de outra planilha, ainda não integrada. Essas alocações permanecem para conferência; não houve atribuição automática baseada nessa cobertura.

## Login por e-mail e importação de contatos

O login aceita e-mail sem distinguir maiúsculas/minúsculas. Usuários legados continuam autenticando pelo usuário anterior até a atualização de seus contatos. Sessões antigas e senhas existentes são preservadas; o backend legado permanece disponível para essas sessões, e as tentativas de login por e-mail passam pela validação de identidade sem ambiguidade. E-mails ambíguos não são aceitos como identidade. O cadastro manual exige e-mail como login.

Em Contas e acessos, cole a tabela de contatos (Markdown, TSV/Excel ou CSV), com até três pares nome/e-mail por linha. A prévia mostra nomes encontrados, conflitos e endereços inválidos antes de qualquer gravação. A confirmação aplica somente linhas selecionadas e revalida o estado atual, em uma transação. Nome exato tem prioridade; normalização de acentos/pontuação só é usada se houver uma única correspondência. Não há associação aproximada por similaridade. Nomes ausentes/ambíguos precisam de revisão e não geram contas soltas. Campos extras de observações são ignorados.

O e-mail é salvo no login e no campo de e-mail. Contas já vinculadas mantêm senha, privilégios e identidade. Usuários técnicos importados da planilha não são renomeados, evitando duplicações em sincronizações futuras. Contas novas ficam sem senha utilizável: o administrador gera um link individual de primeiro acesso e o entrega apenas ao titular, que escolhe a senha. O link vence em 24 horas e deixa de valer após definir a senha. Nenhum e-mail é enviado automaticamente. O código e os testes não contêm a lista real de contatos. Os links individuais ficam restritos ao administrador e devem ser entregues somente ao titular.

A página de prévia e a geração de links são exclusivas de superusuários. Respostas a POST não fazem recarga automática, evitando reenvio da tabela enquanto está em conferência. O fluxo foi validado com contatos fictícios no navegador, incluindo preservação do administrador, definição da senha pelo residente e login com e-mail em maiúsculas.

## Revisão funcional

As correções, validações e próximas melhorias estão em [docs/revisao-funcional.md](docs/revisao-funcional.md). A revisão de 25/10/2026 passou em 103 testes e validou os fluxos de troca e divisão no navegador com dados fictícios. A [auditoria de segurança de 09/11/2026](docs/auditoria-seguranca-2026-11-09.md) registra os achados corrigidos, a evidência dos testes e os limites da revisão.

## Calendário, avisos e leitura diária

Em Google Calendário, gere o link privado e adicione-o no Google como Outros calendários → Por URL. A assinatura é somente da própria escala definida e acompanha as alterações nas próximas consultas do Google; não exige habilitar OAuth nem fornecer uma chave pessoal para essa função. Um novo link invalida o anterior, e a revogação impede novas consultas. As assinaturas dependem da conta criadora e do vínculo atual com o residente.

Avisos reúne alterações reais das importações, com antes/depois de horário e setor, e solicitações recebidas. A primeira importação de uma fonte estabelece a referência sem notificar todo o histórico. Pedidos pendentes válidos recebem lembrete depois de 24 horas; no máximo uma renovação por dia. Os lembretes são internos, sem envio por e-mail.

A interface consulta o SQLite local. A leitura automática das planilhas agora ocorre a cada 24 horas após o ciclo completo, com opção administrativa Atualizar agora em Planilhas e fontes. Durante a espera, o trabalhador só consulta o banco local: registra contato a cada 30 segundos e verifica lembretes a cada cinco minutos. Atualizar a página não acessa o Google. Trocas aceitas aparecem imediatamente no aplicativo e no conteúdo da assinatura, sem esperar a leitura diária; a exibição no Google depende da releitura feita por ele.

Para instalações existentes, altere o intervalo com `bash scripts/set-sync-interval.sh 86400` antes de recriar o serviço; um `SYNC_INTERVAL=300` já salvo não muda só com a atualização do código. O comando `python manage.py sync_status` e a tela de fontes mostram o último contato, progresso, falhas e próxima tentativa prevista.

A gravação automática ampla continua fora do escopo. Há um piloto manual para 23–29/11/2026, com permissão de editor e conexão OAuth Google; veja a seção de teste abaixo. A chave de API pública configurada serve à leitura; as decisões continuam preservadas no banco. A integração da cobertura do pós-plantão fica para uma etapa posterior.

Na etapa de calendário, avisos e leitura diária, 179 testes passaram, incluindo assinatura privada, CSRF, avisos por residente, leitura diária e atualização manual. O navegador validou geração/revogação de assinatura, antes/depois e lembretes no celular com dados fictícios. A inclusão do link em uma conta Google é feita pelo próprio residente; o Google controla quando relê a assinatura. A implantação na Oracle e o ajuste do proxy estão descritos em `deploy/README.md`.

## Plantões mensais com cadastro automático

A migração **0016** registra automaticamente a planilha já fornecida, de ID `1t1pAxNbg1PdAxY4mhsW8JcVPKFnu-RIri-IoS8O-5zE`, com o formato **Plantões completos**. Se ela já estava cadastrada como Cinderelas, o cadastro é atualizado. Não é preciso colar a URL nem adicionar a fonte outra vez. A migração também solicita uma atualização ao trabalhador; o download e a importação acontecem no serviço `sync`, depois de ele iniciar.

O importador lê todas as abas mensais reconhecidas, com mês e ano no título, e os turnos explícitos de R1/R2/R3: cinderelas **18–24**, noturnos como **18–08**, plantões de **08–08** e diurnos de fins de semana e feriados, como **08–20**, **09–21**, **12–24**, **10–18** e **10–16**. O fim de 08–08 fica no dia seguinte e corresponde a 24 horas. Os diurnos preenchidos na grade são incluídos mesmo quando a data cai em um dia útil; não é necessário inferir um feriado. A leitura usa o fuso de São Paulo e nunca inventa datas ou horários.

As fontes semanais são importadas primeiro para disponibilizar os perfis de residentes. Os nomes da fonte mensal são associados a essas identidades existentes, sem criar contas nem escolher correspondências aproximadas. Formulários, TROCAS, Legendas e modelos não são importados como plantões nem reaplicados como decisões. Marcadores como COB UNEX e x não são residentes. Novos pedidos no aplicativo continuam dependendo do aceite do destinatário.

As trocas exigem o mesmo tipo de plantão: cinderela com cinderela, noturno/24h com noturno/24h e diurno com diurno. Em cada tipo, **R1 troca com R1; R2 e R3 podem trocar entre si**. A confirmação também revalida titularidade, datas futuras e conflitos com os demais compromissos. Plantões em conciliação ficam visíveis, mas não disponíveis para novas trocas.

O SQLite mantém a cópia local usada pela interface. Essa fonte participa da leitura automática com pausa de **86400 segundos após cada ciclo completo**, e o botão administrativo **Atualizar agora** antecipa a próxima leitura. Consultar o calendário não baixa novamente a planilha. O formato Cinderelas 18–24 continua disponível como opção restrita para outras fontes; a fonte registrada automaticamente usa Plantões completos.

As identidades dos slots independem do nome do residente e da posição física da linha. As cinderelas já importadas mantêm os IDs dos eventos. Meses com datas, fórmulas sem cache, horários ou nomes sem associação segura preservam os registros anteriores com aviso. Abas ausentes ou duplicadas para o mesmo mês também exigem conferência, sem apagar silenciosamente os demais meses. Na conferência local, **MAIO2026!Q63** contém uma data inválida com plantões preenchidos e **SETEMBRO2026!R74** tem um nome sem associação segura. Esses dois meses históricos ficam preservados para conferência; não são considerados uma importação completa e não são inferidos dias ou identidades. Os outros dez meses puderam ser importados localmente; isso não comprova a atualização da Oracle.

Trocas aceitas no aplicativo são preservadas enquanto a planilha mantém a atribuição antiga. Se a grade passar a refletir a mesma decisão, a leitura reconhece a atualização; uma terceira alteração conflitante preserva a decisão local e sinaliza a divergência. A escrita na planilha mensal e a cobertura do pós-plantão continuam aguardando a integração apropriada. O piloto de escrita disponível é somente para estágios semanais de 23–29/11/2026.

Para aplicar na Oracle, siga [deploy/README.md](deploy/README.md). Atualizar o repositório e aplicar as migrações são passos de implantação; a importação deve ser conferida pelo resumo da fonte e pelo comando `sync_status` na própria instância.

Validação de 09/11/2026: 328 testes passaram, incluindo importação automática dos três tipos, preservação dos IDs de cinderelas, consentimento e grupos nas trocas, revogação de acesso, limite de tentativas de login e limites de XLSX. Fórmulas com texto vazio confirmado no arquivo Google são aceitas; fórmulas sem resultado armazenado preservam a origem anterior.

## Navegação e títulos do calendário

A versão de 12/11/2026 mostra **atividade · local**, inclusive no download e na assinatura Google. As opções de troca são buscadas somente ao abrir a janela, em até 50 resultados por busca, sem carregar todos os meses em cada página. Busque nome, setor, data ou horário para localizar outros plantões. O aceite continua sendo do destinatário e todas as regras são revalidadas no servidor.

A migração **0017** adiciona índices para as consultas do calendário. A página da própria semana enviou aproximadamente 88% menos HTML na comparação local; **343 testes passaram**. Medições, validações e limites estão em [docs/desempenho-calendario-2026-11-12.md](docs/desempenho-calendario-2026-11-12.md). Para ativar na Oracle, atualize o código, reconstrua os serviços e aplique as migrações conforme [deploy/README.md](deploy/README.md).

## Acordo diário dos residentes espelho

A negociação se aplica somente às atividades que oferecem **Combinar divisão**, com duas atividades disponíveis da mesma dupla no dia e horários sobrepostos. As outras atividades seguem a escala importada. A dupla pode combinar livremente os setores em cada dia, sem cota semanal ou alternância obrigatória: distribuições 4–1 e 5–0 no mesmo setor são permitidas quando ambos concordam.

**Concordo e envio para o colega** registra o aceite de quem propõe e cria uma solicitação pendente. A tela identifica quem já concordou e de quem falta o aceite. Apenas o destinatário pode aceitar a divisão; só depois são criadas as duas atividades na escala final. Cada dia exige seu próprio acordo. Recusar ou cancelar mantém as atividades sem definição, e propostas pendentes não aparecem na exportação final do calendário.

Validação desse fluxo: **350 testes passaram**, incluindo semanas sem cota, consentimento por dia, pendências fora do calendário final e proteção das atividades que não permitem divisão.


## Teste de escrita em Sheets: 23–29/11/2026

A migração **0018** acrescenta uma conexão OAuth Google e prévias administrativas. Em **Planilhas e fontes → Testar gravação no Google**, conecte sua conta pessoal de editor, selecione a fonte semanal já importada, faça uma troca com aceite do colega e prepare a prévia como administrador. Somente a confirmação da prévia grava na planilha; aceitar a troca no aplicativo continua sendo uma decisão local e não dispara uma gravação automática.

O piloto aceita somente trocas regulares futuras, da mesma fonte e da semana de 23–29/11/2026, já aceitas pelos dois. Não inclui divisões da dupla, plantões mensais ou atividades que passaram por várias trocas. Datas, horários, setores, nomes e a conexão são conferidos de novo antes do envio. Uma única chamada Google altera os nomes de todas as atividades da troca; a fonte desses nomes fica azul **#0000ff**, preservando fundo, bordas, alinhamento e demais formatos. **Nomes são gravados como valores fixos no Espelho Semanal: fórmulas dessas células são substituídas**, com indicação e confirmação explícita na prévia. Abas de referência não são atualizadas.

O refresh token é criptografado com chave separada do banco. Arquivos OAuth/chave não entram no Git nem na imagem e são montados somente no web; a conexão precisa ser feita por administrador. Código, client secret e tokens não são exibidos. O redator de logs e o complemento Nginx também cobrem a rota OAuth de retorno. A instalação e o roteiro real estão em [deploy/README.md](deploy/README.md#teste-de-escrita-na-semana-de-23-a-29112026).

Após a gravação, a leitura diária reconhece os novos nomes sem duplicar os eventos aceitos ou mudar os UIDs do calendário. Falha de envio não desfaz uma troca aceita no aplicativo: o resultado é marcado para conferência e não é reenviado automaticamente. Prévias vencem em dez minutos e não podem ser reaplicadas. A conferência anterior e a escrita são duas operações Google distintas: Sheets não fornece comparação e gravação condicionais em uma só operação, portanto combine o teste sem edições simultâneas dessas células por terceiros.

Validação do piloto: **373 testes passaram**; Chromium confirmou prévia sem escrita, confirmação explícita e resultado em tela móvel com Google simulado. OAuth real e escrita na Oracle ainda dependem dos arquivos e da autorização da conta de editor.

## Troca direta de dia ou conjunto de estágios

Em **Propor diretamente ao residente**, ou no atalho **Trocar os estágios do dia** da semana, escolha o colega e os dias. O padrão troca o **dia inteiro de estágios regulares**, incluindo todos os estágios definidos de cada lado. Também é possível selecionar atividades específicas. Cada lado deve pertencer a um único dia; os dias dos dois lados podem ser diferentes. Plantões mensais mantêm a troca individual e as restrições de grupo existentes.

Exemplo: PR 07:45–12:00 e CC Leonardo 13:00–19:00 podem ser oferecidos juntos por um Pré-Operatório 07:40–17:00. O sistema compara **10h15 ↔ 9h20**, diferença de **55 minutos**, sem dividir artificialmente o Pré-Operatório. Mostra a carga antes/depois em cada dia, as atividades mantidas, os intervalos e eventuais sobreposições. Atividades que cruzam a meia-noite são repartidas nos totais por dia local.

A proposta é privada e não depende de publicação no mural. Quem propõe confere e aceita os termos; o destinatário recebe uma notificação em Solicitações e precisa abrir a conferência antes de aceitar. Havendo avisos, os dois marcam ciência explícita das ressalvas. Sobreposições e cargas diferentes não são corrigidas automaticamente: os horários publicados são preservados e a dupla precisa combinar a jornada real. Não há limite legal de carga ou tempo mínimo de deslocamento presumido pelo aplicativo.

O aceite revalida os mesmos termos, incluindo compromissos mantidos nos dias envolvidos. Mudança de titularidade, horários ou escala depois do envio exige nova proposta; não basta apenas o destinatário aceitar termos alterados. Só com ambos os aceites todas as atividades mudam de titular em uma transação, mantendo IDs e eventos do calendário. Recusa e cancelamento não transferem nada. Dias com atividades a combinar ou sem horário precisam ser definidos antes de uma troca de dia inteiro; a seleção parcial explicita essa indefinição.

O modal de troca individual também tem busca e seleção explícita do residente destinatário. Suas propostas vão diretamente a ele, sem criar publicação no mural.

As migrações **0019 e 0020** acrescentam o histórico dos conjuntos e permitem sua conferência no piloto Google. Depois de uma troca 2↔1 ser aceita, o administrador pode preparar uma prévia de **todos os nomes** e enviar um único lote para o Espelho Semanal, mantendo as restrições do piloto 23–29/11/2026, a confirmação e a conexão OAuth existentes. A escrita não é automática; nomes nessas células tornam-se valores fixos. Outras fontes, vários arquivos ou decisões posteriores sobre as mesmas atividades continuam exigindo conferência.

Validação desta ampliação: **425 testes passaram**. Chromium verificou proposta privada 2↔1, diferença de 55 minutos, os dois aceites e avisos obrigatórios, seleção parcial com sobreposição e tela móvel. O piloto Google de conjunto foi validado com API simulada, incluindo três nomes em um único lote e prévias sem repetir escrita. Nenhuma planilha Google real ou instância Oracle foi modificada nesses testes.


## Cores originais, observações e reversões manuais no Google

A migração **0021** acrescenta cores e observações às atividades semanais. Na próxima importação, o calendário usa o fundo e a fonte do **cabeçalho do setor**, inclusive quando ele está mesclado, em vez de interpretar as faixas alternadas dos nomes como categorias. A coluna à direita de F/G (H na segunda-feira, O na terça e assim por diante) aparece como **Obs.** na atividade correspondente, inclusive nas atividades a combinar e sem horário. Remover uma observação na origem também a remove no aplicativo; alterar apenas uma cor não gera aviso de mudança de jornada. Uma observação alterada é incluída nos avisos de atualização e nas novas conferências de conjuntos.

Gravações do piloto usam **#0000ff** na fonte dos nomes trocados. Uma nova prévia de uma troca já gravada com outra cor permite corrigir somente a cor, preservando o valor ou fórmula existente. Se nomes e azul já estiverem corretos, nenhuma nova escrita é enviada. Isso continua exigindo prévia e confirmação administrativas; não há recoloração em massa de trocas históricas.

A migração **0022** registra conferências autenticadas de reversão. Ao clicar **Atualizar agora** como administrador com sua própria conta Google conectada, o aplicativo primeiro confere por OAuth as trocas previamente confirmadas no Google. Se **todas** as células voltaram aos residentes originais e as atividades/decisões locais continuam compatíveis, restaura o conjunto completo, mantém os IDs do calendário, marca **Desfeita na planilha** no histórico e avisa os dois residentes. Essa conferência só lê o Google e não grava novos nomes. O restante das fontes continua sendo importado pelo trabalhador.

Uma reversão parcial, terceiro nome, horário/setor alterado ou troca posterior exige conciliação; a atribuição local não é reinterpretada automaticamente. Sem conexão válida, o aplicativo informa que não pôde conferir e preserva as decisões. A exportação XLSX pode estar atrasada: depois da confirmação OAuth da reversão, os nomes restaurados ficam protegidos até a exportação reconhecer os nomes originais. Se a origem mudar novamente antes dessa confirmação da exportação, uma nova atualização manual confere a divergência por OAuth e mantém os nomes para conciliação, sem reaplicar a troca. Depois de reconhecer a reversão, alterações normais da origem continuam sendo importadas. A leitura diária sem OAuth mantém a proteção das trocas aceitas; para reconhecer uma reversão manual, use **Atualizar agora** com a conexão Google ativa.

Validação de 20/11/2026: **480 testes passaram**. A planilha original foi importada em banco temporário, com 389 atividades definidas, 95 a combinar e 31 sem horário, todas com cores de origem; 198 registros tinham observações. Chromium validou cores reais, observações e visualização de 390 pixels, além de prévia/confirmação azul e reversão completa 2↔1 com API Google simulada. Nenhuma escrita Google real ou implantação Oracle foi executada nesta revisão.


## Ajuste visual e conferência independente de trocas

Os cartões usam fundo branco, texto escuro e a cor exata da planilha na faixa lateral. Cabeçalhos por setor e grupos mensais seguem a mesma identificação. No celular, os dias têm largura maior e rolagem horizontal, com ações separadas das observações.

Em **Planilhas e fontes → Conferir trocas agora**, o administrador pode conferir reversões mesmo enquanto a importação está em andamento. Os dez resultados recentes mostram o motivo de uma divergência ou falha. A conferência usa somente a conexão Google do administrador atual, inclusive para envios históricos de outro administrador; não empresta credenciais. As verificações de titularidade, metadados, reversão completa e decisões posteriores continuam obrigatórias. Uma troca local sem gravação confirmada não é desfeita apenas por encontrar os nomes antigos na origem.

Validação: **485 testes passaram**; Chromium conferiu cartões neutros com faixas nas cores exatas e dias maiores no celular. Falhas de leitura Google passam a informar orientação segura por HTTP 400/401/403/404/429 ou renovação da conexão, sem expor conteúdo de exceções de transporte. O diagnóstico fornecido da Oracle identificou resposta Google acima de 20 MB: a leitura passou a solicitar apenas valores para a conferência da reversão e estilos somente nas células de nomes envolvidas na prévia/gravação. O limite de 20 MB permanece; a solução reduz os dados solicitados. A aplicação na instância ainda precisa ser feita.
