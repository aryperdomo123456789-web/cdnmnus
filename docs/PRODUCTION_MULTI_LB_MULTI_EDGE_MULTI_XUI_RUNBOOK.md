# Runbook mestre: VOD, multi-edge, multi-LB e multi-XUI

Se você precisar de uma porta de entrada única para toda a documentação atual,
comece por [DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md](DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md).
Este runbook continua sendo a fonte de verdade da topologia e da sequência
operacional, mas o índice organiza a leitura e conecta o código ao laboratório
de testes. Para certificados, `/play/<token>/m3u8` e namespaces de cache, ele
deve ser lido junto com
[RECIPE_CERTIFICATES_OPAQUE_PLAY_TOKENS_MULTI_XUI_CACHE.md](RECIPE_CERTIFICATES_OPAQUE_PLAY_TOKENS_MULTI_XUI_CACHE.md).

**Data-base:** 01/09/2026
**Topologia alvo:** `.237` LB ativo, `.111` LB standby, `.168/.170` edges.
**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
**Regra:** preservar o tráfego atual até o substituto passar por health,
reprodução, rollback e soak.

## 1. Como usar

Execute os 17 passos na ordem. Cada passo só termina quando o checklist de
aceite estiver completo. Se um resultado divergir: pare, não edite Nginx
manualmente, não repita deploy, execute o rollback e mantenha o nó fora do
pool. Registre erros sem URL, senha ou token.

Legenda: `[x]` comprovado, `[~]` parcial/candidato, `[ ]` pendente.

## 2. Fonte de verdade e estado observado

Quando documentos históricos divergirem, use esta ordem:

1. runtime vivo;
2. banco ativo e manifestos recalculados;
3. código/testes atuais;
4. documento mais recente;
5. evidência histórica.

### Produção viva

- [x] serviços principais ativos e `nginx -t` aprovado na `.111`;
- [x] health `200` e TLS válido nas três edges;
- [x] `.168/.170` na release `20260829012407-d60cfdbf`, digest
  `9e5457a1dd27609a573c0fd7cbcc80db1d84378da118d7e0dc70d70d7a534eb0`;
- [~] `.111` ainda no runtime VOD legado, sem release ativa por symlink;
- [x] novo relay VOD instalado e ativo no systemd de `.168/.170`;
- [ ] HAProxy, PostgreSQL, lease ou fencing instalados;
- [~] `.237` possui papel lógico LB/standby, mas ainda usa release antiga e não
  possui capacidade, health, lease ou fencing de produção.

### Banco e inventário

**Atualização de identidade em 29/08/2026:** os IDs técnicos autoritativos são
`1` para `.111`, `2` para `.168` e `3` para `.170`; o próximo cadastro recebe
`4` automaticamente. Os nomes operacionais das edges são `edge1`/`edge2`;
os Load Balancers são `.111`/`.237`.
de compatibilidade. O nó `1` está registrado `load_balancer/candidate`, sem
promoção ou ativação.

O banco possui um tenant, uma origem, um LB upstream do fornecedor, duas seeds
VOD e apenas `.168/.170` cadastradas. As duas aparecem como `ready`, com a
release convergente registrada; `.111` não aparece. O inventário versionado
contém `.168` e `.170`, ambas alinhadas como `ready`.

Não confunda `tenant_upstreams.kind='lb'` (LB do fornecedor/XUI) com o LB
frontal `.237/.111`, que possui modelo parcial no código, mas ainda precisa de
registro operacional, backends, release e gates de produção.

### Release VOD ativa nas edges

- [x] relay Python com pinning, TLS/SNI, SSRF fail-closed e Range;
- [x] unit por tenant;
- [x] renderer por socket Unix, sem destino VOD dinâmico no Nginx;
- [x] release fechada com runtime/units e verificador independente;
- [x] rollback de symlink, units, serviços e `current.json`;
- [x] testes XCIPTV/IBO simulados e carga curta;
- [ ] alterações consolidadas em commit/tag imutável.

### Componentes existentes no código e gates ainda abertos

- tabelas `nodes`, `load_balancers`, `lb_backends`, `promotion_locks`,
  `node_events` já existem em `core/topology.py`;
