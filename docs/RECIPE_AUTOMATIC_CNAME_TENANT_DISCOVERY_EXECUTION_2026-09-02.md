# Receita de execução: CNAME automático por tenant

**Objetivo:** fazer um alias DNS-only funcionar sem cadastrar o alias em
`tenant_hosts`, escolhendo o tenant pelo canonical terminal do CNAME, sem
misturar XUIs e sem expor origem, LB, VOD ou credenciais.

**Estado real em 2026-09-02:** `cnxt.vr766.com` passou o laboratório completo
com `tvbrasil.phpd77.com`. `on.acxxl.com` ainda retorna `404` porque a release
ativa usa um único `external_alias_tenant_id=xui-tvbrasil`; a credencial do
tenant `xuilab` funciona quando o `Host` canônico é usado. Não trocar esse ID:
isso faria um alias passar às custas de quebrar o outro.

## 0. Modo simples: faça exatamente nesta ordem

Esta seção é a versão operacional curta. Não pule etapas e não avance quando o
resultado esperado não aparecer.

### Antes de começar

Você precisa de:

- acesso ao control plane e à edge de laboratório;
- uma conta de laboratório para cada XUI;
- dois CNAMEs DNS-only apontando para os canonicals corretos;
- certificado TLS contendo cada alias, se o teste for HTTPS;
- janela de mudança e uma release anterior disponível para rollback.

Nunca coloque usuário, senha, token ou M3U no Git, no SQLite ou em mensagem de
log. Use somente arquivo local `root:root` com modo `0600`:

```bash
install -o root -g root -m 600 /dev/null /etc/cdnmnus/lab-player/xuilab.env
editor /etc/cdnmnus/lab-player/xuilab.env
```

O arquivo deve conter somente variáveis locais, por exemplo:

```bash
PLAYER_USERNAME='conta-de-laboratorio'
PLAYER_PASSWORD='senha-de-laboratorio'
PLAYER_BASE_DIRECT='http://xui-de-laboratorio'
PLAYER_BASE_CNAME='http://alias-de-laboratorio'
PLAYER_BASE_CDN='http://canonical-do-tenant'
```

### Passo 1: conferir o DNS

Execute:

```bash
dig +short CNAME on.acxxl.com
dig +short CNAME cnxt.vr766.com
```

O resultado precisa ser, respectivamente, `xuilab.phpd77.com.` e
`tvbrasil.phpd77.com.`. Se aparecer IP direto, CNAME diferente ou nenhum
resultado, pare. Não tente consertar isso no Nginx.

### Passo 2: conferir os tenants

No control plane, confirme que existem dois tenants habilitados, com canonical
único e origem própria. A relação precisa ser:

```text
xuilab.phpd77.com   -> xuilab
tvbrasil.phpd77.com -> xui-tvbrasil
```

Não use `external_alias_tenant_id` para representar os dois. Esse campo é
legado e representa somente um tenant.

### Passo 3: testar o código sem tocar na edge

No repositório:

```bash
cd /opt/cdnmnus
python3 -m unittest discover -s tests -p '*test.py' -v
bash -n lab-player/scripts/sync_playlist.sh lab-player/scripts/run_xuilab_test.sh
python3 -m compileall -q core panel lab-player tests
git diff --check
```

Se qualquer comando falhar, pare. O sistema não está pronto para release.

### Passo 4: executar o laboratório

Use o script oficial, que baixa a playlist, fixa amostras e valida o caminho
do player:

```bash
cd /opt/cdnmnus
PLAYER_CREDENTIALS_FILE=/etc/cdnmnus/lab-player/xuilab.env \
./lab-player/scripts/run_xuilab_test.sh
```

O relatório precisa terminar com `result=ok`. Ele deve mostrar 3 live, 3
filmes e 3 séries. Live precisa retornar `200`; filme e série precisam
retornar `200` e `206` com `Content-Range`.

### Passo 5: validar segurança

Só avance se todos forem verdadeiros:

- links M3U usam o canonical do tenant, nunca IP ou origem;
- `on` nunca recebe conteúdo do tenant `tvbrasil`;
- `cnxt` nunca recebe conteúdo do tenant `xuilab`;
- `/admin`, `/phpmyadmin`, `/internal` e rota aleatória retornam `421`;
- não existe `Location`, `Set-Cookie`, `Server`, `Via` ou header interno;
- um CNAME alterado para outro tenant invalida a decisão anterior;
- CNAME com loop, IP privado ou canonical desabilitado retorna `421`.

