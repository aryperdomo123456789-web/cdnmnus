# Índice documental e receita operacional

Data-base: 2026-08-29

Este documento existe para reduzir ambiguidade. Ele organiza a documentação
atual em uma ordem de leitura e de execução que acompanha o estado real do
repositório.

## 1. Verdade operacional atual

Leia primeiro estes documentos, nesta ordem:

1. [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
2. [REPO_MAP_AND_STATE_2026-08-29.md](REPO_MAP_AND_STATE_2026-08-29.md)
3. [IMPLEMENTATION_RECIPE_WITH_REAL_CODE_2026-08-29.md](IMPLEMENTATION_RECIPE_WITH_REAL_CODE_2026-08-29.md)
4. [PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
5. [PRODUCTION_RISK_ACCEPTANCE_2026-08-29.md](PRODUCTION_RISK_ACCEPTANCE_2026-08-29.md)
6. [NUMERIC_NODE_ID_MIGRATION_2026-08-29.md](NUMERIC_NODE_ID_MIGRATION_2026-08-29.md)
7. [NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md](NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md)
8. [CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md](CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md)
9. [VOD_PLAYER_VALIDATION_2026-08-28.md](VOD_PLAYER_VALIDATION_2026-08-28.md)
10. [VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md](VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md)

Esses documentos cobrem:

- topologia alvo;
- risco aceito e limites do que pode ser feito agora;
- numeração de nós e menu comum;
- contrato de DNS-only e R2 separado;
- validação real de players e laboratório local;
- relay privado VOD e contratos de segurança.

## 2. Documentos de execução por frente

### Frente A: canário e release

Use:

- [PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
- [CANARY_STATE_AND_ROLLBACK_OPERATIONS.md](CANARY_STATE_AND_ROLLBACK_OPERATIONS.md)
- [RELEASE_AND_PROMOTION.md](RELEASE_AND_PROMOTION.md)
- [STORAGE_AND_RELEASE_GATE_2026-08-29.md](STORAGE_AND_RELEASE_GATE_2026-08-29.md)

### Frente B: menu local e promoção de nó

Use:

- [NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md](NODE_LOCAL_MENU_AND_ROLE_PROMOTION.md)
- [MENU_UNIFICADO_OPERACIONAL.md](MENU_UNIFICADO_OPERACIONAL.md)
- [NUMERIC_NODE_ID_MIGRATION_2026-08-29.md](NUMERIC_NODE_ID_MIGRATION_2026-08-29.md)

### Frente C: LB e failover

Use:

- [LOAD_BALANCER_LAB_RUNBOOK.md](LOAD_BALANCER_LAB_RUNBOOK.md)
- [LOAD_BALANCER_143_14_168_66_IMPLEMENTATION_PLAN.md](LOAD_BALANCER_143_14_168_66_IMPLEMENTATION_PLAN.md)
- [MULTI_EDGE_FAILOVER.md](MULTI_EDGE_FAILOVER.md)

### Frente D: Cloudflare e backup externo

Use:

- [CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md](CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md)
- [TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md](TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md)
- [PRODUCTION_SECURITY_AND_CAPACITY.md](PRODUCTION_SECURITY_AND_CAPACITY.md)

### Frente E: VOD e players

Use:

- [VOD_PLAYER_VALIDATION_2026-08-28.md](VOD_PLAYER_VALIDATION_2026-08-28.md)
- [VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md](VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md)
- [VOD_RELAY_CANARY_RUNBOOK_2026-08-28.md](VOD_RELAY_CANARY_RUNBOOK_2026-08-28.md)
- [VOD_DELIVERY_CURRENT_STATE_2026-08-28.md](VOD_DELIVERY_CURRENT_STATE_2026-08-28.md)
- [NGINX_UPSTREAM_RESOLUTION_AUDIT_2026-08-28.md](NGINX_UPSTREAM_RESOLUTION_AUDIT_2026-08-28.md)

### Frente F: PostgreSQL e failover

Use:

- [POSTGRESQL_AND_FAILOVER_LAB_DECISION_2026-08-29.md](POSTGRESQL_AND_FAILOVER_LAB_DECISION_2026-08-29.md)

## 3. Receita operacional em uma página

Ordem recomendada de execução:

1. Conferir `STATE_REAL_2026-08-29.md`.
2. Ler `REPO_MAP_AND_STATE_2026-08-29.md`.
3. Fechar a release candidata e o canário da `.168`.
4. Sincronizar o contrato comum do nó em todas as VPS.
5. Validar o laboratório `lab-player/` e fixar as amostras de teste.
6. Consolidar o modelo autoritativo de nós, LBs e locks.
7. Somente depois avançar Cloudflare write, R2 e promoção edge -> LB.

## 4. Laboratório de testes

O laboratório oficial fica em:

- [lab-player/README.md](/opt/cdnmnus/lab-player/README.md)
- [lab-player/scripts/sync_playlist.sh](/opt/cdnmnus/lab-player/scripts/sync_playlist.sh)
- [lab-player/scripts/test_playback_flow.py](/opt/cdnmnus/lab-player/scripts/test_playback_flow.py)

Ele serve para:

- baixar e versionar playlists localmente;
- selecionar alvos fixos de teste;
- comparar CDN e IP direto;
- validar `HTTP 200`, `HTTP 206`, `Content-Type` e `Content-Range`;
- registrar relatórios em `lab-player/reports/`.

## 5. O que está bloqueado

Até a `.168` passar pelo canário real e os locks/fencing estarem validados, não
deve ser tratado como pronto:

- `.66` ACTIVE;
- promoção edge -> LB em produção;
- Cloudflare write de produção;
- backup R2 de produção sem restore comprovado;
- qualquer cópia de SQLite entre VPS;
- split-brain ou active/active sem a fase de avaliação descrita no runbook.

## 6. Documento a criar depois

Depois que esta base estiver estável, o próximo documento útil é um
"contratos reais do código" reunindo:

- `node-id`, `node-role.json`, `control-plane.conf`;
- `TopologyStore`;
- `lab-player`;
- `samples.json`;
- playbooks de bootstrap e promoção.
