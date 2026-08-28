# cdnmnus: padrao de producao, ocultacao de origem e capacidade

## Finalidade e limite da nota

Este documento define o que a equipe deve implementar e comprovar para classificar o cdnmnus como pronto para producao. Ele foi elaborado a partir do codigo versionado, da configuracao efetivamente carregada pelo Nginx e de testes autorizados com playlists, manifests HLS, segmentos MPEG-TS e arquivos MP4.

Uma nota `10/10` significa que todos os controles e criterios deste documento foram atendidos e que nao existem achados criticos ou altos conhecidos. Nao significa anonimato matematicamente absoluto. A origem ainda pode ser descoberta por informacao externa, historico de DNS, terceiros, falha operacional ou vulnerabilidade futura. A garantia tecnica correta e:

> Um cliente comum nao recebe enderecos de origem nos fluxos testados e, mesmo que descubra um endereco, o firewall da origem rejeita seu acesso direto.

Nunca registre neste repositorio credenciais de assinante, playlists completas, URLs assinadas, IPs privados de operacao ou nomes de relays exclusivos do ambiente.

O ciclo completo de expiracao, renovacao reativa, token broker e testes contra vazamento esta especificado em [TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md](TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md).

## Evidencias do ambiente auditado

Os valores abaixo sao um retrato do teste, nao garantias permanentes:

- host com 2 vCPU, aproximadamente 4 GiB de RAM e sem swap;
- Nginx com dois workers e painel restrito a `127.0.0.1:9090`;
- UFW ativo, permitindo publicamente SSH, HTTP e HTTPS;
- certificado publico valido e TLS 1.2/1.3;
- duas playlists comparadas com 318.342 entradas cada;
- todas as URLs da playlist entregue pela CDN usavam o host publico;
- a playlist direta usava o host de origem;
- amostras LGBT e UFC permaneceram no host publico ate o segmento MPEG-TS;
- filmes e episodios inicialmente redirecionavam o cliente para dois destinos externos;
- relays fixos foram adicionados, e seis amostras de VOD passaram a permanecer no host publico ate o MP4;
- sem cache, o mesmo fluxo passou a receber `503` por volta de 10-14 requisicoes simultaneas;
- com cache HLS e cache lock nas duas etapas do fluxo, um teste frio teve 50/50 sucessos com um `MISS` e 49 `HIT`;
- com o objeto aquecido, o teste chegou a 500/500 respostas `200`, todas `HIT`;
- durante os `503` anteriores e o teste de 500 hits, a CPU da edge permaneceu longe da saturacao;
- a banda de saida medida variou aproximadamente entre 426 e 843 Mbit/s, com mediana proxima de 600 Mbit/s.

### Reprodutibilidade

O inicio da auditoria encontrou divergencia entre o `panel.py` instalado e o arquivo versionado. O gerador instalado, incluindo profiles, LB, relays e cache HLS, foi sincronizado de volta ao worktree e os hashes passaram a coincidir. Os testes Python, Bash, painel, smoke e `nginx -t` foram aprovados.

Enquanto essas alteracoes nao forem revisadas, commitadas e publicadas como release:

- outro servidor ainda nao consegue reproduzir a correcao a partir de `origin/main`;
- o update oficial pode substituir o comportamento se for executado antes da release;
- nenhuma promocao deve ocorrer sem revisar o diff e testar uma instalacao limpa.

## Ocultacao do XUI: requisitos para 10/10

### 1. Origem inacessivel ao cliente

Este e o controle mais importante. No firewall do XUI e de cada relay/LB:

1. permitir a porta de streaming somente a partir dos IPs das edges autorizadas;
2. bloquear todo o restante da Internet;
3. aplicar o mesmo controle no firewall do sistema e no firewall do provedor;
4. impedir acesso por IPv6 quando a ACL cobre apenas IPv4;
5. remover portas alternativas, painel XUI e APIs administrativas da exposicao publica;
6. comprovar o bloqueio a partir de uma rede externa que nao seja a CDN.

O teste deve produzir `timeout`, `filtered` ou rejeicao explicita ao acessar a origem fora da allowlist. Um `200`, `301`, `302`, `401`, `403` ou pagina de login ainda prova que a origem esta publicamente alcancavel.

Sem essa ACL, a ocultacao fica limitada a no maximo `7/10`, mesmo que nenhuma playlist vaze.

