# Modos de resolução VOD/LB no menu SSH

**Data-base:** 2026-09-01
**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
**Escopo:** especificação de implementação para o menu SSH e o control plane

## 1. Objetivo

Permitir que o operador escolha, por tenant/XUI, como o CDNMNUS deve resolver
redirects de VOD e de load balancers do XUI:

- **Modo estrito:** destinos VOD/LB são cadastrados manualmente e somente esses
  destinos podem ser usados.
- **Modo descoberta controlada:** o cadastro inicial exige somente a origem
  XUI e o hostname público; o sistema aprende destinos observados em redirects
  válidos, com TTL curto, validação SSRF, isolamento por tenant e auditoria.

O segundo modo não significa confiar cegamente no XUI. Um redirect do XUI é
apenas uma entrada candidata. A edge deve validá-lo antes de abrir conexão e
deve bloqueá-lo se não cumprir a política de segurança.

O resultado público dos dois modos é o mesmo:

```text
app -> CNAME público -> edge -> relay/broker -> XUI -> destino VOD/LB
                                              (redirect interno)
```

O app nunca deve receber o redirect interno nem qualquer informação do XUI,
VOD, storage ou LB.

## 2. Estado atual do código

### 2.1 Componentes existentes

- [core/db.py](../core/db.py) possui `tenant_upstreams` com `kind` `origin`,
  `lb` e `vod`.
- [core/deploy.py](../core/deploy.py) separa os upstreams por tenant e rejeita
  seleção implícita quando existem múltiplos tenants.
- [core/render_tenants.py](../core/render_tenants.py) produz snapshots com
  `vod_hosts` e `vod_policy.seeds`.
- [panel/vod_relay.py](../panel/vod_relay.py) inicia na origem do tenant,
  segue redirects limitados, fixa o IP resolvido, preserva SNI/Host e suporta
  VOD com Range.
- [panel/token_broker.py](../panel/token_broker.py) resolve redirects de
  manifestos e possui tratamento separado para VOD legado.
- [panel/multi_tenant_broker.py](../panel/multi_tenant_broker.py) valida o
  tenant e o hostname público antes de resolver destinos internos.
- [ansible/roles/node_menu/files/node_menu.py](../ansible/roles/node_menu/files/node_menu.py)
  delega operações autoritativas ao control plane por SSH estrito. O menu não
  deve criar uma segunda fonte de verdade local.
- [cli/mago_cdn.py](../cli/mago_cdn.py) já possui gerenciamento de fontes VOD
  manuais no control plane.

### 2.2 O que já funciona

O modo estrito já pode representar múltiplas fontes por tenant:

```text
tenant xui-tvbrasil
  origin: xui.example:80
  vod:   vod-a.example:80
  vod:   vod-b.example:443
  lb:    lb-a.example:80
  lb:    lb-b.example:80
```

O relay pode seguir qualquer seed VOD compatível com scheme/porta e a cadeia
posterior dentro dos limites definidos. O Nginx usa um socket Unix do tenant,
nunca um `proxy_pass` construído com valor vindo do cliente.

### 2.3 O que ainda não existe

Ainda não existe uma política persistente que permita ao operador alternar,
por tenant, entre `strict` e `controlled_discovery`. Também não existe um
registro separado para destinos aprendidos com TTL, origem da observação,
último uso e motivo de bloqueio.

Não implementar descoberta simplesmente removendo `vod_hosts` ou aceitando
qualquer `Location`. Isso eliminaria o fail-closed e transformaria a edge em
um proxy SSRF.

### 2.4 Divergências que o implementador deve conhecer

Estas divergências são intencionais no estado atual e não devem ser tratadas
como se já estivessem resolvidas:

- o menu já administra fontes manuais por `kind=vod` e `kind=lb`, mas ainda não
  possui a tela de política `strict`/`controlled_discovery`;
- o relay VOD já entende seeds HTTP/HTTPS e pinning, mas o caminho legado
  `query_vod()` de [panel/token_broker.py](../panel/token_broker.py) usa
  `HTTPConnection`/porta 80 e precisa ser atualizado ou retirado do caminho de
  VOD antes de compartilhar o novo contrato;
- o snapshot atual usa `schema_version` 1/2 e não contém `mode`, TTL ou tabela
  de destinos aprendidos;