- role Ansible `load_balancer` e playbooks de preflight/deploy/promoção/
  rebaixamento/rollback já existem;
- HAProxy frontal possui template e testes, mas o `.237` ainda não está
  convergido para a release atual;
- PostgreSQL, controlador contínuo de health/capacidade, quorum e fencing
  externo continuam pendentes;
- `ansible-playbook` continua dependente da instalação no control node.

## 3. Arquitetura final

```text
Clientes
   |
DNS/floating IP com health e fencing
   |
   +--------------------+
   |                    |
LB .237 ACTIVE     LB .111 STANDBY
HAProxy sem cache  HAProxy sem cache
   |                    |
   +---------+----------+
             |
       +-----+-----+
       |           |
   EDGE .168   EDGE .170
 Nginx/broker  Nginx/broker
 relay/cache   relay/cache
       |           |
       +-----+-----+
             |
        XUI 1 ... N
```

Invariantes:

- uma VPS não pode ser EDGE e LB ACTIVE simultaneamente;
- somente um LB ACTIVE na primeira versão;
- LB frontal não cacheia mídia nem executa broker/relay;
- edge reconstrói token/redirect localmente;
- PostgreSQL é fonte autoritativa; SQLite nunca é copiado entre VPSs;
- release de código é idêntica; configuração/capacidade local possui digest;
- toda mudança usa canário, drain, health e rollback;
- active/active permanece proibido até o passo 17.

## 4. Pré-flight global

- [ ] operador e responsável pelo rollback definidos;
- [ ] console/VNC de cada VPS testado;
- [ ] snapshots recuperáveis;
- [ ] TTL e DNS atuais registrados;
- [ ] release/digest de cada nó registrados;
- [ ] segredos fora do Git;
- [ ] nenhuma mudança paralela.

Comandos básicos:

```bash
nginx -t
systemctl is-active nginx cdnmnus-admin cdnmnus-orchestrator
df -h /
df -ih /
journalctl --disk-usage
git status --short --branch
```

Valide cada IP separadamente com `curl --resolve` e exija `HTTP=200` e
`ssl_verify_result=0` antes de qualquer mudança.

### Gate de storage

Espaço foi recuperado, mas a `.111` apresentou `fdatasync` próximo de 1 s e
commit SQLite de vários segundos. Antes de gravar releases/banco:

- [ ] incidente aberto no provedor;
- [ ] snapshot consistente;
- [ ] edge drenável;
- [ ] latência estabilizada;
- [ ] zero erro EXT4/JBD2.

**PARE:** não execute `fsck` online nem reinicie `.111` dentro do pool.

## 5. Receita dos 17 passos

### Passo 1 — Finalizar e homologar VOD na `.168`

**Objetivo:** novo relay apenas na canária, preservando `.111/.170`.

Preparação:

1. corrigir storage do control node;
2. instalar `ansible-core` pelo runbook administrativo;
3. revisar, commitar e taguear o candidato;
4. gerar release persistente e recalcular hashes;
5. retirar `.168` do pool.

Testes locais:

```bash
cd /opt/cdnmnus
export TMPDIR=/dev/shm PYTHONDONTWRITEBYTECODE=1
python3 tests/vod_relay_test.py
python3 tests/vod_player_compatibility_test.py
PYTHONPATH=. python3 tests/release_integrity_test.py
git diff --check
```

Preflight limitado:

```bash
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/preflight-edge.yml --limit edge1
```

Aceite:

- [x] relay ativo e socket com permissões corretas;
- [x] `nginx -t` e cinco health `200`;
- [x] filme/série `200/206`, seeks e reconexão HTTP curta;
- [x] nenhum Location/token/origem/header interno;
- [ ] XCIPTV e IBO reais;
- [ ] VOD superior a 3 h e soak de 6 h;
- [x] rollback real aprovado.

Rollback: o playbook reponta `current`, restaura units/serviços/configuração,
testa Nginx e health. Nunca copie apenas `vod_relay.py`.

**PARE:** qualquer 5xx, vazamento, RSS/FD crescente, Range quebrado ou rollback
incompleto mantém `.168` fora do pool.

