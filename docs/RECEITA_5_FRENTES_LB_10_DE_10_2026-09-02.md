# Receita simples: cinco frentes para a CDN 10/10

Data: 2026-09-02
Objetivo: transformar o desenho atual em uma CDN com um LB principal, um LB
standby, três edges, vários XUIs e failover seguro.

Esta receita é deliberadamente conservadora. Ela não marca uma máquina como
`active` só porque um processo foi instalado. Cada etapa tem um teste e um
rollback. Se um teste falhar, pare naquela etapa.

## 1. Desenho final

```text
Clientes
   |
VIP ou DNS controlado por eleição + fencing
   |
143.14.168.111  LB ACTIVE + control plane
   |\
   | +-- 143.14.168.168  EDGE
   | +-- 143.14.168.170  EDGE
   | `-- 143.14.168.78   EDGE
   |
45.140.192.237  LB STANDBY, sem tráfego normal
```

Regras que não podem ser quebradas:

1. `.111` não entra no pool de edges.
2. `.237` não recebe tráfego enquanto estiver `standby`.
3. Só pode existir um LB `active`.
4. DNS não é mecanismo de fencing.
5. O XUI nunca é consultado diretamente pelo cliente.
6. HAProxy encaminha tráfego; não armazena mídia nem reescreve credenciais.
7. Nenhum segredo entra no Git, no log ou no relatório do laboratório.

## 2. Situação conhecida antes de começar

Executar no control plane:

```bash
cd /opt/cdnmnus
python3 scripts/cdnmnus-readiness-audit.py
```

O estado atual conhecido é:

| Item | Estado | O que falta |
|---|---|---|
| Edges `.168`, `.170`, `.78` | prontas e convergentes | soak e capacidade comprovada |
| LB `.111` | candidato, control plane ativo | HAProxy, TLS, candidato validado |
| LB `.237` | standby lógico | pacote autorizado, mesma release e TLS |
| Lease/fencing | ausente | autoridade externa e teste de corte |
| Capacidade | sem perfis | contrato + medição + validade |

**Gate observado em 2026-09-02:** o control plane aprova a tag
`v0.5.0-managed-node.19` no commit `3decb6f5a4ac33ab07850c4e040ea81ae35e4804`,
mas o checkout local contém outro commit e alterações de trabalho. O
`node-package/install.sh --dry-run` recusou essa divergência. Não faça reset,
checkout forçado ou criação manual de `package.json`; gere uma nova release
imutável após revisar e publicar as alterações atuais.

O auditor deve terminar com `ready=true` antes da publicação. Não altere o
banco manualmente para obter essa resposta.

## 3. Preparação comum

### 3.1 Fazer backup

```bash
sudo install -d -m 0700 /var/backups/cdnmnus
sudo cp --preserve=mode,ownership,timestamps \
  /var/lib/cdnmnus-admin/admin.db \
  /var/backups/cdnmnus/admin-$(date -u +%Y%m%dT%H%M%SZ).db
```

Confirme que o backup pode ser lido em uma cópia de laboratório. Nunca copie o
SQLite ativo entre VPS para formar alta disponibilidade.

### 3.2 Validar inventário

```bash
ansible-inventory -i ansible/inventories/production/hosts.yml --graph
ansible-inventory -i ansible/inventories/production/hosts.yml --host control_plane_1
```

O resultado deve mostrar `control_plane_1` em `load_balancers`, com
`ansible_connection=local` e `load_balancer_mode=active`, e `.237` com modo
`standby`.

## 4. Frente 1: instalar HAProxy sem interromper a CDN

HAProxy é o porteiro: recebe HTTPS, verifica as edges e encaminha cada sessão.
Health checks ativos removem um backend depois de falhas consecutivas e o
recolocam depois de sucessos consecutivos. O projeto já possui essa role em
`ansible/roles/load_balancer`.

### 4.1 O que é obrigatório

Preencha um arquivo temporário `0600` com:

```yaml
load_balancer_action: preflight
load_balancer_environment: production
load_balancer_mode: active
load_balancer_change_id: "lb-YYYYMMDD-001"
load_balancer_haproxy_version: "VERSAO_EXATA_DO_REPOSITORIO"
load_balancer_public_hosts:
  - cdn.phpd77.com
