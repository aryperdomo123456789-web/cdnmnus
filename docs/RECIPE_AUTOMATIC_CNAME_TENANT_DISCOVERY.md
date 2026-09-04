# Receita: CNAME automatico por tenant e playlist publica

**Estado:** implementado no release gerenciado atual. Esta receita descreve o
fluxo que deve ser instalado e validado; ela nao substitui os gates de TLS,
health e laboratorio.

## 0. Receita curta para uma instalação nova

Use sempre esta ordem. O alias externo nao precisa ser gravado em
`tenant_hosts`.

1. Instale uma release que contenha `core/cname_discovery.py`,
   `panel/cname_gateway.py` e a role `cdn_tenants`.
2. Cadastre o XUI com um `canonical_host` unico, seus upstreams autorizados e
   o certificado desse hostname. Nunca cadastre o IP do XUI como hostname
   publico.
3. Confirme `automatic_cname_discovery=true` nas edges e execute `nginx -t`,
   reload controlado e `GET /edge-health` em cada edge.
4. No provedor DNS do cliente, crie somente:
   `alias.cliente.tld CNAME canonical.phpd77.com` em modo DNS-only.
5. Para HTTPS, emita/distribua um certificado cujo SAN contenha o alias. Sem
   SAN, aceite apenas HTTP de laboratorio ou termine TLS no provedor externo.
6. Teste API, M3U, live, filme, série e `Range` pelo alias. Só considere
   operacional depois de todos os checks passarem.

Exemplos atuais:

```text
on.acxxl.com       CNAME xuilab.phpd77.com
novo.exemplo.tld   CNAME turbotv.phpd77.com
```

O sistema conhece `xuilab.phpd77.com` e `turbotv.phpd77.com` como tenants
canônicos, mas não precisa conhecer `on.acxxl.com` ou `novo.exemplo.tld`.

## 1. Resultado esperado

O operador cadastra o XUI canonical e seus upstreams. O alias externo pode nao
existir em `tenant_hosts`, desde que seu CNAME termine em um canonical habilitado:

```text
on.acxxl.com CNAME xuilab.phpd77.com
cnxt.vr766.com CNAME tvbrasil.phpd77.com
```

Ao pedir `/get.php`, `/player_api.php`, `/live/`, `/hls/`, `/movie/` ou
`/series/`, o sistema deve selecionar o tenant pelo canonical terminal e
reescrever todos os links publicos para esse canonical:

```text
on.acxxl.com   -> xuilab.phpd77.com/...
cnxt.vr766.com -> tvbrasil.phpd77.com/...
```

O cliente nunca ve IP/DNS do XUI, LB ou VOD, token, `Location`, `Set-Cookie`,
`Server` ou socket interno.

## 2. Estado real encontrado no codigo

Ja existem:

- `core/db.py`: `tenant_hosts`, `add_cname()` e upstreams por tenant;
- `core/render_tenants.py`: vhosts, brokers, relay VOD e rotas por tenant;
- `core/cname_discovery.py`: resolve a cadeia CNAME e seleciona o tenant;
- `panel/cname_gateway.py`: gateway por socket, rotas permitidas e `421`;
- `panel/multi_tenant_broker.py`: broker separado por socket/tenant;
- `panel/vod_relay.py`: redirects VOD, portas, DNS publico e Range;
- `core/dns_reconciler.py`: reconciliacao Cloudflare;
- `lab-player/scripts/test_playback_flow.py`: M3U, live, movie, series e Range.

As receitas `CNAME_DNS_ONLY_AND_LAB_RECIPE.md`,
`VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md` e
`TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md` cobrem alias conhecido, relay VOD e
protecao da origem.

O fluxo efetivo agora e:

```text
Host desconhecido -> cadeia CNAME -> canonical -> tenant_id -> socket isolado
```

O fallback global legado nao deve ser usado para novos tenants. O Nginx nunca
usa `$host` em `proxy_pass`; a autoridade recebida serve apenas para a
descoberta validada.

## 3. Politica de confianca

O `Host` recebido e sempre nao confiavel. Aceitar o alias somente quando:

1. for hostname valido, sem IP, porta ou esquema;
2. a cadeia tiver no maximo quatro CNAMEs;
3. o terminal for exatamente um `canonical_host` habilitado;
4. esse canonical pertencer a um unico tenant;
5. nao houver loop, erro DNS ou resposta vazia;
6. o canonical resolver para enderecos publicos permitidos;
7. o resultado tiver TTL efetivo limitado, recomendado 15..300 segundos;
8. mudanca de terminal invalidar a decisao anterior;
9. a selecao usar `canonical_host -> tenant_id`, nunca indice da lista;
10. toda falha retornar `421`, sem fallback para outro tenant.

Recusar CNAME para IP, destino privado, canonical inexistente/desabilitado,
cadeia circular, rota administrativa, prefixo `__cdnmnus_` publico ou metodo
nao permitido. Nunca usar query string, cookie, referer ou header do cliente
como destino ou identidade de tenant.

## 4. Arquitetura efetiva

Nao implementar:

```nginx
proxy_pass http://$host;
proxy_pass http://$arg_url;
proxy_pass http://$request_uri;
```

Isso cria proxy aberto, permite SSRF e pode misturar tenants.

O gateway interno dedicado ja e instalado pela role:

```text
cliente -> Nginx TLS/limites -> Unix socket cname-gateway
        -> CNAME validado -> socket broker/relay do tenant
        -> XUI, LB ou VOD autorizado
```

O gateway deve receber o `Host` somente como entrada para descoberta, nunca
como upstream. Deve escolher socket pelo `tenant_id` obtido do DNS, ignorar
`X-CDN-Tenant` externo, impor metodos/caminhos, timeouts, limites e logs
sanitizados.

Para baixo volume, uma alternativa e gerar um `map` Nginx assinado e publicar
uma release. O alias so fica ativo apos `nginx -t`, reload, health e teste real.

## 5. Contrato do resolvedor

`core/cname_discovery.py` implementa uma funcao equivalente a:

```python
discover_alias(host, tenant_index, resolver, now) -> DiscoveryResult
```

O resultado interno deve conter `alias_host`, `canonical_host`, `tenant_id`,
`observed_chain`, `expires_at` e `decision_id`. Implementar:

1. normalizacao IDNA, minusculas e ponto final DNS;
2. limite de 253 bytes;
3. conjunto de hosts visitados para detectar loop;
4. limite de quatro saltos e TTL 15..300 s;
5. indice reconstruido do banco a cada release;
6. invalidacao quando tenant, canonical ou cadeia mudarem;
7. nenhum segredo, token ou URL completa em logs.

O cache opcional deve ser separado de `tenant_hosts`:

```text
cname_discovery_cache(alias_host, canonical_host, tenant_id,
                      observed_chain_json, expires_at, state, last_error)
```

## 6. Transformacao da M3U

Para `/get.php`:

1. manter usuario/senha somente em memoria;
2. consultar o XUI do tenant descoberto;
3. exigir `#EXTM3U` e limite de tamanho;
4. analisar todas as URLs de midia;
5. aceitar somente `origin`, `lb`, `vod` e `canonical` do mesmo snapshot;
6. substituir a autoridade pelo canonical do tenant;
7. preservar apenas path/query necessarios ao broker/relay;
8. remover headers sensiveis e nunca devolver redirect.

Entrada:

```text
http://38.92.25.125/live/user/pass/100.ts
http://38.190.176.174/movie/user/pass/200.mp4
http://servicedovod.lat/series/user/pass/300.mp4
```

Saida:

```text
http://xuilab.phpd77.com/live/user/pass/100.ts
http://xuilab.phpd77.com/movie/user/pass/200.mp4
http://xuilab.phpd77.com/series/user/pass/300.mp4
```

URL absoluta fora do snapshot deve rejeitar a playlist. `sub_filter` pode ser
defesa secundaria, mas nao a unica validacao. Token opaco e etapa posterior,
separada da descoberta DNS, com TTL e testes de compatibilidade.

## 7. Rotas e seguranca

