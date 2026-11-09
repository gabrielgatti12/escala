# Auditoria de segurança — 09/11/2026

A revisão encontrou falhas de revogação de acesso e ausência de limite de tentativas de login, além de riscos na importação e no tratamento de dados privados. As correções estão nesta versão. Elas só passam a valer na Oracle depois de atualizar a aplicação, executar as migrações e recriar os serviços, conforme [implantação](../deploy/README.md).

## Escopo e método

Revisão do código Django, autenticação, ativação, permissões, trocas, divisões, fórum, notificações, assinatura privada do calendário, importadores, dependências e configuração de implantação. Os testes ofensivos foram executados com contas fictícias e bancos isolados. A planilha pública foi lida para conferir o importador real; não houve escrita no Google nem alterações na Oracle.

No endereço público foram feitas somente consultas HTTP sem autenticação. Não houve acesso SSH, inspeção do sistema operacional, varredura de portas ou teste de senhas contra residentes reais. O resultado é uma revisão da aplicação com testes locais; não certifica toda a infraestrutura.

## Achados corrigidos

| Achado | Gravidade no contexto | Evidência e efeito anterior | Correção |
| --- | --- | --- | --- |
| Revogação incompleta entre login e identidade importada | Média | Um login vinculado ainda ativo podia continuar acessando o painel após desativar seu residente. Uma identidade técnica com senha ou ativação também podia contornar o login separado. Exigia credencial ou sessão válida. | Backend, middleware e serviços verificam o estado atual das identidades. Sessões antigas são encerradas; reativar a conta não recupera a sessão anterior. Ativação da identidade técnica já vinculada é recusada. |
| Tentativas de login sem limite | Média | Uma sequência de 32 senhas incorretas no ambiente isolado não recebia bloqueio temporário. Facilitava adivinhação de senhas; não demonstrou descoberta de senha. | Contadores compartilhados no banco: 5 falhas por endereço e identidade, ou 30 por endereço, em janela de 15 minutos. Resposta 429 com `Retry-After`. Chaves derivadas por HMAC, sem guardar e-mail ou IP em texto no contador. |
| Expansão excessiva de XLSX | Média, condicionada ao controle da entrada | Dimensões esparsas e arquivos compactados podiam provocar trabalho excessivo no importador. Exigia influência sobre a planilha de origem ou importação pelo operador; não era upload anônimo. | Inspeção antes de iterar com openpyxl: 20 MiB compactados, 128 MiB expandidos, 4096 partes, 2 milhões de células por aba e 5 milhões no arquivo. Coordenadas explícitas e implícitas, células e linhas repetidas também são contadas quando falta dimensão. DTD, partes duplicadas e compactação não suportada são recusadas. |
| Importação parcial de fórmulas e datas | Baixa, integridade | Fórmulas sem resultado armazenado ou cabeçalhos de data ausentes em dias com residentes podiam ser interpretados como ausência de escala e desativar registros. | A importação semanal recusa esses casos e preserva os dados anteriores em transação. Resultado de texto vazio confirmado no XML é distinguido de fórmula não calculada, sem bloquear semanas válidas. Não executa fórmulas. A inspeção reutiliza o mesmo download. |
| Identidade de plantão dependente de delimitador no título | Baixa, integridade | O caractere `\|` no nome de uma aba podia alterar a construção da chave lógica e gerar identidade indevida após renomeação. | Chave construída com data, grupo, horário e ordinal explícitos. Releitura e renomeação preservam as cinderelas existentes. |
| Importação local podia colidir com fonte sincronizada | Baixa, integridade; exige operador | Um ID do arquivo local podia alcançar um registro pertencente a uma fonte Google. Um login vinculado também podia receber registros separados da identidade do residente. | Colisão com fonte sincronizada é recusada; login vinculado resolve para o residente existente e exige identidade ativa. |
| Arquivos temporários privados elegíveis para Git ou imagem | Baixa, exposição potencial | Uma interrupção do ajuste de intervalo podia deixar cópia temporária do `.env`. Arquivos auxiliares do SQLite também precisavam de exclusão. Não foram encontrados segredos rastreados no Git. | Prefixo `.env.tmp-*`, exclusões em Git e Docker e exclusão de bancos e arquivos auxiliares. O script preserva permissões e não imprime valores privados. |
| Páginas autenticadas sem proibição explícita de armazenamento | Baixa, privacidade | Páginas com contatos e dados pessoais podiam ficar elegíveis ao cache do navegador ou intermediário. | Respostas autenticadas e encerramento de sessão recebem `Cache-Control` privado com `no-store`. Cabeçalhos mais restritos já presentes são preservados. |
| Erro interno em disputa de escrita no SQLite | Baixa, disponibilidade | Aceites concorrentes podiam resultar em um aceite e um erro 500 por banco ocupado. Não foi demonstrado aceite duplo ou quebra de consentimento. | Disputa de escrita retorna 409 com orientação para conferir o resultado e `Retry-After`. Não há repetição automática de uma operação que exige consentimento. |
| Dependência ASN.1 com avisos de negação de serviço | Baixa, preventiva nesta aplicação | A versão transitiva pyasn1 0.6.4 constava em quatro avisos. Não foi demonstrado caminho de entrada ASN.1 controlado por visitante nesta aplicação. | Versão 0.6.5 fixada explicitamente; instalação, dependências e assinatura usada pela autenticação Google verificadas. |

