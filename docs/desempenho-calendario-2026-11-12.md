# Calendário e navegação — 12/11/2026

O usuário relatou demora ao abrir e navegar no calendário. A página carregava antecipadamente todas as opções futuras de troca, mesmo quando a janela de troca permanecia fechada. O navegador também percorria essas opções ao iniciar e a cada busca.

## Mudanças

- O calendário e o mural não incluem mais todas as opções de troca no HTML. A janela busca até 50 opções ao abrir; nome, setor, data ou horário permitem localizar opções fora desse primeiro resultado, inclusive meses distantes.
- A busca aguarda 250 ms após digitar e descarta respostas anteriores. Seleções feitas por um cartão são preservadas; erro ou carregamento pendente impede enviar a proposta e oferece nova tentativa.
- O endpoint exige autenticação e filtra titularidade, residentes ativos, futuro, conciliação, categoria e grupo. O envio e o aceite continuam passando pelas validações dos serviços, sem transferir a decisão de segurança ao navegador.
- A navegação semanal e mensal usa limites de data/hora no fuso de São Paulo, sem converter cada linha para data no filtro SQL. A migração 0017 acrescenta índices para esses intervalos, titular e fonte.
- Abas administrativas e solicitações deixam de calcular dados semanais que não exibem. Contagens de solicitações foram reunidas em menos consultas.
- O título aparece como `PA (Pronto Atendimento) · HOSPITAL` na interface, download e assinatura do calendário. Separadores com espaços não separáveis também são aceitos. Os identificadores de eventos e de importação não mudam.

## Comparação local

Banco local com 5.357 plantões, dos quais 1.137 futuros. Mesma identidade e URLs antes e depois; consulta ao banco somente para leitura. Tamanho é HTML sem compressão; tempos são da execução da view, sem rede, middleware de autenticação ou renderização no navegador. São amostras de diagnóstico, não uma garantia de latência na Oracle.

| Página | HTML antes → depois | Consultas antes → depois | Tempo antes → depois |
| --- | --- | --- | --- |
| Minha semana | 187.555 → 23.171 bytes | 40 → 29 | 0,160 → 0,063 s |
| Semana de todos | 268.072 → 103.688 bytes | 39 → 29 | 0,158 → 0,034 s |
| Solicitações | 178.119 → 13.735 bytes | 40 → 25 | 0,121 → 0,014 s |

A página da própria semana enviou aproximadamente 88% menos HTML nessa comparação. Em banco separado com 24 mil registros fictícios, o plano de consulta passou de varredura e ordenação temporária para busca por índice; a migração 0016 → 0017 foi validada.

## Verificação

343 testes Django passaram. As novas regressões conferem tamanho da página com mil plantões adicionais, limites de semana/mês em horário local, abas administrativas, busca limitada, titularidade, grupos, datas, nomes com acentos e segurança do endpoint. Verificações do Django, migrações, JavaScript e diff também passaram.

Chromium com banco fictício validou ausência de chamadas de opções antes de abrir a janela, seleção por cartão, busca por plantão distante, descarte de resposta atrasada, falha e nova tentativa, envio da proposta, aceite pelo destinatário e tela móvel. Os testes não criaram solicitações em escalas reais.

## Implantação e limites

Aplicar o código e a migração 0017 na Oracle conforme [deploy/README.md](../deploy/README.md). Apenas reconstruir o serviço web não instala os índices. Não há conexão SSH com a Oracle neste ambiente; essas medições não confirmam a atualização remota.

A sincronização permanece diária, com cache SQLite e atualização manual. As importações ainda usam transações por fonte e podem disputar escrita no banco durante uma leitura; esta alteração não troca o banco nem elimina essa possibilidade. Se a navegação continuar lenta depois da atualização, medir o tempo na própria instância e no navegador, junto ao estado do sincronizador. A assinatura no Google só refletirá os títulos quando o Google reler o feed.