- `external_alias_tenant_id` seleciona um único tenant para o fallback externo;
  múltiplos XUIs devem usar hosts públicos registrados individualmente e nunca
  depender de seleção pelo índice da lista;
- o health operacional precisou ser endurecido para validar SAN/SNI real; não
  considerar `validate_certs: false` como prova de produção;
- a descoberta controlada não deve ser habilitada por migração automática. A
  migração inicial de todos os tenants deve criar política `strict`.

## 3. Contrato dos dois modos

### 3.1 Modo estrito

#### Cadastro

O operador cadastra:

1. tenant/XUI;
2. origem XUI e porta;
3. zero ou mais fontes `kind=vod`;
4. zero ou mais fontes `kind=lb`;
5. hostname(s) público(s) do tenant.

Cada upstream pertence a exatamente um tenant. O menu deve mostrar o tenant
na mesma tela de adicionar, editar ou apagar a fonte para evitar cadastro no
XUI errado.

#### Resolução

- primeiro salto: origem XUI;
- primeiro redirect VOD/LB: precisa corresponder a uma seed cadastrada;
- saltos seguintes: somente schemes HTTP/HTTPS, portas autorizadas, DNS
  público e limite de redirects;
- destino fora da política: bloqueio fail-closed;
- nenhuma descoberta é persistida.

#### Uso recomendado

É o modo padrão para produção, tenants sensíveis, XUIs de terceiros e quando
o operador conhece os DNS VOD/LB. É o único modo permitido enquanto a
descoberta controlada não passar pelos testes de abuso e pelo soak.

### 3.2 Modo descoberta controlada

#### Cadastro mínimo

O operador informa apenas:

```text
tenant:       xui-tvbrasil
origem XUI:   38.46.223.77:80
hostname:     tvbrasil.phpd77.com
modo:         controlled_discovery
```

O sistema não deve pedir, guardar ou exibir usuário/senha do player nessa tela.
Credenciais de laboratório pertencem somente ao ensaio do player e não à
política de upstream.

#### Descoberta

Quando o relay recebe `/movie/` ou `/series/`:

1. valida a URI pública e rejeita URL absoluta, traversal e método proibido;
2. conecta somente à origem XUI configurada para aquele tenant;
3. envia ao XUI a URI original, sem entregar ao cliente o resultado interno;
4. lê `Location` somente no processo interno;
5. parseia scheme, hostname, porta, caminho e query sem aceitar userinfo ou
   fragmento;
6. valida o primeiro destino contra a política de descoberta;
7. resolve DNS e rejeita loopback, RFC1918, link-local, multicast, metadata,
   endereço não global, hostname vazio ou resposta DNS mista;
8. permite somente portas `80` e `443`, salvo exceção explícita da política;
9. fixa a conexão no IP validado e mantém o hostname como Host/SNI;
10. grava somente a autorização sanitizada do host, scheme, porta, tenant,
    expiração, origem da observação e hash do redirect;
11. segue a cadeia até o limite de redirects;
12. retorna mídia ao cliente sem `Location` do upstream.

O registro aprendido nunca deve conter URL completa, caminho assinado, query,
credencial, token, cookie ou conteúdo de playlist.

#### Limites obrigatórios

Valores iniciais recomendados:

```text
max_redirects             = 5
discovery_ttl_seconds     = 900
max_discovered_hosts      = 16 por tenant
max_observations_per_min  = 60 por tenant
allowed_ports             = 80,443
require_public_dns        = true
require_http_https        = true
```

O TTL é uma autorização temporária, não uma fonte permanente de confiança.
Após expirar, o destino precisa ser observado e validado novamente. Hosts
bloqueados não devem ser automaticamente promovidos a permitidos por repetição.

#### Limite de confiança

Mesmo com validação SSRF, um XUI comprometido poderia redirecionar para um
host público arbitrário. Por isso, a política deve ter um dos seguintes
controles antes de produção geral:

- allowlist de sufixos/domínios do provedor VOD/LB;
- assinatura ou registro de origem fornecido pelo provedor;
- aprovação manual do primeiro host observado;
- modo descoberta limitado a laboratório/homologação.

Sem uma prova de propriedade do destino, `controlled_discovery` reduz o
cadastro manual, mas não oferece a mesma garantia do modo estrito.

## 4. Modelo de dados proposto