load_balancer_backend_health_host: cdn.phpd77.com
load_balancer_backends:
  - {name: edge168, address: 143.14.168.168, port: 443, state: ready, weight: 100}
  - {name: edge170, address: 143.14.168.170, port: 443, state: ready, weight: 100}
  - {name: edge78, address: 143.14.168.78, port: 443, state: ready, weight: 100}
load_balancer_tls_fullchain_source: "/caminho/seguro/fullchain.pem"
load_balancer_tls_private_key_source: "/caminho/seguro/private.key"
```

O valor `VERSAO_EXATA_DO_REPOSITORIO` não pode ser `latest`. O pacote universal
do nó e seu manifesto precisam coincidir com `expected_node_package_ref`,
`expected_node_package_commit` e `expected_node_manifest_digest` aprovados pelo
control plane.

### 4.2 Executar primeiro somente o preflight

```bash
chmod 0600 /caminho/lb-111-vars.yml
ANSIBLE_CONFIG=/opt/cdnmnus/ansible/ansible.cfg \
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/load-balancer.yml \
  --limit control_plane_1 \
  -e @/caminho/lb-111-vars.yml \
  -e load_balancer_action=preflight
```

Depois execute `deploy` no mesmo `--limit`. `deploy` instala o pacote, salva o
rollback, cria `haproxy.cfg.candidate` e executa `haproxy -c`; ele não publica
DNS. Se falhar, não execute `promote`.

Verificações no `.111`:

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg.candidate
sudo systemctl is-active nginx cdnmnus-admin cdnmnus-orchestrator
sudo systemctl is-active haproxy
```

Se o control plane parar, o HAProxy não é considerado aprovado. O rollback é o
arquivo salvo em `/var/lib/cdnmnus-lb/rollback/<change_id>/` e a ação
`rollback` do mesmo playbook.

## 5. Frente 2: preparar TLS corretamente

O certificado do LB precisa cobrir todos os hostnames públicos que o LB aceita,
por exemplo `cdn.phpd77.com` e os aliases oficialmente cadastrados. Não use um
certificado de teste nem desative verificação de hostname.

### 5.1 Emitir e renovar

Use ACME DNS-01 com uma conta protegida e uma credencial DNS mínima. A renovação
deve acontecer antes da expiração e distribuir o PEM novo para `.111` e `.237`.
O segredo da API fica somente no secret store ou no host autorizado.

### 5.2 Verificar artefatos

```bash
sudo test "$(stat -c %a /caminho/seguro/private.key)" = 600
openssl x509 -in /caminho/seguro/fullchain.pem -noout -subject -issuer -dates -ext subjectAltName
```

O role monta um PEM versionado em `/etc/haproxy/certs/`. Depois de `deploy`:

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg.candidate
openssl s_client -connect 143.14.168.111:443 \
  -servername cdn.phpd77.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

Se o certificado estiver expirado, sem SAN ou divergente entre LBs, pare. Não
abra o endpoint público e não copie a chave para as edges.

## 6. Frente 3: preparar o standby `.237`

O `.237` deve ter o mesmo pacote autorizado, mesma versão HAProxy, mesma
configuração assinada, mesmos backends e mesmo certificado. Ele permanece sem
DNS e sem tráfego.

1. Corrija SSH pinado e console do `.237`.
2. Instale o pacote universal autorizado; não crie `package.json` à mão.
3. Execute `preflight` e `deploy` com `--limit lb_candidate_237`.
4. Confirme `haproxy -c` e `systemctl is-active haproxy`.
5. Teste com `curl --resolve`, sem alterar DNS.
6. Registre o nó como `standby` pelo fluxo do control plane.

O teste no standby deve validar `/lb-health`, `/edge-health`, live, VOD,
série, `HTTP 206`, `Range`, `Content-Range`, SNI e ausência de `Location` que
revele origem. Se `.237` aceitar tráfego normal, houve erro operacional: drene
e pare o serviço até investigar.

## 7. Frente 4: lease e fencing

Lease responde “quem pode ser ACTIVE agora?”. Fencing responde “o antigo ACTIVE
foi realmente impedido de continuar?”. Os dois são obrigatórios.

### 7.1 Escolher o mecanismo

Use uma destas opções, nesta ordem:

1. VIP/floating IP controlado pela API do provedor, com detach confirmado.
2. API DNS idempotente com leitura, escrita mínima e confirmação do record.
3. VRRP/Keepalived somente se os dois servidores realmente compartilharem um
   domínio de rede onde VRRP seja suportado e testado.

