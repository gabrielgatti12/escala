# Revisão funcional — 25/10/2026

As correções desta revisão mantêm a planilha em modo de leitura e preservam as decisões aceitas no aplicativo. Não interpretam a cobertura do pós-plantão, que depende de outra fonte.

## Corrigido

| Fluxo | Problema encontrado | Comportamento atual |
| --- | --- | --- |
| Importação | O mesmo residente em F e G podia gerar dois plantões iguais. | Uma identidade por linha; horários em linhas diferentes continuam separados. Duplicatas antigas não protegidas ficam inativas na próxima importação. |
| Divisão | Uma proposta podia ser criada ou aceita após o início das atividades. | Somente atividades futuras podem ser divididas, tanto na seleção quanto na confirmação. |
| Divisão | Datas eram comparadas em UTC e a dupla podia mudar após a proposta. | Comparação no fuso local e revalidação do destinatário antes de atribuir as atividades. |
| Solicitações | Pedidos vencidos continuavam aumentando o contador e oferecendo aceite. | Saem das caixas de pendências, ficam no histórico e são cancelados no próximo ciclo do sincronizador. Consultar uma página não grava essa mudança. |
| Contas | Migrar para um acesso por e-mail podia manter o login técnico anterior ativo. | Senha da identidade original é inutilizada ao aplicar a importação, encerrando sessões antigas. A conta vinculada mantém sua senha e privilégios. Contas desativadas exigem revisão. |
| Calendário semanal | A abertura considerava apenas plantões definidos e podia ignorar atividades anteriores pendentes do próprio residente. | A semana inicial considera também atividades da dupla e atividades sem horário. A data selecionada explicitamente continua sendo respeitada. |
| Calendário mensal | Faltavam atalhos para navegar entre meses ou abrir a semana de um dia. | Meses anterior/próximo, mês atual e link em cada dia; o filtro de todos os residentes é preservado. |
| Busca de troca | Listas longas dificultavam encontrar um plantão. | Busca por nome, data e setor, sem exigir acentos, com contagem de resultados e ano/horário na opção. |
| Diagnóstico | Erros de formato só mostravam o tipo da exceção; conciliações ficavam ocultas. | Erros conhecidos mostram linha/motivo. A página de fontes permite conferir o resumo e destaca conciliações. Exceções externas continuam sem exibir detalhes que possam conter credenciais. |

A seleção de divisões agrupa candidatos por dupla e dia, evitando comparar todo o histórico entre si. Alterar a data pelo formulário semanal mantém os filtros de residente e interesse em troca. Criar, aceitar, recusar ou cancelar divisões retorna à caixa correspondente de Solicitações.

## Validação

- 103 testes automatizados passaram, incluindo 28 regressões novas.
- Verificação do Django, migrações pendentes e sintaxe JavaScript passaram. Não há migração nova nesta revisão.
- Navegador Chromium: busca e seleção, troca e divisão com confirmação em duas contas, calendário mensal na virada do ano e navegação no celular. Banco temporário separado, apenas dados fictícios.
- Gunicorn/WhiteNoise: login e CSS/JavaScript versionados servidos com configuração de produção e proxy HTTPS. A imagem Docker deve ser reconstruída na Oracle.

## Próximas melhorias úteis

1. Conciliação administrativa dentro do aplicativo: comparar origem e decisão aceita, registrar a escolha e quem a confirmou. Atualmente o sistema preserva a decisão e sinaliza o caso.
2. Recuperação de senha e avisos de solicitações por e-mail, após configurar um serviço de envio. O primeiro acesso continua por link individual entregue pelo administrador.
3. Integrar a planilha de cobertura do pós-plantão antes de automatizar esses casos. O calendário não deve inferir essa regra a partir da repetição dos nomes.

## Aplicar na Oracle

```bash
cd /opt/escala-app &&
git pull --ff-only origin codex/escala-inicial &&
bash scripts/oracle-compose.sh build &&
bash scripts/oracle-compose.sh run --rm web python manage.py migrate --noinput &&
bash scripts/oracle-compose.sh up -d
```

O próximo ciclo aplica as correções de leitura às fontes cadastradas. Se uma duplicata antiga estiver envolvida em troca já aceita, ela é preservada e marcada para conciliação. A migração de credenciais antigas ocorre ao aplicar os contatos selecionados; não há alteração em massa de senhas durante a atualização do código.
