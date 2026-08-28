# Auditoria prática e plano de execução: CDN nível 5/10

Data da evidência: 28/08/2026 UTC.

Escopo: control node `143.14.168.111`, edge candidata `143.14.168.168`,
XUI `38.46.223.77:80`, LB autorizado `38.190.176.172:80` e hostname público
`cdn.phpd77.com`.

Este documento registra fatos medidos, não uma declaração comercial. “5/10”
significa uma CDN privada multi-edge pequena, automatizada e operável, não
equivalência a uma rede global Anycast.

## 1. Resumo executivo

O projeto avançou de um proxy single-edge com perfis alternáveis para um plano
de controle local com:

- SQLite em WAL;
- painel, CLI e menu SSH;
- worker Ansible separado;
- bootstrap com host key pinada e chave operacional Ed25519;
- tenant multi-edge persistido;
- artefato determinístico com SHA-256;
- primeira sincronização serial aprovada na edge candidata.

O data plane **publicado no DNS** ainda é single-edge: `cdn.phpd77.com` resolve
explicitamente para `143.14.168.111`. A candidata `143.14.168.168`, porém, já
possui Nginx, TLS e broker multi-tenant ativos e foi homologada diretamente com
`curl --resolve`, sem receber tráfego público. A nota medida atual é:

| Área | Peso | Atual | Evidência |
| --- | ---: | ---: | --- |
| controle seguro e automação | 15 | 12 | worker, WAL, SSH pinado, deploy serial e idempotente |
| multi-edge real | 20 | 10 | segunda VPS serve o hostname em homologação; DNS ainda não publicado |
| isolamento multi-tenant | 15 | 10 | snapshot, socket, cache, upstream e host isolados por tenant |
| disponibilidade DNS/TLS | 15 | 7 | TLS válido e health na 168; sem controlador DNS/failover |
| cache e proteção da origem | 15 | 7 | cache lock/fail-closed atuais, sem shield hierárquico |
| observabilidade | 10 | 3 | health local e monitor sanitizado, sem visão de frota |
| testes, SLO e desastre | 10 | 5 | unitários, preflight, `--resolve` e soak curto; sem mídia/desastre |
| **Total** | **100** | **54/100** | **4,8/10 pelos gates críticos pendentes** |

O marco 5/10 exige pelo menos 50 pontos e todos os gates críticos. Embora a soma
aritmética seja 54, a nota permanece em 4,8/10 até passar HLS/VOD autorizado,
soak longo, rollback exercitado e failover. Segurança e disponibilidade não
podem ser compensadas apenas pela existência dos componentes.

Status operacional atual: parcial.

O sistema já demonstra controle, bootstrap, TLS, rollback e parte do HLS real,
mas ainda não cumpre os gates de reprodução completa, carga sustentada e
failover de desastre com evidência suficiente para declarar 5/10 de forma
honesta.

## 2. Topologia real observada

```text
DNS atual
cdn.phpd77.com A 143.14.168.111 TTL 150
                         |
                         v
                 Edge atual / control node
                 Nginx :80/:443
                 broker 127.0.0.1:9091
                         |
                  +------+------+
                  |             |
             XUI origem        LB XUI
          38.46.223.77:80  38.190.176.172:80

Edge candidata 143.14.168.168
  SSH por chave: OK
  release sincronizada e ativa: OK
  Nginx/TLS/broker multi-tenant: ativos
  teste direto por hostname: OK
  DNS: não publicado
```

O control node estar também no data plane é aceitável como etapa transitória,
mas reduz independência de falha. Para uma arquitetura madura, painel, banco e
worker devem sair das edges públicas.

## 3. Evidências coletadas

### 3.1 Control plane

```text
cdnmnus-admin.service: active
cdnmnus-orchestrator.service: active
SQLite journal_mode: wal
deployments anteriores ao teste: 0
```

Permissões após correção:

```text
/etc/cdnmnus/ssh/known_hosts       cdn-admin:cdn-admin 0600
/etc/cdnmnus/ssh/lb011.ed25519     cdn-admin:cdn-admin 0600
```

O acesso SSH foi comprovado sob o mesmo usuário do worker:

```text
sudo -u cdn-admin ssh ... cdn-deploy@143.14.168.168
resultado: CDNMNUS_KEY_OK
```

### 3.2 XUI e LB existentes