### Passo 2 — Convergir `.168/.170`

1. confirmar fingerprint da `.170` no console;
2. adicioná-la ao inventário com chave própria;
3. corrigir estados por transição auditada, nunca `UPDATE` cosmético;
4. executar preflight individual;
5. aplicar na `.168` homologada;
6. validar e aplicar na `.170`;
7. executar `audit-edge-releases.yml`.

Aceite nas duas: mesmo release ID, digest, sete hashes, snapshot, units,
`nginx -t` e health. Falha em uma causa rollback somente nela; a outra continua
servindo.

**Estado em 31/08/2026:** `.168/.170` estão na release
`20260829012407-d60cfdbf`, com o mesmo digest de sete artefatos, broker/relay
ativos, `nginx -t`, health, live e VOD Range aprovados. Os estados foram
corrigidos por transição auditada. Permanecem os gates prolongados do passo 1;
esta convergência não autoriza sozinha a virada para o `.237`.

### Contrato obrigatório para toda edge futura

Toda nova edge cadastrada pelo menu entra como `bootstrapping` e participa do
deployment gerado somente pelo modo de onboarding. O pipeline obrigatório é:

```text
preflight de capacidade/NTP/disco/conectividade
-> cópia e verificação da release imutável
-> ativação atômica com rollback
-> auditoria de digest, symlink, broker, relay VOD e health
-> instalação da identidade/menu comum
-> transição transacional legado+topologia para ready
```

O nó deve ter pelo menos 2 vCPU, aproximadamente 4 GiB, 20% de disco livre e
NTP sincronizado para ser aceito como edge preparada para futura evolução a
LB. `ready` não publica DNS automaticamente. Falha em qualquer etapa deixa o nó
fora do pool e registra `failed`; nunca se executa o instalador standalone do
GitHub para contornar esse contrato.

O arquivo local de identidade declara `load_balancer_candidate`, mas isso é
somente capacidade. Promoção exige drain, mudança de papel auditada, role LB,
lease/quorum, fencing e os gates específicos deste runbook.

### Passo 3 — Provar multi-XUI

1. criar hostname e XUI exclusivos de homologação;
2. cadastrar origem, LBs upstream e seeds VOD;
3. emitir TLS DNS-01 nas duas edges;
4. gerar release com dois vhosts, brokers e relays;
5. testar via `--resolve`, sem DNS público;
6. validar live, VOD, Range, seek e refresh em ambos;
7. provar isolamento cruzado e hostname desconhecido fail-closed.

Aceite: cache, socket, origem, seeds e métricas de um tenant nunca atendem o
outro. Remoção do tenant de teste não altera o principal.

Rollback: retirar primeiro qualquer DNS de homologação, drenar, gerar release
sem o tenant e implantar serialmente. Não remova cache/config com tráfego.

### Passo 4 — Confirmar modelo de dados de nós/LBs

O modelo já existe em `core/topology.py`. Nesta fase, não recrie tabelas: faça
backup, execute `TopologyStore.initialize()` de forma controlada e confirme
que o banco usado pela operação contém as tabelas abaixo.

```text
nodes(id,name,ipv4,role,state,release_id,node_config_digest,
      capacity_json,lease_id,created_at,updated_at)
load_balancers(id,node_id,mode,state,public_endpoint,config_version,updated_at)
lb_backends(load_balancer_id,edge_node_id,weight,state,last_health_at)
promotion_locks(service_id,holder_node_id,lease_id,expires_at,fencing_token)
node_events(id,node_id,event_type,operator,reason,payload_sanitized,created_at)
```

Exigir IDs/IPs únicos, backend somente EDGE, um ACTIVE, lock válido e fencing
token monotônico. Criar testes de upgrade/downgrade, concorrência, foreign keys
e duplicidade. Não migrar produção sem backup e restore comprovados.

Aceite adicional: `tenant_upstreams.kind='lb'` continua significando LB do
fornecedor; o LB frontal possui tabelas e APIs próprias.

### Passo 5 — Papéis explícitos

Papéis: `control_plane`, `edge`, `load_balancer`.

Cada VPS recebe:

```text
/etc/cdnmnus/node-id
/etc/cdnmnus/node-role.json
/etc/cdnmnus/control-plane.conf
```

O menu é cliente fino; editar JSON local não concede promoção. EDGE não inicia
HAProxy frontal; LB não inicia cache, broker ou relay de edge. O cabeçalho do
menu deve exibir node ID, papel, estado, release e conexão com o control plane.

### Passo 6 — Validar e completar role Ansible `load_balancer`

Os arquivos abaixo já existem no repositório e precisam ser validados na VPS
`.237`; não instale um role paralelo nem edite a configuração final à mão:

```text
ansible/roles/load_balancer/defaults/main.yml
ansible/roles/load_balancer/tasks/main.yml
ansible/roles/load_balancer/handlers/main.yml
ansible/roles/load_balancer/templates/haproxy.cfg.j2
ansible/playbooks/preflight-load-balancer.yml
ansible/playbooks/deploy-load-balancer.yml
ansible/playbooks/promote-load-balancer.yml
ansible/playbooks/demote-load-balancer.yml
```

Requisitos: pacote pinado, `serial: 1`, backup, `haproxy -c`, reload gracioso,
health, firewall, logs sanitizados e rollback. A role nunca altera DNS.

Aceite:

- [ ] segunda execução idempotente;
- [ ] configuração inválida não reinicia serviço;
- [ ] rollback restaura pacote, config, serviço e role;
- [ ] teste em VPS descartável aprovado.

### Passo 7 — HAProxy frontal sem cache

Deve encaminhar TLS/hostname, usar health `/edge-health`, oferecer drain,
slow-start, afinidade consistente e limites. Não pode cachear mídia, consultar
XUI, seguir redirect VOD ou registrar URI credenciada.

Testar live, VOD/Range, backend caindo, drain, reinserção, hostname desconhecido
e carga. Timeouts precisam comportar VOD longo. O LB deve remover headers
internos e jamais aceitar do cliente um backend arbitrário.

Aceite: um backend falho sai do pool; conexões existentes drenam; a edge volta
gradualmente; o LB não cria nenhum arquivo de cache de mídia.

### Passo 8 — PostgreSQL autoritativo

1. instalar PostgreSQL externo/HA em rede administrativa;
2. criar schema e migrações versionados;
3. importar snapshot SQLite em homologação;
4. comparar contagens, chaves e relacionamentos;
5. executar shadow/read-only;
6. congelar escrita brevemente e aplicar delta final;
7. trocar DSN por secret;
8. manter SQLite intacto para rollback.

Exigir TLS, menor privilégio, backup/PITR, auditoria e pool limitado. Provar dois
workers sem job duplicado e restore real. Nunca usar `scp` ou `rsync` no SQLite
ativo.

Aceite:

- [ ] backup restaurado em instância limpa;
- [ ] painel e worker usam a mesma fonte autoritativa;
- [ ] lock transacional impede job duplicado;
- [ ] perda de uma aplicação não corrompe estado.

### Passo 9 — Lease, eleição e fencing

- lease curta renovada pelo ACTIVE;
- token de fencing crescente;
- standby promove só após expiração e lock atômico;
- antigo endpoint é cercado antes do novo ser publicado;
- promoção offline sem quorum é proibida.

Fencing preferido: floating IP/API do provedor, API DNS ou desligamento de
NIC/VPS. `systemctl stop` por SSH não basta.

Testar partição de rede, processo travado, dois operadores, lease expirando,
API de fencing falhando e retorno do antigo ACTIVE. Em nenhum cenário podem
existir dois ACTIVE com fencing token válido.

### Passo 10 — Preparar `.237` sem DNS

1. validar Ubuntu, console, capacidade, NTP e firewall;
2. conferir fingerprint fora da conexão;
3. cadastrar node `load_balancer/pending`;
4. fazer bootstrap por chave exclusiva e descartar senha;
5. executar preflight;
6. instalar role LB;
7. distribuir TLS via secret store/DNS-01;
8. configurar `.168/.170` como backends;
9. manter estado `candidate`.