### Passo 6: publicar primeiro em uma edge

Ative a flag somente na release candidata:

```text
automatic_cname_discovery=true
```

A ordem obrigatória é:

```text
gerar release -> verificar digest -> instalar gateway -> nginx -t
-> ativar uma edge -> health -> laboratório -> observar -> promover as demais
```

Se o gateway não estiver instalado, a flag deve permanecer `false`. Nunca
remova o `421` do vhost default para “ver se funciona”.

### Passo 7: rollback simples

Se houver `404`, `5xx`, mistura de tenants, vazamento, `Range` quebrado ou
memória crescendo:

```text
desligar a flag -> restaurar release anterior -> nginx -t -> health -> laboratório
```

Depois do rollback, alias desconhecido deve continuar em `421`. Não altere o
tenant global para tentar recuperar apenas um alias.

## 0.1. Como saber se terminou de verdade

A tarefa não termina quando `/get.php` retorna `200`. Ela termina somente quando
`on` e `cnxt` passam a matriz completa em todas as edges aprovadas, cada um
isolado no tenant correto, com M3U sanitizada, live, VOD, `Range`, TLS, health,
logs e rollback comprovados.

## 1. Resultado que deve ser obtido

```text
on.acxxl.com  CNAME  xuilab.phpd77.com   -> tenant xuilab
cnxt.vr766.com CNAME tvbrasil.phpd77.com -> tenant xui-tvbrasil
```

O alias não precisa existir em `tenant_hosts`. O sistema deve aceitar somente
rotas de aplicação e deve produzir:

```text
Host recebido -> CNAME observado -> canonical habilitado -> tenant_id
             -> socket/origem fechado daquele tenant -> resposta sanitizada
```

O cliente deve receber links do canonical do tenant. Falha de DNS, canonical
desconhecido, tenant desabilitado, loop, TTL inválido ou rota não permitida
deve retornar `421` sem fallback para outro tenant.

## 2. Evidência já coletada

| Endpoint | Resultado observado |
| --- | --- |
| `xuilab.phpd77.com` com a conta de laboratório | `200`, M3U válida |
| `on.acxxl.com` com a mesma conta | `404` |
| `on.acxxl.com` com `Host: xuilab.phpd77.com` | `200`, M3U válida |
| `tvbrasil.phpd77.com` | `200`, M3U válida |
| `cnxt.vr766.com` | `200`, M3U válida |

DNS observado:

```text
on.acxxl.com   -> xuilab.phpd77.com -> cdn.phpd77.com -> edges públicas
cnxt.vr766.com -> tvbrasil.phpd77.com -> cdn.phpd77.com -> edges públicas
```

O laboratório de `cnxt` selecionou 9 itens e passou:

- 3 live com `HTTP 200` e bytes;
- 3 filmes com `HTTP 200` e `Range HTTP 206`;
- 3 séries com `HTTP 200` e `Range HTTP 206`;
- `Content-Range` válido;
- playlist sem o IP da origem e com links somente em `tvbrasil.phpd77.com`.

## 3. O que não deve ser feito

Não corrigir o problema com:

```text
external_alias_tenant_id=xuilab
```

Essa configuração é um fallback global e só pode representar um tenant. Com
dois XUIs, ela causa exatamente o erro observado: `on` chega ao XUI de
`tvbrasil` e recebe `404`.

Também não usar:

```nginx
proxy_pass http://$host;
proxy_pass http://$arg_url;
proxy_pass http://$request_uri;
```

Esses padrões criam proxy aberto, permitem SSRF e eliminam o isolamento entre
tenants. O `Host` é apenas uma entrada para descoberta; nunca é um upstream.

## 4. Implementação obrigatória

### 4.1 Snapshot autoritativo

Manter `xui_tenants` como fonte de tenants habilitados e gerar no snapshot uma
relação inequívoca:

```text
canonical_host -> tenant_id, origin, lb, vod, public_hosts, config_version
```

Arquivos envolvidos:

