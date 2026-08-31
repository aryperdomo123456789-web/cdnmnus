# Plano de implementação — Load Balancer próprio `143.14.168.66`

**Status:** planejamento técnico; não publicar o IP no DNS antes dos gates de
homologação. Este documento cruza o plano solicitado com o código existente em
28/08/2026.

**Atualização operacional em 31/08/2026:** a `.111` será o primeiro load
balancer do sistema. Ela está registrada como LB `candidate`, ainda sem
backends, e não deve ser descrita como LB ativo. O role/playbook de HAProxy e o
modelo `load_balancers/lb_backends` já existem em laboratório, contrariando a
fotografia antiga abaixo. O destino `.66` permanece uma etapa posterior. Para
estado executado, prevalece
[STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md).

**Estado operacional no momento da revisão:** as fontes `servicedovod.lat:80` e
`zjo.lat:80` já estão cadastradas no tenant `xui-principal`, mas o deployment
`dep-236418bec22841bca970fe4f4e8ab007` terminou em `failed` durante o Ansible.
As edges ainda não devem ser consideradas convergentes; corrija o erro do
deployment antes de qualquer teste de LB.

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
| Load Balancer | `ansible/roles/load_balancer`, `ansible/playbooks/load-balancer.yml` | Implementado e testado em laboratório; não promovido em produção. |
| Inventário | `ansible/inventories/production/hosts.yml` | Contém `.111` candidata e as edges `.168/.170`. |

O projeto tem base de controle, runtime de edge e perfil LB laboratorial, mas
ainda precisa de promoção real da `.111`, backends, controlador operacional de
health, backup/restore e gates de produção.

## 2. Arquitetura alvo

```text
fase 1: cliente -> cdn.phpd77.com -> LB 143.14.168.111
                                          |-> edge 143.14.168.168
                                          `-> edge 143.14.168.170

fase 2: cliente -> cdn.phpd77.com -> LB 143.14.168.66
                                          |-> edge 143.14.168.168
                                          `-> edge 143.14.168.170
```

Na primeira virada, a `.111` será o LB e `.168/.170` serão seus backends. A
`.66` permanece o destino posterior. Por decisão operacional atual, todos os
nós mantêm `22/80/443` públicos; restringir mídia exclusivamente ao LB será um
hardening futuro separado. Os A records atuais são round-robin DNS, não
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

- firewall atual publica somente `22/80/443` em todos os nós; `.111` também
  preserva `1455`;
- todos os nós possuem acesso SSH mútuo por identidades Ed25519 individuais;
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

## 11. Cadastro e preparação da VPS `143.14.168.66`

### 11.1 Pré-requisitos antes de tocar no DNS

O cadastro deve ocorrer no menu SSH do plano de controle, sem publicar o IP
imediatamente:

```bash
TERM=xterm-256color mago-cdn
```

Fluxo obrigatório:

1. **Gerenciar Edges → Adicionar** (até existir o cadastro específico de LB).
2. Informar ID técnico, nome `lb-primary`, IP `143.14.168.66`, porta e usuário
   SSH.
3. Ler o fingerprint pelo menu e comparar com o console do provedor.
4. Confirmar a senha apenas para o bootstrap; ela não é persistida.
5. Executar preflight: Ubuntu suportado, NTP, disco, RAM, conectividade às
   edges e acesso administrativo.
6. Manter o estado `bootstrapping` até o perfil LB ser aplicado e validado.

O cadastro genérico de edge não transforma a máquina em Load Balancer. A função
LB só deve ser habilitada pelo playbook dedicado `deploy-load-balancer.yml`.

### 11.2 Seção de cadastro dedicada (a implementar)

O menu deverá ganhar **Load Balancers → Cadastrar LB** com os campos:

```text
ID técnico: lb-primary
Nome: LB principal
IPv4: 143.14.168.66
Porta SSH: 22
Usuário operacional: cdn-deploy
Fingerprint Ed25519: leitura + confirmação manual
Modo: active | standby
Backends: edge-main, edge-168, edge-170
```

Ao confirmar, o painel deve criar um registro de LB separado do registro de
edge, executar somente preflight e gerar um deployment de perfil `load_balancer`.
Não deve alterar DNS, firewall das edges ou produção até os gates de aceite.

## 12. Estado unificado e recuperação quando o LB cair

Não sincronizar `/var/lib/cdnmnus-admin/admin.db` por `scp`, `rsync` ou cópia de
arquivo enquanto o serviço estiver ativo. SQLite WAL é local e uma cópia pode
ficar inconsistente ou perder alterações.

### 12.1 Fonte autoritativa recomendada

Para múltiplos menus SSH e promoção offline, adotar uma destas opções:

1. **PostgreSQL externo/HA** como banco autoritativo do control plane; ou
2. um repositório de estado versionado, assinado e criptografado, com snapshots
   transacionais aplicados localmente.

O SQLite atual deve continuar como cache/local fallback, mas não como banco
primário compartilhado entre VPS. Segredos (tokens, chaves privadas e senhas)
ficam fora do estado versionado, em secret store ou arquivos root-only.

Cada LB/edge deve manter um pacote de recuperação local, atualizado após cada
release aprovada, contendo somente configuração segura, manifesto, hashes,
inventário, certificados permitidos e o último snapshot do estado não secreto.

### 12.2 Promoção via SSH

