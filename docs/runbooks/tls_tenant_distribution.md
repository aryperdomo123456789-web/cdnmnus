# Distribuição TLS por Tenant

**Escopo:** emitir, distribuir e validar certificados para tenants nas edges
`.168` e `.170`, sem promover o LB `.237`, alterar DNS/VIP ou interromper o
`.111`. Este runbook não autoriza execução automática em produção.

## Contrato

Para cada tenant habilitado, o certificado deve cobrir todos os hosts
publicados em `tenant_hosts`. O `health_host` é o host configurado no tenant e,
na ausência dele, é o `canonical_host`. O renderer em
`core/render_tenants.py` usa esse valor no `server_name` e no probe
`/edge-health`; o preflight usa o mesmo valor. Não remova a proteção `421` do
vhost default.

No estado de referência, o tenant `xui-tvbrasil` exige
`tvbrasil.phpd77.com`. O certificado antigo `cdn.phpd77.com` não o cobre.

## Pré-requisitos

No control-plane, confirmar primeiro:

```bash
cd /opt/cdnmnus
sqlite3 /var/lib/cdnmnus-admin/admin.db \
  "select id,canonical_host,enabled from xui_tenants;"
openssl x509 -in /etc/letsencrypt/live/tvbrasil.phpd77.com/fullchain.pem \
  -noout -subject -issuer -dates -ext subjectAltName
```

O SAN precisa conter exatamente `DNS:tvbrasil.phpd77.com` ou um wildcard que
legalmente o cubra. A chave privada deve permanecer com modo `0600`.

## Emissão

Prefira ACME DNS-01 no control-plane. Gere SANs a partir do banco, nunca de uma
lista manual. Mantenha o certificado anterior até a nova cadeia e sua chave
passarem nas verificações. O segredo DNS deve estar em arquivo `0600` e não
pode aparecer em logs.

## Distribuição atômica

Configure `/etc/cdnmnus/tls-edges.conf` em modo `0600`, uma edge por linha:

```text
edge168|143.14.168.168|22|cdn-deploy|/etc/cdnmnus/ssh/edge168.ed25519|/etc/cdnmnus/ssh/known_hosts
edge170|143.14.168.170|22|cdn-deploy|/etc/cdnmnus/ssh/edge170.ed25519|/etc/cdnmnus/ssh/known_hosts
```

O hook já existente distribui o par sem escrever o arquivo final pela metade:

```bash
chmod 0600 /etc/cdnmnus/tls-edges.conf
RENEWED_LINEAGE=/etc/letsencrypt/live/tvbrasil.phpd77.com \
  /opt/cdnmnus/scripts/distribute_tls.sh
```

O instalador remoto `scripts/install_tls_from_stdin.sh` verifica fingerprint,
validade mínima, correspondência entre chave e certificado, grava em arquivos
temporários, move os dois atomicamente, executa `nginx -t` e só então faz
`systemctl reload nginx`. Não use `restart` nem copie somente a chave.

## Validação por edge

Execute após a distribuição, sem desabilitar verificação TLS:

```bash
for ip in 143.14.168.168 143.14.168.170; do
  curl --resolve tvbrasil.phpd77.com:443:$ip \
    --fail --silent --show-error --http2 \
    -D /tmp/headers-$ip \
    https://tvbrasil.phpd77.com/edge-health -o /dev/null
  grep -q '^HTTP/2 200' /tmp/headers-$ip
done
```

Audite SAN e configuração em cada edge:

```bash
ssh cdn-deploy@143.14.168.168 \
  'sudo nginx -t && sudo openssl x509 -in /etc/letsencrypt/live/tvbrasil.phpd77.com/fullchain.pem -noout -ext subjectAltName'
ssh cdn-deploy@143.14.168.170 \
  'sudo nginx -t && sudo openssl x509 -in /etc/letsencrypt/live/tvbrasil.phpd77.com/fullchain.pem -noout -ext subjectAltName'
```

Depois, execute o gate read-only do candidato:

```bash
/opt/cdnmnus/scripts/cdnmnus-lb-candidate-preflight \
  --node 45.140.192.237
```

`421` significa hostname fora do vhost e `curl` exit `60` significa SAN/SNI
incorreto. Corrija o certificado ou o contrato de host; nunca use
`--insecure` como aceite.

## Estado e rollback

Não marque `tls_status=valid` apenas porque os arquivos existem. A transição
só é aceitável depois de SAN, `nginx -t` e `/edge-health` `200` em todas as
edges. A atualização administrativa do estado deve registrar hostname,
fingerprint, vencimento e evidência, nunca chave ou token.

Se qualquer edge falhar, não altere a outra: mantenha o tenant como pendente,
preserve o certificado anterior naquela edge e corrija a candidata. Para
rollback, restaure o par anterior pelo mesmo instalador atômico, repita
`nginx -t`, reload e o probe com SNI. Não altere `.111`, `.237`, DNS ou VIP
durante o rollback.

## Capacidade do LB candidato

Somente após receber o contrato assinado, o operador pode registrar o payload
abaixo pelo caminho administrativo de `TopologyStore`:

```json
{"node_id":"4","capacity_mbps":5000,"confidence":"contracted","source":"provider-contract","expires_at":"2026-12-31T23:59:59Z"}
```

O método `register_contracted_capacity` é idempotente, exige nó com papel
`load_balancer`, rejeita contrato expirado e grava evento sanitizado. Ele não
concede lease, fencing, promoção ou tráfego. Sem evidência do provedor, deixe
o perfil ausente e mantenha `promotion_allowed=false`.
