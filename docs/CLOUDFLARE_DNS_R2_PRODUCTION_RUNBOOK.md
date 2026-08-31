# Runbook de produção: Cloudflare DNS-only, R2 isolado e menu unificado

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
Consulte este arquivo antes de qualquer mudança externa; ele é a fotografia
operacional que deve ser mantida atualizada após cada alteração real.

**Data-base:** 29/08/2026
**Objetivo:** administrar DNS da Conta Cloudflare A e backups na Conta
Cloudflare B pelo mesmo menu `mago-cdn`, sem nuvem laranja, sem copiar segredos
para edges/LBs e sem publicar nós antes dos gates de produção.
**Regra principal:** automatizar preparação e verificação; publicação, promoção,
troca de autoridade e restauração continuam fail-closed e auditadas.

Legenda: `[x]` já existe no código, `[~]` existe parcialmente/laboratório,
`[ ]` precisa ser implementado ou comprovado.

## 1. Resultado esperado

```text
                            OPERADOR
                               |
                  mesmo menu mago-cdn em toda VPS
                               |
                     cliente fino autenticado
                               |
              CONTROL PLANE + PostgreSQL autoritativo
                    |                         |
          controlador de topologia       jobs de backup
                    |                         |
          Conta Cloudflare A            Conta Cloudflare B
          DNS-only (cinza)               R2 privado + lock
                    |                         |
             .66 ACTIVE LB              objetos criptografados
             .111 STANDBY LB            + restore comprovado
                    |
              .168/.170 EDGES
```

Invariantes:

1. todos os nós recebem o mesmo código, agente e menu;
2. somente o papel atual inicia os serviços daquele papel;
3. EDGE nunca é LB ACTIVE simultaneamente;
4. existem exatamente dois LBs designados na operação normal: um ACTIVE e um
   STANDBY;
5. qualquer edge pode ser **elegível** para conversão emergencial, mas precisa
   drenar e abandonar o papel EDGE antes de iniciar HAProxy frontal;
6. Cloudflare A contém DNS; Cloudflare B contém somente R2;
7. toda escrita DNS usa `proxied=false`; `proxied=true` aborta a operação;
8. tokens, Access Key e Secret Access Key nunca entram no banco, Git, evento,
   argumento de processo ou arquivo distribuído às edges;
9. instalar uma edge prepara o plano DNS, mas não a publica automaticamente;
10. backup só é válido depois de integridade, criptografia, upload, conferência
    remota e teste de restauração.

## 2. Verdade do código atual

### Já existe

- [x] `Database.sync_dns_matrix()` calcula registros a partir de tenants e
  edges `ready` em `core/db.py`;
- [x] tabela local `dns_records`;
- [x] hook opcional `CDNMNUS_DNS_SYNC_SCRIPT` em `cli/admin_cli.py`;
- [x] menu completo no control node em `cli/mago_cdn.py`;
- [x] cliente de menu comum instalado por `ansible/roles/node_menu`;
- [x] cadastro de edge, fingerprint SSH, release, health e rollout serial;
- [x] sanitização de chaves chamadas `token`, `secret`, `credential` e
  `private_key` nos eventos;
- [x] backup SQLite consistente descrito em
  `ADMIN_CONTROL_PLANE_EXECUTION.md`;
- [~] schema de PostgreSQL, lease e restore em laboratório.

### Ainda não existe

- [ ] cliente Cloudflare API;
- [ ] modelo de provider/zone/hostname/reconciliação DNS;
- [ ] armazenamento seguro/versionado de credenciais;
- [ ] API autenticada para o menu fino operar o control plane;
- [ ] reconciliação real entre matriz local e Cloudflare;
- [ ] cliente S3/R2;
- [ ] criptografia `age`;
- [ ] timer de backup, retenção, Bucket Lock e alertas;
- [ ] catálogo de backups e restore automatizado;
- [ ] PostgreSQL autoritativo de produção;
- [ ] fencing real do provedor.

**PARE:** não confunda um item desta receita com funcionalidade já entregue.
Cada fase precisa de testes e aceite antes da próxima.

## 3. Contas e isolamento

