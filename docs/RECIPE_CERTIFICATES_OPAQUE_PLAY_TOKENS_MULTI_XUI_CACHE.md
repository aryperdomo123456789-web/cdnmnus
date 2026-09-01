# Receita de produção: certificados, `/play/<token>/m3u8`, multi-XUI e cache

**Data-base:** 2026-09-01
**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
**Objetivo:** permitir novos subdomínios sem erro de TLS e transformar URLs de
playlist que contêm usuário/senha em URLs públicas com token opaco, mantendo
vários XUIs isolados e a abertura dos conteúdos rápida.

Este documento é uma receita de implementação. Ele descreve o estado real do
código, a mudança necessária e a ordem segura para colocá-la em produção.
Não pule etapas. Se um teste falhar, mantenha o fluxo antigo ativo e faça
rollback da release candidata.

## 1. Resultado final

O cliente verá somente nomes públicos e tokens opacos:

```text
cliente
  -> https://tv.exemplo.com/get.php?...       (playlist)
  -> https://tv.exemplo.com/play/AbC.../m3u8   (playlist transformada)
  -> https://tv.exemplo.com/hls/...ts          (segmentos)
```

O cliente nunca verá:

- IP ou hostname do XUI;
- usuário ou senha;
- token emitido pelo LB/XUI;
- `Location` de um redirect interno;
- rota `__cdnmnus_*` ou endereço de storage.

O caminho interno será:

```text
HTTPS público
  -> Nginx da edge
  -> broker do tenant/XUI
  -> XUI selecionado
  -> LB do XUI, se houver redirect
  -> segmento no Nginx
  -> cache local da edge
```

O LB frontal não deve cachear mídia nem decidir XUI. Ele apenas encaminha a
requisição para uma edge saudável. A eleição do LB frontal continua sujeita ao
lease, fencing e promoção descritos em
[CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md](CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md).

## 2. O que existe e o que falta

### Já existe

- `core/render_tenants.py` gera vhosts separados por `tenant_id`.
- `panel/multi_tenant_broker.py` valida tenant e hostname e usa socket por
  tenant.
- `panel/token_broker.py` resolve redirects internos, usa singleflight e
  expiração curta.
- O Nginx usa `X-Accel-Redirect` e locations `internal` para esconder os
  destinos.
- HLS/live usa `proxy_cache_lock`, cache curto e retry para respostas de token
  expirado.
- VOD usa relay separado, `Range` e limites de redirect.
- As fontes de um XUI são modeladas como `tenant_upstreams` e podem ser
  cadastradas no painel.

### Ainda não é o requisito pedido

O fluxo atual aceita uma URL legada parecida com:

```text
/usuario/senha/123.m3u8
```

e a encaminha internamente. Isso protege a origem, mas não transforma o texto
da playlist em:

```text
/play/<token>/m3u8
```

Além disso, o certificado configurado no vhost é associado ao hostname
canônico. Um certificado de `cdn.exemplo.com` não cobre automaticamente
`tv.exemplo.com`, `cliente2.exemplo.com` ou um subdomínio criado amanhã.

Portanto, esta receita exige duas implementações independentes:

1. emissão/renovação/distribuição de certificado com SAN ou wildcard;
2. transformador de playlist e endpoint interno para token opaco.

Não resolver esses itens com `sub_filter`: `sub_filter` troca texto, mas não
cria uma autorização reversível, não protege credenciais e não controla
expiração, revogação ou isolamento entre XUIs.

### Diagnóstico obrigatório de HTTP 421

O health deve usar o `health_host` do tenant, que por padrão é o seu
`canonical_host`. Não use `cdn.phpd77.com` por conveniência se ele não estiver
em `tenant_hosts`: o vhost default pode rejeitá-lo deliberadamente com `421`.

Fluxo read-only:

```bash
curl --resolve tvbrasil.phpd77.com:443:143.14.168.168 \
  https://tvbrasil.phpd77.com/edge-health
curl --resolve tvbrasil.phpd77.com:443:143.14.168.170 \
  https://tvbrasil.phpd77.com/edge-health
```

Interpretar os resultados nesta ordem:

1. falha de conexão: investigar rota/firewall, sem alterar regra durante o
   diagnóstico;
2. erro TLS/SNI: comparar SAN e hostname, sem desabilitar verificação;
3. HTTP `421`: o handshake chegou ao vhost, mas o `Host` não pertence ao
   `server_name`; corrigir `health_host`/certificado pelo renderer e pipeline;
4. HTTP `200`: health aprovado para aquele tenant e edge.

No estado real de 2026-09-01, `cdn.phpd77.com` retornou `421` porque o vhost
default contém rejeição explícita para esse host. O certificado apresentado
pelas edges tem somente `DNS:cdn.phpd77.com`, enquanto o tenant real é
`tvbrasil.phpd77.com` e está com `tls_status=pending`. A ação correta é emitir
um certificado que cubra o canonical real, distribuí-lo junto com a chave,
executar `nginx -t` e fazer reload pelo playbook; nunca remover a proteção
`421` do vhost default.

Para auditar a divergência de release sem instalar nada:

```bash
scripts/cdnmnus-reconcile-managed-release \
  --node 45.140.192.237
```

Código `0` significa correspondência integral; código `2` significa que a
release deve ser aprovada formalmente ou revertida pelo runbook. O comando não
altera registry, symlink, pacote ou serviço.

## 3. Regra de nomes e certificados

Escolha uma das duas estratégias por zona DNS.

### Estratégia A: wildcard DNS-01

Use quando todos os subdomínios estão sob a mesma zona e o provedor permite
desafio DNS:

```text
*.exemplo.com
exemplo.com                 # inclua o apex se ele for usado
```

O wildcard cobre `tv.exemplo.com` e `cliente2.exemplo.com`, mas não cobre
`sub.tv.exemplo.com`. Para esse terceiro nível, emita outro SAN/wildcard.

### Estratégia B: SAN por hostname

Use quando os nomes são conhecidos e o wildcard não é desejado:

```text
DNS:tv.exemplo.com
DNS:cliente2.exemplo.com
DNS:api.exemplo.com
```

Toda criação de tenant deve gerar também um pedido de certificado. O tenant
só pode ficar `enabled` depois de o certificado conter todos os seus hosts.

### Regras obrigatórias

1. O hostname público precisa apontar para a entrada pública correta antes da
   validação ACME.
2. O segredo DNS-01 fica somente no host de controle, com permissão `0600`.
3. A chave privada fica somente nas edges que terminam TLS, com permissão
   `0600`.
4. O certificado e a chave devem ser distribuídos como uma unidade, nunca
   atualizar apenas um dos dois.
5. Antes do reload, execute `nginx -t`.
6. Faça reload, não restart, para preservar conexões existentes.
7. Renove antes do vencimento e mantenha o certificado anterior até o novo
   passar em teste.
8. Registre somente hostname, fingerprint, vencimento e resultado. Nunca
   registre chave privada, token DNS ou conteúdo do certificado em log público.

### Fluxo de renovação

```text
1. descobrir todos os hosts ativos no banco;
2. gerar SAN/wildcard;
3. emitir por DNS-01;
4. validar cadeia, nomes e vencimento;
5. instalar fullchain + key em diretório temporário;
6. mover os dois atomicamente para o diretório ativo;
7. executar nginx -t;
8. executar systemctl reload nginx;
9. testar cada hostname com SNI;
10. marcar o certificado como válido no estado operacional.
```

Comandos de verificação, sem expor segredos:

```bash
openssl s_client -connect tv.exemplo.com:443 -servername tv.exemplo.com \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
nginx -t
systemctl reload nginx
curl --fail --silent --show-error https://tv.exemplo.com/edge-health
```