Configuração migrada do painel legado:

```yaml
tenant_id: xui-principal
canonical_host: cdn.phpd77.com
origin: 38.46.223.77:80
load_balancers:
  - 38.190.176.172:80
```

Teste controlado de 20 requisições sem credenciais, executado da edge 168:

```text
38.46.223.77  count=20  HTTP 200=20  média=0,427s  máximo=0,428s
38.190.176.172 count=20 HTTP 200=20  média=0,428s  máximo=0,430s
```

Isso prova conectividade básica e estabilidade da rota curta da amostra. Não
prova capacidade HLS/VOD, cache hit ratio, expiração de token ou throughput.

### 3.3 Edge `lb011`

```text
IP: 143.14.168.168
SO: Ubuntu 22.04
vCPU: 2
RAM: 3940 MB
Disco: 30 GB total, 24 GB livres
SSH: porta 22
NTP sincronizado: sim
UFW: inativo
Nginx: não instalado/ativo
nofile da sessão: 1024
somaxconn: 4096
passwordauthentication: yes
permitrootlogin: yes
```

Esses valores são suficientes para homologação e carga moderada, mas ainda não
são um baseline de edge em produção. UFW, limites, sysctl, Nginx e hardening SSH
precisam ser aplicados somente após preservar acesso de recuperação.

### 3.4 Preflight Ansible real

Playbook: `ansible/playbooks/preflight-edge.yml`.

```text
ok=6 changed=0 unreachable=0 failed=0
Ubuntu >= 20: aprovado
RAM >= 1 GB: aprovado
origem TCP/80: aprovado
LB TCP/80: aprovado
```

### 3.5 Primeira release

```text
deployment: dep-f565cf935c6c44519f1e5ad7bf06a700
state: succeeded
release: 20260828091410-f75ffb63
config_digest: 1838858bbf45054533b3815092da5fa1c217eab52a47c1efdd2af747fb20b75e
tenant_count: 1
```

Arquivos sincronizados em:

```text
/opt/cdnmnus/releases/20260828091410-f75ffb63/
├── manifest.json
├── broker/tenants.json
├── nginx/tenants/xui-principal.conf
└── SYNCED
```

Após a instalação do runtime, `/opt/cdnmnus/current` passou a apontar
atomicamente para essa release. O digest foi conferido antes da ativação.

### 3.6 Ativação real da edge 168

As roles `edge_base`, `cdn_runtime` e `cdn_tenants` foram aplicadas com
`serial: 1`. A repetição integral do playbook terminou com:

```text
ok=25 changed=0 unreachable=0 failed=0
nginx -t: aprovado
nginx: active
cdnmnus-tenant-broker@xui-principal: active
UFW: active; entrada permitida somente em 22, 80 e 443
```

O certificado de `cdn.phpd77.com` foi distribuído por canal SSH pinado, fora de
Git, SQLite e artefatos comuns. A chave remota está `root:root 0600`; o
fingerprint público é idêntico nas edges e a validade observada vai até
26/11/2026. Em 28/08/2026 foi instalado e exercitado o deploy hook do Certbot:
ele envia o certificado por SSH pinado, valida validade, correspondência entre
chave e certificado e SHA-256, executa `nginx -t` e recarrega a edge.

### 3.7 Homologação sem alteração de DNS

```text
HTTPS por --resolve:                 HTTP 200
/edge-health por --resolve:          HTTP 200
hostname desconhecido:               HTTP 421 (fail-closed)
soak curto /edge-health:              120/120, 0 falhas, máximo 104 ms
broker com tenant/host correto:       rota processada; mídia falsa recusada 503
broker com tenant incorreto:          bloqueado 502
```

O teste usou uma URI HLS falsa e sem credenciais para comprovar falha segura.
Não comprova reprodução: nenhuma credencial foi recuperada do sistema nem
persistida. HLS real, VOD Range, refresh e soak de seis horas continuam sendo
gates obrigatórios.

### 3.8 Rollback exercitado em homologação

Em 28/08/2026 uma release de teste derivada da ativa foi promovida na edge
`143.14.168.168`. Depois de `nginx -t`, reload e `/edge-health` 200, o symlink
`/opt/cdnmnus/current` retornou à release `20260828091410-f75ffb63`; novo teste,
reload e health 200 confirmaram o rollback. A evidência sanitizada está em
`/var/lib/cdnmnus/rollback-evidence.txt` na edge.