### 2. DNS e certificados sem historico obvio da origem

- o dominio publico deve apontar somente para as edges;
- nenhum registro `A`, `AAAA`, `CNAME`, `MX`, TXT operacional ou subdominio deve expor a origem;
- certificados da origem nao devem reutilizar o nome publico da edge quando isso permitir correlacao;
- dominios antigos devem ser removidos ou protegidos;
- ferramentas de inventario e historico de DNS devem ser verificadas durante cada release.

DNS limpo reduz descoberta casual, mas nao substitui o firewall da origem.

### 3. Reescrita completa e fail-closed

O proxy deve tratar explicitamente:

- `Location` absoluto e relativo;
- variantes HTTP/HTTPS e portas explicitas;
- playlists M3U/M3U8;
- manifests HLS mestre e de midia;
- segmentos TS/fMP4;
- API JSON, XMLTV/EPG e logos;
- filmes, episodios e requisicoes `Range`;
- IPv4, IPv6 e nomes DNS conhecidos;
- erros 4xx/5xx e paginas HTML do upstream;
- redirects em cadeia.

Destinos autorizados devem usar relays fixos e separados. Nunca aceite um hostname fornecido pelo cliente para decidir o destino do `proxy_pass`: isso criaria SSRF ou um proxy aberto.

Qualquer `Location` desconhecido deve ser bloqueado ou transformado em erro controlado, e nao repassado ao cliente. Novos hosts observados devem passar por revisao antes de entrar na allowlist.

### 4. Headers e identidade da origem

Remover ou substituir, em todas as locations:

- `Server` do upstream;
- `Via`;
- `X-Powered-By`;
- headers de painel ou framework;
- headers canonicos e links absolutos;
- cookies de dominio da origem;
- mensagens de erro que contenham host, IP ou caminho interno.

O header `Server: nginx` da edge nao revela o XUI, mas pode ser removido por uma build/modulo apropriado caso a politica exija ocultar tambem a tecnologia da edge.

### 5. IP e identidade encaminhados

O XUI nao deve receber o IP real do assinante se isso permitir contato direto, bloqueio incorreto ou vazamento em respostas. Defina uma politica unica para `X-Real-IP` e `X-Forwarded-For` e teste o comportamento do XUI. Nao confie em headers enviados pelo cliente.

### 6. Credenciais e transporte

- redirecionar HTTP para HTTPS antes de processar endpoints com credenciais;
- entregar playlists e streams somente por HTTPS quando os aplicativos forem compativeis;
- impedir query strings e caminhos credenciados em access logs, tracing e mensagens de erro;
- usar usuarios de teste exclusivos e rotaciona-los apos auditorias;
- nunca colocar credenciais em documentacao, Git, shell history ou tickets;
- definir expiracao, limite e revogacao no XUI.

### 7. Teste externo obrigatorio

O teste de ocultacao deve partir de pelo menos duas redes externas. Executar apenas na propria edge nao comprova que o firewall da origem bloqueia clientes.

A matriz minima por release deve cobrir:

| Fluxo | Amostras minimas | Validacoes |
| --- | ---: | --- |
| Playlist completa | 1 | hosts, IPs, URLs absolutas, credenciais inesperadas |
| Canais ao vivo | 20 | redirect, master, media manifest e 3 segmentos |
| Filmes | 20 | cadeia de redirects, `Range`, MP4 final |
| Series | 20 episodios | cadeia de redirects, `Range`, MP4 final |
| EPG/XMLTV | 3 | URLs, logos, headers e erros |
| APIs XUI usadas pelos apps | todas | JSON, headers, redirects e erros |
| Canais offline | 10 | resposta controlada sem identidade da origem |

O teste falha se qualquer host/IP nao autorizado aparecer em header, corpo, redirect, manifest, cookie ou URL efetiva.

## Seguranca geral: requisitos para 10/10

### Sistema operacional

- aplicar todas as atualizacoes de seguranca;
- reiniciar no kernel atualizado;
- habilitar atualizacao automatica de seguranca com janela e monitoramento;
- configurar uma swap pequena ou outro mecanismo controlado contra OOM;
- remover pacotes e servicos desnecessarios;
- verificar vulnerabilidades das versoes instaladas em cada release.

### SSH administrativo