Não use `ping`, `systemctl stop` via SSH ou DNS round-robin como fencing.

### 7.2 Contrato do adaptador

Antes de promover, o adaptador externo deve aceitar somente:

```text
fence(old_lb, fencing_token) -> confirmed=true/false
publish(new_lb, fencing_token) -> confirmed=true/false
observe() -> endpoint atual + token observado
```

Cada operação deve registrar LB antigo, LB novo, token crescente, horário,
resposta do provedor e operador. Sem `confirmed=true`, a promoção termina com
falha e nenhum DNS/VIP é publicado.

### 7.3 Teste obrigatório

Faça primeiro em laboratório: corte o `.111`, confirme que o VIP/DNS não aponta
para ele, promova `.237`, valide mídia e depois retorne controladamente ao
`.111`. O resultado aceitável é sempre exatamente um `active`; o antigo volta
como `standby` ou `fenced`.

## 8. Frente 5: capacidade real

Não invente `1`, `5` ou `10 Gbps` a partir de um speedtest. Registre duas
evidências:

1. capacidade contratada no provedor;
2. medição controlada entre várias fontes externas.

Em janela autorizada, use `iperf3` com 8 a 16 fluxos por 60 a 120 segundos,
repita três vezes e guarde mediana, retransmissões, perda e interface. Não
misture tráfego de clientes com teste.

Registre cada LB com `capacity_mbps`, `measured_mbps`, `max_connections`,
`confidence`, `measured_at` e `expires_at`. A capacidade utilizável é:

```text
usable_mbps = capacity_mbps * 0.75
```

Se a medição ficar abaixo de 80% do contratado, marque a capacidade como
suspeita e não aumente o peso para “compensar”. O controlador deve reduzir peso,
fazer drain e abrir alerta; não deve criar ou comprar VPS sem aprovação.

## 9. Promoção controlada

Só execute esta ordem quando o auditor estiver `ready=true`:

1. congelar deploys;
2. confirmar release/digest das três edges;
3. confirmar `.237` pronto e sem tráfego;
4. adquirir lease exclusiva e token crescente;
5. executar fencing do endpoint anterior e confirmar;
6. validar novamente o candidato `.111`;
7. promover `.111` usando o playbook real com `--limit control_plane_1`;
8. verificar `/lb-health`, `/edge-health`, live, VOD e `Range`;
9. publicar VIP/DNS somente após os testes;
10. observar 5xx, latência, sessões, banda e stalls.

Se qualquer etapa falhar, não “tente de novo” aumentando timeout ou removendo
validações. Execute rollback, mantenha o endpoint anterior cercado ou restaure
o estado comprovado, e arquive os logs sem URLs credenciadas.

## 10. Checklist de aprovação 10/10

- [ ] `.111` tem HAProxy instalado, configuração validada e serviço saudável.
- [ ] `.237` tem a mesma release, TLS e configuração, sem tráfego normal.
- [ ] TLS cobre os hostnames públicos, renova e distribui sem expor chaves.
- [ ] Existe autoridade única de lease e fencing externo comprovável.
- [ ] O teste de split-brain não produz dois ACTIVE.
- [ ] Perfis de capacidade têm fonte, medição, validade e margem.
- [ ] Três edges têm release e digest iguais.
- [ ] Health remove e reintegra edge sem quebrar sessões existentes.
- [ ] Live, filme, série, SNI, `HTTP 200`, `HTTP 206` e `Range` passam.
- [ ] O laboratório executa o fluxo dos apps e arquiva evidências.
- [ ] O auditor retorna `ready=true`.

Enquanto qualquer caixa estiver desmarcada, a CDN está em preparação, não em
10/10. Essa regra protege o XUI e evita uma falsa sensação de alta
disponibilidade.

## 11. Referências técnicas

- [HAProxy health checks](https://www.haproxy.com/documentation/configuration-tutorials/reliability/health-checks/)
- [HAProxy Runtime API](https://www.haproxy.com/documentation/haproxy-runtime-api/)
- [Let's Encrypt DNS-01](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)
- [Keepalived/VRRP guide](https://www.keepalived.org/pdf/UserGuide.pdf)
- [Cloudflare proxy status e DNS-only](https://developers.cloudflare.com/dns/proxy-status/)
- [Runbook mestre deste projeto](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
- [Auditor executável](../scripts/cdnmnus-readiness-audit.py)