- `core/db.py`
- `core/render_tenants.py`
- `core/deploy.py`
- `core/cname_discovery.py`

O índice deve ser reconstruído em cada release. Canonical duplicado, tenant
sem origem única ou tenant desabilitado bloqueia a release.

### 4.2 Gateway interno

Criar `panel/cname_gateway.py` e a unit
`panel/cdnmnus-cname-gateway.service`. O serviço deve escutar somente em
`/run/cdnmnus/cname-gateway.sock` e executar esta sequência:

1. aceitar `GET` e `HEAD` apenas;
2. normalizar o `Host` sem aceitar esquema, porta, IP ou caracteres inválidos;
3. consultar a cadeia CNAME pelo resolver do sistema;
4. limitar a cadeia a 4 saltos e TTL efetivo entre 15 e 300 segundos;
5. rejeitar loops, destinos privados, respostas mistas e prefixo
   `__cdnmnus_`;
6. procurar o canonical terminal no snapshot, nunca em uma lista por posição;
7. escolher o upstream pelo `tenant_id` encontrado;
8. aceitar somente `/get.php`, `/player_api.php`, `/live/`, `/hls/`,
   `/movie/`, `/series/` e manifestos autorizados;
9. ignorar `X-CDN-Tenant` e qualquer identidade enviada pelo cliente;
10. encaminhar internamente `Host` e SNI do upstream definido no snapshot;
11. remover `Location`, `Set-Cookie`, `Server`, `Via`, `X-Powered-By`,
    `X-Accel-Redirect`, `X-CDN-*` e `X-Upstream-*`;
12. retornar `421` em qualquer falha de decisão.

O gateway não deve logar URL completa, query string, usuário, senha, token,
cookie, IP de origem ou redirect. O log mínimo é `decision_id`, alias
normalizado, tenant, rota, status e bytes.

### 4.3 M3U real

Para `/get.php`:

- manter credenciais somente em memória;
- exigir `#EXTM3U`;
- aplicar limite de corpo configurável de pelo menos 128 MiB, ou implementar
  transformação streaming equivalente;
- aceitar apenas hosts `canonical`, `origin`, `lb` e `vod` do mesmo tenant;
- reescrever a autoridade de todas as URLs absolutas para o canonical;
- preservar path e query necessários ao broker/relay;
- rejeitar URL absoluta de host externo;
- nunca devolver redirect do XUI.

O módulo existente `core/m3u_transform.py` é a base de regras, mas seu limite
default de 1 MiB não atende a playlist real observada de aproximadamente
81 MiB. Antes do rollout real, adicionar teste de tamanho próximo do limite e
teste de transformação sem manter uma segunda cópia desnecessária em memória.

### 4.4 Nginx

Manter os vhosts canônicos existentes sem alteração de comportamento. O vhost
default deve continuar bloqueando o tráfego até o gateway estar aprovado.
Depois, substituir somente as locations de aplicação do fallback global por:

```nginx
location = /get.php { proxy_pass http://cname_gateway; }
location = /player_api.php { proxy_pass http://cname_gateway; }
location ~ ^/(?:live|hls|movie|series)/ { proxy_pass http://cname_gateway; }
location ~ ^/[^/]+/[^/]+/[0-9]+\.m3u8$ { proxy_pass http://cname_gateway; }
location / { return 421; }
```

Essas locations devem existir em HTTP e HTTPS, com `proxy_pass_request_body`
controlado, limites, timeouts, headers públicos sanitizados e sem expor o
socket. A entrada do gateway deve ser incluída somente após `nginx -t`.

O `external_alias_tenant_id` pode permanecer como compatibilidade durante a
migração, mas não pode mais decidir requisições quando
`automatic_cname_discovery=true`. O default continua `421` se o gateway estiver
indisponível.

## 5. Autorização correta

DNS não autoriza usuário. Ele apenas identifica o tenant candidato. A decisão
completa precisa combinar:

- cadeia CNAME observada e terminal exato;
- canonical presente no snapshot de tenant habilitado;
- resolução pública coerente com as edges autorizadas;
- rota e método permitidos;
- credencial validada pelo próprio XUI do tenant;
- isolamento de socket/upstream por `tenant_id`;
- ausência de mudança de cadeia durante a validade do cache.

