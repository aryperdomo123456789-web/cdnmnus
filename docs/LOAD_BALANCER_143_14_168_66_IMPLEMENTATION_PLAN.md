# Plano de implementação — Load Balancer próprio `143.14.168.66`

**Status:** planejamento técnico; não publicar o IP no DNS antes dos gates de
homologação. Este documento cruza o plano solicitado com o código existente em
28/08/2026.

## 1. Estado real do código

| Área | Evidência | Estado |
|---|---|---|
| Cadastro de edges | `web/app.py`, `core/db.py` | Existe bootstrap SSH e estados `ready`, `draining`, `failed`. |
| Deploy de edge | `ansible/playbooks/deploy-and-activate-edge.yml` | Existe deploy serial; é perfil de edge, não de LB. |
| Runtime | `ansible/roles/cdn_runtime`, `core/render_tenants.py` | Gera Nginx, broker, TLS e tenants. |
| Health | `POST /api/edges/{id}/health` | Teste manual; não há controlador contínuo. |
| DNS | `Database.sync_dns_matrix()` | Calcula matriz local; não altera Cloudflare/provedor. |
| Orquestração | `orchestrator/worker.py` | Executa Ansible; não gerencia pool de LB. |
| VOD | `panel/token_broker.py` | Redirect e `Range` seguros, condicionados a `vod_hosts`. |
| Load Balancer | nenhum role/config dedicado | **Não implementado.** |
| Inventário | `ansible/inventories/production/hosts.yml` | Atualmente contém apenas `lb011` (`143.14.168.168`). |

O projeto tem uma base de controle e runtime de edge, mas ainda precisa de
perfil LB, controlador de health, backup/restore e promoção edge→LB.

## 2. Arquitetura alvo

```text
cliente -> cdn.phpd77.com -> LB 143.14.168.66
                                  |-> edge 143.14.168.111
                                  |-> edge 143.14.168.168
                                  `-> edge 143.14.168.170
```

Na virada, o DNS deve conter apenas `143.14.168.66`; as edges devem aceitar
80/443 somente do LB. Os três A records atuais são round-robin DNS, não
failover. O LB deve preservar Host/SNI, Range, respostas 206, timeouts e
headers do broker.

## 3. Componentes que ainda devem ser implementados

### 3.1 Perfil Ansible de LB

Criar `ansible/roles/load_balancer/` e
`ansible/playbooks/deploy-load-balancer.yml`, separados do playbook de edge.
Instalar HAProxy (preferido para pools e health checks) ou Nginx, com:

- backends, pesos e limites de conexão;
- health checks HTTPS usando `cdn.phpd77.com` e `/edge-health`;
- timeouts, keepalive e logs sem URIs credenciadas;
- validação (`haproxy -c` ou `nginx -t`) antes de reload;
- métricas e serviço systemd próprio.

### 3.2 Controlador de health/failover

Serviço a cada 10 segundos: TCP, TLS/SNI, `/edge-health` HTTP 200, latência e
timeout. Após 3 falhas consecutivas, marcar `down` e retirar do pool; após 5
sucessos, marcar `ready` e reinserir. Usar estados `unknown`, `suspect`, `down`,
`draining` e histerese contra flapping. Toda alteração deve ser atômica,
validada e auditável.

### 3.3 Painel e banco

Adicionar entidades para `load_balancers`, `lb_backends`, políticas de health,
manifestos de backup e eventos de promoção/rollback. Criar no `mago-cdn` as
telas **Load Balancers**, **Backends**, **Health & Failover**, **Backup/Restore**
e **Promoção**. A tela existente **DNS & Failover** deve dizer que é matriz
local enquanto não houver API do provedor.

### 3.4 Fontes VOD

O tenant atual possui origem XUI e LB, mas ainda precisa dos upstreams VOD
allowlisted:

```text
servicedovod.lat:80
zjo.lat:80
```

O cadastro deve gerar `kind='vod'`, nova release e deploy nas edges. Redirects
fora da allowlist devem continuar falhando fechado.

## 4. Inventário proposto

```yaml
all:
  children:
    load_balancers:
      hosts:
        lb-primary:
          ansible_host: 143.14.168.66
    cdn_edges:
      hosts:
        edge-main: { ansible_host: 143.14.168.111 }
        edge-168:  { ansible_host: 143.14.168.168 }
        edge-170:  { ansible_host: 143.14.168.170 }