O automatizador deve usar os caminhos e permissões já definidos pelo
provisionamento, e não editar manualmente o vhost gerado por
`core/render_tenants.py`.

## 4. Contrato do token opaco

O token público não é usuário, senha, ID de XUI nem token do upstream. Ele é
um identificador aleatório sem significado para o cliente.

### Conteúdo interno mínimo

O broker deve guardar, em memória ou armazenamento protegido:

```text
token_hash
tenant_id
xui_profile_id
credential_partition
channel_id
variant
created_at
refresh_at
expires_at
revoked
edge_id, se houver afinidade
```

Nunca guarde usuário/senha em logs. Se for necessário particionar credenciais,
use HMAC com segredo da edge:

```text
credential_partition = HMAC-SHA256(edge_secret, user_identifier)
```

O token deve ser criado com um gerador criptograficamente seguro, por exemplo
`secrets.token_urlsafe(32)`. No banco ou log, persista somente o hash do token.

### Endpoints públicos

O contrato público recomendado é:

```text
GET /play/<token>/m3u8
```

O endpoint deve aceitar somente `GET` e `HEAD`, rejeitar token vazio ou
excessivamente longo e retornar erro genérico quando o token for inválido,
expirado, revogado ou pertencer a outro tenant.

O endpoint não deve redirecionar o cliente para o XUI. O broker responde a uma
rota interna, por exemplo:

```text
X-Accel-Redirect: /__cdnmnus_play/<opaque-route>/index.m3u8
```

Essa location precisa conter `internal;`. O cliente que tentar chamá-la
diretamente recebe erro.

### Expiração

Comece com valores medidos em laboratório, não com valores presumidos:

```text
validade do token público: 5 a 15 minutos
refresh interno: antes da expiração, com margem de segurança
tentativas: uma renovação e uma repetição
redirects internos: no máximo 2 para HLS/live
```

Se o token do LB expirar antes do token público, o broker deve invalidar o
mapeamento, emitir outro token interno e repetir uma única vez. No segundo
erro, falha fechado sem `Location`.

## 5. Como transformar a playlist

A transformação deve ocorrer no caminho `get.php` ou no endpoint que devolve o
manifesto. Ela é uma transformação de conteúdo controlada pelo tenant, não
uma substituição textual global.

### Entrada

O XUI pode devolver, por exemplo:

```text
https://xui-interno.example/usuario/senha/123.m3u8
```

O Nginx deve retirar `Accept-Encoding` para receber texto transformável e
encaminhar a playlist ao transformador do tenant. O transformador deve:

1. validar o `tenant_id` obtido do vhost;
2. selecionar o `xui_profile_id` pela configuração interna;
3. localizar linhas de URI e atributos HLS que contenham URI;
4. resolver a URL legada no broker;
5. emitir um token opaco para cada stream autorizado;
6. preservar tags, comentários, bitrate, resolução e ordem da playlist;
7. converter URI absoluta e relativa para hostname público;
8. remover usuário, senha, host, IP e query secreta do corpo final;
9. devolver `Content-Type` de playlist e `Cache-Control: no-store`.

### Saída mínima

```text
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=...
https://tv.exemplo.com/play/<token-variante-1>/m3u8
```

Para uma playlist de mídia, o token pode apontar para o canal/variante e as
linhas de segmentos devem ser relativas ao endpoint público. O cliente deve
buscar segmentos pelo hostname público, nunca pelo XUI.

### Não fazer

- não usar regex para substituir qualquer ocorrência de `usuario` ou `senha`;
- não colocar credencial em query string pública;
- não cachear o corpo original do `get.php`;
- não retornar redirect HTTP do XUI;
- não aceitar XUI escolhido por header, query ou caminho enviado pelo cliente;
- não usar o mesmo token entre tenants;
- não compartilhar token vinculado a uma edge com outra edge.

## 6. Multi-XUI sem mistura

