# Especificação de implementação: multi-edge com Ansible desacoplado

Status: proposta técnica baseada no código real em 28/08/2026. Este documento
define a implementação incremental; o playbook inicial sob `ansible/` já existe,
mas os exemplos avançados não devem ser tratados como funcionalidade pronta.

Atualização de implementação: o primeiro esqueleto funcional do plano de
controle agora existe em `core/`, `cli/`, `web/`, `orchestrator/` e `ansible/`.
Ele cobre cadastro, bootstrap, renderização, fila e sincronização serial de
artefatos. A ativação do novo data plane multi-tenant continua condicionada à
migração do token broker para sockets por tenant e aos testes operacionais das
Fases 2–4; portanto, os exemplos avançados deste documento ainda são contrato
alvo, não declaração de produção concluída.

Este documento é a especificação de arquitetura. O mapeamento executável,
arquivo por arquivo, com exemplos **como está / como ficará**, contratos entre
componentes e sequência de pull requests está em
[`MULTI_EDGE_OPTION_B_CODE_GUIDE.md`](MULTI_EDGE_OPTION_B_CODE_GUIDE.md).

### Legenda de maturidade

| Marca | Significado |
| --- | --- |
| **ATUAL** | existe hoje no repositório e foi conferido no código |
| **ADAPTAR** | código atual que deve ser extraído ou generalizado |
| **NOVO** | arquivo, tabela, serviço ou comportamento ainda não implementado |
| **OPERACIONAL** | depende de infraestrutura externa, DNS ou novas VPSs |

## 1. Objetivo

Transformar a instalação atual, que aplica um XUI ativo em uma única máquina,
em uma plataforma que gerencie simultaneamente:

- vários XUIs, cada um identificado pelo hostname público;
- três ou mais edges compartilhadas;
- implantação idempotente e serial;
- bootstrap SSH sem conservar a senha inicial;
- configuração, certificados, health checks, rollback e auditoria;
- DNS apenas, sem proxy de terceiros no caminho de mídia.

Exemplo desejado:

```text
xui1.cdn.phpd77.com ─┬─ Edge 111 ─┐
                     ├─ Edge 170 ─┼─ XUI 1 + LBs/VOD autorizados
                     └─ Edge 168 ─┘

xui2.cdn.phpd77.com ─┬─ Edge 111 ─┐
                     ├─ Edge 170 ─┼─ XUI 2 + LBs/VOD autorizados
                     └─ Edge 168 ─┘
```

Ansible atua apenas no plano de gestão. Nenhum play, segmento ou token passa
por Ansible durante a reprodução.

## 2. Diagnóstico do código atual

### 2.1 O que já pode ser reutilizado

| Componente atual | Capacidade aproveitável |
| --- | --- |
| `install.sh` | instalação idempotente parcial de Nginx, painel e units |
| `scripts/update.sh` | backup e atualização local |
| `render_include()` | geração segura de um virtual host XUI |
| `apply_config()` | escrita atômica, `nginx -t`, reload e rollback local |
| `token_broker.py` | DNS pinning, allowlists, HLS/VOD fail-closed e refresh |
| `/edge-health` | probe composto para retirar/recolocar uma edge |
| monitor sanitizado | métricas sem URI, usuário, senha ou token |
| soak systemd | validação temporal usando URL em arquivo 0600 |

### 2.2 Limitação que impede multi-XUI simultâneo

Hoje `save_profile()` adiciona o perfil à lista, mas também o transforma no
único perfil ativo:

```python
normalized.update({
    "profiles": profiles,
    "active_profile_id": profile_id,
})
apply_config(normalized)
```

`apply_config()` produz somente:

```text
/etc/nginx/conf.d/99-cdnmnus-upstream.conf
/etc/cdnmnus/token-broker.json
```

E `render_include()` recebe apenas um objeto:

```python
def render_include(config: dict[str, Any]) -> str:
    host = str(config["upstream_host"])
    ...
    server_name config["public_host"]
```

Consequências:

- trocar o perfil substitui o XUI anterior;
- o broker possui uma única origem/allowlist global;
- nomes de upstream e cache não possuem `tenant_id`;
- o painel executa `systemctl` somente na edge local;
- não há inventário, jobs remotos ou controle de versão da implantação.

## 3. Arquitetura alvo

```text
Operador
   │ HTTPS/SSH tunnel
   ▼
Painel de controle ── SQLite/PostgreSQL (estado desejado + jobs)
   │
   ├── grava snapshot imutável da configuração
   └── solicita job ao Orquestrador local
                            │
                    ansible-runner / ansible-playbook
                            │ SSH por chave
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
          Edge 111      Edge 170      Edge 168
          Nginx+broker  Nginx+broker  Nginx+broker
              │             │             │
              └──── XUI 1 / XUI 2 / XUI N ────┘
```

Separação obrigatória:

- **Painel:** CRUD, validação, aprovação e visualização.
- **Orquestrador:** processo separado, sem porta pública, executando jobs.
- **Ansible:** descrição idempotente do estado das edges.
- **Edge:** data plane; continua funcionando se painel/Ansible cair.
- **DNS controller:** integração futura e separada; só publica edge saudável.

O processo web não deve executar `ansible-playbook` diretamente como root. Ele
insere um job; um worker dedicado consome a fila com allowlist de operações.

Na **Opção B**, “desacoplado” significa desacoplamento de processo, privilégio e
ciclo de falha. O painel não importa Ansible, não abre SSH e não recebe senha de
bootstrap. O worker pode começar na mesma máquina de gestão, mas roda com outro
usuário, outra unit e diretórios próprios; em produção madura, pode ser movido
para um control node sem alterar o contrato de jobs. Painel ou worker jamais
devem ser instalados nas edges de dados.

## 4. Estrutura proposta no repositório

```text
ansible/
├── ansible.cfg
├── inventories/
│   └── production/
│       ├── hosts.yml
│       ├── group_vars/
│       │   ├── all.yml
│       │   └── vault.yml          # criptografado
│       └── host_vars/
│           ├── edge-111.yml
│           ├── edge-170.yml
│           └── edge-168.yml
├── playbooks/
│   ├── bootstrap-edge.yml
│   ├── deploy-edge.yml
│   ├── deploy-tenants.yml
│   ├── rotate-key.yml
│   ├── drain-edge.yml
│   └── rollback-edge.yml
└── roles/
    ├── edge_base/
    ├── cdn_runtime/
    ├── cdn_tenants/
    ├── cdn_tls/
    ├── cdn_monitoring/
    └── edge_health/

orchestrator/
├── worker.py
├── event_filter.py
└── cdnmnus-orchestrator.service
```

Arquivos gerados nunca devem ser commitados com credenciais ou tokens.

## 5. Modelo de dados alvo

O modelo `settings(key,value)` continua útil para migração, mas não é adequado
para relacionar XUIs, aliases, edges e deployments. A evolução recomendada:

```sql
CREATE TABLE xui_tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    origin_host TEXT NOT NULL,
    origin_port INTEGER NOT NULL DEFAULT 80,
    enabled INTEGER NOT NULL DEFAULT 0,
    config_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tenant_hosts (
    hostname TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    is_canonical INTEGER NOT NULL DEFAULT 0,
    tls_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE tenant_upstreams (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('origin','lb','vod')),
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 80,
    UNIQUE(tenant_id, kind, host, port)
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    management_host TEXT NOT NULL,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL DEFAULT 'cdn-deploy',
    host_key_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
      ('pending','bootstrapping','ready','draining','failed','disabled')),
    last_health_at TEXT,
    deployed_version TEXT
);

CREATE TABLE tenant_edges (
    tenant_id TEXT NOT NULL REFERENCES xui_tenants(id) ON DELETE CASCADE,
    edge_id TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(tenant_id, edge_id)
);

CREATE TABLE deployments (
    id TEXT PRIMARY KEY,
    requested_by TEXT NOT NULL,
    desired_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE deployment_edges (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    edge_id TEXT NOT NULL REFERENCES edges(id),
    state TEXT NOT NULL,
    sanitized_event TEXT,
    PRIMARY KEY(deployment_id, edge_id)
);
```