O menu em qualquer edge deverá oferecer **Recuperação → Promover esta máquina
para LB** somente depois de confirmar:

- quorum/lock de promoção, para impedir dois LBs ativos;
- snapshot de estado com assinatura válida;
- acesso aos três backends ou ao subconjunto disponível;
- certificado e chave de assinatura de token presentes;
- capacidade mínima e portas livres.

O fluxo é:

```text
adquirir lock/quorum
drain da função atual
instalar role load_balancer
restaurar snapshot aprovado
renderizar backends
validar configuração e health checks
ativar serviço LB
publicar o novo endpoint por DNS/API
registrar evento e monitorar
```

Se a máquina promovida era uma edge, ela deixa de servir mídia durante a
operação. O rollback executa o caminho inverso e só reinclui a edge após health
e release válidos.

### 12.3 Limite importante

Sem um mecanismo de quorum/lock e sem estado autoritativo, “entrar por SSH em
qualquer edge e promover” pode criar split-brain: dois LBs anunciando o mesmo
serviço, configurações divergentes e tokens incompatíveis. A opção de promoção
deve permanecer bloqueada até esses componentes existirem e serem testados.

## 13. Ordem de implementação para tornar a promoção real

1. Criar migração do modelo para `load_balancers` e `lb_backends`.
2. Criar role/playbook `load_balancer` idempotente.
3. Criar controlador de health, lock/quorum e eventos de promoção.
4. Criar pacote de recuperação e rotina de backup/restore testável.
5. Adicionar **Cadastrar LB** e **Promover esta máquina para LB** ao menu SSH.
6. Preparar `143.14.168.66` em homologação e validar failover.
7. Preparar uma segunda máquina standby.
8. Só depois alterar `cdn.phpd77.com` para o LB.

Até a conclusão desses itens, o comportamento correto é manter o menu de
promoção indisponível e usar o runbook manual, evitando uma falsa sensação de
alta disponibilidade.

## 14. Receita mastigada de execução

Não pule etapas e não altere o DNS antes do passo 20.

### A — verificar a central

1. Entre no servidor de controle como root.
2. Execute `df -h /`; o uso deve estar abaixo de 80%.
3. Execute `systemctl is-active cdnmnus-admin.service nginx.service cdnmnus-orchestrator.service`.
4. Abra `TERM=xterm-256color mago-cdn`.
5. Em **Gerenciar XUIs**, confirme origem, tenant e as fontes VOD autorizadas.
6. Confirme que painel e worker usam `/var/lib/cdnmnus-admin/admin.db`.
7. Nunca copie o SQLite ativo entre servidores.

### B — cadastrar e preparar edges

8. Em **Gerenciar Edges**, cadastre `143.14.168.111`, `.168` e `.170` sem
   duplicar registros.
9. Compare cada fingerprint SSH com o console do provedor.
10. Aguarde bootstrap e preflight. `bootstrapping` não entra no DNS; somente
    `ready` pode entrar no pool.
11. Teste cada edge com o hostname canônico e `/edge-health`; espere HTTP 200.

### C — publicar a mesma release

12. Em **Fontes VOD por XUI**, confirme `servicedovod.lat:80` e `zjo.lat:80`.
13. Em **Deployments e rollout serial**, escolha **Compilar release e
    enfileirar deploy serial** uma única vez.
14. Aguarde `running` e depois `succeeded`.
15. Se ficar `queued`, corrija o worker/banco; se ficar `failed`, leia a causa e
    não repita cegamente.
16. Valide em cada edge `nginx -t`, TLS e versão da release.
17. Teste `/movie/` e `/series/` com `Range`, seek, refresh e reprodução curta.

### D — testar LB temporário na 111

18. Coloque a 111 em `draining` e confirme 168/170 saudáveis.
19. Instale o perfil LB dedicado na 111 e registre 168/170 como backends.
20. Teste o LB com `curl --resolve cdn.phpd77.com:443:IP_DO_LB` e repita HLS,
    filme, série, `Range` e refresh.
21. Só após sucesso altere o DNS para o LB temporário.

### E — adicionar e promover a 66

22. Prepare `143.14.168.66` como edge sem alterar DNS.
23. Distribua a mesma release e segredos não versionados; teste-a diretamente.
24. Adicione-a como backend do LB 111 e teste falha/reinserção.
25. Prepare backup e um segundo LB standby.
26. Faça drain da 66, instale nela o perfil LB e restaure o snapshot aprovado.
27. Valide TLS, backends, health e rollback.
28. Troque o DNS para 66 somente após todos os testes.

### F — recuperação

29. Nunca promova duas máquinas simultaneamente.
30. Adquira lock/quorum de promoção.
31. Restaure o snapshot assinado e valide hashes.
32. Publique DNS somente com o LB ativo e backends saudáveis.
33. Registre release, horário, motivo, operador e resultado.

### Resultado final esperado

```text
LB ativo:   143.14.168.66
LB standby: 143.14.168.111 (ou outra VPS preparada)
Edges:      143.14.168.168 e 143.14.168.170
DNS:        cdn.phpd77.com aponta somente para o LB ativo
VOD:        fontes isoladas por tenant e publicadas por release
```

Se uma etapa falhar, pare nela e preserve o estado anterior. Não faça alteração
manual permanente para “forçar” o próximo passo.
