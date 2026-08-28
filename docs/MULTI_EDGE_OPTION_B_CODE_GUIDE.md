# Guia de código: multi-edge — Opção B, Ansible desacoplado

Status: plano de implementação, conferido contra o código do repositório em
28/08/2026. Os blocos marcados como **proposto** são exemplos de desenho e ainda
não existem. Este guia complementa a
[`especificação de arquitetura`](ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md).

## 1. Resultado esperado e fronteira da Opção B

A implementação termina com quatro responsabilidades independentes:

| Componente | Responsabilidade | Pode falhar sem parar mídia? |
| --- | --- | --- |
| painel | CRUD de tenants/edges e criação de deployment | sim |
| worker/orquestrador | materializar jobs e chamar Ansible | sim |
| Ansible control node | convergir releases nas edges via SSH | sim |
| edge | Nginx, broker, cache e health do data plane | não para aquela edge |

O caminho de uma reprodução permanece `cliente → edge → XUI/LB/VOD`. Não há
consulta ao painel, banco, worker ou Ansible por segmento.

“Desacoplado” não significa apenas iniciar `ansible-playbook` em background. O
contrato correto é persistente:

```text
Painel --INSERT deployment/job--> banco/fila
Worker --claim atômico--> snapshot imutável --> runner
Runner --SSH--> edge canário --> edge N ...
Worker --eventos sanitizados--> banco
Painel --somente leitura--> status/eventos
```

## 2. Mapeamento confirmado do código atual

### 2.1 Painel e persistência

**Atual — `panel/panel.py`:**

- `initialize_db()` cria apenas `users` e `settings`;
- `stored_profiles()` conserva vários perfis dentro de um JSON;
- `save_profile()` sempre define o perfil salvo como `active_profile_id`;
- `apply_config()` aplica imediatamente na máquina local;
- o serviço do painel roda como `root` para escrever Nginx e chamar systemd.

Trecho atual:

```python
normalized.update({
    "profiles": profiles,
    "active_profile_id": profile_id,
})
apply_config(normalized)
```

Isso é um catálogo de perfis com **um runtime ativo**, não multi-XUI. Salvar o
XUI 2 reescreve o único include e a única configuração do broker usados pelo
XUI 1.

**Proposto — painel grava intenção, não executa deploy:**

```python
def request_deployment(actor: str, tenant_ids: list[str], edge_ids: list[str]) -> str:
    snapshot = build_canonical_snapshot(tenant_ids, edge_ids)
    digest = sha256(canonical_json(snapshot)).hexdigest()
    deployment_id = new_id("dep")
    with db_connect() as db:
        db.execute(
            "INSERT INTO deployments(id,requested_by,desired_version,"
            "config_digest,state,snapshot_json) VALUES(?,?,?,?,?,?)",
            (deployment_id, actor, APP_VERSION, digest, "queued",
             canonical_json(snapshot).decode()),
        )
    return deployment_id
```

Não deve existir `subprocess.run(["ansible-playbook", ...])` no handler HTTP.

### 2.2 Renderização Nginx

**Atual — `render_include(config)`:**

```python
upstream_name = "cdnmnus_dynamic_backend"
...
proxy_cache_path /var/cache/nginx/cdnmnus-hls ...
upstream cdnmnus_token_broker {
    server 127.0.0.1:9091;
}
server_name {config["public_host"]};
```

Os nomes globais colidem se dois resultados forem incluídos juntos. Além disso,
há destinos VOD fixos no render atual. Antes de distribuir com Ansible, o
renderizador precisa receber tenant e produzir nomes isolados.

**Proposto — função pura por tenant:**

```python
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

def render_tenant(tenant: dict[str, object]) -> RenderedTenant:
    tenant_id = validate_tenant_id(str(tenant["id"]))
    prefix = f"cdn_{tenant_id}"
    return RenderedTenant(
        relative_path=f"tenants/{tenant_id}.conf",
        content=render_nginx_server(tenant, prefix=prefix),
        broker_config=render_broker_tenant(tenant),
    )
```

Exemplo de saída:

```nginx
proxy_cache_path /var/cache/nginx/cdnmnus/xui1
    keys_zone=cache_xui1:32m max_size=2g inactive=2m;

upstream origin_xui1 {
    server 192.0.2.10:80;
    keepalive 32;
}

upstream broker_xui1 {
    server 127.0.0.1:9091;
    keepalive 16;
}

server {
    listen 443 ssl;
    server_name xui1.cdn.example;

    location ^~ /hls/ {
        proxy_pass http://broker_xui1;
        proxy_set_header X-CDN-Tenant xui1;
        proxy_set_header X-CDN-Public-Host $host;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
    }
}
```

