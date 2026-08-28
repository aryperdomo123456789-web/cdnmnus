# Auditoria técnica integral — 28/08/2026

## Escopo e método

Foram revisados integralmente o instalador, scripts de atualização/hardening,
template e configuração Nginx gerada, painel Python/SQLite, token broker,
units systemd, testes e estado ativo do host. A validação incluiu compilação
Python, sintaxe Bash, testes HTTP, testes concorrentes do broker, `nginx -t`,
probes públicos/internos, TLS, permissões e análise systemd.

Esta auditoria não promete a inexistência matemática de bugs. O resultado é o
estado comprovado pelos testes descritos abaixo. Também não autoriza tráfego de
origem: a operação deve usar conteúdo e infraestrutura licenciados.

## Resultado executivo

- Nginx, painel e token broker: ativos após a implantação.
- TLS do domínio público: certificado verificado (`ssl_verify_result=0`).
- Painel: somente `127.0.0.1:9090`, exige autenticação.
- Broker: somente `127.0.0.1:9091`, usuário `www-data`, exposição systemd 1.3.
- Rotas resolvidas: `internal`; `Location` e `X-Accel-Redirect` não aparecem
  nas respostas públicas testadas.
- Cache HLS: manifests não são gravados; apenas segmentos com extensão finita
  entram no cache curto.
- Backup anterior à implantação: `/var/backups/cdnmnus/audit-20260828073106`.

## Falhas confirmadas e corrigidas

### 1. Rollback incompleto do banco — crítico

`save_config()` fazia UPSERT. Se a aplicação do Nginx falhasse, restaurar as
chaves antigas não apagava chaves novas. O SQLite e o Nginx poderiam divergir.

Correção: `replace_config()` substitui a tabela `settings` em transação. O
rollback agora restaura exatamente banco, include Nginx e configuração do
broker. Há teste de regressão para remoção das chaves temporárias.

### 2. Primeiro uso podia deixar HLS indisponível — crítico

Em instalação nova, o broker era habilitado sem iniciar quando ainda não havia
JSON. O primeiro salvamento criava rotas dependentes dele, mas não iniciava o
serviço.

Correção: a aplicação inicia/verifica o broker antes de testar e recarregar o
Nginx. Falha em qualquer etapa aciona rollback e restauração do broker.

### 3. Credenciais podiam aparecer no cache em disco — crítico

O cache usava `$request_uri` também para manifests no formato
`/<usuario>/<senha>/<id>.m3u8`. O cabeçalho interno do arquivo de cache poderia
conter essa chave.

Correção: `map` fail-closed desabilita leitura e gravação de cache para tudo que
não seja segmento `.ts`, `.m4s`, `.mp4`, `.aac`, `.mp3` ou `.vtt`. O cache
anterior foi limpo durante a implantação.

### 4. Janela de DNS rebinding no broker — alta

O código validava DNS e depois `HTTPConnection(host)` resolvia o nome novamente.
Um DNS mutável poderia devolver outro endereço entre as duas operações.

Correção: o broker conecta diretamente ao IP previamente resolvido e validado,
preservando o `Host` HTTP necessário. Redirect HTTPS, que este perfil HTTP não
sabe consumir, passou a ser rejeitado explicitamente.

### 5. Instalação remota com painel quebrada — alta

No modo remoto, `--with-panel` não baixava arquivos do painel/broker e falhava
mais tarde.

Correção: os quatro artefatos são validados no modo local e baixados no modo
remoto. O backup de atualização passou a incluir `token-broker.json`.

### 6. Superfície HTTP e recursos — média

Rotas GET desconhecidas do painel retornavam o frontend com 200 e POST aceitava
tipo de conteúdo não JSON. O broker também não possuía tetos explícitos no unit.

Correção: GET desconhecido retorna 404, POST exige `application/json`, e o
broker possui `TasksMax=512` e `MemoryMax=512M`. As units receberam hardening
adicional.