Não reutilizar `tenant_upstreams` para misturar destinos manuais e aprendidos.
Adicionar uma política por tenant e uma tabela de observações:

```sql
CREATE TABLE tenant_resolution_policies (
    tenant_id TEXT PRIMARY KEY REFERENCES xui_tenants(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('strict','controlled_discovery')),
    discovery_ttl_seconds INTEGER NOT NULL DEFAULT 900,
    max_redirects INTEGER NOT NULL DEFAULT 5,
    allowed_ports_json TEXT NOT NULL DEFAULT '[80,443]',
    max_discovered_hosts INTEGER NOT NULL DEFAULT 16,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tenant_discovered_targets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('vod','lb')),
    scheme TEXT NOT NULL CHECK(scheme IN ('http','https')),
    host TEXT NOT NULL,
    port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('xui_redirect','manual_approval')),
    redirect_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','active','expired','blocked')),
    UNIQUE(tenant_id, target_kind, scheme, host, port)
);
```

Regras de persistência:

- `host` deve ser normalizado e validado antes do insert;
- nunca armazenar URL completa;
- nunca armazenar IP como substituto do hostname de destino;
- nunca armazenar `Location` bruto no evento;
- `redirect_hash` deve ser calculado de uma representação sanitizada;
- eventos devem registrar apenas tenant, host sanitizado, porta, resultado,
  motivo e timestamps;
- operações de alteração devem usar transação imediata e evento auditável;
- snapshots de broker devem ser imutáveis por release;
- a exclusão de uma política deve deixar o tenant em `strict` fail-closed, não
  aberto.

Para compatibilidade, snapshots antigos sem política devem ser interpretados
como `strict`, usando somente `vod_hosts`/`vod_policy.seeds` já existentes.

## 5. Fluxo do menu SSH

O menu local deve apenas abrir o menu autoritativo do control plane, como já
faz em [node_menu.py](../ansible/roles/node_menu/files/node_menu.py). Não
adicionar banco, cache ou credencial no nó edge.

### 5.1 Nova entrada

Adicionar em `mago-cdn`:

```text
Tenants/XUI
  -> Política de resolução VOD/LB
      -> Listar políticas
      -> Criar/alterar política
      -> Ver destinos descobertos
      -> Aprovar candidato
      -> Bloquear destino
      -> Expirar cache de tenant
      -> Rollback da política
```

### 5.2 Alterar política

O menu deve exibir:

```text
Tenant: xui-tvbrasil
Origem: 38.46.223.77:80
Modo atual: strict
Seeds VOD manuais: 2
Seeds LB manuais: 2
Destinos descobertos ativos: 0
```

Ao escolher `controlled_discovery`, exigir confirmação explícita:

```text
ATENÇÃO: o XUI poderá propor destinos públicos temporários.
O sistema bloqueará redes privadas, portas não autorizadas, URLs com userinfo,
traversal, cookies e redirects fora da política. Ainda assim, um XUI
comprometido poderá propor um host público arbitrário.

Digite ATIVAR-DESCOBERTA-XUI para continuar:
```

A confirmação deve ser validada no control plane, não no edge. A troca deve:

1. carregar tenant existente;
2. verificar uma única origem;
3. validar limites da política;
4. gravar a política em transação;
5. incrementar a geração de configuração;
6. invalidar cache de resolução daquele tenant;
7. criar deployment pendente;
8. não alterar DNS automaticamente;
9. mostrar release/config digest e exigir implantação separada.

### 5.3 Destinos descobertos

O menu deve mostrar somente dados sanitizados:

```text
Tenant | Tipo | Scheme | Host | Porta | Estado | Expira em | Fonte
xui-tvbrasil | vod | https | vod-a.example | 443 | active | 14m | xui_redirect
```

Nunca mostrar no menu a URL completa do redirect. Ações permitidas:

- aprovar candidato, se o modo exigir aprovação;
- bloquear host/scheme/porta;
- expirar todos os destinos do tenant;
- voltar para `strict`;
- exportar relatório sem query, path credenciado ou token.

## 6. Contrato de resolução no relay/broker

### 6.1 Função comum

Extrair uma função de política compartilhada para evitar que
`token_broker.py`, `multi_tenant_broker.py` e `vod_relay.py` adotem regras
diferentes. A função deve receber:

```text
tenant_id
request_kind: live|vod|manifest
current_target
redirect_hop
policy_snapshot
```

E retornar apenas:

```text
allow/deny
normalized_target
reason_code
```

O `reason_code` deve ser sanitizado, por exemplo:

```text
private_address
invalid_scheme
invalid_port
cross_tenant_target
expired_discovery
redirect_limit
userinfo_present
invalid_path
```

### 6.2 VOD

O relay deve continuar aceitando publicamente somente `/movie/` e `/series/`.
O destino inicial permanece a origem do tenant. A decisão do primeiro redirect
é:

```text
strict:
    target in manual_vod_seeds

controlled_discovery:
    target in manual_vod_seeds
    ou target em discovered_targets ativo
    ou target passa pelo fluxo de candidato/aprovação configurado
```

Depois do primeiro destino autorizado, cada hop deve continuar limitado por
scheme, porta, DNS público, redirect máximo e ausência de userinfo/fragment.

### 6.3 Live, manifesto e LB

Não reutilizar automaticamente a política VOD para live. Live e manifestos
possuem caminhos e comportamento próprios no broker. O destino de LB deve ser
classificado pelo contexto da resolução do XUI e armazenado como `target_kind`
`lb`, nunca inferido pelo índice global de uma lista.

O hostname público recebido do cliente não pode escolher tenant, LB ou origem.
O broker deve continuar exigindo os headers internos assinados/fechados:

```text
X-CDN-Tenant
X-CDN-Public-Host
X-Broker-Action
X-Original-URI
```

Esses headers são produzidos pelo Nginx interno e validados pelo broker; não
devem ser aceitos como autoridade quando enviados diretamente pelo cliente.

## 7. Não vazamento

### 7.1 Resposta pública

O relay, broker e Nginx devem remover ou substituir:

- `Location`;
- `Set-Cookie`;
- `Server`;
- `Via`;
- `X-Powered-By`;
- `X-Accel-Redirect`;
- `X-Forwarded-For` e `X-Real-IP` quando carregarem origem interna;
- URL de origem, VOD ou storage em corpo de playlist.

### 7.2 Logs e auditoria

Logs podem registrar:

```text
tenant_id, route_kind, host_hash, status, reason_code, hop_count,
latency_ms, bytes, range_status, policy_mode
```

Logs não podem registrar:

```text
username, password, token, cookie, query completa, URL completa,
Location bruto, IP privado, caminho assinado ou conteúdo de playlist
```

### 7.3 Falha segura

Se a política estiver ausente, inválida, expirada ou inconsistente:

- VOD deve retornar erro controlado, nunca fallback para destino arbitrário;
- live/manifesto deve manter o fluxo atual, desde que sua política própria
  esteja válida;
- rota desconhecida deve continuar `421`;
- endpoint administrativo deve continuar `421` ou exigir a proteção existente;
- não usar `-k`, não desligar validação TLS e não remover `internal`.

### 7.4 Mapa de implementação arquivo-a-arquivo

Execute nesta ordem. Cada etapa deve terminar com testes antes de iniciar a
seguinte.

| Ordem | Arquivo | Mudança obrigatória |
| --- | --- | --- |
| 1 | `core/db.py` | migration transacional, CRUD de política, destinos aprendidos, TTL e eventos sanitizados |
| 2 | `core/render_tenants.py` | incluir política e destinos ativos no snapshot imutável; default compatível `strict` |
| 3 | `core/deploy.py` | carregar política por tenant, incluir digest/snapshot e impedir alias ambíguo |
| 4 | `panel/policy.py` novo | parser, normalização, DNS público, ports, TTL, reason codes e função comum de autorização |
| 5 | `panel/vod_relay.py` | consultar a política comum antes de cada conexão e aprender somente após redirect válido |
| 6 | `panel/token_broker.py` | usar a mesma política para LB/manifesto; corrigir o caminho legado HTTP-only ou removê-lo do VOD |
| 7 | `panel/multi_tenant_broker.py` | passar `tenant_id`, tipo da rota e snapshot à política; rejeitar headers internos enviados pelo cliente |
| 8 | `cli/mago_cdn.py` | menu autoritativo de política, confirmação forte e operações de aprovação/bloqueio/expiração |
| 9 | `ansible/roles/node_menu/files/node_menu.py` | somente delegar a nova opção ao control plane; não criar banco no edge |
| 10 | `web/app.py` | opcionalmente expor a mesma operação administrativa, com a mesma autorização e sem duplicar regra |
| 11 | `ansible/roles/cdn_tenants/tasks/main.yml` | publicar snapshot/units, manter socket fixo e `421`/fail-closed |
| 12 | `tests/` | cobrir migração, política, relay, broker, menu, isolamento, headers e rollback |

