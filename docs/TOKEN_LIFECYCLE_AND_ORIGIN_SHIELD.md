# cdnmnus: tokens inteligentes e origin shield

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)

## Estado atual do tenant tvbrasil

O XUI `38.46.223.77` pode emitir dois formatos de playlist. No formato
legado, os itens usam `/play/<token>/m3u8`; no formato atualmente observado,
usam `/<usuario>/<senha>/<id>.m3u8`. O renderer atual do cdnmnus reescreve
somente a autoridade HTTP para `tvbrasil.phpd77.com` e preserva o caminho
recebido. Portanto, ele oculta a origem, mas não transforma automaticamente o
segundo formato em token opaco.

Não é seguro resolver isso com um `sub_filter` fixo: ele não cria um
mapeamento reversível, não pode armazenar credenciais em logs e pode quebrar
tokens, query strings e URLs assinadas. A correção profissional exige uma
camada de transformação de playlist com token opaco, expiração, revogação e
broker de mídia que aceite o token sem devolver o caminho original ao cliente.
Essa camada deve ser ativada somente depois de validar os fluxos HLS e VOD em
ambos os hosts.

O certificado de `cdn.phpd77.com` também não é herdado automaticamente por
`tvbrasil.phpd77.com`: clientes TLS validam o nome solicitado. Como a zona é
DNS-only, é necessário distribuir um certificado com SAN para cada hostname
gerenciado, ou um wildcard apropriado; aliases em outra zona precisam estar
incluídos no certificado das edges ou terminar TLS no provedor dessa zona.

## Objetivo

Esta especificacao define como manter URLs estaveis da CDN enquanto tokens, redirects, hosts e IPs do XUI/LB permanecem exclusivamente internos.

```text
cliente -> URL estavel da CDN -> cache de segmento -> conteudo
                                  |
                                  +-> token broker -> XUI -> token do LB
```

O cliente nunca deve receber um `Location` da origem, token do LB, host, IP, porta ou caminho interno. Tambem nao pode escolher dinamicamente o destino de `proxy_pass`.

`10/10` significa que todos os gates deste documento passaram. Nao significa anonimato matematico: ACL na origem, egress controlado, atualizacoes e operacao continuam obrigatorios.

## Resposta objetiva

Sim, a CDN pode descobrir e renovar automaticamente um token expirado.

O primeiro cache auditado renovava entradas depois de 30 segundos. A implementacao atual separa o mapeamento do broker (15 segundos) do conteudo live (seis segundos), usando singleflight e `cache_lock`. O modelo anterior atingiu 50/50 clientes em cache frio e 500/500 em cache aquecido.

Entretanto, existe uma janela nao coberta: o token do LB pode expirar antes do redirect armazenado. Nesse caso, a CDN pode reutilizar o redirect, apresentar o token vencido ao LB e receber `401`, `403`, `404` ou `410`. O Nginx atual nao associa o erro do token a entrada pai para renova-la e repetir toda a cadeia transparentemente.

Servir stale ajuda em falhas temporarias, mas nao renova tokens e nao pode manter segmentos live antigos indefinidamente.

## Comportamento-alvo

```text
1. Receber URL publica canonica.
2. Consultar cache do segmento final.
3. Em HIT, servir sem acessar XUI/LB.
4. Em MISS, adquirir lock por canal/segmento.
5. Consultar mapeamento interno de token.
6. Se ausente ou perto de expirar, consultar o XUI.
7. Validar o redirect contra allowlist e politica de rede.
8. Buscar o segmento internamente no LB.
9. Armazenar resposta 200/206 e servir.
10. Em erro de token, invalidar, renovar e tentar uma unica vez.
11. Em segundo erro, falhar fechado sem Location ou identidade da origem.
```

## Arquitetura recomendada

### Nginx como data plane

O Nginx permanece responsavel por:

- TLS e conexoes publicas;
- streaming sem manter videos inteiros em memoria;
- cache de segmentos e `Range`;
- limites, timeouts e respostas genericas;
- remocao de headers e redirects do upstream.

### Token broker como control plane

Criar um servico local pequeno, por Unix socket ou `127.0.0.1`, sem porta publica. Ele resolve tokens, mas nao transporta continuamente o video.

Responsabilidades:

- receber uma chave canonica opaca;
- consultar o XUI somente quando necessario;
- extrair `Location` sem expo-lo;
- validar esquema, host, porta, caminho, DNS e IP;
- armazenar mapeamentos por poucos segundos;
- renovar antecipadamente;
- invalidar em resposta de token vencido;
- usar singleflight/lock por chave;
- limitar redirects e retries;
- devolver ao Nginx somente um identificador de rota interna;
- produzir metricas e logs sem tokens.

Recomendacao: broker em Go ou Rust, com Nginx fazendo o streaming. Python serve para prototipo, mas nao e a primeira escolha para esse hot control path em producao.

### Estado compartilhado

| Opcao | Adequacao |
| --- | --- |
| Broker local em memoria | recomendado para uma edge |
| OpenResty shared dictionary | eficiente, exige Lua/OpenResty |
| Redis protegido | util quando varias edges realmente compartilham tokens |
| SQLite/disco no hot path | nao recomendado |

Tokens vinculados ao IP da edge nao devem ser compartilhados com outra edge.

## Maquina de estados

```text
EMPTY      sem token
RESOLVING  uma rotina consulta o XUI
VALID      token utilizavel
REFRESHING token atual ainda valido e renovacao em andamento
STALE      expirado, utilizavel somente na janela segura definida
FAILED     renovacao falhou; circuit breaker ativo
REVOKED    destino/token bloqueado explicitamente
```

Entrada interna sugerida:

```text
canonical_key_hash
credential_partition_hmac
upstream_id
opaque_upstream_path
created_at
refresh_at
expires_at
last_success_at
failure_count
state
```

Tokens e URLs completas nunca entram em logs.

## Expiracao e renovacao

### Expiracao declarada

Quando a expiracao for confiavel e autenticada:

```text
refresh_at = expires_at - safety_margin - jitter
```

Nao confiar em payload JWT sem validar assinatura quando ele orientar decisoes de seguranca.

### Token opaco

Quando o token nao declara expiracao:

1. observar `Cache-Control`/`Expires` apenas do upstream confiavel;
2. aplicar teto local curto;
3. medir validade real em homologacao;
4. reduzir TTL depois de erros associados a expiracao;
5. aplicar jitter entre canais.

Ponto inicial conservador para testes:

```text
ttl_maximo = 20 segundos
margem = 5 segundos
jitter = 0-2 segundos
refresh_at = 13-15 segundos apos criacao
```

Os valores finais devem vir de medicao.

### Renovacao antecipada

- o primeiro pedido apos `refresh_at` inicia a renovacao;
- outros usam o token anterior apenas se ainda for valido;
- somente uma renovacao por chave pode executar;
- falhas usam backoff exponencial com jitter;
- um circuit breaker impede tempestade contra o XUI.

### Renovacao reativa

| Resposta do upstream | Acao |
| --- | --- |
| `200/206` | servir e registrar sucesso |
| `301/302/307/308` autorizado | seguir internamente, nunca repassar Location |
| `401/403/410` | invalidar e renovar imediatamente |
| `404` | renovar uma vez; depois tratar como expirado/offline |
| `429` | respeitar backoff/Retry-After |
| `500/502/503/504` | stale curto se seguro e circuit breaker |
| host nao autorizado | bloquear e alertar |

Cada pedido admite no maximo dois redirects internos, uma renovacao e uma repeticao do conteudo. Isso evita loops e amplificacao.

## Dois caches separados

### Cache de mapeamento

```text
chave publica -> token/destino interno
```

- somente memoria;
- TTL menor que a validade do token;
- invalidacao imediata em erro;
- nunca exposto em header publico.

### Cache de segmento

```text
chave publica canonica -> bytes MPEG-TS/fMP4
```

- dados em disco e chaves em memoria;
- `cache_lock` por chave;
- segmento imutavel pode continuar valido depois que o token expira;
- manifests nao podem ficar stale tempo suficiente para congelar o player.

Separar caches permite apagar um token sem perder bytes validos ja obtidos.

## Chave canonica e isolamento

A chave precisa distinguir:

- perfil/tenant;
- particao de credencial;
- canal ou asset;
- numero do segmento;
- variante e bitrate;
- formato;
- `Range`, quando aplicavel.

Derivar a particao de credencial sem plaintext:

```text
credential_partition = HMAC-SHA256(edge_secret, user_identifier)
```

A auditoria encontrou 406 caminhos e 406 valores unicos, mas isso e evidencia de uma janela, nao garantia universal do XUI.