- `PermitRootLogin no`;
- `PasswordAuthentication no`;
- chaves individuais, protegidas e rotacionaveis;
- usuario administrativo nominal com `sudo`;
- acesso somente por VPN ou allowlist de IP;
- Fail2ban ou controle equivalente;
- `MaxAuthTries` reduzido e logs/alertas de tentativa de acesso;
- desabilitar X11 forwarding quando nao utilizado.

Alteracoes SSH exigem uma segunda sessao aberta e teste de acesso antes de recarregar o daemon, para evitar bloqueio administrativo.

### Nginx e borda

- redirecionamento HTTP para HTTPS;
- TLS moderno validado externamente;
- HSTS somente depois de confirmar todos os subdominios e clientes;
- limites de conexao e requisicao por IP/credencial, sem quebrar HLS;
- timeouts e tamanho de headers/corpo revisados;
- respostas de erro proprias;
- protecao DDoS upstream, porque UFW sozinho nao protege o link;
- nenhum relay dinamico controlado pelo usuario;
- limite de banda e concorrencia coerente com o plano vendido;
- health checks que validem origem e conteudo, nao apenas o processo Nginx.

### Painel

- permanecer em localhost ou VPN;
- executar como usuario dedicado, sem root, quando a arquitetura permitir;
- separar o componente que valida dados do helper privilegiado que grava Nginx;
- eliminar Basic Auth em HTTP e preferir sessao segura ou mTLS pelo tunel/VPN;
- protecao contra brute force e auditoria de alteracoes;
- CSRF quando autenticacao baseada em cookie for adotada;
- segredo e banco com backup criptografado e permissao minima;
- unit file com `ProtectSystem`, `PrivateTmp`, `NoNewPrivileges`, restricoes de syscall e paths minimos;
- nenhuma senha inicial plaintext persistente.

### Observabilidade e resposta

A instalacao atual desliga access logs globalmente. Producao precisa de metricas sem registrar credenciais:

- conexoes ativas e simultaneos por edge;
- Mbit/s de entrada e saida;
- status 2xx/3xx/4xx/5xx por rota sanitizada;
- latencia, connect time e first-byte de cada upstream;
- uso de CPU, RAM, swap, disco, file descriptors e conntrack;
- expiracao de certificado;
- disponibilidade de canais de teste;
- alertas para aumento de `502`, `503`, `504` e redirects desconhecidos;
- retencao definida e acesso auditado;
- runbook de incidente e rotacao imediata de credenciais.

Nunca use `$request_uri` bruto em logs de IPTV quando usuario/senha aparecem no caminho ou query string.

### Disponibilidade e supply chain

- no minimo duas edges em provedores ou zonas distintas;
- health check e failover DNS/balanceador testado;
- backup versionado de configuracao, banco e certificados conforme politica;
- restore testado periodicamente;
- deploy imutavel a partir de commit/tag revisado;
- Git e servidor sem divergencia nao documentada;
- CI com sintaxe, testes unitarios, integracao Nginx, segredo e SAST;
- canary antes de producao;
- rollback testado e independente de `git reset --hard`;
- inventario de dependencias e responsavel por atualizacoes.

## Capacidade: modelo correto

Esta arquitetura nao faz multicast, cache compartilhado de live nem consolidacao de streams. Cada espectador gera trafego completo entre upstream e edge e novamente entre edge e cliente.

Para estimativa inicial:

```text
clientes_por_banda = banda_util_mbit / bitrate_medio_mbit
banda_util_mbit = banda_menor_direcao_mbit * fator_de_seguranca
```

Use fator de seguranca entre `0,60` e `0,70`. A menor direcao inclui:

- entrada do upstream para a edge;
- saida da edge para os clientes;
- limite contratual, policer e franquia do provedor;
- capacidade de rede do XUI e dos relays.

Com 70% de utilizacao teorica:

| Link sustentado | 4 Mbit/s | 6 Mbit/s | 8 Mbit/s | 10 Mbit/s |
| ---: | ---: | ---: | ---: | ---: |
| 1 Gbit/s | 175 | 116 | 87 | 70 |
| 2,5 Gbit/s | 437 | 291 | 218 | 175 |
| 5 Gbit/s | 875 | 583 | 437 | 350 |
| 10 Gbit/s | 1.750 | 1.166 | 875 | 700 |

Esses numeros sao tetos de banda, nao capacidade garantida. CPU de TLS, limite do upstream, portas efemeras, file descriptors, conntrack, perda, latencia e tamanho dos segmentos podem reduzir o resultado.