O header de tenant é aceitável somente porque o broker fica em loopback/socket
local. O broker deve validar `tenant_id + public_host` contra o snapshot; não
deve confiar no header isoladamente.

### 2.3 Aplicação e rollback local

**Atual — `apply_config()`:** salva o banco, escreve
`/etc/cdnmnus/token-broker.json`, escreve
`/etc/nginx/conf.d/99-cdnmnus-upstream.conf`, roda `nginx -t`, recarrega Nginx e
tenta restaurar os três itens em caso de erro. Essa lógica é útil, mas está
acoplada ao request web e à edge local.

**Proposto — dividir em três camadas:**

```python
# biblioteca sem root, usada no build do artefato
artifact = build_release(snapshot)
validate_artifact(artifact)

# worker: somente escolhe operação e parâmetros previamente cadastrados
runner.run(playbook="deploy-edge.yml", artifact_id=artifact.id,
           limit=approved_edge_ids)

# edge: Ansible instala release e muda symlink somente após validações
/opt/cdnmnus/releases/2026.08.28.1/
/opt/cdnmnus/current -> releases/2026.08.28.1
```

O rollback passa a trocar o symlink para uma release íntegra anterior. Não deve
remontar configuração a partir de eventos ou da versão corrente do banco.

### 2.4 Token broker

**Atual — `panel/token_broker.py`:**

```python
self.config: dict[str, object] = {}
key = hashlib.sha256(uri.encode()).hexdigest()
internal = query_vod(uri, config) if vod else query_origin(uri, config)
```

Há uma configuração global. Como a chave usa somente URI, credenciais/caminhos
iguais em dois tenants poderiam compartilhar uma decisão de rota incorreta.

**Proposto:**

```python
def resolve(self, tenant_id: str, public_host: str, uri: str,
            force: bool = False, media_kind: str = "hls") -> str:
    tenant = self.snapshot.tenant_for(tenant_id, public_host)
    key_material = f"{tenant_id}\0{media_kind}\0{uri}".encode()
    key = hashlib.sha256(key_material).hexdigest()
    return self.resolve_for_tenant(tenant, key, uri, force, media_kind)
```

Snapshot proposto:

```json
{
  "schema_version": 1,
  "generation": 42,
  "tenants": {
    "xui1": {
      "public_hosts": ["xui1.cdn.example"],
      "origin": {"host": "origin-one.example", "port": 80},
      "load_balancers": ["lb-one.example"],
      "vod_hosts": ["vod-one.example"],
      "ttl_seconds": 15
    }
  }
}
```

### 2.5 Instalador e atualizador

**Atual:** `install.sh` instala pacotes, configura o SO, copia os runtimes e
reinicia serviços. `scripts/update.sh` faz `git fetch/merge` diretamente na
máquina, copia arquivos e recarrega tudo. Ambos assumem uma única máquina.

**Proposto:** converter comportamento, não executar os scripts remotamente de
dentro de uma task genérica.

| Código atual | Role alvo | Mudança |
| --- | --- | --- |
| `install_packages()` | `edge_base` | `apt` idempotente |
| `run_tuning()` | `edge_base` | template sysctl + handler |
| `run_firewall()` | `edge_base` | regras por inventário |
| `install_panel()` | nenhuma role de edge | painel fica no control plane |
| cópia de Python/units | `cdn_runtime` | artefato versionado |
| `deploy_nginx()` | `cdn_tenants` | release, teste, symlink, reload |
| backup de `update.sh` | `cdn_runtime` | retenção de releases |

## 3. Estrutura concreta a criar

```text
cdnmnus/
├── cdnmnus_config/
│   ├── schema.py
│   ├── canonical.py
│   ├── renderer.py
│   └── artifact.py
├── orchestrator/
│   ├── worker.py
│   ├── operations.py
│   ├── event_filter.py
│   └── cdnmnus-orchestrator.service
├── ansible/
│   ├── ansible.cfg
│   ├── inventories/production/{hosts.yml,group_vars,host_vars}/
│   ├── playbooks/{bootstrap-edge,deploy-edge,rollback-edge}.yml
│   └── roles/{edge_base,cdn_runtime,cdn_tenants,cdn_tls,cdn_monitoring,edge_health}/
└── tests/
    ├── renderer_multi_tenant_test.py
    ├── broker_multi_tenant_test.py
    ├── artifact_test.py
    └── orchestrator_test.py
```

O pacote `cdnmnus_config` é compartilhável, mas o painel não precisa de acesso
SSH. O diretório `ansible/` e chaves privadas pertencem ao usuário do worker.