## SSRF, DNS rebinding e egress

Seguir redirects dinamicos sem validacao seria uma falha critica. Controles obrigatorios:

1. aceitar somente esquemas autorizados;
2. exigir host em allowlist administrativa;
3. limitar portas;
4. rejeitar credenciais embutidas e fragmentos;
5. resolver todos os IPs antes da conexao;
6. rejeitar loopback, link-local, multicast, metadata cloud e redes privadas nao autorizadas;
7. fixar o IP validado durante a conexao ou controlar egress por firewall;
8. revalidar DNS quando o TTL expirar;
9. limitar tamanho do header e quantidade de redirects;
10. impedir host vindo de query, header ou caminho do cliente.

A defesa ideal combina allowlist no broker, DNS pinning e firewall de saida permitindo somente XUI/LBs.

## Fail-closed e nao vazamento

Em qualquer erro:

- remover `Location`, `Server`, `Via`, `X-Powered-By` e cookies da origem;
- devolver resposta generica;
- nunca incluir host, IP, token, porta ou stack trace;
- nunca registrar URI credenciada;
- gerar correlation ID aleatorio;
- alertar internamente usando hashes.

Destino fora da allowlist deve falhar. Nunca repassar o redirect apenas para manter o stream funcionando.

## Contrato interno sugerido

Pedido ao broker por Unix socket:

```text
GET /v1/resolve/<canonical-key-hash>
X-Internal-Route: identificador-opaco
```

Sucesso:

```text
204 No Content
X-Accel-Redirect: /__cdnmnus_resolved/<opaque-id>
```

`opaque-id` deve ser aleatorio, de vida curta e valido somente em uma location Nginx marcada `internal;`.

Falha:

```text
503 Service Unavailable
Retry-After: valor-curto
```

O cliente nunca recebe o destino real.

## Observabilidade segura

Metricas:

- hit/miss do cache de token;
- renovacoes por resultado e latencia;
- respostas de token expirado;
- redirects bloqueados;
- hit ratio de segmentos;
- fetches reais na origem;
- waiters por singleflight;
- status por upstream ID abstrato;
- stale servido e circuit breaker.

Logs podem conter correlation ID, hash da chave, upstream ID, transicao de estado, status, latencia e numero da tentativa.

Logs nao podem conter usuario, senha, token, `Location`, URI bruta ou identidade real da origem.

## Alta disponibilidade

Em uma edge, o broker guarda estado em memoria e reconstrói mappings sob demanda depois de restart. O cache Nginx em disco pode sobreviver a reload.

Em varias edges:

- cada edge resolve tokens localmente por padrao;
- tokens vinculados a IP ficam isolados;
- allowlists/configuracao sao distribuidas por control plane;
- failover emite novo token para a nova edge;
- metricas agregadas nunca carregam segredos.

## Estado da implementacao no servidor auditado

Implementado e validado:

- broker local em `127.0.0.1:9091`, sem porta publica;
- execucao como `www-data` com configuracao `0640 root:www-data`;
- systemd hardened com exposure score `1.3 OK`;
- allowlist exata de origem e LBs;
- rejeicao de traversal, host desconhecido, metadata/link-local e porta nao autorizada;
- mapeamento em memoria com TTL, singleflight e limpeza periodica;
- backlog de 512 conexoes;
- `X-Accel-Redirect` interno;
- locations de destino e retry marcadas `internal`;
- retry unico por `error_page` em `401/403/404/410/500/502/503/504`;
- manifest inicial e segmentos entregues como `200`, sem `Location` publico;
- nenhum `X-Accel-Redirect` observado pelo cliente;
- renovacao apos 16 segundos com manifest/segmento `200`;
- novo teste integrado com 50, 100, 250 e 500 clientes, todos `200` e sem headers internos.

Ainda depende de trabalho externo ou validacao prolongada:

- ACL no XUI/LB aceitando somente edges;
- egress firewall por IP/CIDR no provedor;
- DNS pinning que elimine completamente a janela entre validacao e conexao;
- testes sustentados e distribuidos durante expiracao/revogacao real;
- segunda edge, failover e SLO comercial.

## Plano de implementacao e evolucao

### Fase 1: laboratorio

1. medir validade real de tokens em canais/LBs diferentes;
2. definir allowlist e redes de egress;
3. criar XUI/LB falsos com tokens curtos;
4. implementar broker, estados e singleflight;
5. integrar uma location interna de homologacao.

