# Receita: LB cerebro, edges musculos e playback adaptativo

Data: 2026-09-03
Projeto: cdnmnus
Objetivo: adicionar roteamento por sessao e troca de edge por erro de reproducao sem quebrar as rotas atuais.

## 1. Resultado esperado

O Load Balancer decide. A edge transporta a midia pesada.

```text
Player
  |-- controle, sessao, token e eventos pequenos --> Load Balancer
  |                                                  cerebro
  |<-- URL assinada para a edge --------------------|
  |
  +---------------- midia HLS/VOD -----------------> Edge
                                                    musculos
```

O LB nao deve inspecionar cada segmento, armazenar catalogos ou transportar o fluxo de video quando o modo direto para edge estiver habilitado.

## 2. Regra de seguranca

O fluxo atual e a referencia de compatibilidade. Nao remover nem alterar inicialmente:

- `/get.php`;
- `/player_api.php`;
- `/live/` e `/hls/`;
- `/movie/` e `/series/`;
- descoberta de tenant por CNAME;
- broker de tokens existente;
- relay VOD por socket Unix;
- cache e includes Nginx ja gerados.

Adicionar o recurso atras da flag:

```text
playback_sessions_v1 = false
```

Com a flag desligada, o comportamento deve ser identico ao atual. Ativar primeiro em um tenant de laboratorio, depois em um tenant de cada vez.

## 3. O que o codigo atual ja fornece

Os pontos reais de integracao sao:

- `panel/cname_gateway.py`: resolve CNAME para o tenant e encaminha API, live e VOD.
- `panel/token_broker.py`: valida caminhos, consulta upstream e evita expor credenciais.
- `panel/vod_relay.py`: aplica politica VOD, valida destino, redirects e `Range`.
- `core/cname_discovery.py`: descoberta e cache de aliases.
- `core/m3u_transform.py`: reescrita de URLs publicas de playlist.
- `core/render_tenants.py`: isolamento de vhosts, upstreams e cache por tenant.
- `core/topology.py`: edges, estados, leases, fencing logico e capacidade.
- `core/capacity_policy.py`: classifica edges como `ready`, `pressured`, `draining` ou `down`.
- `core/db.py`: banco autoritativo, tenants, edges, upstreams, deployments e auditoria.
- `web/app.py`: API administrativa e estado do control plane.
- `orchestrator/worker.py`: reconciliacao de configuracao e deployments.

O que nao existe ainda e deve ser adicionado isoladamente:

- registro de sessao de playback;
- endpoint de eventos do player;
- contador de falhas por sessao;
- decisao de rota e cooldown;
- emissao de URL assinada para edge;
- contrato do aplicativo para trocar a URL;
- testes de failover por sessao.

## 4. Arquitetura de componentes

### 4.1 Load Balancer

Responsabilidades:

- autenticar e identificar o tenant;
- criar a sessao;
- escolher uma edge `ready`;
- emitir token assinado e curto;
- receber eventos de erro e heartbeat;
- decidir troca de edge;
- aplicar limites e circuit breaker;
- registrar somente eventos agregados e sanitizados.

Nao fazer no LB:

- proxy de segmentos quando o modo edge-direct estiver ativo;
- download antecipado de M3U ou VOD;
- escrita no SQLite a cada segmento;
- decisao baseada somente no IP do cliente;
- aceitar `tenant_id` livre enviado pelo cliente.

### 4.2 Edge

Responsabilidades:

- servir playlist, segmentos e VOD;
- validar token e tenant;
- conectar-se ao upstream permitido;
- executar cache sob demanda;
- expor health check local;
- enviar metricas agregadas ao control plane;
- respeitar peso, drenagem e limite de capacidade.

### 4.3 Estado

Usar armazenamento efemero com TTL para sessoes e contadores. Redis e a opcao recomendada para alta concorrencia. PostgreSQL e o destino do estado duravel e auditoria. O SQLite autoritativo atual deve continuar apenas para configuracao enquanto a migracao de control plane nao for aprovada.

