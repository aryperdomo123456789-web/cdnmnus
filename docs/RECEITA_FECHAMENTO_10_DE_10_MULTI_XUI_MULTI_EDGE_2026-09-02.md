# Receita de fechamento 10/10: multi-edge e multi-XUI

Data: 2026-09-02
Escopo: cdnmnus, edges gerenciadas, tenants multi-XUI, DNS-only, Nginx, HAProxy e failover.

## Objetivo

Este documento e um runbook de execucao, nao uma declaracao de disponibilidade. A nota 10/10 somente pode ser atribuida quando cada gate abaixo tiver evidencia produzida no ambiente real. Documentacao, configuracao declarada ou um teste unitario isolado nao substituem evidencia operacional.

Regra principal: se um gate falhar, parar naquele gate, registrar a falha, corrigir e repetir o gate. Nunca promover uma edge, alterar DNS ou endurecer SSH enquanto a etapa anterior nao estiver comprovada.

## Fontes de verdade

O banco autoritativo do plano multi-edge e:

```text
/var/lib/cdnmnus-admin/admin.db
```

`/etc/cdnmnus/admin.db` e apenas compatibilidade legada e deve resolver para o banco autoritativo. `/etc/cdnmnus/panel.db` pertence ao painel legado de upstream unico e nao deve ser mesclado.

Variaveis usadas nesta receita:

```bash
export CONTROL_DB=/var/lib/cdnmnus-admin/admin.db
export PROJECT=/opt/cdnmnus
export RELEASE_ROOT=/opt/cdnmnus/releases
export SSH_DIR=/etc/ssh/sshd_config.d
```

## Mapa do codigo real

Antes de alterar qualquer fluxo, consulte os pontos abaixo:

- `core/db.py`: schema, tenants, edges, deployments, locks, capacidade e transacoes.
- `core/deploy.py`: preparacao, renderizacao, validacao e aplicacao de deployment.
- `core/render_tenants.py`: isolamento e renderizacao por tenant.
- `core/cname_discovery.py`: descoberta automatica de CNAME e associacao ao tenant.
- `core/tls_provisioner.py`: certificados e aliases publicados.
- `core/topology.py`: estados e transicoes das edges.
- `core/capacity_policy.py`: calculo e politica de capacidade.
- `core/dns_reconciler.py`: reconciliacao de DNS.
- `panel/cname_gateway.py`: entrada por CNAME sem confiar em header enviado pelo cliente.
- `panel/token_broker.py`: tokens, validade e vinculacao ao tenant.
- `panel/vod_relay.py`: relay VOD, ranges, redirects e protecao SSRF.
- `web/app.py`: API e painel administrativo.
- `orchestrator/worker.py`: reconciliacao continua.
- `scripts/cdnmnus-readiness-audit.py`: auditoria, que deve ser complementada pelos gates reais abaixo.

## Gate 0: proteger o estado atual

Nao executar migracao, failover ou hardening sem backup verificavel.

```bash
sudo systemctl stop cdnmnus-orchestrator.service
sudo systemctl stop cdnmnus-admin.service
sudo install -d -m 0700 /var/lib/cdnmnus-admin/backups
sudo -u cdn-admin sqlite3 "$CONTROL_DB" '.backup /var/lib/cdnmnus-admin/backups/admin-pre-10-10.db'
sudo -u cdn-admin sqlite3 "$CONTROL_DB" 'PRAGMA integrity_check;'
sudo -u cdn-admin sqlite3 "$CONTROL_DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
sha256sum "$CONTROL_DB" /var/lib/cdnmnus-admin/backups/admin-pre-10-10.db
```

O backup so e aceito se `integrity_check` retornar `ok` e o hash for registrado. Manter tambem uma copia externa antes de testes destrutivos.

## Gate 1: um banco para todos os componentes

```bash
readlink -f /etc/cdnmnus/admin.db
readlink -f "$CONTROL_DB"
systemctl cat cdnmnus-admin.service
systemctl cat cdnmnus-orchestrator.service
```