## 4. Contrato de snapshot e artefato

O snapshot deve ser autocontido, canônico e imutável após entrar na fila.
Ordenar chaves e listas cuja ordem não tenha semântica antes do hash:

```python
def canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        normalize_order(value), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
```

Layout do artefato:

```text
release-2026.08.28.1.tar.gz
├── manifest.json
├── snapshot.json
├── nginx/tenants/xui1.conf
├── nginx/tenants/xui2.conf
├── broker/tenants.json
├── runtime/panel-free edge files...
└── SHA256SUMS
```

`manifest.json` informa schema, geração, versão, quantidade de tenants e hashes.
A edge rejeita schema incompatível, hash divergente, hostname duplicado ou
artefato fora da allowlist do deployment.

## 5. Contrato painel → worker

Tabelas mínimas adicionais:

```sql
CREATE TABLE deployments (
  id TEXT PRIMARY KEY,
  requested_by TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('deploy','rollback','drain','undrain')),
  desired_version TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('queued','claimed','validating','canary','rolling','verifying',
     'succeeded','failed','rolled_back')),
  claimed_by TEXT,
  claimed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Claim atômico conceitual (SQLite com um único worker inicial):

```sql
BEGIN IMMEDIATE;
UPDATE deployments
SET state='claimed', claimed_by=:worker, claimed_at=CURRENT_TIMESTAMP
WHERE id=(SELECT id FROM deployments WHERE state='queued'
          ORDER BY created_at LIMIT 1)
  AND state='queued';
COMMIT;
```

Para vários workers ou alta disponibilidade do control plane, migrar a fila
para PostgreSQL e usar `FOR UPDATE SKIP LOCKED`. Não introduzir workers paralelos
sobre SQLite sem uma estratégia explícita de locking.

Allowlist do worker:

```python
OPERATIONS = {
    "deploy": Operation("playbooks/deploy-edge.yml", allowed_vars={"deployment_id"}),
    "rollback": Operation("playbooks/rollback-edge.yml", allowed_vars={"deployment_id"}),
    "drain": Operation("playbooks/drain-edge.yml", allowed_vars={"edge_id"}),
}
```

O navegador fornece IDs; nunca fornece shell, caminho, inventário, `--limit` ou
`extra_vars` arbitrárias.

## 6. Exemplo Ansible completo do primeiro marco

Inventário:

```yaml
all:
  children:
    cdn_edges:
      hosts:
        edge-111:
          ansible_host: 143.14.168.111
          edge_region: primary
        edge-170:
          ansible_host: 143.14.168.170
          edge_region: secondary-a
        edge-168:
          ansible_host: 143.14.168.168
          edge_region: secondary-b
      vars:
        ansible_user: cdn-deploy
        ansible_become: true
```

Playbook de rollout:

```yaml
- name: Rollout serial das edges
  hosts: cdn_edges
  become: true
  serial: 1
  any_errors_fatal: true
  max_fail_percentage: 0
  vars:
    release_dir: "/opt/cdnmnus/releases/{{ release_id }}"
  pre_tasks:
    - name: Drenar no balanceador DNS
      ansible.builtin.include_role:
        name: edge_health
        tasks_from: drain

  roles:
    - cdn_runtime
    - cdn_tenants

  tasks:
    - name: Validar configuração candidata
      ansible.builtin.command: >-
        nginx -t -c {{ release_dir }}/nginx/nginx.conf
      changed_when: false

    - name: Ativar release verificada
      ansible.builtin.file:
        src: "{{ release_dir }}"
        dest: /opt/cdnmnus/current
        state: link
        force: true
      notify: reload nginx

    - name: Executar handlers antes do health
      ansible.builtin.meta: flush_handlers

    - name: Confirmar health composto
      ansible.builtin.uri:
        url: https://127.0.0.1/edge-health
        headers:
          Host: "{{ canonical_health_host }}"
        validate_certs: false
        status_code: 200
      register: health
      retries: 5
      delay: 3
      until: health.status == 200

  post_tasks:
    - name: Recolocar edge saudável
      ansible.builtin.include_role:
        name: edge_health
        tasks_from: undrain
