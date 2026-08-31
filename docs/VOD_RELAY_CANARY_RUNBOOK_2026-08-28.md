# Homologação isolada do relay VOD privado

**Estado:** ativa em `.168/.170`; aceite funcional curto e rollback real
aprovados em 31/08/2026; soak prolongado ainda aberto
**Componente:** `panel/vod_relay.py`
**Escopo:** somente `/movie/` e `/series/`

## Resultado implementado

O relay lê a origem e as seeds do snapshot imutável do tenant. A conexão começa
no XUI, exige uma seed cadastrada no primeiro redirect e permite hosts
posteriores somente quando vierem do `Location` da conexão anterior. Cada salto
resolve todos os endereços, rejeita o hostname inteiro se algum IP não for
global e abre TCP diretamente no IP escolhido. Em HTTPS, o hostname validado é
mantido no `Host`, no SNI e na verificação do certificado.

O cliente recebe somente `200` ou `206` sanitizado. `Location`, `Server`,
`Set-Cookie`, `Via`, `X-Powered-By`, `X-Accel-Redirect`, URLs e destinos não são
propagados pelo renderer. `Range`, `If-Range`, `Content-Range` e streaming sem
buffer integral são preservados.

## Como executar o canário sem tocar no runtime vivo

Use um tenant e um socket exclusivos. Não reutilize
`/run/cdnmnus/broker-*.sock` e não altere includes ativos:

```bash
CDNMNUS_TENANT_ID=xui-canary \
CDNMNUS_TENANTS_CONFIG=/caminho/imutavel/tenants.json \
CDNMNUS_VOD_RELAY_SOCKET=/run/cdnmnus/vod-relay-xui-canary.sock \
python3 /opt/cdnmnus/panel/vod_relay.py
```

O snapshot continua com `schema_version: 1` para não quebrar o broker HLS
existente e recebe o campo aditivo `vod_policy`. Releases antigas que tenham
somente `vod_hosts` são aceitas temporariamente, com esquema inferido pela
porta. Antes da ativação, gere uma release candidata e confirme que ela contém:

```text
upstream vod_relay_<tenant> -> unix:/run/cdnmnus/vod-relay-<tenant>.sock
/movie e /series -> vod_relay_<tenant>
nenhum proxy_pass dinâmico baseado em hostname
nenhuma location __cdnmnus_*_dynamic_vod
```

## Testes reproduzíveis

```bash
cd /opt/cdnmnus
python3 tests/vod_relay_test.py
PYTHONPATH=. python3 tests/admin_core_test.py
python3 tests/multi_tenant_broker_test.py
python3 tests/token_broker_test.py
git diff --check
```

Os testes VOD usam origens falsas e fábricas de conexão injetadas; provam a
cadeia XUI -> seed -> host desconhecido, pinning no IP validado, `Host`, HTTPS
com SNI, `Range`/`If-Range`, seed obrigatória, DNS misto/privado bloqueado,
traversal e multi-range recusados. Nenhum teste acessa o Nginx ou fornecedores
reais.

## Evidência do canário isolado desta implementação

Foi gerada em `/dev/shm` uma release a partir do banco atual, sem alterar o
symlink ativo nem os includes públicos. O verificador independente recalculou
os hashes dos sete artefatos e confirmou o digest. O relay dessa release foi
iniciado em socket exclusivo e produziu:

```text
release verificada:              PASS
socket Unix isolado:             PASS
GET /health:                     HTTP 200
rota fora de /movie e /series:   HTTP 400
Nginx público alterado:          não
tráfego/DNS alterado:            não
```

As duas seeds administradas do tenant resolveram apenas para IPs públicos e
aceitaram TCP/80. Não foi usada credencial de playlist e, portanto, esta rodada
não comprova reprodução/seek real.

## Gates ainda abertos

Esta versão usa `ThreadingMixIn` e `http.client` da biblioteca padrão porque Go
não está instalado no host de desenvolvimento. Ela é adequada para homologação
funcional isolada, não para tráfego VOD de produção em alta concorrência.

Antes da promoção são obrigatórios:

1. portar o mesmo contrato para Go ou Rust, ou aprovar formalmente benchmarks e
   limites do processo Python;
2. o serviço, usuário sem privilégios, socket e artefatos já estão empacotados
   na release; ainda falta impor e homologar isolamento de egress por processo;
3. testar certificados TLS reais, IPv4/IPv6, timeout e rotação DNS em canário;
4. executar player/seek real, arquivo maior que três horas, carga e soak de seis
   horas;
5. validar limites de concorrência, memória, cancelamento e backpressure;
6. testar rollback da release e health do relay antes de inserir a edge no pool;
7. cadastrar a seed correta no tenant; nenhum hostname é hardcoded no código.

Na fotografia original de 28/08, esses gates bloqueavam apontar `/movie/` e
`/series/` públicos para o relay. A atualização abaixo registra a ativação
posterior; os gates prolongados continuam bloqueando a migração para LB.

## Atualização operacional de 31/08/2026

A causa do `403 Assinatura inválida` foi confirmada na release antiga: o broker
legado entregava ao Nginx uma rota interna dinâmica e o segundo parse alterava
escapes percentuais do caminho assinado. A release
`20260829012407-d60cfdbf` remove esse caminho dinâmico do Nginx e transmite o
VOD no próprio relay após validar e fixar cada salto.

Evidência observada diretamente em `.168` e `.170`:

```text
release/digest iguais:              PASS
relay e broker ativos:              PASS
nginx -t e health/TLS:              PASS
live:                               HTTP 200
filme e série, Range inicial:       HTTP 206
seek intermediário e suffix:       HTTP 206
HEAD filme e série:                HTTP 200
16 seeks concorrentes de 4 KiB:     16/16 HTTP 206
Location/header interno da origem:  AUSENTE
restart do relay:                   zero
rollback real na .168:              PASS
reativação da candidata:          PASS
```

O rollback restaurou a release `20260829154052-a11e5eed`, sua unit antiga e a
ausência anterior de `current.json`; cinco health consecutivos passaram e o
`403` histórico reapareceu, comprovando a relação causal. Depois a candidata
foi reativada e o VOD voltou a `206`.

A ativação funcional foi concluída, mas continuam obrigatórios antes da
migração para LB: player real fixado por edge, arquivo superior a três horas,
carga representativa e soak de seis horas.