Não armazenar:

- senha root do bootstrap;
- senha XUI/M3U;
- token temporário de stream;
- chave SSH privada no SQLite;
- URI completa de mídia em eventos.

## 6. Estado desejado: antes e depois

### Atual

```json
{
  "active_profile_id": "abc123",
  "upstream_host": "origin-one.example",
  "public_host": "xui1.cdn.example",
  "load_balancers": ["lb-one.example"]
}
```

### Alvo

```yaml
schema_version: 1
generation: 42
tenants:
  - id: xui1
    canonical_host: xui1.cdn.example
    aliases: [play.customer-one.example]
    origin: {host: origin-one.example, port: 80}
    load_balancers: [lb-one.example]
    vod_hosts: [vod-one.example]
  - id: xui2
    canonical_host: xui2.cdn.example
    aliases: []
    origin: {host: origin-two.example, port: 80}
    load_balancers: [lb-two.example]
    vod_hosts: [vod-two.example]
```

Esse snapshot deve ser validado, serializado deterministicamente e identificado
por SHA-256. Todas as edges de uma geração devem reportar o mesmo digest.

## 7. Renderização Nginx multi-XUI

Em vez de um arquivo global, cada tenant ganha um include independente:

```text
/etc/nginx/cdnmnus/tenants/xui1.conf
/etc/nginx/cdnmnus/tenants/xui2.conf
/etc/nginx/cdnmnus/tenants/xui3.conf
```

O include principal contém apenas:

```nginx
include /etc/nginx/cdnmnus/tenants/*.conf;
```

Exemplo alvo simplificado:

```nginx
proxy_cache_path /var/cache/nginx/cdnmnus/xui1
    keys_zone=cache_xui1:32m max_size=2g inactive=2m;

upstream origin_xui1 {
    server 192.0.2.10:80;
    keepalive 32;
}

upstream broker_xui1 {
    server unix:/run/cdnmnus/broker-xui1.sock;
    keepalive 16;
}

server {
    listen 443 ssl;
    server_name xui1.cdn.example play.customer-one.example;

    location ^~ /hls/ {
        proxy_pass http://broker_xui1;
        proxy_set_header X-CDN-Tenant xui1;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
    }

    location ^~ /movie/ {
        proxy_pass http://broker_xui1;
        proxy_set_header X-CDN-Tenant xui1;
        proxy_set_header X-Broker-Action resolve-vod;
        proxy_set_header X-Original-URI $request_uri;
    }
}
```

Requisitos do gerador:

- nomes Nginx derivados somente de IDs `[a-z0-9_-]`, nunca de entrada bruta;
- `server_name` único em todo o snapshot;
- zonas e diretórios de cache por tenant;
- nenhum fallback de hostname desconhecido para algum XUI;
- todo relay/token route marcado `internal`;
- arquivo temporário, `fsync`, rename atômico e `nginx -t` antes do reload;
- remoção de tenant em duas fases: retirar do DNS, drenar, remover config/cache.

## 8. Broker multi-tenant

Hoje o broker lê um único `/etc/cdnmnus/token-broker.json`. O alvo é um arquivo
sem segredos por tenant ou um snapshot único:

```json
{
  "generation": 42,
  "tenants": {
    "xui1": {
      "public_hosts": ["xui1.cdn.example", "play.customer-one.example"],
      "origin_host": "origin-one.example",
      "load_balancers": ["lb-one.example"],
      "vod_hosts": ["vod-one.example"],
      "ttl_seconds": 15
    }
  }
}
```

Mudança de API interna:

```python
# atual
STATE.resolve(uri, force=True, vod=False)

# alvo
STATE.resolve(
    tenant_id=validated_tenant,
    request_host=validated_public_host,
    uri=uri,
    force=True,
    media_kind="hls",
)
```

A chave de singleflight/cache passa a incluir tenant:

```python
key = sha256(f"{tenant_id}\0{media_kind}\0{uri}".encode()).hexdigest()
```