Nunca gravar cada segmento, heartbeat ou erro bruto no SQLite.

## 5. Contrato da sessao

Criar uma sessao somente depois que o CNAME ou token ja tiver identificado o tenant. O servidor gera `session_id`; o cliente nao escolhe `tenant_id` nem `edge_id` sem validacao.

### Criacao

Endpoint novo, por exemplo:

```text
POST /api/playback/sessions
```

Requisicao minima:

```json
{
  "channel_id": "canal-123",
  "media_type": "live"
}
```

Resposta:

```json
{
  "session_id": "abc",
  "tenant_id": "xuilab",
  "edge_id": "edge-168",
  "play_url": "https://edge-168.example/...token-assinado...",
  "expires_in": 300,
  "telemetry_url": "/api/playback/sessions/abc/events"
}
```

O tenant da resposta vem do CNAME autenticado, nao de um campo confiado do cliente.

### Campos da sessao

```text
session_id
tenant_id
channel_id normalizado
media_type live ou vod
edge_id atual
edges ja tentadas
created_at
expires_at
switch_count
last_event_at
state active, degraded, switched ou expired
```

TTL sugerido: 15 minutos para live, renovavel por heartbeat. O valor final deve ser medido com o aplicativo real.

## 6. Token de playback

O token deve ser assinado pelo control plane e validado na edge. Claims minimas:

```json
{
  "sid": "abc",
  "tid": "xuilab",
  "cid": "canal-123",
  "eid": "edge-168",
  "scope": "playback",
  "exp": 1730000000,
  "jti": "id-unico"
}
```

Regras:

- assinatura HMAC ou chave assimetrica conforme o broker existente;
- `tid`, `sid`, `cid` e `eid` devem ser validados na edge;
- expiracao curta;
- novo token ao trocar de edge;
- sem username, password ou URL credenciada;
- nao aceitar edge fornecida pelo cliente sem comparacao com a sessao;
- nao registrar o token completo em logs.

## 7. Telemetria do player

O player envia eventos apenas quando houver erro relevante ou heartbeat limitado.

```text
POST /api/playback/sessions/{session_id}/events
```

Payload:

```json
{
  "event_id": "evt-001",
  "tenant_id": "xuilab",
  "channel_id": "canal-123",
  "edge_id": "edge-168",
  "type": "segment_timeout",
  "sequence": 3,
  "observed_at": "2026-09-03T12:00:00Z"
}
```

O servidor deve ignorar ou rejeitar:

- tenant diferente da sessao;
- edge diferente da edge atual sem justificativa;
- sequencia repetida;
- evento fora do TTL;
- payload acima do limite;
- mais eventos que o rate limit.

Nao enviar coordenadas, senha, playlist completa ou IP publico desnecessario.

## 8. Classificacao dos erros

### Erros que podem disparar troca

- `playlist_timeout`;
- `segment_timeout`;
- `connection_refused`;
- HTTP `502`, `503` ou `504` da edge;
- falha de autorizacao causada pela edge;
- falha repetida de `Range` no VOD;
- heartbeat perdido quando a rede do cliente continua alcancavel.

### Erros que nao devem disparar troca automaticamente

- codec incompatível;
- arquivo corrompido na origem;
- internet desligada no cliente;
- aplicativo sem permissao de rede;
- erro isolado sem repeticao;
- canal inexistente na origem.

O tipo do erro deve ser validado pelo servidor e nao apenas aceito como verdade porque veio do aplicativo.

## 9. Algoritmo de troca

Configuracao inicial conservadora:

```text
janela: 60 segundos
limiar: 3 erros classificaveis
maximo de trocas: 3 por sessao
cooldown da edge: 60 segundos
cooldown global por tenant/canal/edge: 120 segundos
```

Ao atingir o limiar:

1. carregar a sessao pelo `session_id`;
2. confirmar tenant, canal e edge atual;
3. descartar edges `failed`, `disabled`, `draining` ou sem capacidade;
4. excluir edges tentadas recentemente;
5. ordenar por saude, capacidade, latencia e peso;
6. reservar a nova edge de forma atomica;
7. emitir novo token vinculado a mesma sessao;
8. devolver `switch_edge` ao player;
9. registrar evento sanitizado;
10. iniciar cooldown da rota anterior.