Não alterar `core/render_tenants.py` para inserir `proxy_pass` com hostname
observado. O Nginx deve continuar apontando para o socket fixo do tenant; a
resolução de destino pertence ao processo Python protegido.

### 7.5 Pseudocódigo obrigatório

O comportamento mínimo deve ser equivalente a:

```python
def resolve_target(tenant_id, route_kind, current_url, hop, snapshot):
    policy = snapshot.policy_for(tenant_id)
    target = parse_redirect_without_userinfo(current_url)
    require(target.scheme in {"http", "https"})
    require(target.port in policy.allowed_ports)
    require(target.path.startswith("/"))
    reject_if_private_or_non_global_dns(target.hostname)

    if hop == 0:
        require(target == snapshot.tenant(tenant_id).origin)
    elif policy.mode == "strict":
        require(policy.manual_seed(route_kind, target))
    else:
        learned = policy.active_discovery(route_kind, target)
        if not learned:
            candidate = validate_public_target(target)
            require(candidate.allowed_by_provider_boundary or policy.approval_required)
            if policy.approval_required:
                record_candidate_without_connecting(candidate)
                raise PolicyDenied("discovery_candidate_pending")
            record_sanitized_target_with_ttl(candidate)

    ip = resolve_and_pin_public_ip(target.hostname)
    return connect_with_hostname_and_pinned_ip(target, ip)
```

No modo de descoberta totalmente automática, `record_sanitized_target_with_ttl`
só pode ocorrer depois de `validate_public_target` e dos limites da política.
Se a organização não tiver uma fronteira verificável de domínio/provedor, o
default deve ser `approval_required=True`; isso mantém o cadastro simples sem
conceder ao XUI comprometido uma capacidade de proxy arbitrária.

### 7.6 Sequência de implementação verificável

Para cada alteração, o implementador deve registrar:

```text
1. migration aplicada e backup verificado;
2. tenant antigo lido como strict;
3. snapshot novo validado sem segredo;
4. serviço broker/relay iniciado em ambiente de laboratório;
5. teste de VOD 1 e VOD 2;
6. teste de LB 1 e LB 2;
7. teste de bloqueio SSRF;
8. teste de isolamento A/B;
9. teste de rollback;
10. release, digest, nginx -t e health;
11. canário sem alteração DNS;
12. aprovação formal antes de habilitar descoberta em outro tenant.
```

Se qualquer item falhar, mantenha a política anterior. Não faça uma migração
parcial de tenants nem altere o default global para descoberta.

## 8. Implementação por fases

### Fase 0 — Contrato e migração

- adicionar enums e tabelas;
- migrar tenants existentes para `strict`;
- manter snapshots antigos funcionando como strict;
- adicionar backup e teste de restore da migração;
- não mudar comportamento de playback.

### Fase 1 — Menu estrito

- mover a gestão manual de VOD/LB para a tela de política;
- exibir múltiplas seeds por tenant;
- validar host, scheme e porta antes de gravar;
- gerar diff de política e release;
- testar rollback para política anterior.

### Fase 2 — Motor de descoberta em observação

- observar redirects sem usá-los para proxy;
- aplicar parser, DNS pinning e reason codes;
- armazenar somente host/scheme/porta/hash/TTL;
- mostrar candidatos no menu;
- medir taxa de bloqueios e falsos positivos.

### Fase 3 — Descoberta controlada em laboratório

- habilitar em apenas um tenant de laboratório;
- testar dois DNS VOD e dois destinos LB;
- testar redirect para IP privado, localhost, metadata, porta inválida,
  userinfo, traversal, loop e excesso de hops;
- confirmar ausência de `Location`, cookie, origem e credenciais;
- executar soak mínimo de 6 horas.

### Fase 4 — Canário de produção

- retirar a edge canária do pool, conforme runbook;
- emitir/distribuir TLS correto para o hostname real;
- ativar uma política por vez;
- validar `nginx -t`, reload, health, live, movie, series e Range;
- manter rollback explícito da política e da release;
- não alterar DNS durante o canário.