### 3.9 Playback real por URL autorizada

Para tornar a repetição segura e auditável, foi incluído
`scripts/media_validation.py`. Ele lê a playlist de um arquivo `0600`,
seleciona categorias por nome, testa o primeiro recurso de cada categoria,
envia `Range: bytes=0-1` e repete o refresh três vezes. O JSON gerado contém
somente hostname, status, latência, tipo MIME e contadores; não grava a URL
assinada, usuário, senha, token ou caminho de origem:

```bash
install -m 600 /caminho/playlist.url /etc/cdnmnus/playlist.url
python3 /opt/cdnmnus-panel/media_validation.py \
  --url-file /etc/cdnmnus/playlist.url \
  --result /var/lib/cdnmnus/media-validation.json
```

Esse comando é uma ferramenta de evidência, não um atestado automático de
sucesso: `Range` só fecha o gate VOD quando o recurso escolhido for realmente
um objeto VOD e responder `206`.

### 3.10 Estado DNS verificado em 28/08/2026

Foi feita consulta externa aos apontamentos informados:

```text
cdn.phpd77.com       A -> 143.14.168.111
teste.phpd77.com     A -> 143.14.168.111, 143.14.168.168, 143.14.168.170
```

Por ser um registro explícito, `cdn.phpd77.com` não herda os três endereços do
wildcard. Os health checks diretos pelo mesmo hostname retornaram:

```text
143.14.168.111 -> HTTPS /edge-health 200
143.14.168.168 -> HTTPS /edge-health 200
143.14.168.170 -> sem resposta (HTTP 000)
```

O wildcard, portanto, já serve como laboratório de distribuição, mas não deve
ser usado como failover. A edge `170` precisa ser provisionada e validada antes
de qualquer publicação. A adição de múltiplos A em `cdn.phpd77.com` só é segura
depois de fechar o estado compartilhado/renovação de tokens entre as edges;
caso contrário, uma sessão iniciada em uma edge pode falhar ao trocar de edge.

### 3.11 Incidente de reprodução e correção aplicada

Durante a reprodução foram observados avisos recorrentes do Nginx
`ignore long locked inactive cache entry`. A causa era a chave de cache contendo
o query string completo; como cada segmento HLS possui token diferente, isso
fragmentava o cache e acumulava locks. O renderer passou a usar `$uri`, aplicar
`proxy_cache_lock_timeout`/`proxy_cache_lock_age` e bypass de cache para
`Range`. O vhost ativo foi recarregado após `nginx -t`, e o cache antigo foi
moviado para um diretório de recuperação, sem apagar dados de forma
irreversível.

Também foi confirmado que HLS voltou a responder ponta a ponta:

```text
manifesto de canal: HTTP 200
segmento TS real:   HTTP 200 (~8,8 MB na amostra)
/edge-health:        HTTP 200
```

O VOD continua bloqueado de forma intencional quando o XUI redireciona para um
host novo não presente na allowlist (`solitary-cloud-*.workers.dev`). Esse host
precisa ser validado e cadastrado como upstream VOD aprovado; aceitar qualquer
redirect automaticamente seria uma falha crítica de segurança.

Em 28/08/2026 foi validado o fluxo real do player contra a URL autorizada
informada pelo operador, comparando o acesso público `cdn.phpd77.com` com a
origem `38.46.223.77:80`.

Playlist principal:

```text
[cdn] status=200
[cdn] body_prefix: #EXTM3U ... https://cdn.phpd77.com/domagopdproje/domagopdproje/1207343.m3u8
[origin] status=200
[origin] body_prefix: #EXTM3U ... http://38.46.223.77:80/domagopdproje/domagopdproje/1207343.m3u8
```

Evidencia objetiva:

- a playlist publica do CDN reescreve os links para `cdn.phpd77.com`;
- a playlist direta da origem expõe `38.46.223.77`;
- o retorno publico nao trouxe leak de IP/host da origem no corpo da playlist;
- o retorno direto da origem expõe o IP e o host interno, portanto a ocultacao
  depende do acesso pelo dominio publico.

Testes de categorias solicitadas:

```text
UFC PASS 24H:
  playlist 200; segmento filho retornou 503

XXX - ADULTO LGBT +18:
  playlist 200; segmento filho retornou 200 e video/mp2t

SERIES 24H:
  playlist 200; segmento filho retornou 503

FILMES & SERIES:
  playlist 200; segmento filho retornou 200 e video/mp2t
```

Teste de VOD/Range:

```text
007 Contra GoldenEye:
  Range bytes=0-1023 -> HTTP 502
```

Conclusão desta rodada: o fluxo de playlist e parte do HLS real funcionam pelo
domínio público, mas ainda existem falhas reais em alguns segmentos e o teste
de VOD com `Range` não passou. Isso ainda nao fecha o gate de HLS/VOD/Range.

## 4. Correções realizadas durante a auditoria

### 4.1 Propriedade das chaves

O bootstrap executado pelo menu root criava arquivos `root:root 0600`, tornando
o inventário inutilizável pelo worker. Foi corrigido para herdar UID/GID do
diretório `/etc/cdnmnus/ssh`.

### 4.2 Estado da edge

Antes, login por chave promovia a edge diretamente para `ready`. Agora:

```text
bootstrap SSH concluído -> bootstrapping
runtime + TLS + broker + health + digest -> ready
```

`lb011` foi corrigida para `bootstrapping`. Ela não entra na matriz DNS.

### 4.3 Host key

O pinning aceita host key Ed25519, ECDSA ou RSA, sempre com fingerprint SHA-256,
priorizando Ed25519. A chave de acesso criada para `cdn-deploy` continua Ed25519.

### 4.4 Artefatos

Novas releases recebem modo explícito `0640`. Snapshot, vhost e manifesto não
devem depender do `umask` do processo que enfileirou o job.

## 5. O que significa chegar a 5/10

O objetivo não é copiar em pequena escala um provedor global. É atingir uma CDN
privada com dois ou três pontos, isolamento por tenant, operação previsível e
falha controlada.

Critérios mínimos e não negociáveis:

- duas edges realmente servindo o mesmo hostname;
- rollout `serial: 1` com canário e rollback exercitado;
- broker multi-tenant isolado por socket/configuração;
- cache, upstreams e rotas internas isolados por tenant;
- TLS válido em todas as edges;
- health composto externo;
- retirada automática de edge falha;
- origem aceitando somente IPs das edges;
- métricas sem URL credenciada;
- teste HLS/VOD e desastre com evidência.

Provedores globais usam muitos PoPs, Anycast, cache em camadas, grande capacidade
de absorção e serviços distribuídos. A arquitetura de referência da Cloudflare
explica que Anycast entrega proximidade, redundância e distribuição de carga;
também destaca tiered cache para reduzir acessos repetidos à origem. Esse é o
referencial de “topo”, não algo reproduzível com duas VPSs.

## 6. Plano de implementação até o marco 5/10

### Fase A — runtime seguro na edge 168 — concluída

Implementar roles idempotentes:

```text
edge_base
  apt: nginx, ufw, ca-certificates
  sysctl/limits
  diretórios e usuários
  firewall preservando SSH

cdn_runtime
  token broker multi-tenant
  units por tenant ou dispatcher seguro
  monitor sanitizado

cdn_tenants
  release candidata
  nginx -t isolado
  symlink atômico
  rollback
```

Gates:

- segunda execução Ansible com `changed=0`;
- SSH novo testado antes de endurecer root/password;
- Nginx local respondendo apenas em teste por IP/Host;
- nenhum DNS alterado.

### Fase B — broker multi-tenant — concluída na primeira versão

O renderizador já produz:

```text
/run/cdnmnus/broker-xui-principal.sock
cache_xui-principal
origin_xui-principal
rotas /__cdnmnus_xui-principal_* marcadas internal
```

O novo broker usa uma instância/socket Unix por tenant, valida tenant e hostname
contra o snapshot e reaproveita o resolvedor fail-closed. Foram implementados:

- `tenant_id + public_host` validados contra snapshot;
- chave de cache `tenant_id\0media_kind\0uri`;
- allowlist por tenant;
- socket Unix com owner/mode compatíveis com Nginx;
- reload atômico de geração;
- health incluindo schema, geração e digest;
- teste de redirect cruzado entre tenants falhando fechado.

### Fase C — TLS sem alterar o hostname público — funcional

