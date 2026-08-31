# Contratos reais do código e receita de laboratório

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
Este arquivo deve acompanhar a fotografia operacional. Qualquer mudança em
contrato, laboratório ou validação precisa atualizar o estado real primeiro ou
no mesmo ciclo.

Data-base: 2026-08-29

Este documento registra os contratos que já existem no código e que devem ser
seguros para um operador seguir sem improviso.

## 1. Contratos de nó

Arquivos:

- `ansible/roles/node_menu/tasks/main.yml`
- `ansible/roles/node_menu/files/node_menu.py`
- `ansible/roles/node_menu/files/mago-cdn`

Contratos locais:

- `/etc/cdnmnus/node-id`
- `/etc/cdnmnus/node-role.json`
- `/etc/cdnmnus/control-plane.conf`

Campos obrigatórios de `node-role.json`:

- `schema`
- `node_id`
- `name`
- `role`
- `state`
- `control_plane`
- `release_id`
- `config_digest`

Chaves obrigatórias de `control-plane.conf`:

- `CONTROL_PLANE_HOST`
- `CONTROL_PLANE_PORT`
- `CONTROL_PLANE_SCHEME`
- `CONTROL_PLANE_VERIFY`
- `NODE_BOOTSTRAP_MODE`

## 2. Contratos de topologia

Arquivo:

- `core/topology.py`

Tabelas autoritativas:

- `nodes`
- `load_balancers`
- `lb_backends`
- `promotion_locks`
- `node_events`

Invariantes principais:

- um nó `edge` não pode ser `load_balancer active` ao mesmo tempo;
- `promotion_locks.fencing_token` é monotônico;
- backend precisa ser nó `edge`;
- só um LB pode ficar `active`;
- eventos são auditáveis e sanitizados;
- promoção exige lease válida e fencing compatível.

## 3. Contratos de validação

Arquivos:

- `tests/topology_model_test.py`
- `tests/load_balancer_role_test.py`
- `tests/postgres_lab_test.py`
- `tests/vod_relay_test.py`
- `tests/vod_player_compatibility_test.py`
- `tests/token_broker_test.py`
- `tests/multi_tenant_broker_test.py`
- `tests/panel_http_test.py`
- `tests/admin_web_test.py`

Regra operacional:

- `python3 -m unittest discover -s tests -p '*test.py' -v` deve passar;
- os testes não devem depender de estado global vazado;
- handlers e monkeypatches devem ser restaurados no fim de cada arquivo.

## 4. Laboratório de mídia

Diretório:

- `lab-player/`

Arquivos:

- `lab-player/README.md`
- `lab-player/scripts/sync_playlist.sh`
- `lab-player/scripts/test_playback_flow.py`

Saídas:

- `lab-player/playlists/`
- `lab-player/reports/`

O laboratório faz:

- sincronização de playlists via CDN e IP direto;
- preservação de amostras fixas em `samples.json`;
- comparação das duas rotas;
- validação de `HTTP 200`, `HTTP 206`, `Content-Type` e `Content-Range`;
- gravação de relatórios locais.

## 5. Contratos de operação do laboratório

Variáveis esperadas:

- `LAB_DIR`
- `PLAYER_USERNAME`
- `PLAYER_PASSWORD`
- `PLAYER_BASE_CDN`
- `PLAYER_BASE_DIRECT`
- `PLAYER_LATEST_PLAYLIST`
- `PLAYER_SAMPLE_COUNT`
- `PLAYER_TIMEOUT`
- `PLAYER_RETRY_COUNT`

Modos do validador:

- `--cdn`
- `--direct`
- `--both`
- `--refresh-samples`

## 6. Ordem de execução recomendada

1. Baixar playlists frescas no laboratório.
2. Fixar ou recalcular amostras.
3. Validar CDN e IP direto em paralelo.
4. Registrar relatório local.
5. Só então usar os resultados como pré-requisito de canário/produção.

## 7. O que não deve acontecer

- credencial em arquivo;
- URL sensível em log público;
- SQLite copiado entre VPS;
- promoção de LB sem lock/fencing;
- alteração de DNS em produção antes do canário;
- validação de player sem relatório local.