Resposta:

```json
{
  "action": "switch_edge",
  "session_id": "abc",
  "edge_id": "edge-170",
  "play_url": "https://edge-170.example/...novo-token...",
  "reason": "segment_timeout_threshold",
  "expires_in": 300
}
```

Se nao houver edge elegivel:

```json
{
  "action": "keep_current",
  "retry_after": 10
}
```

Nunca trocar indefinidamente nem alterar todos os clientes por causa de uma sessao.

## 10. Isolamento multi-XUI

Toda chave de estado deve conter, no minimo:

```text
tenant_id + session_id
```

Para circuit breaker e metricas:

```text
tenant_id + channel_id + edge_id
```

Exemplo:

```text
xuilab + sessao-A + canal-10 -> edge-168
xuilab + sessao-B + canal-10 -> edge-170
tvbrasil + sessao-C + canal-10 -> edge-78
```

Uma falha em `xuilab` nao pode alterar sessoes de `tvbrasil`. Testar tambem tokens cruzados, CNAME cruzado, canal cruzado e edge solicitada manualmente.

## 11. Integracao sem quebrar o fluxo atual

Implementar nesta ordem:

1. Criar modulo novo `playback/session_store.py` sem importar no fluxo legado.
2. Criar `playback/route_policy.py` como funcao pura e testavel.
3. Criar `playback/token.py` reutilizando as regras do broker atual.
4. Adicionar endpoints de sessao e eventos atras da feature flag.
5. Adicionar no player suporte para resposta `switch_edge`.
6. Renderizar aliases publicos das edges com TLS valido.
7. Fazer a URL da sessao apontar diretamente para a edge.
8. Manter proxy legado como fallback durante a migracao.
9. Ativar somente um tenant de laboratorio.
10. Aumentar gradualmente depois de observar erros e capacidade.

Nao editar manualmente os includes gerados pelo painel. Alteracoes devem entrar no renderer ou no playbook que gera o arquivo e passar por `nginx -t`.

## 12. Cache nas edges

O cache continua sob demanda: somente conteudo solicitado entra no disco. Nao armazenar listas M3U completas nem catalogos antecipadamente.

Regras:

- cache separado por tenant;
- chave sem credenciais;
- segmentos HLS cacheaveis;
- manifests sensiveis fora do cache;
- `Range` tratado explicitamente;
- erros nunca persistidos como sucesso;
- `proxy_cache_lock` para evitar avalanche na origem;
- limite baseado em espaco livre e reserva do sistema;
- limpeza LRU ou equivalente sob pressao;
- metricas de hit, miss, bytes e eviction.

O Nginx atual possui cache por tenant em partes do renderer, enquanto o include efetivo tambem possui um cache HLS compartilhado de 2 GB. A nova sessao nao pode alterar essa politica sem teste comparativo. Primeiro medir; depois ajustar o limite por edge.

## 13. Health e capacidade

A selecao de edge deve aceitar somente estado operacional comprovado:

- estado topologico `ready`;
- health check recente;
- release e digest aprovados;
- TLS valido;
- capacidade nao expirada;
- sem circuito aberto;
- sem NIC errors;
- p95 e HTTP 5xx abaixo do SLO.

Usar `core/capacity_policy.py` para a classificacao, mas nao tratar capacidade declarada como medida. O controlador deve drenar novas sessoes quando a edge estiver pressionada e nao interromper sessoes saudaveis sem necessidade.

## 14. Rate limit e custo do LB

O LB recebe controle, nao video. Para evitar sobrecarga:

- limitar criacao de sessoes por tenant e cliente;
- limitar eventos por sessao;
- deduplicar `event_id`;
- heartbeat entre 30 e 60 segundos;
- agregar metricas de edge em janelas de 5 a 10 segundos;
- usar Redis ou memoria com TTL para contadores;
- persistir somente mudancas de estado e amostras agregadas;
- aplicar backpressure e responder `429` para excesso.