Não confiar cegamente em `X-CDN-Tenant`. O broker só escuta socket Unix/local e
confirma que o hostname pertence ao tenant no snapshot carregado.

## 9. Inventário Ansible

Exemplo sem senhas:

```yaml
# inventories/production/hosts.yml
all:
  children:
    cdn_edges:
      hosts:
        edge-111:
          ansible_host: 143.14.168.111
          ansible_port: 22
          edge_region: primary
        edge-170:
          ansible_host: 143.14.168.170
          ansible_port: 22
          edge_region: secondary-a
        edge-168:
          ansible_host: 143.14.168.168
          ansible_port: 22
          edge_region: secondary-b
      vars:
        ansible_user: cdn-deploy
        ansible_become: true
        ansible_python_interpreter: /usr/bin/python3
```

`host_vars` guarda somente diferenças não secretas. Dados confidenciais ficam
em secret manager ou Ansible Vault, com `no_log: true` e `diff: false`. Vault
protege dados em repouso; não elimina a obrigação de impedir vazamento durante a
execução.

## 10. Bootstrap SSH profissional

Fluxo único para uma edge nova:

1. operador informa IP, porta e usuário inicial;
2. sistema busca a host key via canal separado e mostra SHA-256 para aprovação;
3. operador confirma a fingerprint exibida no console do provedor;
4. senha root é solicitada por prompt/secret broker, nunca por argumento CLI;
5. play de bootstrap cria `cdn-deploy` e instala chave pública exclusiva;
6. sudoers libera somente comandos necessários ou, inicialmente, `become` com
   política controlada;
7. login por chave é testado em nova conexão;
8. senha deixa a memória e nunca é persistida;
9. edge passa de `pending` para `ready` somente após deploy e `/edge-health`.

Exemplo conceitual do play:

```yaml
- name: Bootstrap de edge
  hosts: bootstrap_target
  gather_facts: false
  serial: 1
  tasks:
    - name: Criar usuário operacional
      ansible.builtin.user:
        name: cdn-deploy
        shell: /bin/bash
        create_home: true

    - name: Instalar chave exclusiva desta edge
      ansible.posix.authorized_key:
        user: cdn-deploy
        key: "{{ edge_public_key }}"
        exclusive: true
      no_log: true

    - name: Instalar sudoers validado
      ansible.builtin.copy:
        src: files/cdn-deploy.sudoers
        dest: /etc/sudoers.d/cdn-deploy
        owner: root
        group: root
        mode: '0440'
        validate: /usr/sbin/visudo -cf %s
```

Não desativar login root/senha antes de comprovar a chave em outra sessão. A
rotação deve manter chave antiga e nova por uma janela curta e auditável.

## 11. Playbook de deploy serial

Exemplo de alto nível:

```yaml
- name: Implantar CDN sem indisponibilidade global
  hosts: cdn_edges
  become: true
  serial: 1
  any_errors_fatal: true
  max_fail_percentage: 0
  roles:
    - edge_base
    - cdn_runtime
    - cdn_tenants
    - cdn_tls
    - cdn_monitoring
  pre_tasks:
    - name: Retirar edge do DNS/LB
      ansible.builtin.include_role:
        name: edge_health
        tasks_from: drain

  tasks:
    - name: Validar Nginx antes do reload
      ansible.builtin.command: nginx -t
      changed_when: false

    - name: Recarregar Nginx
      ansible.builtin.systemd_service:
        name: nginx
        state: reloaded

    - name: Validar health local
      ansible.builtin.uri:
        url: https://127.0.0.1/edge-health
        headers: {Host: "{{ canonical_health_host }}"}
        validate_certs: false
        status_code: 200
      register: edge_health
      retries: 5
      delay: 3
      until: edge_health.status == 200

  post_tasks:
    - name: Recolocar edge no DNS/LB
      ansible.builtin.include_role:
        name: edge_health
        tasks_from: undrain
```

`serial: 1` garante atualização de uma edge por vez. `--check --diff` é útil
antes da implantação, mas diff deve ser desabilitado em templates sensíveis.

## 12. Templates e artefatos