```

Nota: `post_tasks` não é garantia de execução após qualquer falha. O play real
deve usar `block/rescue/always`: no `rescue`, repontar a release anterior,
validar, recarregar e manter a edge drenada se o rollback também falhar.

## 7. Bootstrap sem guardar senha

O bootstrap é uma operação separada do deploy recorrente:

1. obter fingerprint por canal confiável e piná-la em `known_hosts`;
2. solicitar a senha inicial interativamente ou via secret broker efêmero;
3. criar `cdn-deploy` e uma chave exclusiva daquela edge;
4. testar uma segunda conexão por chave;
5. somente então endurecer SSH;
6. apagar o segredo de memória/arquivo temporário e não gravar stdout bruto.

Não usar `ansible_password` em inventário, URL, argumento de processo ou tabela.
Ansible Vault é aceitável para segredos duráveis, não para conservar a senha
root entregue apenas para bootstrap.

## 8. Estados e gates operacionais

```text
pending -> bootstrapping -> ready -> draining -> deploying -> verifying -> ready
                                      |             |
                                      +-> failed <-+
```

Uma edge só entra no DNS quando todos os gates passarem:

- host key confere com a fingerprint aprovada;
- release e config digest conferem;
- certificado cobre os hostnames daquela edge;
- `nginx -t` passa;
- broker carrega o snapshot;
- `/edge-health` retorna 200 cinco vezes;
- hostname desconhecido falha fechado;
- teste `--resolve` de cada tenant passa.

Round-robin DNS sozinho não garante retirada de edge falha. A integração de
drain/undrain exige API autoritativa ou balanceador com health check; até essa
decisão existir, os playbooks devem parar antes de simular que houve failover.

## 9. Sequência de implementação em pull requests

### PR 1 — caracterizar o comportamento atual

- testes de golden file para `render_include()`;
- teste provando que dois perfis alternam o runtime ativo;
- testes de rollback de `apply_config()`;
- nenhum comportamento de produção alterado.

### PR 2 — extrair renderizador e snapshot

- criar `cdnmnus_config/`;
- schema multi-tenant e JSON canônico;
- `render_tenant()` com nomes/cache isolados;
- golden files de dois XUIs e rejeição de hostname duplicado.

### PR 3 — broker multi-tenant

- carregar snapshot por geração;
- validar par tenant/hostname;
- chave de cache com tenant e tipo de mídia;
- teste de redirect cruzado XUI 1 → allowlist XUI 2 falhando fechado.

### PR 4 — artefato e runtime de edge

- manifesto e hashes;
- diretórios de release e symlink atômico;
- unit do broker sem painel na edge;
- rollback local testável.

### PR 5 — Ansible desacoplado, ainda manual

- inventário, bootstrap e roles;
- deploy canário/serial com `block/rescue`;
- segunda execução com `changed=0`;
- operar primeiro com um tenant nas três edges.

### PR 6 — fila e worker

- tabelas de edges/deployments;
- worker sob usuário dedicado;
- allowlist de operações;
- callback sanitizado e API de consulta.

### PR 7 — DNS/TLS e vários XUIs

- DNS-01 ou estratégia TLS escolhida;
- drain/undrain real;
- adicionar XUI 2 sem DNS, testar via `curl --resolve`;
- publicar somente após os gates.

## 10. Matriz de testes e aceite

| Área | Teste | Resultado obrigatório |
| --- | --- | --- |
| render | mesmo snapshot duas vezes | bytes e SHA-256 iguais |
| isolamento | URI igual em xui1/xui2 | cache/rota distintos |
| segurança | tenant/header divergente | 4xx/5xx, sem upstream |
| deploy | segunda execução | `changed=0` |
| rollout | `nginx -t` falha no canário | rollback; demais intactas |
| disponibilidade | Nginx para em uma edge | edge retirada; outra serve |
| consistência | digest diferente | edge não volta ao DNS |
| segredo | evento contém URI/token | evento descartado/redigido |

Definição de pronto da Opção B:

- painel não roda como root e não chama systemd/Ansible;
- worker não atende porta pública e aceita somente operações cadastradas;
- edges não têm painel, repositório Git ou chave privada do control node;
- três edges convergem para o mesmo digest com rollout serial;
- dois XUIs coexistem por hostname, broker, allowlist e cache isolados;
- queda do control plane não interrompe streams já atendidos pelas edges;
- rollback e drain foram exercitados, não apenas documentados.

## 11. O que não está implementado hoje

Na data deste mapeamento, não existem no repositório `ansible/`,
`orchestrator/`, tabelas relacionais de tenants/edges/deployments, renderização
multi-tenant nem broker multi-tenant. O endpoint `/edge-health` já existe, mas
somente comprova Nginx + carregamento da configuração local; ele não prova por
si só failover DNS, igualdade de digest ou alcance real de cada origem.

Portanto, este documento autoriza e orienta a implementação, mas não deve ser
usado como evidência de que a plataforma já oferece multi-edge em produção.