### Conta Cloudflare A — DNS

Criar token exclusivo com:

```text
Zone / DNS / Read
Zone / DNS / Write
Resource / Include / somente a zona escolhida
```

Não conceder R2, Workers, WAF, cache, billing ou outras zonas. Restringir IP de
origem e validade quando operacionalmente possível.

### Conta Cloudflare B — backup R2

Usar outro e-mail administrativo, senha, MFA/passkey e recuperação offline.
Criar bucket privado, por exemplo:

```text
mago-cdn-production-backups
```

Criar uma credencial R2 S3 `Object Read & Write` limitada somente a esse bucket.
Criar separadamente uma credencial `Object Read only` para restore/auditoria.
Não habilitar domínio público, `r2.dev`, Worker ou acesso anônimo.

### Credenciais no servidor

Estrutura proposta:

```text
/etc/cdnmnus/secrets/
├── cloudflare-dns/
│   ├── cf-dns-v1.token
│   └── cf-dns-v2.token
└── r2-backup/
    ├── r2-writer-v1.env
    └── age-recipient.txt
```

Permissões:

```text
root:cdn-admin 0750 /etc/cdnmnus/secrets
root:cdn-admin 0640 credencial necessária ao serviço
root:root      0644 chave pública age
```

A chave privada `age` fica fora de todas as VPS. O banco guarda apenas
`credential_ref`, nunca o segredo.

## 4. Modelo de dados a implementar

Criar migração opt-in e testes antes de alterar o banco real:

```text
dns_providers(
  id, kind, account_hint, credential_ref, state,
  created_at, updated_at
)

dns_zones(
  id, provider_id, external_zone_id, zone_name,
  authoritative_nameservers_json, state, last_verified_at
)

dns_managed_hosts(
  id, zone_id, hostname, proxy_required,
  ttl, desired_generation, state
)

dns_reconciliations(
  id, managed_host_id, change_id, operator,
  desired_json, observed_before_json, observed_after_json,
  state, error_sanitized, created_at, finished_at
)

secret_references(
  id, provider_kind, filesystem_ref, version, state,
  created_at, retired_at
)

backup_targets(
  id, kind, account_hint, bucket, endpoint,
  credential_ref, encryption_recipient_fingerprint,
  state, created_at, updated_at
)

backup_runs(
  id, target_id, database_kind, object_key,
  source_digest, encrypted_digest, size_bytes,
  state, reason, started_at, finished_at,
  verified_at, error_sanitized
)

restore_tests(
  id, backup_run_id, state, counts_json,
  integrity_result, started_at, finished_at,
  error_sanitized
)
```

Regras de banco:

- apenas um provider DNS `active` por hostname;
- `proxy_required` deve ser sempre falso nesta versão;
- hostname precisa pertencer à zona;
- `credential_ref` não pode conter `/`, segredo ou conteúdo do token;
- estado observado nunca substitui o estado desejado sem reconciliação;
- eventos não aceitam query string, Authorization, token ou URL assinada;
- backup `verified` exige digest remoto e integridade local;
- restore `passed` exige banco novo, nunca o banco ativo.

## 5. Componentes de código

Criar os módulos sem importar no caminho de produção até os testes passarem:

```text
core/dns_provider.py             contrato abstrato
core/cloudflare_dns.py           implementação Cloudflare DNS-only
core/dns_reconciler.py           diff, plano, apply e rollback
core/secret_store.py             escrita atômica e referência versionada
core/backup.py                   snapshot, manifesto e catálogo
core/r2_backup.py                S3/R2 upload/head/get/list
core/restore.py                  restore isolado e comparação
scripts/cdnmnus-backup.py        entrada não interativa do timer
scripts/cdnmnus-restore-test.py  restore em diretório/DB descartável
```

Units:

```text
cdnmnus-backup.service
cdnmnus-backup.timer
cdnmnus-backup-verify.service
cdnmnus-backup-verify.timer
cdnmnus-dns-reconcile.service
```

Não usar shell construído com token. Usar biblioteca HTTP/S3, arrays de
argumentos e timeouts. Nunca imprimir resposta completa de erro de provider.