O limite de login também cobre `/admin/login/`, não conta formulários recusados por CSRF e falha de forma fechada se o banco dos contadores estiver indisponível. O endereço encaminhado só é usado com a configuração de proxy confiável; o Nginx precisa substituir `X-Forwarded-For`, e a porta da aplicação deve continuar restrita ao loopback. Esse limite reduz adivinhação de senha; não é proteção contra DDoS.

Avisos da dependência corrigida:

- [GHSA-2qm8-x737-gmw5](https://osv.dev/vulnerability/GHSA-2qm8-x737-gmw5)
- [GHSA-fm9c-5rcf-2q5p](https://osv.dev/vulnerability/GHSA-fm9c-5rcf-2q5p)
- [GHSA-mm57-4v3p-rchx](https://osv.dev/vulnerability/GHSA-mm57-4v3p-rchx)
- [GHSA-9m75-vc3g-4x39](https://osv.dev/vulnerability/GHSA-9m75-vc3g-4x39)

Os metadados do PyPI foram consultados para as versões das dependências instaladas. Não foi executado `pip-audit`; a consulta não garante ausência de vulnerabilidades ainda não publicadas ou não cadastradas.

## Controles conferidos

Os testes de regressão recusaram acesso administrativo por residente, respostas a solicitações de terceiros, mutações por GET e POST sem CSRF. Verificaram também escape de conteúdo no HTML, filtros sem interpolação de SQL, IDs Google restritos e assinatura de calendário revogável e vinculada à conta. Não foi encontrado desvio nesses casos testados.

As trocas revalidam titularidade, conta ativa, datas futuras, categoria, grupo e conflitos no momento do aceite. Cinderela troca com cinderela, noturno com noturno e diurno com diurno; R1 com R1 e R2/R3 entre si. A divisão de atividades continua exigindo a mesma dupla e o mesmo dia, horários sobrepostos e uma atividade para cada residente.

Os arquivos privados de configuração e banco não têm rota pública na aplicação. Os logs privados de assinatura de calendário exigem também a configuração de Nginx documentada em [deploy/README.md](../deploy/README.md); não foi possível confirmar sua instalação na Oracle.

## Validação desta versão

- 328 testes Django passaram, incluindo os casos de exploração e suas regressões.
- Verificações do Django, migrações, sintaxe JavaScript, dependências instaladas e espaços inválidos do diff passaram.
- Chromium validou login com CSRF real, bloqueio temporário, revogação de sessão e os três tipos de plantão em banco isolado, inclusive em tela móvel.
- Gunicorn e WhiteNoise foram verificados com configuração de produção, proxy HTTPS e arquivos estáticos com hash.
- A importação real preservou os IDs existentes de cinderelas e incluiu noturnos e diurnos sem duplicar registros na releitura. A migração 0016 cadastra a fonte automaticamente; não exige colar o link na interface.

Dois meses históricos foram preservados por problemas na origem: data inválida em `MAIO2026!Q63` e nome sem correspondência em `SETEMBRO2026!R74`. Os meses atuais e futuros foram importados sem pendências de conciliação nesta validação. Os avisos aparecem na fonte; não foram adivinhadas datas nem associações de nomes.

## Limites e acompanhamento

A consulta pública confirmou redirecionamento para login das páginas protegidas, login com proibição de cache e cabeçalhos `DENY` e `nosniff`. Não havia HSTS na resposta consultada. HSTS pode ser adotado gradualmente depois de conferir a configuração HTTPS e a renovação do certificado; sua ausência, isoladamente, não demonstrou exploração.

A atualização da Oracle, configuração do proxy, atualizações do Ubuntu, regras de rede, permissões dos arquivos e rotina de backup ainda precisam ser conferidas na própria instância. Autenticação de dois fatores para administradores é uma melhoria futura. Nenhuma dessas limitações foi tratada como vulnerabilidade crítica sem evidência.

As planilhas Google continuam somente leitura. Trocas aceitas alteram o calendário local e são protegidas de sobrescrita por uma origem desatualizada. Gravar na planilha original depende de acesso de editor e credencial autenticada de escrita, ainda não configurados. A leitura permanece diária, com antecipação pelo botão **Atualizar agora**.
