# Receita: CNAME DNS-only com teste de aplicação real

Esta receita adiciona um alias de aplicação sem alterar a origem XUI. O
O tenant atual é `tvbrasil.phpd77.com`; `cdn.phpd77.com` é somente o pool das edges. Um alias externo deve apontar para o hostname do tenant:

```text
cnxt.vr766.com  CNAME  tvbrasil.phpd77.com  DNS-only  TTL Auto
```

O registro não deve apontar para `38.46.223.77`. Esse IP é origem e deve
continuar protegido pelas ACLs existentes. Também não se deve usar um CNAME na
raiz da zona; aliases como `cnxt.vr766.com` são o formato correto.

## 1. Publicar com segurança

1. No provedor autoritativo, crie `cnxt` na zona `vr766.com`.
2. Selecione `CNAME` e informe `tvbrasil.phpd77.com` como destino.
3. Deixe o proxy/nuvem desligado, isto é, `proxied=false` ou `DNS-only`.
4. Confirme que não existe outro `A`, `AAAA` ou `CNAME` para `cnxt.vr766.com`.
5. Como o modo é DNS-only, a requisição chega à edge com `Host:
   cnxt.vr766.com`. A role `cdn_tenants` instala um fallback controlado no
   vhost padrão: sem cadastrar o alias no banco, ele encaminha somente
   `get.php`, `player_api.php`, HLS e mídia ao tenant principal e envia o
   `Host` canônico ao upstream. Rotas administrativas e desconhecidas continuam
   bloqueadas com `421`.
6. Para HTTPS, emita um certificado que contenha `cnxt.vr766.com` no SAN, via
   DNS-01 ou outro ACME controlado. DNS-only não altera o certificado da edge.
7. Aguarde o TTL e confira de resolvedores externos:

```bash
dig +short CNAME cnxt.vr766.com
dig +short A cnxt.vr766.com
dig +short A cdn.phpd77.com
```

O alias deve resolver para endereços compatíveis com o canônico. Round-robin
DNS não é failover por sessão; retirada automática exige health controller,
fencing e a receita de multi-LB.

Um `HTTP 200` no alias não aprova HTTPS. Confira também:

```bash
curl --fail --max-time 10 https://cnxt.vr766.com/edge-health
openssl s_client -connect cnxt.vr766.com:443 -servername cnxt.vr766.com </dev/null 2>/dev/null \
  | openssl x509 -noout -ext subjectAltName
```

O SAN deve conter `DNS:cnxt.vr766.com`. Sem isso, aplicações que validam TLS
recusarão a conexão mesmo que o Nginx responda corretamente.

## 2. Testar como aplicativo

Use uma conta de laboratório com limite e passe os segredos somente pelo
ambiente ou pelo menu, nunca em arquivo versionado:

```bash
cd /opt/cdnmnus
PLAYER_BASE_CNAME=https://cnxt.vr766.com \
PLAYER_BASE_CDN=https://tvbrasil.phpd77.com \
PLAYER_BASE_DIRECT=http://38.46.223.77 \
PLAYER_USERNAME='usuario-de-laboratorio' \
PLAYER_PASSWORD='senha-de-laboratorio' \
python3 lab-player/scripts/test_playback_flow.py --cname
```

O comando executa, nesta ordem:

1. resolução DNS do alias e comparação com o canônico;
2. handshake `player_api.php`;
3. captura da playlist `get.php` pelo alias;
4. amostras live, filme e série;
5. resposta não vazia e `HTTP 200`;
6. requisição `Range` e `HTTP 206` nos itens VOD;
7. relatório sanitizado em `lab-player/reports/`.

## 3. Operar pelo SSH

No menu local, abra `Capacidade, consumo e saúde do cluster`, escolha
`Testar reprodução pelo CNAME DNS-only` e informe:

- base CNAME: `https://cnxt.vr766.com`;
- base canônica: `https://tvbrasil.phpd77.com`;
- comparação direta: `http://38.46.223.77`;
- usuário e senha exclusivos do laboratório.

O menu executa o mesmo script, mostra o resultado e não grava a senha. O
relatório também remove `username` e `password` das URLs.

## 4. Critérios de aprovação

Considere o alias aprovado somente se todos os itens forem verdadeiros:

- CNAME observado é `tvbrasil.phpd77.com`;
- handshake fica `active`;
- playlist é entregue pelo alias;
- live retorna dados;
- filme/série retornam `206` para `Range`;
- nenhum redirect revela `38.46.223.77` ou o host XUI;
- `/edge-health` permanece saudável em todas as edges publicadas;
- relatório não contém credenciais.

Se qualquer teste falhar, retire o alias do DNS ou desative sua publicação no
control plane, sem mexer na origem durante o diagnóstico. Depois de corrigir,
repita a receita inteira; não valide apenas com `ping`, `HTTP 200` ou uma
página HTML retornada pelo vhost padrão.