## 6. Contrato Cloudflare DNS-only

O cliente deve implementar:

```text
verify_token()
discover_zone(exact_zone_name)
authoritative_nameservers(zone_id)
list_records(zone_id, exact_hostname)
plan_records(hostname, desired_ipv4s, ttl)
apply_plan(change_id, expected_before_digest)
verify_authoritative()
rollback(snapshot_id)
```

Validações obrigatórias:

1. zona retornada precisa ter nome exatamente igual ao informado;
2. hostname precisa terminar em `.` + zona ou ser o apex;
3. aceitar somente A/AAAA previstos; CNAME conflitante aborta;
4. IP precisa existir em `nodes/edges` e estar elegível;
5. todas as escritas enviam `proxied=false`;
6. se o observado contiver registro proxied, abortar e alertar;
7. ler novamente antes de escrever e comparar digest do snapshot;
8. alteração concorrente causa conflito, não overwrite;
9. verificar os nameservers autoritativos e resolvers após apply;
10. não chamar round-robin de failover.

O preview deve ser simples:

```text
DNS ATUAL
cdn.exemplo.com -> 203.0.113.10, 203.0.113.11

DNS DESEJADO
cdn.exemplo.com -> 203.0.113.11, 203.0.113.12

REMOVER: 203.0.113.10
ADICIONAR: 203.0.113.12
PROXY: DNS ONLY
TTL: 300
```

Nunca mostrar token, record ID completo desnecessário ou headers da API.

## 7. Menu idêntico em todas as VPS

Menu lógico comum:

```text
1. Visão geral
2. Nós, papéis e sincronismo
3. Edges
4. Load balancers e promoção
5. DNS Cloudflare
6. Backups R2 e recuperação
7. XUIs, domínios e VOD
8. Releases e deployments
9. Diagnóstico local
0. Sair
```

O arquivo pode ser idêntico, mas o menu é cliente fino:

- no control plane, chama a API local;
- em EDGE/LB, usa canal administrativo autenticado para o control plane;
- sem conexão, permite diagnóstico local e leitura do último snapshot assinado;
- sem quorum/lease, promoção e DNS ficam bloqueados;
- editar `/etc/cdnmnus/node-role.json` nunca concede papel.

Não distribuir banco ou token para obter “menu igual”. O sincronismo vem da API
e do PostgreSQL autoritativo.

### Submenu Cloudflare

```text
1. Consultar conexão e zona
2. Conectar novo token
3. Selecionar zona e hostname
4. Auditar nuvem cinza
5. Comparar DNS desejado x observado
6. Aplicar plano aprovado
7. Restaurar RRset anterior
8. Rotacionar token
9. Migrar conta/zona
0. Voltar
```

### Submenu R2

```text
1. Consultar destino e último backup
2. Conectar bucket R2
3. Testar escrita/leitura em prefixo temporário
4. Executar backup agora
5. Listar backups verificados
6. Executar restore de teste
7. Rotacionar credencial
8. Alterar retenção
9. Desabilitar destino (não apaga objetos)
0. Voltar
```

Password box limpa a variável após uso. “Desabilitar” nunca apaga bucket,
objetos, token no provider ou backup.

## 8. Conectar a Conta A

Receita operacional:

1. criar token mínimo no painel Cloudflare;
2. abrir `mago-cdn -> DNS Cloudflare -> Conectar novo token`;
3. colar o token no password box;
4. informar `phpd77.com` ou a zona nova;
5. o sistema valida token e descobre exatamente uma zona;
6. conferir os NS mostrados com `dig +short NS zona`;
7. informar o hostname gerenciado;
8. executar auditoria somente leitura;
9. exigir que todos os A/AAAA estejam `proxied=false`;
10. salvar a referência versionada;
11. executar preview; não aplicar ainda;
12. registrar change ID e responsável pelo rollback;
13. aplicar somente em janela aprovada;
14. verificar autoritativos, resolvers, TLS e reprodução.

Se o token falhar, nada muda no DNS e a credencial candidata é removida do
secret store.

## 9. Nova edge