Não usar User-Agent, Referer, cookie, IP do cliente ou query string como
substituto de autorização. Esses sinais podem ser falsificados e não provam
que o alias pertence ao tenant.

## 6. Ordem segura de rollout

1. Confirmar `xuilab` e `xui-tvbrasil` habilitados, cada um com uma origem,
   broker e relay VOD próprios.
2. Gerar snapshot com `automatic_cname_discovery=false` e verificar digest.
3. Implementar e testar o gateway somente em socket temporário, sem alterar
   Nginx ativo.
4. Executar testes unitários de DNS, isolamento, rotas, headers, M3U de 81 MiB
   e falhas fechadas.
5. Adicionar o gateway à release com a flag ainda desligada e executar
   `nginx -t`, health e auditoria.
6. Ativar primeiro em uma edge canária, preservando a release anterior e o
   rollback atômico.
7. Testar `on` com a conta do `xuilab` e `cnxt` com a conta do `tvbrasil`.
8. Testar simultaneamente que cada alias recebe somente o canonical correto.
9. Promover as demais edges somente após os relatórios serem verdes.
10. Remover o fallback global apenas em uma release posterior, depois de um
    período de observação; manter `421` para qualquer alias sem descoberta.

Não alterar manualmente o banco de produção nem fazer reload fora da release.
As últimas releases falharam em gates de deploy; isso deve ser corrigido e
retestado antes de qualquer ativação.

## 7. Matriz de aceite

Para cada alias, o laboratório deve executar:

```bash
python3 -m unittest discover -s tests -p '*test.py' -v
bash -n lab-player/scripts/sync_playlist.sh lab-player/scripts/run_xuilab_test.sh
git diff --check
```

Depois, com credenciais root-only `0600`, executar uma rodada por tenant:

```bash
PLAYER_BASE_CNAME='http://on.acxxl.com' \
PLAYER_BASE_CDN='http://xuilab.phpd77.com' \
PLAYER_USERNAME='[conta de laboratório xuilab]' \
PLAYER_PASSWORD='[segredo fora do repositório]' \
python3 lab-player/scripts/test_playback_flow.py --cname --refresh-samples

PLAYER_BASE_CNAME='http://cnxt.vr766.com' \
PLAYER_BASE_CDN='http://tvbrasil.phpd77.com' \
PLAYER_USERNAME='[conta de laboratório tvbrasil]' \
PLAYER_PASSWORD='[segredo fora do repositório]' \
python3 lab-player/scripts/test_playback_flow.py --cname --refresh-samples
```

O aceite exige, para cada alias:

- handshake `active` no tenant correto;
- exatamente 9 itens selecionados: 3 live, 3 filme e 3 série;
- live `200` com conteúdo não vazio;
- filme e série `200`, `206` e `Content-Range`;
- M3U válida e todas as autoridades públicas no canonical correto;
- ausência de IP, origem, LB, VOD privado, token e hostname interno;
- ausência de `Location`, `Set-Cookie`, `Server`, `Via` e headers internos;
- `/admin`, `/phpmyadmin`, `/internal` e rota desconhecida em `421`;
- troca simulada do CNAME invalidando a decisão anterior;
- alias com loop, IP privado ou canonical desabilitado em `421`.

## 8. Rollback

Em qualquer `5xx`, `404` no tenant correto, vazamento, `Range` quebrado,
decisão ambígua ou crescimento de memória:

1. desligar `automatic_cname_discovery` na próxima release;
2. restaurar a release anterior pelo playbook de ativação/rollback;
3. validar `nginx -t`, health, `cnxt` e os vhosts canônicos;
4. deixar aliases não cadastrados em `421`;
5. preservar o relatório sanitizado e o `decision_id` para diagnóstico.

Rollback nunca deve escolher automaticamente outro tenant para tentar recuperar
uma requisição.

## 9. Critério de conclusão

O recurso só é considerado funcionando de fato quando o gateway estiver
integrado ao vhost default, a flag estiver ativa na release canária, `on` e
`cnxt` passarem a matriz acima, e a mesma decisão for reproduzida em todas as
edges aprovadas. A existência de `core/cname_discovery.py` isoladamente ou um
`HTTP 200` na playlist não fecha esse gate.
