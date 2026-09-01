# Ciclo de Vida do TLS por Tenant
Escopo: emissão ACME DNS-01, validação SAN, distribuição e health por tenant.
Não altera DNS de tráfego, VIP, leases, fencing, cache ou rotas de mídia.

## Fluxo

```text
queued -> running -> succeeded | failed
pending -> valid | failed
```

O worker reserva um job por vez. Jobs `running` antigos são recuperados pelo
mesmo `claim_tls_job()` usando timeout explícito.

## Schema

### `tls_jobs`

Campos:

- `id`
- `tenant_id`
- `state` (`queued`, `running`, `succeeded`, `failed`)
- `attempts`
- `error`
- `created_at`
- `started_at`
- `finished_at`

Regras:

- só existe um job `queued` ou `running` por tenant;
- `attempts` sobe quando o job entra em `running`;
- jobs abandonados são reenfileirados até o limite de tentativas;
- ao exceder o limite, o job vira `failed`.

### `tls_events`

Campos:

- `id`
- `tenant_id`
- `event_type`
- `operator`
- `reason`
- `payload_sanitized`
- `created_at`

Eventos usados aqui:

- `tls_status_changed`
- `tls_job_timeout`

## ACME

Pré-requisitos:

- `certbot` disponível no control-plane;
- plugin `dns-cloudflare` instalado;
- credencial em `/etc/cdnmnus/secrets/cloudflare_acme.ini`;
- arquivo `0600` e `root:root`;
- o helper deve ser o único caminho privilegiado:
  `/opt/cdnmnus/scripts/cdnmnus-acme-helper`.

O helper:

- aceita apenas `--action`, `--canonical`, `--sans` e `--tenant-id`;
- rejeita flags extras e argumentos inválidos;
- não usa `eval`;
- não imprime certificado, chave, token ou saída integral do Certbot;
- valida `live/` e `archive/` do lineage;
- valida leitura como `cdn-admin` com `sudo -u cdn-admin test -r`.

## Permissões da lineage

Objetivo: permitir leitura somente da lineage do tenant solicitado, sem abrir
`/etc/letsencrypt` inteiro para `cdn-admin`.

Permissões esperadas depois da emissão:

- `/etc/letsencrypt/live/<canonical>`: `root:cdn-admin`, `0710`
- `/etc/letsencrypt/archive/<canonical>`: `root:cdn-admin`, `0710`
- `fullchain.pem` e `privkey.pem`: `root:cdn-admin`, `0640`

Rollback de permissões:

```bash
chown root:root /etc/letsencrypt/live/<canonical> /etc/letsencrypt/archive/<canonical>
chmod 0700 /etc/letsencrypt/live/<canonical> /etc/letsencrypt/archive/<canonical>
chmod 0600 /etc/letsencrypt/archive/<canonical>/{fullchain,privkey}.pem
```

## Sudoers

Permitido apenas:

```text
cdn-admin ALL=(root) NOPASSWD: /opt/cdnmnus/scripts/cdnmnus-acme-helper *
```

Regras:

- não conceder `sudo certbot`;
- não conceder `sudo systemctl`;
- instalar com Ansible e validar com `visudo -cf`.

## Health Gate

O health usa `health_host` explícito do tenant. Não volte para
`cdn.phpd77.com` por fallback.

Classificações esperadas:

- conexão recusada;
- timeout;
- SAN/TLS verification failed;
- HTTP 421;
- HTTP 4xx/5xx.

Regras:

- usar `--resolve` com SNI correto;
- exigir `HTTP 200`;
- não aceitar `--insecure`.

## Estado transacional

- falha ACME -> somente o tenant alvo vira `failed`;
- SAN ausente -> somente o tenant alvo vira `failed`;
- distribuição falha -> somente o tenant alvo vira `failed`;
- health falha -> somente o tenant alvo vira `failed`;
- sucesso total -> todos os hosts do tenant viram `valid`;
- outros tenants permanecem inalterados.

## Auditoria

```bash
sqlite3 /var/lib/cdnmnus-admin/admin.db \
  'select id,tenant_id,state,attempts,error,created_at,started_at,finished_at from tls_jobs order by created_at desc;'
sqlite3 /var/lib/cdnmnus-admin/admin.db \
  'select tenant_id,event_type,operator,reason,created_at from tls_events order by created_at desc;'
sudo -u cdn-admin test -r /etc/letsencrypt/live/<canonical>/fullchain.pem
sudo -u cdn-admin test -r /etc/letsencrypt/live/<canonical>/privkey.pem
```

Não registrar chave privada, token Cloudflare, credencial ACME ou conteúdo
integral do Certbot nos logs estruturados.

## Recuperação de jobs abandonados

`claim_tls_job()` trata jobs `running` cujo `started_at` passou do timeout
configurado. O comportamento é:

- registrar `tls_job_timeout` em `tls_events`;
- reenfileirar o job se ainda houver tentativas;
- marcar `failed` ao atingir o máximo.

Isso não afeta outros tenants.

## Critérios de aceite

1. ACME emite a lineage correta.
2. SAN contém todos os hosts publicados.
3. `distribute_tls.sh` recebe somente a lineage real.
4. `nginx -t` remoto passa.
5. `/edge-health` retorna `200` nas edges alvo.
6. `tls_status` vira `valid` apenas após o gate completo.
7. Segredos não aparecem em stdout, stderr, `tls_jobs.error` ou eventos.

## Laboratório

Ordem recomendada:

1. teste local com mocks;
2. teste do helper com credencial fictícia;
3. ACME em staging;
4. tenant de laboratório;
5. distribuição em uma edge canária;
6. health da edge canária;
7. distribuição na segunda edge;
8. health nas duas edges;
9. validar `tls_status=valid`.

## Troubleshooting

- `SAN ausente`: o certificado emitido não contém o host publicado;
- `SNI/host incorreto`: o health bateu no vhost errado;
- `HTTP 421`: o hostname não pertence ao vhost TLS;
- `Exit code 60`: falha de verificação TLS/SAN;
- `connection refused`: edge indisponível ou porta fechada;
- `timeout`: rede ou processo remoto travado.