Todos os processos administrativos devem usar `CDNMNUS_ADMIN_DB=$CONTROL_DB`. Confirmar tambem os defaults em `web/app.py`, `orchestrator/worker.py` e `cli/admin_cli.py`. O symlink em `/etc` e temporario; o codigo nao pode depender dele.

Validacao objetiva:

```bash
sudo -u cdn-admin env PYTHONPATH="$PROJECT" CDNMNUS_ADMIN_DB="$CONTROL_DB" \
  "$PROJECT/venv/bin/python3" - <<'PY'
from core.db import Database
db = Database()
db.initialize()
print('tenants=', len(db.tenants()))
print('edges=', len(db.edges()))
print('deployments=', len(db.rows('SELECT 1 FROM deployments')))
print('locks=', len(db.rows('SELECT 1 FROM promotion_locks')))
PY
```

Esperado: os 3 tenants e as 5 edges existentes continuam presentes. O banco `panel.db` deve permanecer intacto e separado.

## Gate 2: dependencias e smoke real

Instalar o modulo `headers-more` pelo mecanismo suportado pela distribuicao e pelo role Ansible do projeto. Nao comentar `more_clear_headers` para fazer o teste passar.

```bash
nginx -V 2>&1 | tr ' ' '\n' | grep -E 'headers-more|more-http'
sudo "$PROJECT/tests/smoke.sh"
```

O template Nginx deve ser renderizado antes do `nginx -t`; validar o arquivo renderizado, os includes, certificados e portas reais. Se o modulo nao existir, o gate falha.

## Gate 3: qualidade reproduzivel

```bash
cd "$PROJECT"
python3 -m unittest discover -s tests -p '*test.py' -q
python3 -m compileall -q core panel web orchestrator cli scripts
find . -name '*.sh' -print0 | xargs -0 -r -n1 bash -n
ansible-playbook --syntax-check -i ansible/inventory.ini ansible/site.yml
git diff --check
```

O pipeline CI deve instalar as dependencias em ambiente limpo, instalar `pytest` ou equivalente explicitamente, executar todos os testes e publicar artefatos. Um teste que depende de pacotes ja instalados na maquina nao e CI reproduzivel.

## Gate 4: adicionar uma nova edge sem risco

Toda edge nova comeca em `bootstrap` ou `standby`, nunca em `active`.

1. Criar o registro no banco autoritativo com nome, IP, papel, fingerprint SSH e capacidade declarada.
2. Aplicar o bootstrap Ansible com usuario administrativo e chave, sem senha.
3. Validar OS, horario, firewall, Nginx, HAProxy, TLS, disco, memoria e limites de conexao.
4. Instalar exatamente o release aprovado e comparar manifesto/hash com o controlador.
5. Renderizar a configuracao para a edge e executar `nginx -t` e `haproxy -c` no host.
6. Executar health checks locais e testes de playlist/VOD antes de publicar DNS.
7. Marcar `ready` somente depois de todos os gates. A edge ready pode entrar no pool; uma edge degraded fica fora.

Comandos minimos no candidato:

```bash
ssh -o BatchMode=yes edge-admin@EDGE 'sudo -n true'
ssh edge-admin@EDGE 'sudo nginx -t && sudo haproxy -c -f /etc/haproxy/haproxy.cfg'
ssh edge-admin@EDGE 'systemctl is-active nginx haproxy'
```

Se `sudo -n` pedir senha, nao prosseguir. Corrigir sudoers de comandos especificos, com `requiretty` e acesso root interativo proibidos para o agente.

## Gate 5: multi-XUI e isolamento

Cada XUI e um tenant independente. O CNAME publicado determina o tenant; nenhum `Host`, header, parametro ou cookie fornecido pelo cliente pode trocar o tenant depois da resolucao.

Para cada tenant, provar:

- playlist autorizada aponta para a origem correta;
- CNAME correto chega somente ao tenant correto;
- token de tenant A falha no tenant B;
- configuracao, cache, limites e upstreams nao vazam entre tenants;
- um upstream indisponivel nao altera o upstream de outro tenant.

Executar a matriz com pelo menos 3 tenants e repetir em todas as edges. Registrar status HTTP, `Content-Type`, tamanho, latencia, `Age`, `X-Cache` e identificador da edge.

## Gate 6: TLS e aliases

Para cada alias publicado, validar de fora e na edge:

```bash
openssl s_client -connect ALIAS:443 -servername ALIAS </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
curl --fail --silent --show-error --resolve ALIAS:443:EDGE_IP https://ALIAS/healthz
```

O certificado precisa estar valido, dentro da validade, com SAN para o alias exato e cadeia confiavel. Certificado existente em disco sem handshake externo valido nao conta.

## Gate 7: live, playlist e tokens

Validar cada origem XUI autorizada com credenciais armazenadas fora do codigo e fora deste documento. Para cada tenant:

1. Obter playlist M3U autorizada.
2. Selecionar 3 canais live e 3 itens VOD.
3. Testar entrada direta e entrada pelo CNAME CDN.
4. Verificar que o token expira, nao e reutilizavel fora do tenant e nao revela credencial upstream.
5. Se `/play/<token>/m3u8` for requisito, testar o fluxo completo: emissao, playlist, segmentos, expiracao, replay e tenant errado.

Nao registrar tokens ou senhas em logs, relatorios ou commits.

## Gate 8: VOD e relay HTTP

Para cada VOD valido, provar `GET 200`, `HEAD`, `Range 206`, range invalido, redirect permitido, timeout e origem indisponivel. A resposta deve preservar o contrato de streaming sem transformar um erro da origem em falso sucesso.

```bash
curl -fsS -D headers.txt -o /dev/null 'https://ALIAS/...'
curl -fsS -I 'https://ALIAS/...'
curl -fsS -H 'Range: bytes=0-1023' -D range.txt -o /dev/null 'https://ALIAS/...'
```

Bloquear SSRF: somente esquemas, hosts, portas e caminhos permitidos pela politica do projeto. A fonte `turbotv` permanece fora do pool se nao retornar VOD valido; corrigir ou retirar, sem mascarar 503.

## Gate 9: capacidade medida

Nao usar a declaracao de 10 Gbps como evidencia. Medir por edge, protocolo e tenant, em janela sustentada, coletando throughput, conexoes, CPU, memoria, disco, drops, erros, p95/p99 e saturacao do upstream.

Definir previamente o SLO e o limite de entrada. O controlador automatico so pode ampliar o pool quando a edge nova estiver `ready`, saudavel e abaixo dos limites; ao degradar, deve drenar conexoes antes de remover a edge.

Guardar os comandos, versoes, gerador de carga, tamanho dos objetos, duracao e graficos. O resultado deve ser reproduzivel por outra pessoa.

## Gate 10: soak de seis horas

Executar no trafego representativo, nunca apenas em `/healthz`:

- playlist e segmentos live;
- VOD com ranges;
- 3 tenants e todas as edges;
- renovacao de tokens;
- rotacao/renovacao TLS quando aplicavel;
- reconciliacao do worker e reconciliacao DNS.

Aprovacao exige seis horas sem erro acima do SLO, sem crescimento ilimitado de memoria, sem vazamento de conexoes, sem divergencia do banco e sem deployment inesperado. Registrar inicio, fim, alertas e amostra de metricas.

## Gate 11: failover com fencing e rollback

O `readiness-audit` nao pode considerar lock existente como suficiente. O gate deve verificar lock nao expirado, dono, TTL, fencing externo e acoes de promocao reais. Lock SQLite sozinho nao e fencing.

Antes do teste:

```bash
python3 scripts/cdnmnus-readiness-audit.py --db "$CONTROL_DB"
python3 scripts/lb_candidate_preflight.py --node-id NODE_ID
```