Automático após cadastro:

```text
pending -> fingerprint -> bootstrap -> release sincronizada
-> hashes -> TLS -> nginx -t -> health -> testes -> ready
-> plano DNS preparado
```

Não automático:

```text
publicação no RRset
```

Antes de publicar:

- capacidade das edges restantes aprovada;
- live/VOD/Range/seek aprovados;
- origem autoriza o novo IP;
- cinco health 200;
- rollback real da release;
- preview DNS sem remoção inesperada;
- operador confirma change ID.

Falha após publicação restaura o snapshot do RRset e marca a edge `draining` ou
`failed`; não apaga a VPS nem sua evidência.

## 10. Rotação e migração DNS

### Trocar apenas o token

1. cadastrar token candidato como nova versão;
2. validar acesso à mesma zona;
3. ler RRset e comparar digest;
4. executar operação somente leitura;
5. trocar `credential_ref` atomicamente;
6. manter referência anterior durante a janela de rollback;
7. revogar o token antigo no provider após soak;
8. apagar o arquivo antigo somente depois da revogação confirmada.

Trocar token não altera registros.

### Mudar de conta Cloudflare mantendo o domínio

1. criar zona completa na conta nova;
2. comparar todos os registros relevantes, não apenas CDN;
3. conferir MX/TXT/SPF/DKIM/DMARC e validações TLS;
4. obter NS da zona nova;
5. mudar delegação no registrador — a API DNS não faz isso;
6. aguardar e verificar a delegação;
7. manter zona antiga durante propagação;
8. trocar provider ativo somente após os NS novos serem autoritativos;
9. não apagar a zona antiga durante o soak.

### Mudar de domínio

1. adicionar domínio novo como paralelo;
2. emitir TLS DNS-01;
3. gerar release com os dois hosts;
4. testar via `--resolve`;
5. homologar XUI e players;
6. publicar domínio novo;
7. migrar clientes;
8. drenar o antigo;
9. remover somente após soak.

O sistema não “adivinha” que um domínio deve substituir outro.

## 11. Conectar a Conta B/R2

1. criar conta separada e MFA;
2. habilitar R2;
3. criar bucket privado;
4. bloquear acesso público;
5. configurar Bucket Lock conforme a política aprovada;
6. criar token S3 limitado ao bucket;
7. copiar uma única vez Account ID, Access Key ID e Secret Access Key;
8. abrir `mago-cdn -> Backups -> Conectar bucket R2`;
9. informar endpoint, bucket e credenciais por password box;
10. informar a chave pública/recipient `age`;
11. testar PUT/HEAD/GET num prefixo `connection-tests/<uuid>`;
12. comparar bytes e SHA-256;
13. remover somente o objeto de teste, antes de aplicar lock ao prefixo final;
14. salvar `credential_ref`, nunca o secret;
15. executar primeiro backup manual;
16. executar restore de teste;
17. somente então habilitar o timer.

## 12. Pipeline de backup SQLite

Entrada autoritativa atual:

```text
/var/lib/cdnmnus-admin/admin.db
```

Pipeline:

1. criar diretório temporário explícito em storage saudável;
2. abrir origem read-only e usar `sqlite3.Connection.backup()`;
3. fechar destino;
4. executar `PRAGMA integrity_check` e exigir `ok`;
5. executar `PRAGMA foreign_key_check` e exigir zero linhas;
6. capturar schema version, contagens e digest lógico;
7. gerar manifesto sem dados sensíveis;
8. criptografar com recipient público `age`;
9. calcular SHA-256 do objeto criptografado;
10. enviar com nome único para R2;
11. executar HEAD e conferir tamanho/metadados;
12. baixar amostra completa no primeiro backup e periodicamente;
13. marcar `verified` somente após comparação;
14. remover temporários com alvo explícito;
15. registrar evento sanitizado.

Nunca copiar apenas `admin.db` enquanto WAL está ativo. Nunca enviar
`admin.db-wal`, token, inventário SSH ou URLs de mídia ao R2.

Nomes:

```text
sqlite/daily/2026/08/30/cdnmnus-20260830T030000Z-<uuid>.sqlite.age
manifests/2026/08/30/cdnmnus-20260830T030000Z-<uuid>.json
```

## 13. Timer, retenção e alertas

Agenda inicial:

```text
snapshot consistente: a cada 6 horas
backup diário marcado: 03:00 UTC
backup pre-change: antes de schema/DNS/promoção
restore automático de laboratório: semanal
restore operacional acompanhado: mensal
```

Retenção sugerida:

```text
diário: 35 dias
semanal: 120 dias
mensal: 400 dias
pre-change: 180 dias
```

Usar Bucket Lock no destino; o job local não tenta apagar objeto ainda
protegido. Falha dispara status vermelho no menu e alerta. Se o último backup
verificado exceder 24 horas, bloquear migração, promoção planejada e mudança de
schema.

## 14. Restore

Restore nunca aponta primeiro para o banco ativo:

1. escolher backup `verified`;
2. baixar para diretório temporário explícito;
3. conferir SHA-256 criptografado;
4. descriptografar com chave obtida pelo procedimento de recuperação;
5. conferir digest do arquivo e manifesto;
6. abrir banco restaurado read-only;
7. executar integrity/foreign keys;
8. comparar schema e contagens;
9. iniciar painel/worker de laboratório sem acesso a produção;
10. registrar `passed`;
11. destruir somente o temporário validado.

Restore de desastre para produção exige janela, serviços parados, cópia do
banco danificado preservada, caminho explícito, ownership `cdn-admin`, health e
rollback. Nunca restaurar “por cima” enquanto painel/worker escrevem.

## 15. PostgreSQL futuro

Quando PostgreSQL virar autoritativo:

- `pg_dump --format=custom` diário para recuperação lógica;
- backup físico e arquivamento WAL para PITR;
- `pg_service.conf`, nunca DSN em argumento/log;
- upload criptografado ao R2;
- restore em instância limpa;
- dois workers disputando o mesmo job sem duplicidade;
- comparação lógica com o snapshot de origem.

O backup SQLite permanece preservado durante a janela de rollback, mas não volta
a receber escritas depois do corte definitivo.

## 16. Testes obrigatórios no código

Criar:

```text
tests/cloudflare_dns_test.py
tests/dns_reconciler_test.py
tests/secret_store_test.py
tests/backup_test.py
tests/r2_backup_test.py
tests/restore_test.py
tests/unified_menu_contract_test.py
```

Cobertura mínima Cloudflare:

- token inválido/expirado;
- zero, uma e múltiplas zonas retornadas;
- hostname fora da zona;
- `proxied=true` observado ou solicitado;
- CNAME conflitante;
- IP não inventariado;
- alteração concorrente entre preview e apply;
- timeout, 429, 5xx e JSON inválido;
- apply parcial e rollback;
- rotação de token sem alteração do RRset;
- migração de zona sem NS autoritativo recusada;
- nenhum segredo em exceção/log/evento.

Cobertura mínima R2/backup:

- credencial inválida e bucket errado;
- bucket público recusado pelo preflight;
- snapshot SQLite sob escrita concorrente;
- integrity/foreign key falhando;
- criptografia obrigatória;
- upload interrompido;
- HEAD divergente;
- objeto já existente nunca sobrescrito;
- Bucket Lock/retention respeitado;
- rotação de credencial;
- restore com chave errada, digest errado e dump truncado;
- temporário removido sem aceitar caminho amplo;
- segredo ausente de banco, processo e logs.

Testes offline usam servidor HTTP/S3 falso somente em loopback. Integração usa
zonas e buckets exclusivos de laboratório; nunca o hostname público.

## 17. Fases de implementação

### Fase 0 — contratos e testes

- [ ] migrations;
- [ ] interfaces provider/secret/backup;
- [ ] doubles offline;
- [ ] CI executando testes novos no mesmo processo e isoladamente.

### Fase 1 — Cloudflare somente leitura

- [ ] secret store;
- [ ] validar token/zone;
- [ ] listar e auditar RRset;
- [ ] menu mostra drift, sem botão apply.

### Fase 2 — preview e rollback DNS

