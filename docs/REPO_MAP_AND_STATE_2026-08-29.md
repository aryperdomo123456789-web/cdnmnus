# Mapa completo do repositório e estado real por área

Data-base: 2026-09-01

Este documento existe para responder uma pergunta simples: "o que este
repositório realmente contém hoje?" Ele classifica os arquivos por função e por
status operacional, com prioridade para fidelidade ao código e não para
marketing técnico.

Regra de rastreio:

- se o código mudar, este mapa precisa ser revisado;
- se uma pasta não estiver listada aqui, ela deve ser tratada como fora da
  superfície principal até revisão explícita;
- este mapa complementa, mas não substitui, `STATE_REAL_2026-08-29.md`.

## 1. Visão executiva

O repositório está organizado em seis blocos reais:

1. instalador e hardening de proxy Nginx;
2. painel administrativo HTTP/SQLite;
3. CLI e menu comum;
4. núcleo de dados, deploy e topologia;
5. laboratório de playback e validação de mídia;
6. documentação operacional e auditorias.

O que já está confirmado no código:

- o projeto possui um painel administrativo local e uma CLI operacional;
- o menu local unificado lê contratos de nó em modo read-only;
- a topologia autoritativa já controla nós, load balancers, backends e locks;
- o laboratório `lab-player/` executa captura, seleção fixa e testes de playback;
- a documentação já distingue produção, laboratório e contratos;
- os testes foram endurecidos para não vazar estado global; a última execução
  teve 37 aprovações e 1 falha em `admin_web_test.py`.

O que ainda continua sendo gate:

- promoção edge -> LB em produção;
- Cloudflare write de produção;
- R2 de produção com restore comprovado;
- alta disponibilidade real com segunda camada física confirmada;
- PostgreSQL/failover de produção;
- `.237` ACTIVE e `.111` STANDBY, ainda não publicados.
- transformação efetiva de manifesto para `/play/<token>/m3u8`.
- controlador contínuo de capacidade e fencing externo.

## 2. Árvore funcional do repositório

### 2.1 Instalação e proxy base

Arquivos:

- `install.sh`
- `install-from-github.sh`
- `scripts/sysctl_tuning.sh`
- `scripts/firewall_hardening.sh`
- `scripts/install_tls_from_stdin.sh`
- `nginx/nginx.conf`

Estado:

- produção/base;
- já documentado no `README.md` e `docs/OPERATIONS.md`;
- foco em instalação enxuta, firewall, sysctl e template Nginx.

Função real:

- instalar e ajustar a camada de proxy Nginx do host;
- não é o plano de controle principal do CDN multi-edge;
- mantém a base de deployment e hardening do host.

### 2.2 Núcleo de domínio e persistência

Arquivos:

- `core/db.py`
- `core/deploy.py`
- `core/edge_manager.py`
- `core/render_tenants.py`
- `core/topology.py`
- `core/postgres_lab.py`

Estado:

- produção + laboratório;
- `core/db.py` ainda é o banco operacional principal para tenants, edges,
  deployments e DNS;
- `core/topology.py` é o modelo autoritativo novo para nós, LBs, locks e
  auditoria;
- `core/postgres_lab.py` é laboratório de lock/fencing em Postgres, não o
  plano de produção atual.

Função real:

- persistir tenants, edges, CNAMEs, DNS e deploys;
- renderizar Nginx por tenant;
- gerenciar bootstrap SSH das edges;
- orquestrar topologia transacional e locks;
- prover um laboratório isolado para discutir/ensaiar Postgres.

### 2.3 Interface administrativa

Arquivos:

- `web/app.py`
- `panel/panel.py`
- `cli/admin_cli.py`
- `cli/mago_cdn.py`
- `cli/mago-cdn`
- `run_admin.sh`

Estado:

- produção + interface operacional;
- `web/app.py` é o painel HTTP administrativo principal do control plane;
- `panel/panel.py` e `panel/*` compõem a superfície de runtime e relay/broker;
- `cli/mago_cdn.py` e wrapper `cli/mago-cdn` fornecem o menu local unificado.

Função real:

- expor a administração local;
- permitir bootstrap, cadastro de tenants, CNAMEs, VOD e deploy;
- fornecer menu read-only no nó;
- servir a operação pelo SSH e pelo painel.

### 2.4 Broker, relay e isolamento de origem

Arquivos:

- `panel/token_broker.py`
- `panel/multi_tenant_broker.py`
- `panel/vod_relay.py`
- `panel/*.service`

Estado:

- produção controlada + contrato de segurança;
- estes módulos não são “apenas testes”; fazem parte do caminho operacional
  atual;
- o relay VOD é fail-closed e o broker valida destino/pinning.

Função real:

- resolver origem e VOD com proteção;
- servir conteúdo de forma restrita;
- impedir vazamento desnecessário de origem, token e redirects públicos.

### 2.5 Orquestração e systemd

Arquivos:

- `orchestrator/worker.py`
- `orchestrator/cdnmnus-orchestrator.service`
- `panel/cdnmnus-panel.service`
- `panel/cdnmnus-monitor.service`
- `panel/cdnmnus-monitor.timer`
- `panel/cdnmnus-token-broker.service`
- `panel/cdnmnus-vod-relay@.service`
- `panel/cdnmnus-tenant-broker@.service`
- `panel/cdnmnus-soak@.service`
- `web/cdnmnus-admin.service`

Estado:

- produção operacional;
- contém o encaixe de services/timers/workers que sustentam o painel e os
  brokers.

Função real:

- manter worker desacoplado;
- sustentar painel HTTP;
- executar monitoramento e soak;
- encapsular serviços por tenant ou fluxo.

### 2.6 Laboratório de mídia

Arquivos:

- `lab-player/README.md`
- `lab-player/scripts/sync_playlist.sh`
- `lab-player/scripts/test_playback_flow.py`
- `lab-player/playlists/`
- `lab-player/reports/`

Estado:

- laboratório real e ativo;
- este bloco já foi executado com playlists reais, amostras fixas e relatórios.

Função real:

- baixar playlists por dois caminhos;
- fixar amostras;
- comparar CDN vs IP direto;
- validar `HTTP 200`, `HTTP 206`, `Content-Type` e `Content-Range`;
- gerar relatórios locais.

### 2.7 Testes

Arquivos:

- `tests/admin_core_test.py`
- `tests/admin_web_test.py`
- `tests/edge_manager_test.py`
- `tests/load_balancer_role_test.py`
- `tests/multi_tenant_broker_test.py`
- `tests/panel_http_test.py`
- `tests/postgres_lab_test.py`
- `tests/release_integrity_test.py`
- `tests/smoke.sh`
- `tests/token_broker_test.py`
- `tests/topology_model_test.py`
- `tests/vod_player_compatibility_test.py`
- `tests/vod_relay_test.py`

Estado:

- base sólida de regressão;
- `unittest discover` está verde no estado atual;
- alguns testes foram reescritos para eliminar vazamento de `os.environ`,
  mock global e estado de servidor HTTP.

Função real:

- verificar o contrato do painel, do broker, da topologia e do laboratório;
- proteger contra regressão de semântica e segurança.

### 2.8 Ansible

Arquivos:

- `ansible/ansible.cfg`
- `ansible/roles/node_menu/*`
- `ansible/roles/load_balancer/*`
- `ansible/playbooks/*.yml`

Estado:

- produção operacional e migração de contrato;
- o role `node_menu` já escreve os contratos do nó;
- o role `load_balancer` prepara a pilha de LB;
- os playbooks cobrem preflight, deploy, activate, rollback e auditoria.

Função real:

- bootstrap de VPS;
- instalação do menu comum;
- preparação de edge/LB;
- deploy e rollback controlados.

### 2.9 Scripts auxiliares

Arquivos:

- `scripts/distribute_tls.sh`
- `scripts/media_validation.py`
- `scripts/migrate_numeric_node_ids.py`
- `scripts/sanitized_monitor.py`
- `scripts/soak_test.py`
- `scripts/update.sh`

Estado:

- misto: produção + utilitários de validação;
- alguns scripts são claramente de operação;
- outros são ferramentas de laboratório/inspeção.

Função real:

- distribuir TLS;
- migrar IDs numéricos;
- validar mídia;
- monitorar sem vazar conteúdo sensível;
- executar soak;
- atualizar o repositório de forma controlada.

## 3. Mapa de verdade por responsabilidade

| Área | Arquivos principais | Estado | Observação prática |
| --- | --- | --- | --- |
| Instalação base | `install.sh`, `scripts/*`, `nginx/nginx.conf` | Produção | Base de proxy Nginx do host |
| Control plane | `web/app.py`, `core/db.py`, `core/deploy.py` | Produção | Painel HTTP e persistência principal |
| Menu local | `cli/mago_cdn.py`, `cli/mago-cdn`, `ansible/roles/node_menu/*` | Produção/contrato | Read-only local, sem verdade paralela |
| Topologia | `core/topology.py` | Produção/contrato | Nó, LB, backend, lock, fencing |
| Brokers/relay | `panel/token_broker.py`, `panel/vod_relay.py`, `panel/multi_tenant_broker.py` | Produção controlada | Isolamento e proteção de origem |
| Laboratório VOD | `lab-player/*` | Laboratório real | Comparação CDN vs IP direto |
| Failover/LB | `core/topology.py`, `ansible/roles/load_balancer/*`, `docs/CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md` | Parcial/laboratório | Modelo e role existem; controlador contínuo, fencing e failover real continuam pendentes |
| PostgreSQL | `core/postgres_lab.py`, docs de failover | Laboratório | Não é banco de produção atual |
| Testes | `tests/*` | Produção de qualidade | Cobertura de regressão |

## 4. Pontos que não devem ser confundidos

1. `web/app.py` não é o único sistema do projeto; ele é o painel
   administrativo principal.
2. `panel/` não substitui `core/`; ele executa uma parte específica do fluxo
   de runtime/broker/relay.
3. `core/topology.py` é o contrato autoritativo novo, mas ainda convive com
   peças legadas.
4. `lab-player/` é laboratório real e persistente, não uma pasta descartável.
5. `docs/` contém tanto decisão de produção quanto auditoria histórica; o
   cabeçalho de estado real agora existe para reduzir ambiguidade.

## 5. Ordem prática de leitura

Se alguém for assumir o projeto hoje, esta é a ordem que mais reduz risco:

1. `docs/STATE_REAL_2026-08-29.md`
2. `docs/DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md`
3. `docs/PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md`
4. `docs/CODE_CONTRACTS_AND_LAB_RECIPE_2026-08-29.md`
5. `core/topology.py`
6. `ansible/roles/node_menu/tasks/main.yml`
7. `ansible/roles/node_menu/files/node_menu.py`
8. `lab-player/scripts/test_playback_flow.py`
9. `web/app.py`
10. `panel/token_broker.py`

## 6. Limitações honestas deste mapa

Este documento é amplo e fiel ao estado atual, mas ainda não substitui uma
revisão linha por linha de todos os módulos. Ele registra:

- o que existe;
- o que cada arquivo faz;
- qual é o papel operacional de cada bloco;
- e qual parte ainda precisa de validação de produção.

Se o objetivo for fechar 100% de rastreabilidade, o próximo passo é criar um
mapa de “arquivo -> função -> leitura -> escrita -> risco”.