Cada XUI é um perfil interno de um tenant. O hostname público identifica o
tenant; o perfil XUI é escolhido somente pela configuração administrativa.

Exemplo de modelo lógico:

```text
tenant tvbrasil
  profile xui-a -> origem A, LBs A1/A2
  profile xui-b -> origem B, LBs B1/B2
```

Toda chave de estado deve incluir:

```text
tenant_id | xui_profile_id | credential_partition |
channel_id | variant | formato
```

O cache de um perfil nunca pode responder uma requisição de outro perfil. Se
dois perfis tiverem o mesmo canal, eles ainda são partições diferentes até que
a equipe prove que o objeto é idêntico e autorizado para ambos.

### Ordem de seleção

```text
1. obter tenant pelo hostname SNI/Host já validado;
2. obter perfil XUI pela regra administrativa do tenant;
3. validar que o perfil está enabled e saudável;
4. escolher origem/LB daquele perfil;
5. criar token com tenant + perfil + canal;
6. encaminhar internamente;
7. se falhar, tentar somente outro upstream do mesmo perfil;
8. nunca saltar silenciosamente para o XUI de outro tenant.
```

O `panel/multi_tenant_broker.py` já fornece a fronteira de tenant. A evolução
para vários perfis deve manter essa fronteira e adicionar `xui_profile_id` à
configuração, ao mapping, à chave de cache e às métricas sanitizadas.

## 7. Cache para abertura rápida

Há dois caches diferentes. Eles não devem ser juntados.

### Cache A: autorização/mapeamento

```text
tenant + perfil + credencial + canal + variante -> rota interna/token upstream
```

Características:

- memória local da edge;
- TTL curto e menor que a validade do token upstream;
- singleflight por chave;
- refresh antecipado;
- invalidação em `401`, `403`, `404` ou `410`;
- nunca persistir URL completa ou senha;
- nunca enviar esse valor em header público.

### Cache B: bytes de mídia

```text
tenant + perfil + canal + variante + segmento -> bytes
```

Características:

- disco local Nginx;
- `proxy_cache_lock on`;
- keepalive com edge e upstream;
- segmentos imutáveis podem ter TTL maior;
- manifestos live têm TTL muito curto ou `no-store`;
- requisições com `Range` não devem contaminar cache de objeto completo;
- respostas de erro não devem ser armazenadas como conteúdo válido.

O cache de bytes deve ser baseado na identidade canônica do conteúdo, não no
token bruto do LB. Caso o token altere o caminho do upstream, remova-o da chave
por uma variável interna segura ou use um identificador de segmento derivado
pelo broker. Se isso ainda não estiver implementado, mantenha a chave com o
token interno e aceite menor hit ratio até a migração correta; nunca remova o
token por substituição textual insegura.

### Valores iniciais para laboratório

```text
manifest live: não armazenar ou no máximo 1-2 segundos
segmentos live: 6 segundos, conforme o intervalo real do canal
mapping do broker: 10-15 segundos
VOD sem Range: 30 segundos, somente após validar isolamento
stale live: curto e somente em falha de upstream
cache_lock_timeout: menor que o timeout de abertura do player
```

O valor final vem de medição. Cache não deve servir segmento live antigo por
tempo suficiente para congelar o player.

### Para abrir instantaneamente

1. mantenha conexão persistente edge -> XUI/LB;
2. resolva o primeiro token com singleflight;
3. não espere todos os segmentos antes de devolver o manifesto;
4. entregue o primeiro segmento assim que estiver disponível;
5. aqueça apenas canais explicitamente autorizados e em baixo volume;
6. não faça prewarm de todos os canais, pois isso cria tempestade no XUI;
7. meça `time_to_first_byte`, `time_to_first_segment`, hit ratio e fetches reais;
8. use `stale` somente para erro transitório e dentro da janela definida.

## 8. Alterações de código por arquivo

Implementar em uma branch de release, nesta ordem:

1. `core/db.py`: adicionar modelo de perfil XUI, hosts públicos e política de
   certificado, com migração reversível.
2. `core/render_tenants.py`: gerar vhost por tenant, aliases e locations
   públicas `/play/`; manter a rota legada durante a compatibilidade.
3. `panel/multi_tenant_broker.py`: incluir `xui_profile_id`, emitir/validar
   token opaco e devolver somente `X-Accel-Redirect` interno.
4. `panel/token_broker.py`: manter compatibilidade HLS, adicionar refresh
   reativo e separar mapping de bytes.
5. `panel/panel.py`: gerar configuração sem `sub_filter` para credenciais;
   usar transformador de playlist e `Cache-Control: no-store` em manifesto.
6. `ansible/roles/cdn_tenants`: instalar certificado, units, sockets,
   diretórios e permissões; executar `nginx -t` antes do reload.
7. `tests/`: testar transformação, expiração, isolamento de XUI, cache lock,
   ausência de segredos e fallback legado.

Não alterar simultaneamente LB frontal, DNS público e transformação de
playlist. São mudanças separadas para que cada rollback seja simples.

## 9. Testes obrigatórios

### Certificado

Para cada hostname:

```bash
curl --fail --silent --show-error --resolve \
  tv.exemplo.com:443:EDGE_IP https://tv.exemplo.com/edge-health
```

Aceite somente se o handshake validar nome, cadeia e data.

### Segurança da playlist

Baixe uma playlist autorizada e verifique:

```bash
curl --fail --silent https://tv.exemplo.com/get.php?... > /tmp/playlist.m3u8
! rg -n "usuario|senha|IP_DO_XUI|hostname-do-xui|Location|__cdnmnus" /tmp/playlist.m3u8
rg -n '/play/[A-Za-z0-9_-]+/m3u8' /tmp/playlist.m3u8
```

O teste real deve usar valores sanitizados ou variáveis protegidas. Não grave
credenciais no repositório, relatório ou shell history.

### Isolamento multi-XUI

- token do tenant A em hostname do tenant B: `403` ou `404` genérico;
- perfil A indisponível: não usar perfil de outro tenant;
- mesma URI em perfis A e B: caches diferentes;
- duas edges: token vinculado à edge A não é aceito pela edge B, salvo se a
  política explicitamente suportar isso;
- nenhum teste encontra `Location`, usuário, senha, IP ou token upstream.

### Expiração e concorrência

- token válido: `200`;
- token upstream expirado: uma renovação e `200`;
- segundo erro: erro genérico, sem loop;
- 500 clientes em cache frio: um resolver por chave;
- 500 clientes em cache quente: todos recebem cache HIT dentro do SLO;
- XUI indisponível: stale curto ou erro controlado, sem tempestade;
- restart do broker: reconstrução sem vazamento.

## 10. Rollout seguro

### Fase 0: congelar o que funciona

1. gerar manifesto da configuração atual;
2. guardar hash do vhost, broker e certificados sem incluir segredos;
3. registrar playlists e segmentos de teste sanitizados;
4. confirmar que o fluxo legado continua reproduzindo.

### Fase 1: certificado

1. emitir SAN/wildcard em laboratório;
2. instalar em uma edge canário;
3. testar todos os hosts com SNI;
4. distribuir a mesma release às demais edges;
5. somente então habilitar novos subdomínios.

### Fase 2: token opaco em canário

1. habilitar `/play/` somente para um tenant/perfil;
2. manter URLs legadas funcionando;
3. comparar playlist antiga e nova sem comparar credenciais em texto;
4. testar reprodução HLS, troca de variante, refresh e seek VOD;
5. observar latência, cache HIT/MISS, `401/403/404/410` e erros do player;
6. fazer rollback se qualquer fluxo antigo piorar.

### Fase 3: expandir por tenant/XUI

1. habilitar um XUI por vez;
2. publicar a mesma configuração em todas as edges;
3. validar isolamento e cache;
4. ampliar gradualmente;
5. só depois alterar o LB/DNS de produção.