| Rota | Componente | Resultado |
| --- | --- | --- |
| `/get.php` | gateway + XUI | M3U com canonical correto |
| `/player_api.php` | gateway + XUI | JSON sanitizado |
| `/live/`, `/hls/` | broker do tenant | 200 e bytes |
| `/movie/`, `/series/` | relay VOD do tenant | 200/206 |
| admin/desconhecida | Nginx/gateway | 421 |

Remover sempre `Location`, `Server`, `Via`, `X-Powered-By`, `X-Accel-Redirect`,
`Set-Cookie`, `X-CDN-*` e `X-Upstream-*`. Manter `Range` e `If-Range`.

O relay VOD continua usando seeds por tenant, validacao SSRF, portas,
redirects, DNS pinning e limite de saltos. O alias nao muda essa politica.

## 8. HTTPS e Cloudflare

DNS-only nao fornece certificado para dominio de outra zona. Para HTTPS, o
certificado precisa conter o alias no SAN, ou a terminacao deve ser Cloudflare
proxied. Sem isso, aprovar apenas HTTP de laboratorio.

Em zona Cloudflare controlada, o menu pode validar conflito A/AAAA/CNAME,
criar CNAME DNS-only, emitir TLS e iniciar release. Em zona externa, como
`acxxl.com`, o proprietario cria o CNAME; o sistema nao deve fingir controlar
essa zona.

## 9. Evolucao segura

### Fase A: recurso existente

Nao recrie os componentes acima. A suite `tests/cname_discovery_test.py` deve
passar antes de qualquer rollout. A descoberta permanece protegida por flag:

```text
automatic_cname_discovery = true
```

Em canário, a flag pode ficar `false` até a release passar pelos gates. Em
produção, `false` é somente rollback: ele faz aliases desconhecidos voltarem a
`421`; não use fallback global para “fazer funcionar”.

### Fase B: evolucao de token

O formato legado reescrito para o canonical protege a origem, mas ainda pode
conter credenciais no caminho interno da URL. A transformacao para
`/play/<token>/m3u8` e uma mudanca separada, descrita em
`RECIPE_CERTIFICATES_OPAQUE_PLAY_TOKENS_MULTI_XUI_CACHE.md`; nao a marque como
implantada enquanto o manifesto real nao passar pelo gate de ausencia de
usuario, senha, IP e host upstream.

### Fase C: menu

Adicionar `Validar CNAME`, `Mostrar tenant`, `Publicar CNAME`, `Emitir TLS`,
`Testar playback` e `Revogar cache`. Publicacao Cloudflare exige zona
controlada e tenant canonical explicito.

## 10. Laboratorio e aceite

Para alias nao cadastrado em `tenant_hosts`:

1. criar CNAME de laboratorio para `xuilab.phpd77.com`;
2. executar `lab-player/scripts/run_xuilab_test.sh` com env root-only 0600;
3. exigir M3U valida com links somente em `xuilab.phpd77.com`;
4. exigir live 200, movie/series 200 e Range 206 com `Content-Range`;
5. testar `/admin`, `/phpmyadmin`, `/internal` e rota aleatoria com 421;
6. verificar ausencia de origem, LB, VOD, `Location`, cookie e `Server`;
7. repetir para `tvbrasil.phpd77.com`;
8. trocar o CNAME entre tenants e confirmar expiracao do cache anterior.

Antes da ativacao: suite unittest, `bash -n`, `git diff --check`, render,
`nginx -t`, canario, reload controlado, health, isolamento e relatorio
`result=pass`. Em rollback, desligar a flag, usar playbook para release
anterior, validar Nginx e deixar alias desconhecido em `421`.

## 11. Regra operacional final

Enquanto todos os gates nao forem comprovados:

```text
alias desconhecido -> 421
alias cadastrado e validado -> fluxo protegido
```

O recurso nao deve ser habilitado removendo o `421` do vhost default. A prova
DNS e a selecao por tenant sao obrigatorias. A transformacao para token opaco
tem gate proprio e nao pode ser inferida apenas porque o CNAME funcionou.