### Fase 2: seguranca e recuperacao

1. renovacao antecipada;
2. invalidacao reativa;
3. limite de redirect/retry;
4. anti-SSRF e DNS pinning;
5. fail-closed e respostas genericas;
6. stale curto, backoff e circuit breaker;
7. auditoria de logs e permissoes.

### Fase 3: carga e canary

1. cache frio com 50 clientes e token de cinco segundos;
2. expiracao durante 500 clientes;
3. revogacao inesperada;
4. thundering herd;
5. 100 canais diferentes renovando;
6. canary com rollback por taxa de erro.

### Fase 4: producao

1. release reproduzivel;
2. ACL na origem/LB;
3. egress allowlist;
4. dashboards e alertas;
5. carga externa sustentada;
6. SLO e capacidade comercial documentados.

## Matriz de testes

| Caso | Resultado esperado |
| --- | --- |
| token valido | `200/206`, sem Location publico |
| token expira antes do TTL local | invalidar, renovar uma vez e servir |
| token revogado | renovar uma vez; depois erro generico |
| 500 clientes com cache frio | uma resolucao e um fetch por segmento |
| 500 clientes com cache aquecido | todos HIT dentro do SLO |
| redirect desconhecido | bloqueado e alertado |
| redirect para metadata/localhost | bloqueado |
| loop de redirects | interrompido no limite |
| XUI offline | stale curto ou erro generico |
| LB lento | timeout/circuit breaker sem tempestade |
| restart do broker | reconstrucao sem expor token |
| duas edges | isolamento de tokens ligados a IP |
| logs | nenhum segredo ou URI bruta |
| resposta 4xx/5xx | nenhuma identidade da origem |

## Gates para 10/10

- [ ] nenhum `Location` da origem chega ao publico;
- [ ] token e renovacao sao exclusivamente internos;
- [ ] renovacao antecipada e reativa passam;
- [ ] existe no maximo um resolver simultaneo por chave;
- [ ] retry e redirect possuem limites rigidos;
- [ ] allowlist, DNS pinning e egress firewall estao ativos;
- [ ] origem aceita somente IPs das edges;
- [ ] cache keys isolam tenant, credencial, canal e segmento;
- [ ] 500 clientes atravessam expiracao sem vazamento/tempestade;
- [ ] 100 canais renovam concorrentemente;
- [ ] logs e metricas passaram por auditoria de segredo;
- [ ] broker/Nginx possuem testes, health checks e rollback;
- [ ] carga externa sustentada atende ao SLO;
- [ ] todas as falhas sao fail-closed.

## Conclusao e limite atual

O broker local, o `X-Accel-Redirect` interno, a renovacao por TTL e o retry
reativo existem para o caminho legado e foram testados. Isso não significa que
a transformação pública para `/play/<token>/m3u8` já exista: o renderer ainda
aceita/encaminha manifestos no formato `/<usuario>/<senha>/<id>.m3u8`. A emissão
de token opaco e o transformador de playlist continuam pendentes.

O Nginx permanece no data plane e o broker somente no control plane. Para
completar a meta `10/10`, faltam a transformação de manifesto, chaves de cache
canônicas multi-XUI, ACL/egress de rede, teste externo sustentado, DNS pinning
mais forte, fencing externo e alta disponibilidade real.

## Implementacao efetiva de 28/08/2026

- HTTP publico responde somente `308` para a mesma URI em HTTPS; a aplicacao
  funciona na 443 com TLS 1.2/1.3 e HSTS de um ano.
- URLs reescritas em playlists usam `https://` para o hostname publico.
- HLS e VOD passam pelo broker local. `/movie/` e `/series/` seguem no maximo
  cinco redirects, exclusivamente entre origem e hosts VOD autorizados.
- Relays VOD sao `internal`, ocultam `Location` e repetem a resolucao autorizada
  em erro elegivel. `Range` segue para a requisicao final.
- `/edge-health` valida Nginx + broker/configuracao para DNS/LB multi-edge.
- Metricas sem URI/credencial ficam em `/var/lib/cdnmnus/metrics.prom`.
- Soak: arquivo `/etc/cdnmnus/soak-NOME.url` modo 0600; iniciar com
  `systemctl start cdnmnus-soak@NOME.service`; resultado agregado em
  `/var/lib/cdnmnus/soak-NOME.json`.