Exemplo: 10.000 clientes com heartbeat de 30 segundos geram cerca de 333 eventos por segundo, muito menos que os bytes dos fluxos de video.

## 15. Testes obrigatorios

### Unitarios

- escolha deterministica de edge;
- edge `ready` preferida;
- edge `draining` excluida;
- threshold de 3 erros;
- janela expirando;
- maximo de trocas;
- cooldown;
- deduplicacao de evento;
- token expirado;
- tenant cruzado rejeitado.

### Integracao

- criar sessao para cada XUI;
- playlist recebida pela edge direta;
- segmento servido pela edge;
- erro enviado ao LB;
- troca de edge;
- novo token aceito na nova edge;
- token antigo rejeitado quando aplicavel;
- VOD com `Range` preservado;
- origem com erro nao vira falso sucesso.

### Isolamento

Executar a matriz:

```text
3 tenants x 3 edges x live x VOD x rota normal/troca
```

Confirmar que nenhuma resposta contem credencial, tenant errado, upstream errado ou edge nao autorizada.

### Carga

Comecar com 100 sessoes, depois 300, 1.000 e somente entao aumentar. Medir:

- CPU e memoria do LB;
- requests de sessao por segundo;
- eventos por segundo;
- latencia p95/p99 dos endpoints de controle;
- banda do LB e das edges;
- cache hit/miss;
- HTTP 4xx/5xx;
- trocas por mil sessoes;
- conexoes abertas;
- disco e evictions.

Nao usar carga de health check como carga representativa.

## 16. Observabilidade

Metricas minimas, sem segredos:

```text
playback_sessions_created_total{tenant}
playback_events_total{tenant,type}
playback_edge_switch_total{tenant,from,to,reason}
playback_switch_failed_total{tenant,reason}
playback_session_active{tenant}
edge_playback_errors_total{tenant,edge,type}
edge_cache_hit_ratio{tenant,edge}
```

Logs devem conter `session_id` truncado ou hash, tenant, edge, tipo e timestamp. Nunca registrar token, senha, query completa ou URL upstream.

## 17. Rollback

O rollback deve ser simples:

1. desligar `playback_sessions_v1` para o tenant;
2. deixar novas sessoes usarem a rota legada;
3. manter sessoes antigas ate expirarem ou solicitar encerramento seguro;
4. remover apenas o include novo apos `nginx -t`;
5. restaurar release/configuracao anterior se necessario;
6. executar smoke e testes de tenant;
7. registrar causa e evidencia.

Nao apagar sessoes, tokens ou dados de auditoria durante rollback. Nao alterar DNS para esconder falha.

## 18. Criterios de liberacao

Liberar um tenant somente quando todos forem verdadeiros:

```text
[ ] flag desligada continua identica ao comportamento anterior
[ ] sessao identifica tenant pelo CNAME/token confiavel
[ ] midia pesada chega diretamente a edge
[ ] token e aceito somente no tenant/edge corretos
[ ] player consegue trocar URL
[ ] threshold e cooldown foram testados
[ ] nenhuma troca cruzou tenant
[ ] cache e Range passaram na matriz
[ ] LB permaneceu abaixo do SLO
[ ] todas as edges candidatas tem TLS e health reais
[ ] rollback executado com sucesso
```

## 19. Definicao de pronto

O recurso esta pronto para producao quando:

- o fluxo legado permanece aprovado;
- a sessao adaptativa funciona em todas as edges homologadas;
- o LB nao transporta midia no modo edge-direct;
- o aplicativo envia eventos reais e troca a URL;
- erros locais nao causam rotacao indevida;
- multi-XUI esta isolado;
- carga sustentada permanece abaixo do SLO;
- existe rollback documentado e testado;
- capacidade e cache sao medidos no ambiente real.

Nao declarar o recurso pronto apenas porque o endpoint de sessao responde `200`. A prova precisa incluir reproducao real, falha real, troca real e retorno controlado.