`cdn.phpd77.com` possui o mesmo certificado Let's Encrypt válido nas edges 111
e 168 até 26/11/2026. Falta automatizar renovação e distribuição segura.

Estratégias:

1. DNS-01 centralizado, preferível para múltiplas edges;
2. certificado distribuído como segredo versionado fora do artefato comum;
3. emissão independente por edge apenas se o fluxo ACME e DNS suportarem.

DNS-01 permite validar controle do nome sem depender de qual edge recebeu uma
requisição HTTP. Credenciais DNS precisam de escopo mínimo e armazenamento
separado.

### Fase D — teste sem DNS — parcial

Após runtime e TLS:

```bash
curl --resolve cdn.phpd77.com:443:143.14.168.168 \
  https://cdn.phpd77.com/edge-health
```

Executar também:

- raiz e endpoints não credenciados;
- hostname desconhecido falhando fechado;
- HLS autorizado com URL em arquivo `0600`;
- VOD com Range;
- refresh de token;
- ausência de `Location`, origem e headers internos na resposta;
- soak mínimo de seis horas.

### Fase E — segunda edge ativa e DNS

A edge 111 já atende produção; a 168 será o canário. Não publicar A para 168
enquanto algum gate anterior estiver pendente.

DNS atual:

```text
cdn.phpd77.com A 143.14.168.111 TTL 150
```

Para failover real, é necessário conhecer e integrar a API do DNS autoritativo.
Round-robin com dois registros A não detecta falha. DNS também possui caches e
alguns resolvedores podem servir dados stale; logo, TTL baixo não equivale a
failover instantâneo.

Política inicial:

```text
probe: HTTPS /edge-health por hostname
intervalo: 15s
timeout: 5s
retirada: 3 falhas consecutivas
retorno: 5 sucessos consecutivos
TTL: 30–60s durante homologação, após medir o provedor
```

### Fase F — origin shield e segurança

- permitir origem/LB somente a partir de `143.14.168.111` e
  `143.14.168.168`, depois das provas;
- negar acesso direto geral sem perder canal de recuperação;
- limitar root/password na edge 168 somente após validar console e chave;
- substituir `NOPASSWD: ALL` por helper/allowlist compatível com Ansible ou
  runner assinado;
- ativar UFW preservando SSH;
- elevar `nofile` via unit e limites;
- adicionar swap pequeno apenas se a política operacional exigir proteção a
  picos, sem tratá-lo como capacidade.

### Fase G — observabilidade e desastre

Métricas mínimas por edge/tenant:

- disponibilidade e latência do health;
- release/config digest;
- conexões ativas;
- cache HIT/MISS/STALE/BYPASS;
- bytes entregues;
- erros upstream por classe, sem URI;
- CPU, memória, disco e file descriptors;
- falhas de broker e refresh, sem token.

Teste de desastre:

1. iniciar stream de homologação;
2. retirar edge canário de forma controlada;
3. comprovar remoção do pool;
4. parar Nginx;
5. confirmar nova sessão na edge restante;
6. restaurar e exigir cinco health checks;
7. verificar que digest divergente impede retorno;
8. registrar tempos e impacto percebido.

## 7. Sequência operacional exata

| Ordem | Ação | Situação |
| ---: | --- | --- |
| 1 | corrigir owner das chaves | concluída |
| 2 | SSH pelo usuário do worker | concluída |
| 3 | migrar XUI para tenant | concluída |
| 4 | preflight Ansible | concluída |
| 5 | sincronizar release/digest | concluída |
| 6 | implementar broker multi-tenant | concluída; isolamento testado |
| 7 | implementar roles de runtime | concluída; repetição `changed=0` |
| 8 | instalar Nginx/broker na 168 | concluída para `xui-principal` |
| 9 | emitir/distribuir TLS | distribuído; renovação automática instalada e exercitada |
| 10 | teste `--resolve` e soak | parcial: HTTPS/health/120 ciclos OK; mídia e 6h pendentes |
| 11 | integrar API DNS/health | bloqueado por decisão do provedor |
| 12 | publicar edge 168 | proibido antes dos gates |
| 13 | teste de desastre | pendente |

## 8. Critérios objetivos para promover `lb011` a `ready`