### Perfil recomendado por escala

| Simultaneos pretendidos | Perfil inicial | Rede minima recomendada |
| ---: | --- | --- |
| ate 100 | 4 vCPU, 8 GiB | 1 Gbit/s sustentado |
| 100-300 | 8 vCPU, 16 GiB | 2,5 Gbit/s sustentado |
| 300-700 | 16 vCPU, 32 GiB | 5-10 Gbit/s sustentado |
| acima de 700 | multiplas edges | 10 Gbit/s por node e balanceamento |

Dimensione pelo bitrate medido no horario de pico. O numero de entradas da playlist nao representa simultaneos.

## Protocolo de teste de carga

1. Usar conta de teste com limite confirmado no banco e no painel XUI.
2. Confirmar um conjunto de canais ativos antes da carga.
3. Medir diretamente o upstream e depois a CDN, com o mesmo perfil.
4. Subir em etapas: 10, 25, 50, 100 e depois incrementos definidos.
5. Sustentar cada etapa por pelo menos 15 minutos para teste de capacidade; amostras curtas medem apenas burst.
6. Distribuir clientes de redes externas. Gerar clientes na propria edge nao mede a saida publica corretamente.
7. Registrar somente identificadores sanitizados, status, bytes, bitrate e tempos.
8. Parar ao atingir qualquer criterio de abortagem.

Criterios de abortagem:

- erro acima de 1%;
- `502/503/504` sustentado;
- CPU acima de 75% por 10 minutos;
- memoria acima de 80% ou OOM;
- perda acima de 0,5%;
- buffer/stall acima do SLO;
- banda acima de 70% do limite sustentado;
- latencia first-byte acima do SLO;
- upstream recusando conexoes.

O teste sem cache chegou a `503` antes de saturar a edge. O cache eliminou a multiplicacao do mesmo segmento e chegou a 500/500 hits locais. Canais diferentes ainda dependem de fluxos diferentes no XUI; upgrade da edge nao corrige recusas da origem.

## Gates objetivos para a nota 10/10

Todos os itens abaixo devem estar aprovados:

- [ ] origem e LBs bloqueiam qualquer IP que nao seja uma edge autorizada;
- [ ] teste externo confirma que a origem nao e acessivel diretamente;
- [ ] nenhuma amostra da matriz de live/VOD/API/EPG vaza destino;
- [ ] redirects desconhecidos falham fechados;
- [ ] todo trafego credenciado usa HTTPS;
- [ ] sistema e dependencias sem atualizacoes criticas pendentes;
- [ ] SSH sem root/senha e restrito por VPN/allowlist;
- [ ] painel sem exposicao publica e com privilegio minimo;
- [ ] protecao DDoS, rate limits e limites de conexao validados;
- [ ] metricas, alertas e logs sanitizados operacionais;
- [ ] duas edges e failover testado, se o SLO exigir alta disponibilidade;
- [ ] backup e restore testados;
- [ ] Git, artefato instalado e configuracao ativa correspondem a uma release;
- [ ] CI e testes de seguranca/carga passam;
- [ ] teste sustentado atende capacidade e SLO sem erro acima de 1%;
- [ ] runbook de incidente, responsaveis e rotacao de segredo documentados.

## Ordem recomendada de execucao

### P0: impede promessa de ocultacao

1. Aplicar allowlist nas origens e testar de uma rede externa.
2. Reconciliar painel instalado, gerador Nginx e repositorio.
3. Fazer redirects desconhecidos falharem fechados.
4. Forcar HTTPS para endpoints credenciados.
5. Rotacionar toda credencial exposta durante testes.

### P1: risco de invasao e indisponibilidade

1. Atualizar o sistema e reiniciar no kernel corrigido.
2. Remover SSH root/senha e restringir a administracao.
3. Implementar observabilidade sanitizada e alertas.
4. Investigar os `503` do upstream e confirmar o limite real da conta.
5. Adicionar protecao DDoS e limites testados.

### P2: maturidade profissional

1. Criar segunda edge e failover.
2. Separar privilegios do painel.
3. Implementar CI, canary e releases reproduziveis.
4. Executar carga externa sustentada e registrar baseline por perfil de maquina.
5. Testar restore e resposta a incidentes.

Somente depois dos P0, P1 e gates aplicaveis do P2 a solucao pode ser apresentada como `10/10` dentro do escopo definido neste documento.