Ansible não deve reconstruir a lógica Python em Jinja. A opção segura é:

1. painel/orquestrador exporta snapshot canônico;
2. um comando versionado do projeto renderiza todos os tenants localmente;
3. testes validam os artefatos;
4. Ansible distribui o pacote/digest já aprovado;
5. a role valida novamente na edge.

Manifesto de release:

```json
{
  "release": "2026.08.28.1",
  "generation": 42,
  "config_sha256": "...",
  "artifact_sha256": "...",
  "tenant_count": 3,
  "required_schema": 1
}
```

Cada edge grava `current.json` e mantém pelo menos duas releases anteriores em
`/opt/cdnmnus/releases/`. O symlink `current` muda atomicamente.

## 13. Jobs e feedback no painel

API proposta:

```text
POST /api/edges/bootstrap
POST /api/deployments
GET  /api/deployments/{id}
GET  /api/deployments/{id}/events
POST /api/edges/{id}/drain
POST /api/deployments/{id}/rollback
```

Estados:

```text
queued → validating → canary → rolling → verifying → succeeded
                                      └→ failed → rolled_back
```

O worker deve usar `ansible-runner` ou callback JSON e aceitar somente IDs de
playbooks cadastrados. Nunca aceitar comando, `extra_vars` arbitrárias ou caminho
de playbook vindo diretamente do navegador.

Evento sanitizado:

```json
{
  "deployment_id": "dep_01",
  "edge_id": "edge-170",
  "task": "validate_nginx",
  "state": "ok",
  "changed": false,
  "timestamp": "2026-08-28T08:00:00Z"
}
```

Remover de eventos `stdout`, argumentos, diffs e resultados que contenham URI,
Authorization, cookies, tokens ou conteúdo de arquivos protegidos.

## 14. TLS e DNS sem proxy

Todos os registros A de `xuiN.cdn.phpd77.com` apontam para as edges saudáveis.
Cada edge precisa do certificado de todos os hostnames que atende.

Para três edges, DNS-01 é preferível: a emissão não depende de qual IP recebeu
HTTP-01. Credencial da API DNS fica no secret manager/Vault, nunca na edge após
uso, quando o cliente ACME permitir.

O Ansible não deve adicionar uma edge ao DNS antes de:

- certificado válido;
- configuração/digest corretos;
- `nginx -t` aprovado;
- broker ativo;
- `/edge-health` 200 em cinco verificações;
- teste de hostname desconhecido falhando fechado.

Round-robin com vários registros A distribui respostas, mas não é failover. O
controlador DNS precisa remover IP após falhas e respeitar TTL/cache dos clientes.

## 15. Rollout e rollback

### Rollout

1. validar schema, DNS e colisão de hostnames;
2. gerar artefato e digest;
3. `ansible-playbook --check --diff --limit edge-canary`;
4. drenar Edge 170 como canário;
5. backup de configuração/certificados/metadados;
6. instalar em release nova;
7. executar compile, `nginx -t`, testes broker e health;
8. recolocar canário e observar;
9. repetir com `serial: 1` nas demais edges;
10. declarar geração ativa somente quando todas reportarem sucesso.

### Rollback

Falha em qualquer gate:

- manter edge fora do DNS;
- repontar `current` para release anterior;
- restaurar snapshot/configuração do broker;
- reiniciar broker, testar Nginx e recarregar;
- verificar health cinco vezes;
- recolocar somente se saudável;
- marcar deployment como `rolled_back`, sem apagar evidência sanitizada.

Rollback não deve fazer downgrade do banco sem migração reversa comprovada.

## 16. Testes obrigatórios

### Unitários

- hostname duplicado entre tenants;
- cache/singleflight isolado por tenant;
- `X-CDN-Tenant` inválido;
- redirect cruzando do XUI 1 para allowlist do XUI 2;
- geração determinística e digests;
- sanitização de eventos Ansible.

### Integração

- duas origens falsas com playlists diferentes no mesmo Nginx;
- aliases resolvendo apenas o tenant correto;
- HLS/VOD/Range/refresh por tenant;
- host desconhecido sem alcançar upstream;
- remoção de um tenant sem reload inválido dos demais;
- broker reiniciando e reconstruindo cache por tenant.