- [ ] snapshot/digest;
- [ ] plano determinístico;
- [ ] apply com confirmação;
- [ ] rollback real em zona de laboratório.

### Fase 3 — R2 e backup manual

- [ ] snapshot SQLite;
- [ ] `age`;
- [ ] upload/HEAD/GET;
- [ ] restore real aprovado.

### Fase 4 — timer e retenção

- [ ] systemd hardened;
- [ ] Bucket Lock;
- [ ] alertas;
- [ ] retenção e restore periódico.

### Fase 5 — menu remoto comum

- [ ] API administrativa TLS/mTLS ou canal SSH restrito;
- [ ] autorização por papel/operação;
- [ ] nenhuma credencial nas edges;
- [ ] modo offline somente leitura.

### Fase 6 — nova edge assistida

- [ ] bootstrap prepara preview;
- [ ] gates de mídia;
- [ ] publicação explícita;
- [ ] rollback DNS e drain.

### Fase 7 — promoção/failover

- [ ] PostgreSQL;
- [ ] lease;
- [ ] fencing real;
- [ ] exatamente um ACTIVE;
- [ ] Cloudflare apenas publica o resultado da eleição.

## 18. Checklist de produção

### DNS

- [ ] token limitado à Conta A/zona;
- [ ] `proxied=false` no desejado e observado;
- [ ] NS e TTL registrados;
- [ ] preview, apply e rollback aprovados;
- [ ] mudança concorrente recusada;
- [ ] nenhuma credencial em evento/log.

### R2

- [ ] Conta B independente com MFA;
- [ ] bucket privado;
- [ ] token limitado ao bucket;
- [ ] Bucket Lock;
- [ ] criptografia client-side;
- [ ] backup verificado;
- [ ] restore real;
- [ ] chave privada fora das VPS.

### Menu e topologia

- [ ] mesmo artefato de menu em todos os nós;
- [ ] menu fino não possui banco/segredo local;
- [ ] exatamente dois LBs designados;
- [ ] EDGE não inicia HAProxy frontal;
- [ ] LB não inicia relay/cache EDGE;
- [ ] promoção exige drain, lease e fencing;
- [ ] estado/digest sem drift.

## 19. Pare imediatamente se

- a API indicar `proxied=true`;
- zona/hostname não forem exatamente os esperados;
- RRset mudar entre preview e apply;
- token possuir escopo de conta inteira sem justificativa;
- segredo aparecer em log, SQLite, PostgreSQL ou `ps`;
- edge nova ainda não tiver testes/rollback;
- backup não passar integrity/foreign keys;
- objeto R2 não estiver criptografado;
- restore não reproduzir contagens/schema;
- Bucket Lock não estiver comprovado;
- dois LBs aparecerem ACTIVE;
- fencing não confirmar o antigo ACTIVE.

## 20. Definição de pronto

Pronto significa: menu igual e autenticado em todas as VPS; estado autoritativo
central; Conta A gerenciada somente como DNS-only; Conta B com R2 privado,
criptografado e locked; nova edge preparada automaticamente mas publicada só
após gates; rotação de token sem alteração DNS; migração de conta/domínio com
delegação e TLS explícitos; backup dentro do RPO; restore comprovado; exatamente
um LB ACTIVE e um STANDBY pronto; nenhum segredo distribuído ou vazado.

Até todos os itens estarem comprovados, preserve o DNS e o runtime atuais e use
somente laboratório/preview para os componentes novos.

## 21. Referências oficiais

- Cloudflare API tokens: https://developers.cloudflare.com/fundamentals/api/get-started/create-token/
- Cloudflare DNS records: https://developers.cloudflare.com/api/resources/dns/subresources/records/
- Cloudflare DNS TTL: https://developers.cloudflare.com/dns/manage-dns-records/reference/ttl/
- Cloudflare R2 S3: https://developers.cloudflare.com/r2/get-started/s3/
- Cloudflare R2 tokens: https://developers.cloudflare.com/r2/api/tokens/
- Cloudflare R2 Bucket Locks: https://developers.cloudflare.com/r2/buckets/bucket-locks/