### Fase 4: produção

Publicar somente quando todos os itens abaixo forem verdadeiros:

- certificado cobre cada hostname publicado;
- renovação automática foi testada;
- playlist nova contém somente `/play/<token>/m3u8`;
- playlist legada continua em compatibilidade ou foi oficialmente desativada;
- nenhum segredo aparece no corpo, header, redirect, log ou métrica;
- cada tenant/XUI possui cache e mapping isolados;
- 500 clientes atravessam expiração sem thundering herd;
- `nginx -t`, health, rollback e soak passaram;
- lease/fencing do LB estão ativos antes de qualquer promoção.

## 11. Diagnóstico simples

### Erro de certificado em novo subdomínio

Verifique, nesta ordem:

1. o DNS aponta para a entrada correta;
2. o hostname está no SAN ou coberto pelo wildcard;
3. a edge recebeu fullchain e chave correspondentes;
4. `nginx -t` passou;
5. o reload ocorreu;
6. o cliente usa SNI correto.

### Playlist ainda mostra usuário/senha

Não aumente `sub_filter` nem edite o vhost. Verifique:

1. o hostname está associado ao tenant correto;
2. o endpoint passa pelo transformador, não pelo proxy legado;
3. o manifesto está `no-store`;
4. o broker conseguiu criar mapping;
5. o perfil XUI está habilitado;
6. a release distribuída é a mesma em todas as edges.

### Conteúdo abre lentamente

Meça separadamente:

```text
DNS/TLS -> manifesto -> resolução do token -> primeiro segmento -> playback
```

Se o atraso estiver no token, ajuste singleflight/TTL. Se estiver no primeiro
segmento, verifique upstream, `cache_lock`, keepalive e capacidade. Não aumente
TTL de manifesto live como primeira reação.

### Conteúdo de XUI errado

Interrompa o rollout. Isso indica chave de cache ou seleção de perfil
incorreta. Invalide somente o namespace afetado, corrija a chave incluindo
tenant/perfil/credencial/canal/variante e repita o canário.

## 12. Checklist final

- [ ] SAN ou wildcard cobre todos os subdomínios publicados.
- [ ] Renovação DNS-01 e distribuição atômica foram testadas.
- [ ] Todo reload é precedido por `nginx -t`.
- [ ] `/play/<token>/m3u8` é emitido pelo transformador, não por `sub_filter`.
- [ ] Token público é opaco, curto e isolado por tenant/perfil.
- [ ] Usuário/senha nunca aparecem no corpo público.
- [ ] `Location` upstream nunca chega ao cliente.
- [ ] Mapping e cache de bytes são caches separados.
- [ ] Manifestos live não ficam stale por tempo perigoso.
- [ ] Cache inclui tenant, XUI, credencial, canal e variante.
- [ ] `Range` não contamina cache de objeto completo.
- [ ] Singleflight limita consultas concorrentes ao XUI.
- [ ] Retry tem limite de uma renovação e uma repetição.
- [ ] Todas as edges carregam a mesma release e allowlist.
- [ ] HLS, VOD, refresh, seek e troca de variante passaram.
- [ ] Logs e métricas foram auditados para ausência de segredos.
- [ ] Rollback foi executado em laboratório.
- [ ] LB frontal só é promovido com lease e fencing ativos.

## Referências do repositório

- [TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md](TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md)
- [VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md](VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md)
- [PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
- [PRODUCTION_SECURITY_AND_CAPACITY.md](PRODUCTION_SECURITY_AND_CAPACITY.md)
- [CLOUDFLARE_API_AUTOMATION_RECIPE.md](CLOUDFLARE_API_AUTOMATION_RECIPE.md)
- [CNAME_DNS_ONLY_AND_LAB_RECIPE.md](CNAME_DNS_ONLY_AND_LAB_RECIPE.md)