- [x] release instalada e symlink ativo;
- [x] Nginx e broker habilitados no boot;
- [x] `nginx -t` passa na configuração ativa;
- [x] socket do tenant existe com owner/mode correto;
- [x] certificado válido para `cdn.phpd77.com`;
- [x] `/edge-health` retorna 200 cinco vezes;
- [x] digest ativo é `1838858...b20b75e` ou geração posterior aprovada;
- [x] origem e LB alcançáveis;
- [x] hostname não cadastrado não alcança upstream;
- [ ] HLS, VOD Range e refresh passam;
- [ ] logs/eventos não contêm credenciais;
- [x] rollback foi executado em homologação;
- [ ] DNS ainda não aponta para a edge durante todos os testes anteriores.

## 9. O que falta para o 5/10 ficar honesto

O que ainda bloqueia a promoção da nota não é “mais infraestrutura”; são
evidências operacionais que fechem os gates críticos:

- HLS autorizado precisa passar ponta a ponta com reprodução estável pelo
  hostname público;
- VOD precisa responder `206 Partial Content` em pelo menos um fluxo real com
  `Range`;
- refresh/token precisa renovar sem expor `Location`, host interno ou token ao
  cliente;
- soak contínuo de seis horas precisa completar sem erro material e com
  estatística consolidada;
- rollback real precisa permanecer repetível como procedimento, não só como
  evento isolado de laboratório;
- failover/desastre precisa mostrar que uma edge sai do pool e a outra assume
  sem interromper o playback;
- DNS/controller precisa refletir o estado saudável, ou ficar explicitamente
  fora do escopo até a automação existir;
- logs e métricas precisam seguir sem credenciais, URLs assinadas ou origem
  explícita.

## 10. O que não fazer

- não marcar `ready` apenas porque SSH funciona;
- não copiar certificado via artefato público ou Git;
- não abrir o XUI para toda a Internet;
- não ativar os vhosts gerados contra o broker single-tenant atual;
- não colocar senha root em Vault “para facilitar” bootstrap recorrente;
- não executar carga com URLs de clientes sem autorização/limites;
- não adicionar o segundo A record sem health controller;
- não desligar root/password antes de provar console e recuperação;
- não chamar round-robin DNS de failover automático.

## 10. Referências oficiais usadas

- [Cloudflare CDN Reference Architecture](https://developers.cloudflare.com/reference-architecture/architectures/cdn/): Anycast, proximidade, redundância e tiered cache.
- [Cloudflare Load Balancing Reference Architecture](https://developers.cloudflare.com/reference-architecture/architectures/load-balancing/): health monitoring e retirada/reinserção de endpoints.
- [NGINX proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html): cache key, cache lock, stale e timeouts.
- [NGINX HTTP load balancing](https://nginx.org/en/docs/http/load_balancing.html): balanceamento, `max_fails`, `fail_timeout` e health passivo.
- [NGINX slice module](https://nginx.org/en/docs/http/ngx_http_slice_module.html): Range/cache de objetos grandes e limitações.
- [Ansible execution strategies](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_strategies.html): batches e `serial`.
- [Ansible rolling upgrade example](https://docs.ansible.com/projects/ansible/latest/playbook_guide/guide_rolling_upgrade.html): drain e atualização coordenada.
- [Let's Encrypt challenge types](https://letsencrypt.org/ca/docs/challenge-types/): HTTP-01 e DNS-01.
- [SQLite WAL](https://www.sqlite.org/wal.html): concorrência, checkpoint e restrição ao mesmo host.
- [RFC 9199](https://www.rfc-editor.org/info/rfc9199/): impacto de TTL em cache, resiliência e seleção CDN.
- [RFC 8767](https://www.rfc-editor.org/info/rfc8767/): possibilidade de resolvedores servirem DNS stale em falhas.

## Conclusão

A base agora possui um segundo data plane funcional em homologação: o XUI real
está modelado, a edge aceita SSH pinado, o runtime é idempotente, o TLS é válido
e os testes diretos passaram. Ainda não existe CDN multi-edge **publicada**,
porque o DNS permanece corretamente na 111 e não há controlador de failover.

O caminho restante para 5/10 é fornecer uma URL de homologação autorizada em
arquivo 0600, passar HLS/VOD/Range/refresh e soak de seis horas, exercitar
rollback e integrar health controller antes de qualquer publicação DNS. A edge
continua corretamente em `bootstrapping` até esses gates terminarem.
