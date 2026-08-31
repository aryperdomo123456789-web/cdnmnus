# Progresso de implementação — 2026-08-29

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
Este documento é um resumo de execução e deve permanecer alinhado ao estado
real atualizado após cada mudança importante.

Leitura recomendada antes deste documento:

1. [DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md](DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md)
2. [PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)

Escopo desta execução: leitura e validação remota não mutante na Frente 1;
implementação local/laboratorial nas Frentes 2–4. Nenhum DNS, serviço, symlink,
estado do banco real ou tráfego de produção foi alterado.

## Frente 1 — canário `.168`

- [x] DNS autoritativo identificado: Cloudflare
  (`curt.ns.cloudflare.com` e `lovisa.ns.cloudflare.com`).
- [x] TTL autoritativo observado: 300 segundos.
- [x] RRset público observado: `.111` e `.170`; `.168` já está fora do pool.
- [x] 12 consultas recursivas devolveram os dois endereços publicados.
- [x] health direto com `curl --resolve`: `.111`, `.168` e `.170` retornaram
  HTTP 200 com `ssl_verify_result=0`.
- [x] `ansible-core==2.17.14` confirmado em `/opt/cdnmnus/venv`.
- [x] inventário preserva `lb011` como nome técnico da `.168`.
- [x] candidata `20260829012407-d60cfdbf` já estava sincronizada na `.168`.
- [x] sete hashes recalculados remotamente e aprovados; digest
  `9e5457a1dd27609a573c0fd7cbcc80db1d84378da118d7e0dc70d70d7a534eb0`.
- [x] `current` permaneceu em `20260828205549-34caf01e`; `current.json`
  permaneceu ausente.
- [x] transição de edge auditada implementada localmente.
- [x] rollback standalone fail-closed implementado e validado por syntax-check.
- [ ] capacidade de `.111/.170` sob carga representativa.
- [ ] credencial/canal de mudança do RRset Cloudflare e ensaio de reinserção.
- [ ] ativação, testes live/VOD/seek, players reais, rollback real e soak.

Health instantâneo não é evidência de capacidade. Por isso a ativação continuou
bloqueada, mesmo com a `.168` já drenada do DNS.

## Frente 2 — modelo nodes/roles/LBs

Implementados no laboratório `nodes`, `load_balancers`, `lb_backends`,
`promotion_locks` e `node_events`, com papéis `control_plane`, `edge` e
`load_balancer`. Há unicidade de ID/IP, backend apenas EDGE, um LB ACTIVE,
transições auditadas, promoção sob lease/lock, fencing monotônico, demote e
rollback. Testes cobrem upgrade/downgrade, concorrência, invariantes e tentativa
de adulteração direta do fencing.

## Frente 3 — HAProxy/LB

Role e playbook laboratoriais entregues para preflight, deploy, promote, drain,
demote e rollback. Deploy produz candidato e executa `haproxy -c` sem ativar.
Promoção/rollback validam antes da publicação e restauram fail-closed. O modo de
laboratório recusa qualquer backend fora de loopback. Testes usaram somente
servidores HTTP falsos locais.

## Frente 4 — PostgreSQL/failover

Decisão provisória: banco dedicado/gerenciado em rede administrativa, não nos
nós EDGE/LB nem no control node com storage degradado. Foram preparados schema,
migrations, importação/comparação lógica, TLS opt-in, claim concorrente, lease,
fencing falso e plano de backup/restore. O host atual não possui PostgreSQL nem
runtime de container; integração e restore reais aguardam instância de lab.

A pergunta para a BlazeHosting está pronta em
`POSTGRESQL_AND_FAILOVER_LAB_DECISION_2026-08-29.md`, mas não foi enviada por
ausência de canal autenticado/autorização para comunicação externa.

## Validação agregada

- todas as suítes `tests/*_test.py` aprovadas;
- smoke Nginx aprovado;
- `compileall` aprovado;
- syntax-check Ansible dos fluxos edge/LB aprovado;
- `git diff --check` aprovado.

## Próximo gate de produção

Obter acesso auditável ao Cloudflare, provar carga em `.111/.170`, fornecer
usuário/XUI/domínio exclusivos de homologação e executar o rollback real ainda
com a `.168` fora do RRset. Somente depois disso ativar, validar players reais e
iniciar soak. A `.66` continua bloqueada até os Passos 4–9 terem fluxo funcional.