Aceite: `haproxy -c`, health das duas edges, portas administrativas privadas e
nenhum A/AAAA público apontando para `.237`.

### Passo 11 — Testar `.237 -> .168/.170`

Health sem alterar DNS:

```bash
curl --fail --resolve cdn.phpd77.com:443:45.140.192.237 \
  https://cdn.phpd77.com/edge-health
```

Com URL autorizada em arquivo `0600`: playlist, live por 30 minutos, filme,
série, seeks, Range, reconexão, retirada/reinserção de cada edge e slow-start.
Não imprima a URL.

Aceite:

- [ ] nenhum vazamento;
- [ ] LB sem cache;
- [ ] drain preserva conexões existentes;
- [ ] failover fica dentro do SLO;
- [ ] rollback do `.237` aprovado.

### Passo 12 — Drenar `.111` como edge

Pré-condições: `.168/.170` aprovadas e com capacidade suficiente; `.237`
candidata aprovada; backup/rollback da `.111`; storage estabilizado.

1. marcar `.111` `draining` no estado autoritativo;
2. impedir sessões novas pelo mecanismo público atual;
3. aguardar tráfego cair até o limite definido;
4. confirmar sessões novas nas outras edges;
5. registrar release, configuração e serviços da `.111`;
6. parar somente serviços EDGE após drain.

**PARE:** se `.168/.170` saturarem, gerarem stalls ou 5xx, restaure `.111`
como edge e não avance.

### Passo 13 — Preparar `.111` como standby

1. promover por operação autorizada, nunca editando JSON;
2. instalar a mesma release LB do `.237`;
3. configurar `.168/.170`;
4. instalar secrets/TLS pelo canal autorizado;
5. registrar `standby` sem lease ACTIVE;
6. garantir que broker, relay e cache EDGE não estejam ativos.

Aceite: mesmo release digest do `.237`, health dos backends, nenhum endpoint
público e reversão para EDGE já testada em homologação.

### Passo 14 — Ensaiar promoção e retorno

Testar:

1. parada controlada e abrupta do `.237`;
2. partição entre `.237` e banco;
3. falha DNS/floating IP;
4. retorno do `.237` após promoção da `.111`;
5. rollback para `.237`;
6. queda de edge durante failover de LB.

Registrar tempos de detecção, expiração, fencing, promoção, primeiro health,
primeira reprodução e RTO. Aceite: nunca dois ACTIVE; antigo ativo retorna
standby; Range/reconexão funciona; eventos e fencing token são auditáveis.

### Passo 15 — Publicar `.237` ACTIVE

1. congelar deployments e qualquer transformação de playlist em rollout;
2. confirmar todos os nós e PostgreSQL;
3. confirmar snapshot e rollback;
4. reduzir TTL com antecedência planejada;
5. adquirir lease no `.237`;
6. cercar o endpoint anterior;
7. publicar floating IP/DNS;
8. monitorar health, 5xx, latência, banda e stalls;
9. aumentar tráfego gradualmente.

Falha de SLO: fence `.237`, restaure endpoint anterior, valide reprodução e
preserve evidências. DNS nunca é “corrigido no susto” sem fencing.

### Passo 16 — Operar `.111` STANDBY

- mesma release LB do `.237`;
- health contínuo das edges;
- certificado e snapshot atualizados;
- drift, relógio, disco e conectividade monitorados;
- promoção programada testada periodicamente;
- nenhuma lease ACTIVE na `.111`.

Standby não é apenas “servidor ligado”: precisa ter teste de promoção ainda
válido e capacidade comprovada.

### Passo 17 — Avaliar active/active

Somente após active/standby estável, soak, failover/fencing repetidos, estado
stateless ou afinidade comprovados, mecanismo DNS/VIP apropriado, token ligado
a IP testado e rollback para active/standby.

Active/active só deve ser adotado para remover gargalo medido. Na topologia
atual, active/standby continua recomendado.

## 6. Checklist mestre de produção

### Código e supply chain

- [ ] worktree limpo, commit revisado e tag imutável;
- [ ] release fechada e hashes recalculados;
- [ ] dependências/SBOM registrados;
- [ ] nenhum segredo no Git ou manifesto.

### Estado e banco