Promocao somente se preflight, `sudo -n`, release, TLS, HAProxy, Nginx, capacidade e health checks estiverem verdes.

Executar pelo menos duas rodadas ida e volta:

1. Confirmar edge A ativa e edge B ready.
2. Isolar A usando fencing externo verificavel.
3. Promover B e provar trafego real em B.
4. Confirmar que A nao consegue escrever/servir como ativa.
5. Drenar B, revalidar A e fazer rollback controlado.
6. Repetir invertendo os papeis.

Falha parcial, timeout ou impossibilidade de provar fencing cancela o teste. Nao forcar promocao manual para obter um resultado verde.

## Gate 12: PostgreSQL e restore

SQLite pode continuar no desenvolvimento, mas a nota 10/10 de producao exige PostgreSQL de laboratorio/producao com backup e restore comprovados.

1. Criar schema por migration versionada.
2. Configurar TLS, usuario minimo, backups e retencao.
3. Exportar um backup consistente.
4. Restaurar em banco vazio.
5. Validar tenants, edges, deployments, locks e historico.
6. Executar a suite e o failover contra o banco restaurado.

O restore deve ser testado periodicamente, nao apenas documentado.

## Gate 13: hardening SSH

Somente depois de confirmar acesso por chave em uma segunda sessao e console/out-of-band:

```bash
sshd -T | grep -E 'passwordauthentication|permitrootlogin|pubkeyauthentication'
```

O estado final deve ser `passwordauthentication no`, `permitrootlogin no` e `pubkeyauthentication yes`, com usuario administrativo sem senha e sudo restrito. Aplicar uma edge por vez e validar acesso antes da proxima.

## Rollback universal

Todo deployment deve registrar release anterior, hash, configuracao renderizada, banco usado e edge afetada. Para reverter:

1. interromper o reconciliador que esta aplicando mudanca;
2. restaurar configuracao e release anterior;
3. validar `nginx -t`, `haproxy -c` e health checks;
4. restaurar o banco somente se a mudanca de schema exigir;
5. reiniciar o servico afetado;
6. executar smoke e registrar a causa.

Nunca apagar o banco anterior, sobrescrever backups ou usar `git reset --hard` como rollback operacional.

## Matriz final de aprovacao

| Area | Evidencia obrigatoria | Aprovado |
|---|---|---|
| Banco | unico DB, integridade, backup e restore | [ ] |
| Codigo | testes, compile, shell, Ansible e CI limpo | [ ] |
| Dependencias | headers-more e smoke completo | [ ] |
| Edge nova | bootstrap, release, configs e health reais | [ ] |
| Multi-XUI | isolamento dos 3 tenants em todas as edges | [ ] |
| TLS | handshake e SAN de todos os aliases | [ ] |
| Live/VOD | matriz real, ranges e tokens | [ ] |
| Capacidade | medicao sustentada e limites do controlador | [ ] |
| Soak | seis horas abaixo do SLO | [ ] |
| Failover | fencing, ida, volta e rollback repetido | [ ] |
| Seguranca | SSH sem senha/root e sudo minimo | [ ] |
| Origem | turbotv corrigida ou fora do pool | [ ] |

## Estado conhecido em 2026-09-02

O banco autoritativo ja foi separado e os servicos administrativos foram apontados para ele. A suite atual tem 72 testes passando. Isso nao equivale a 10/10: o smoke local ainda depende de `headers-more`, o preflight do LB `.237` falha por `sudo -n`, o failover/fencing, soak, carga, PostgreSQL/restore, hardening final e SAN completo ainda precisam de evidencia real. `turbotv` tambem nao deve ser declarado pronto enquanto seus VODs retornarem 503.

A plataforma so esta pronta para receber novas edges e trafego multi-XUI quando a matriz final estiver totalmente marcada e cada item tiver relatorio, comando, horario e resultado anexados ao estado do projeto.