### Multi-edge

- canário e `serial: 1`;
- uma edge falha no `nginx -t` e recebe rollback;
- outra edge continua servindo durante deploy;
- retirada DNS após três falhas e retorno após cinco sucessos;
- divergência de digest impede publicação DNS;
- soak de seis horas por tenant e teste de desastre.

## 17. Migração em fases

### Fase 0 — segurança do controle

- instalar `ansible-core`/runner em ambiente dedicado;
- criar usuário do orquestrador e diretórios 0700;
- implementar fingerprint obrigatória e chave por edge;
- cadastrar as três edges sem ainda alterar tráfego.

### Fase 1 — Ansible para o estado atual

- transformar instalação/update/monitor em roles;
- implantar o único XUI atual nas três edges;
- provar idempotência: segunda execução com `changed=0`;
- habilitar health e rollout serial.

### Fase 2 — renderização multi-tenant

- normalizar banco relacional;
- extrair `render_tenant()` puro e testável;
- adicionar tenant ao broker e às chaves de cache;
- gerar um include por XUI;
- migrar perfil atual para `xui1` sem alterar URLs.

### Fase 3 — vários XUIs

- cadastrar `xui2`, emitir TLS e implantar ainda sem DNS;
- testar via `--resolve` em cada edge;
- publicar registros A somente após gates;
- repetir para novos XUIs.

### Fase 4 — DNS failover e operação

- integrar provedor DNS autoritativo;
- drain/undrain automatizado;
- alertas e SLO;
- rotação de chaves, disaster recovery e auditorias periódicas.

## 18. Critérios de aceite

- [ ] três edges autenticam exclusivamente por chaves individuais;
- [ ] senha de bootstrap não existe em banco, arquivo, processo ou journal;
- [ ] host key é pinada e divergência bloqueia deploy;
- [ ] segunda execução Ansible é idempotente;
- [ ] pelo menos dois XUIs funcionam simultaneamente e isolados;
- [ ] nenhuma resposta revela origem, LB, token ou header interno;
- [ ] deploy ocorre uma edge por vez com drain e rollback;
- [ ] edge divergente não volta ao DNS;
- [ ] todas as edges reportam o mesmo release/config digest;
- [ ] certificados cobrem todos os hostnames em todas as edges;
- [ ] métricas/eventos não contêm segredo ou URI credenciada;
- [ ] live 6h e VOD >3h passam em pelo menos duas edges;
- [ ] queda completa de uma edge não interrompe a reprodução além da janela
      esperada de DNS/reconexão do player.

## 19. Decisões que precisam ser tomadas antes de codificar

1. Provedor/API DNS autoritativo para health failover sem nuvem laranja.
2. Subdomínio canônico e aliases de cada XUI.
3. Porta/fingerprint e acesso inicial das novas edges.
4. Secret manager: Vault é o mínimo; serviço externo/KMS é preferível.
5. SQLite no controle único ou PostgreSQL antes de introduzir workers paralelos.
6. Certificados compartilhados com DNS-01 ou emissão independente por edge.

## 20. Referências oficiais

- Inventário, grupos e variáveis: https://docs.ansible.com/ansible/latest/user_guide/intro_inventory.html
- Rolling update com `serial`: https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_strategies.html
- Check/diff e risco de segredo em diff: https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_checkmode.html
- Ansible Vault: https://docs.ansible.com/projects/ansible/latest/vault_guide/vault.html
- Health HTTP com `ansible.builtin.uri`: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/uri_module.html
- Drain/wait: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/wait_for_module.html

## Conclusão

A Opção B se encaixa porque o projeto já possui um data plane funcional e
precisa transformar implantação local imperativa em gerenciamento de estado de
uma frota. O primeiro marco não deve ser “criar todos os recursos no painel”,
mas tornar o deploy atual idempotente em três edges. Depois disso, multi-XUI é
introduzido com isolamento por hostname, tenant, broker e cache, sem colocar o
Ansible no caminho crítico do play.