## Reprodução e regressão

Comandos aprovados após as correções:

```bash
python3 -Wd -m py_compile panel/panel.py panel/token_broker.py
bash -n install.sh install-from-github.sh scripts/*.sh tests/smoke.sh
python3 tests/panel_http_test.py
python3 tests/token_broker_test.py
bash tests/smoke.sh
nginx -t
```

Os testes cobrem autenticação, troca obrigatória de senha, 404, Content-Type,
substituição exata de configurações, parser/traversal, allowlist, bloqueio de
porta/HTTPS, IP fixado após validação, singleflight, refresh, instalação em
dry-run, sysctl, firewall e renderização Nginx.

## Reprodução longa: live e VOD

Uma sessão de cinco horas em um aplicativo não é uma conexão HTTP única em
HLS. O player atualiza manifests e baixa segmentos sucessivos. O desenho atual
mantém o cliente inteiramente no domínio CDN; só o broker local consulta a
origem quando o mapeamento expira ou um segmento devolve erro elegível.

- Manifest: nunca cacheado em disco, para evitar congelamento e credenciais.
- Segmento: cache de 6 segundos e `cache_lock`, reduzindo rajadas na origem.
- Token: TTL de 15 segundos, singleflight por URI e refresh forçado em
  401/403/404/410/5xx.
- VOD: `proxy_buffering off`, suporte transparente a `Range`/206 e timeout de
  leitura de 300 segundos. O timeout mede inatividade entre leituras, não a
  duração total do filme; portanto não impõe limite de três horas.
- Live: cada segmento é uma requisição finita; não existe expiração global de
  cinco horas na CDN.

Para homologação antes de produção, execute um soak autorizado de 6 horas com
um player real e registre somente métricas sem URI: erros por status, p95/p99,
bytes/s, reconexões e consumo de CPU/RAM. Teste unitário ou benchmark curto não
substitui essa prova temporal.

## Capacidade real

Não existe número universal de “clientes”. O teto depende principalmente de
`largura_de_banda_disponivel / bitrate_médio`, descontando 20–30% de margem.
Em 1 Gbit/s útil e streams de 8 Mbit/s, por exemplo, o limite de rede é da ordem
de 87 clientes com 30% de margem; em 4 Mbit/s, cerca de 175. Cache só economiza
tráfego da origem quando clientes assistem ao mesmo segmento; ele não reduz os
bytes enviados aos espectadores.

O benchmark já realizado do caminho do broker respondeu corretamente até 500
requisições concorrentes, mas isso não equivale a 500 vídeos simultâneos. Para
certificar capacidade é necessário medir a interface e o bitrate real durante
um teste autorizado, evitando atingir o XUI de terceiros de forma destrutiva.

## Riscos que permanecem fora da CDN

1. O XUI continua acessível diretamente enquanto firewall/security group não
   aceitar porta 80 somente a partir dos IPs desta CDN. Sem essa ACL, “nunca
   vazar” não pode ser garantido pela máquina CDN.
2. O primeiro pedido deliberadamente feito em HTTP chega ao servidor antes do
   308; HSTS protege clientes após o primeiro contato. Playlists já usam HTTPS.
3. O broker VOD agora é fail-closed e os relays são internos. Mudança do
   fornecedor exige atualização explícita da allowlist.
4. O endpoint `/edge-health` e o procedimento de failover estão prontos, mas
   alta disponibilidade exige uma segunda edge física e configuração DNS/LB.
5. Monitoramento sanitizado está ativo a cada minuto. O soak de seis horas
   exige uma URL autorizada em arquivo modo 0600.

## Nota profissional honesta

No host atual: **8,5/10 em segurança/engenharia da CDN**. HTTPS obrigatório,
broker VOD fail-closed e monitoramento já estão ativos. 10/10 ainda depende de
ACL na origem, uma segunda edge física/DNS failover e soak real de seis horas.
Esses itens não podem ser substituídos por uma afirmação de marketing.