- [ ] PostgreSQL HA, backup/PITR e restore;
- [ ] nodes, roles, LBs e backends consistentes;
- [ ] lease, lock e fencing testados;
- [ ] SQLite não copiado.

### Edges e XUIs

- [ ] `.168/.170` na mesma release;
- [ ] relay por tenant e multi-XUI isolado;
- [ ] cache dimensionado por edge, sem VOD integral;
- [ ] health, métricas, alertas e rollback individual.

### Load Balancers

- [ ] `.237` ACTIVE e `.111` STANDBY;
- [ ] mesma release, HAProxy sem cache;
- [ ] health, drain, slow-start e TLS;
- [ ] promoção e retorno ensaiados.

### Segurança e reprodução

- [ ] firewall mínimo, painel privado e SSH pinado;
- [ ] origem aceita somente edges;
- [ ] egress, SSRF, DNS rebinding, TLS e não vazamento;
- [ ] XCIPTV/IBO reais, live 6 h e VOD superior a 3 h;
- [ ] carga distribuída, banda, RSS, FD, CPU e I/O dentro do SLO;
- [ ] desastre de edge e LB durante reprodução.

## 7. Pare imediatamente se

- `nginx -t` ou `haproxy -c` falhar;
- health não for `200`;
- release/digest divergir;
- aparecer Location, token, origem ou header interno;
- Range/seek falhar ou 5xx exceder o SLO;
- storage, RSS ou FD degradar;
- banco perder consistência;
- fencing não confirmar o antigo ACTIVE;
- dois LBs aparecerem ACTIVE;
- rollback não restaurar reprodução.

## 8. Nunca faça

- editar vhost isoladamente;
- copiar SQLite ativo;
- usar `git reset --hard` como rollback;
- publicar DNS antes do canário;
- transformar `.111` em LB sem capacidade nas outras edges;
- manter `.111` EDGE e LB ACTIVE;
- chamar round-robin DNS de failover;
- usar apenas SSH/systemctl como fencing;
- colocar cache, broker ou relay no LB frontal;
- cachear filme inteiro em disco pequeno;
- apagar deployment histórico falho;
- registrar URL, senha, token ou Location;
- declarar active/active antes do passo 17.

## 9. Evidência por mudança

Registrar: change ID, operador, motivo, janela, node/role, release/digests,
health antes/depois, validação Nginx/HAProxy, teste live/VOD/Range, rollback e
resultado. Nunca registrar credenciais, URIs de mídia, cookies ou redirects.

## 10. Documentos subordinados

- [CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md](CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md)
- [VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md](VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md)
- [VOD_PLAYER_VALIDATION_2026-08-28.md](VOD_PLAYER_VALIDATION_2026-08-28.md)
- [VOD_RELAY_CANARY_RUNBOOK_2026-08-28.md](VOD_RELAY_CANARY_RUNBOOK_2026-08-28.md)
- [RELEASE_AND_PROMOTION.md](RELEASE_AND_PROMOTION.md)
- [ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md](ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md)
- [MULTI_EDGE_OPTION_B_CODE_GUIDE.md](MULTI_EDGE_OPTION_B_CODE_GUIDE.md)
- [NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md](NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md)
- [LOAD_BALANCER_143_14_168_66_IMPLEMENTATION_PLAN.md](LOAD_BALANCER_143_14_168_66_IMPLEMENTATION_PLAN.md)
- [EDGE_STORAGE_REMEDIATION_2026-08-28.md](EDGE_STORAGE_REMEDIATION_2026-08-28.md)
- [PRODUCTION_SECURITY_AND_CAPACITY.md](PRODUCTION_SECURITY_AND_CAPACITY.md)
- [TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md](TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md)

## 11. Definição de pronto

Pronto significa: `.168/.170` na mesma release multi-XUI/VOD; `.237` ACTIVE por
lease/fencing; `.111` STANDBY sem role EDGE; PostgreSQL autoritativo; promoção e
rollback reproduzíveis; players, soak, carga e desastre aprovados; nenhum
vazamento; capacidade dentro do SLO. Até lá, preserve o legado funcional e
opere a migração em active/standby.