### Fase 5 — Expansão

- habilitar o segundo edge após o primeiro passar;
- comparar digest e snapshot nas duas edges;
- executar auditoria multi-tenant;
- somente depois considerar o modo descoberta como opção padrão para novos
  tenants.

## 9. Testes obrigatórios

### Testes de unidade

- normalização de host e porta;
- modo ausente interpretado como strict;
- TTL expirado bloqueado;
- limite de hosts descobertos;
- limite de redirects;
- múltiplas seeds VOD no mesmo tenant;
- dois tenants com hosts iguais rejeitados;
- tenant A nunca acessa seed do tenant B;
- nenhum segredo no evento ou razão de erro.

### Testes de relay

- origem XUI -> VOD 1 -> storage final;
- origem XUI -> VOD 2 -> storage final;
- `/movie/` com `Range` retorna `206` e `Content-Range`;
- `/series/` com `Range` retorna `206` e `Content-Range`;
- redirect sem `Location` bloqueia;
- redirect `127.0.0.1`, RFC1918, link-local e metadata bloqueia;
- redirect HTTPS preserva SNI e fixa IP;
- `Location` do upstream nunca chega ao cliente;
- `Set-Cookie`, `Server` e headers internos nunca chegam ao cliente;
- destino desconhecido retorna erro controlado, não proxy aberto.

### Testes do menu SSH

- menu edge delega ao control plane e não grava banco local;
- cancelamento não muda política;
- confirmação incorreta não muda política;
- alteração cria deployment pendente, mas não faz reload;
- visualização não mostra URLs completas;
- rollback restaura modo, TTL e seeds anteriores;
- operador sem autorização não altera política.

### Testes de produção controlada

Executar somente com conta de laboratório e sem imprimir variáveis:

```bash
python3 -m unittest discover -s tests -p '*test.py'
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/preflight-edge.yml --limit edge1
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/audit-edge-releases.yml --limit edge1
```

Depois do certificado correto e da autorização da janela:

```bash
curl --resolve tvbrasil.phpd77.com:443:143.14.168.168 \
  --fail --silent --show-error https://tvbrasil.phpd77.com/edge-health
```

Não considerar `curl -k` como aceite. O `--resolve` deve validar hostname,
certificado, SNI e IP da edge simultaneamente.

## 10. Critérios de aceite

### Modo estrito

- [ ] múltiplas seeds VOD/LB por tenant;
- [ ] destino fora da lista bloqueado;
- [ ] origem e socket sempre do tenant correto;
- [ ] live canônico sem regressão;
- [ ] VOD canônico com `200/206`;
- [ ] CNAME com `200/206`;
- [ ] nenhum dado interno no cliente;
- [ ] rollback testado.

### Modo descoberta controlada

- [ ] cadastro inicial sem seed manual funciona somente após política explícita;
- [ ] redirect é observado no servidor;
- [ ] primeiro destino é validado antes da conexão;
- [ ] DNS público é resolvido e pinado;
- [ ] rede privada e metadata são bloqueadas;
- [ ] scheme/porta/redirect/TTL têm limites;
- [ ] host aprendido é isolado por tenant;
- [ ] somente host/scheme/porta/hash/TTL são persistidos;
- [ ] `Location`, credenciais, cookies e origem não vazam;
- [ ] bloqueios são auditáveis sem segredo;
- [ ] expiração e rollback funcionam;
- [ ] soak e teste real de player passam.

## 11. Decisão recomendada

Implementar primeiro o modo estrito como contrato comum e o modo descoberta
controlada como feature flag por tenant, com default `strict`.

Não remover o cadastro manual de VOD/LB até que a descoberta controlada tenha:

1. testes de SSRF e isolamento multi-tenant;
2. prova de que o XUI real retorna os dois DNS VOD esperados;
3. teste de Range, seek, reconexão e playlist;
4. logs sanitizados revisados;
5. rollback de política e release;
6. soak prolongado;
7. certificado SAN correto para os CNAMEs reais.

Essa estratégia reduz o trabalho operacional sem enfraquecer a fronteira de
confiança. O sistema pode assumir a resolução do XUI, mas continua obrigado a
provar que cada destino é permitido antes de conectar.
