# Receita simples: Cloudflare API e proteção dos XUIs

**Estado real:** o cliente Cloudflare e o reconciliador local já existem em
`core/cloudflare_dns.py` e `core/dns_reconciler.py`. A escrita real depende de
token/zona configurados e continua sujeita aos gates de produção. O código
atual ainda publica o pool de edges prontas; ele não deve ser tratado como o
failover final do LB `.237` até existir VIP/fencing ou publicação controlada do
endpoint LB.

Esta é a sequência oficial para o operador. O sistema controla somente DNS
das zonas informadas, sempre `DNS-only`, e nunca envia o IP da origem para a
Cloudflare. `cdn.phpd77.com` é somente o pool DNS-only das edges; nenhum XUI fica associado a ele. O tenant atual é `xui-tvbrasil`, publicado em `tvbrasil.phpd77.com`, com origem privada `38.46.223.77`.

## 1. Preparar uma vez no control-plane

Use um token da Cloudflare com apenas `Zone - DNS - Edit` nas zonas necessárias.
Não use Global API Key, não cole o token no SQLite e não o coloque em Ansible,
Git ou edge.

Crie-o no painel em **My Profile -> API Tokens -> Create Token -> Create
Custom Token**. A documentação oficial é
<https://developers.cloudflare.com/fundamentals/api/get-started/create-token/>.

```bash
install -d -m 700 /etc/cdnmnus/cloudflare
install -m 600 /dev/null /etc/cdnmnus/cloudflare/api-token
vi /etc/cdnmnus/cloudflare/api-token
printf '%s\n' 'phpd77.com' > /etc/cdnmnus/cloudflare/zones
chmod 644 /etc/cdnmnus/cloudflare/zones
```

Para este projeto, `tvbrasil.phpd77.com`, novos aliases e `cdn.phpd77.com` pertencem à zona `phpd77.com`. Aliases de outra zona, como `cnxt.vr766.com`, são mantidos no provedor externo.

Teste sem revelar o token:

```bash
cd /opt/cdnmnus
./venv/bin/python cli/admin_cli.py --db /var/lib/cdnmnus-admin/admin.db dns cloudflare-reconcile
```

## 2. Estado que a reconciliação aplica

O reconciliador atualmente aplica, quando autorizado, estas regras:

```text
cdn.phpd77.com A 143.14.168.168 DNS-only
cdn.phpd77.com A 143.14.168.170 DNS-only
```

O `.237` não entra nesse pool enquanto for LB. O sistema publica somente
edges com estado `ready`; edge `pending`, `bootstrapping`, `draining`,
`failed` ou `disabled` não entra no DNS. A operação substitui todos os A,
AAAA e CNAME do hostname canônico para impedir estado misto.

Importante: isso é limitado ao hostname controlado `cdn.phpd77.com`. Outros
subdomínios existentes na zona nunca são varridos, editados ou excluídos.
Aliases só são alterados quando estão registrados no tenant correspondente.

Para cada novo tenant/alias cadastrado:

```text
gomes.phpd77.com CNAME cdn.phpd77.com DNS-only
```

Qualquer A/AAAA anterior do alias é removido. O CNAME nunca aponta para o IP
do XUI. O Nginx aceita somente as rotas de aplicação previstas e transforma a
playlist para usar sempre o host canônico, evitando vazamento do origin ou do
alias no conteúdo M3U. O acesso direto a `cdn.phpd77.com` responde `421` e não entrega o XUI. O tenant atual é `tvbrasil.phpd77.com -> xui-tvbrasil -> 38.46.223.77`.

## 3. Cadastrar um novo XUI pelo SSH

1. Entre como root em qualquer VPS que tenha o menu comum.
2. Se estiver em uma Edge/LB, escolha `Abrir menu do Control Plane
   (DNS/Cloudflare/XUI)`. O token será tratado somente no control-plane.
3. Abra `Infraestrutura e distribuição` -> `DNS e matriz de distribuição`.
4. Escolha `Configurar conta/token Cloudflare`.
5. Informe `phpd77.com` e cole o token no campo protegido. O menu
   valida o token e grava somente em `/etc/cdnmnus/cloudflare/api-token` com
   modo `0600`.
6. Escolha `Reconciliar Cloudflare agora` e confirme para corrigir o pool.
7. Abra `XUIs, domínios e conteúdo` -> `Nova arquitetura` -> `Cadastrar novo XUI/tenant`.
8. Informe o IP do novo XUI como origem, a porta real, e o hostname público,
   por exemplo `gomes.phpd77.com`.
9. O menu grava o tenant, remove A/AAAA conflitantes e cria/atualiza
   `gomes.phpd77.com CNAME cdn.phpd77.com DNS-only` pela API. Um alias externo pode apontar para o hostname do tenant, por exemplo `cnxt.vr766.com CNAME tvbrasil.phpd77.com`.
10. Gere uma implantação para instalar a configuração nas edges. O XUI nunca
   é publicado diretamente no DNS.

Para a topologia de produção desejada, a publicação final deve ser alterada
para o endpoint do LB ativo (`.237` ou VIP), depois de lease e fencing
confirmados. Não publique `.237` manualmente e não mantenha simultaneamente o
pool de edges e o endpoint LB sem uma decisão arquitetural registrada.

Se a API falhar, o cadastro local permanece identificado como não aplicado e
o menu manda repetir a reconciliação. Não considere o hostname pronto nesse
caso.

## 4. Verificação externa e laboratório

```bash
dig +short CNAME gomes.phpd77.com
dig +short A cdn.phpd77.com
```

O primeiro comando deve mostrar `cdn.phpd77.com.`. O segundo deve mostrar
somente `143.14.168.168` e `143.14.168.170` quando essas duas edges estiverem
`ready`.

Depois execute o laboratório oficial com uma conta exclusiva de teste:

```bash
cd /opt/cdnmnus
PLAYER_BASE_CNAME='http://gomes.phpd77.com' \
PLAYER_BASE_CDN='http://cdn.phpd77.com' \
PLAYER_USERNAME='usuario-de-laboratorio' \
PLAYER_PASSWORD='senha-de-laboratorio' \
./venv/bin/python lab-player/scripts/test_playback_flow.py --cname
```

A aprovação exige playlist `200`, live funcionando, VOD com `206` em Range,
nenhuma ocorrência do IP/origem ou hostname interno no M3U e relatório sem
credenciais. HTTP e HTTPS são aceitos pelo laboratório; HTTPS ainda exige
certificado com SAN do alias.

## 5. Auditoria, rollback e emergência

Os eventos ficam em `dns_reconciliation_events`, com hostname, tipo, desejado,
resultado, operador e erro sanitizado:

```bash
sqlite3 /var/lib/cdnmnus-admin/admin.db \
  'select created_at,action,hostname,record_type,result,error from dns_reconciliation_events order by created_at desc limit 30;'
```

Para retirar uma edge, marque-a como `draining` no menu e execute a
reconciliação. Para recolocá-la, só use `ready` depois do health check e de uma
implantação validada. Não edite o DNS manualmente para “compensar” o banco:
reconcilie o estado autoritativo pelo menu.

Se o token foi exposto, revogue-o na Cloudflare e instale outro com modo
`0600`. As credenciais usadas em URLs de laboratório devem ser trocadas
imediatamente; nunca as repita em tickets, logs ou documentação.