```

Confirmar usuário, porta, chave e fingerprint antes de aplicar. Senhas nunca
devem ser gravadas no YAML, Git ou SQLite.

## 5. Backup e continuidade

Backup criptografado e externo deve incluir `/etc/haproxy` ou `/etc/nginx`,
`/etc/cdnmnus`, banco admin, releases, certificados, known_hosts, firewall,
sysctl, versão ativa, inventário, hashes e manifesto. Excluir senhas e tokens em
claro.

```text
snapshot de configuração: 15 min (RPO máximo)
backup completo: diário
retenção local: 7 dias
retenção externa: 30 dias
teste de restauração: mensal e antes de release crítica
```

Restaurar primeiro em diretório temporário, validar permissões e hashes, testar
configuração e ativar atomicamente. O Git guarda código e playbooks, não segredos
nem banco de produção.

## 6. LB standby e promoção

Preparar um segundo LB com o mesmo perfil. VRRP/IP flutuante só funciona se o
provedor fornecer rede Layer 2; em VPS distintas, usar DNS/API ou serviço do
provedor, aceitando o atraso de TTL/cache.

Uma edge pode assumir função de LB somente por runbook:

```text
drain da edge -> instalar role load_balancer -> restaurar backup
-> validar TLS/backends/health -> publicar -> monitorar
```

Durante a promoção ela deixa de servir mídia. O rollback reinstala a role edge,
restaura o runtime e só então reinsere o backend. Promoção automática deve
exigir política/quorum, não ocorrer em qualquer falha isolada.

## 7. Segurança

- firewall das edges permite mídia somente do LB;
- SSH administrativo restrito e autenticado por Ed25519;
- tokens/chaves de assinatura idênticos entre edges, fora do Git;
- NTP sincronizado;
- `Location`, `Server` e URIs credenciadas ocultos dos logs públicos;
- limites de redirects, Range, conexões e headers;
- testes de bypass direto à origem e às edges devem falhar.

## 8. Execução por gates

### Gate A — preparação

1. Cadastrar e validar as três edges no painel.
2. Confirmar `ready`, release e TLS em cada uma.
3. Cadastrar as fontes VOD allowlisted.
4. Preparar `143.14.168.66` sem alterar DNS.

### Gate B — LB em homologação

1. Aplicar o perfil LB e registrar os três backends.
2. Testar cada backend e o LB com `curl --resolve`.
3. Testar HLS, filme, série, `Range`, seek e refresh.
4. Testar carga de 500 conexões controladas.
5. Derrubar cada backend e confirmar remoção/reinserção automática.

### Gate C — desastre

1. Restaurar backup em VPS limpa.
2. Promover LB standby.
3. Promover uma edge a LB e executar rollback.
4. Medir RTO <= 10 min e RPO <= 15 min.

### Gate D — produção

1. Reduzir TTL.
2. Alterar `cdn.phpd77.com` para `143.14.168.66`.
3. Monitorar reprodução, 4xx/5xx, latência e distribuição.
4. Remover A records diretos das edges somente após estabilidade comprovada.

## 9. Critérios de aceite

O LB só é produção quando 3 falhas removem o backend, 5 sucessos o reinserem,
HLS e VOD `Range` continuam durante troca, tokens funcionam em outra edge,
origem/edges não permitem bypass, backup restaura em máquina limpa, standby
assume dentro do RTO e rollback foi executado e registrado.

Até lá, o estado honesto é **multi-edge publicado por DNS, sem failover
garantido**, e não Load Balancer de produção.

## 10. Documentos relacionados

- `docs/MULTI_EDGE_FAILOVER.md`
- `docs/RELEASE_AND_PROMOTION.md`
- `docs/TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md`
- `docs/PRODUCTION_SECURITY_AND_CAPACITY.md`
- `docs/CDN_5_OF_10_EXECUTION_AUDIT.md`
