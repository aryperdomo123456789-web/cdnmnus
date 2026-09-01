# Receita de Migração de Domínio, Cloudflare e Edges

Este documento descreve o procedimento oficial para trocar a conta/zona
Cloudflare e o domínio público do CDNMNUS sem abrir o XUI, sem expor a origem e
sem interromper os hosts que já funcionam.

## Objetivo operacional

O cliente sempre acessa um hostname público DNS-only. O edge recebe a
requisição, escolhe o tenant pelo hostname cadastrado e encaminha somente para
upstreams fechados daquele tenant. Quando o XUI responde com um redirect de
live, VOD ou direct source, o broker segue a cadeia apenas se cada salto estiver
autorizado, resolver para IP global e usar porta 80. O `Location` original
nunca é devolvido ao cliente.

O fluxo não é um proxy aberto:

```text
app -> CNAME/A público -> edge -> tenant -> XUI
                                  -> redirect autorizado -> LB/VOD
```

## Estado atual do código

Os pontos de controle são:

- `core/cloudflare_dns.py`: cliente Cloudflare com token Bearer, seleção de
  zona mais específica e somente registros DNS-only.
- `core/dns_reconciler.py`: publica o pool de edges e os CNAMEs dos tenants.
- `core/db.py`: conserva tenants, hosts, upstreams, jobs TLS e configurações.
- `cli/mago_cdn.py`: menu SSH root, confirmação humana e chamada dos serviços.
- `core/tls_provisioner.py`: ACME, SAN, distribuição e health por tenant.
- `core/deploy.py`: release imutável com todos os tenants e snapshots dos
  brokers.
- `panel/token_broker.py`: redirects controlados para live/direct source.
- `core/render_tenants.py`: vhosts Nginx fechados e headers de origem ocultos.

## O que a troca automática cria

Ao trocar para `dominionovo.com`, o menu deriva:

```text
cdn.dominionovo.com
tvbrasil.dominionovo.com    # tenant cujo canonical anterior começava por tvbrasil
xuilab.dominionovo.com      # tenant cujo canonical anterior começava por xuilab
```

Para cada tenant habilitado:

1. o hostname novo é adicionado ao tenant;
2. ele vira o novo `canonical_host`, `health_host` e `playlist_host` quando os
   valores anteriores eram o canonical;
3. o hostname antigo permanece como alias de compatibilidade;
4. `managed_domain` e `managed_canonical_host` são atualizados no banco;
5. um job TLS é enfileirado;
6. a reconciliação cria CNAME DNS-only para o pool novo;
7. a próxima release renderiza todos os hosts antigos e novos.

Nenhum tenant é escolhido pelo índice da lista. O vínculo continua sendo pelo
ID técnico do tenant.

## O que não é feito automaticamente

- O registrador não é alterado.
- Nameservers da nova zona não são trocados.
- O Cloudflare Proxy não é ativado; o modo continua DNS-only.
- Registros arbitrários da zona não são apagados.
- O domínio antigo não é removido na mesma operação.
- Nenhuma edge é promovida sem `preflight`, release, `nginx -t`, reload e
  health.
- Um redirect para hostname não cadastrado não é aprendido nem seguido.

## Procedimento pelo menu SSH

Execute o menu no control-plane, ou em uma edge escolha a opção para abrir o
menu autoritativo do control-plane:

```text
Mago CDN
 -> Infraestrutura e distribuição
 -> DNS e Cloudflare
 -> Trocar Cloudflare e domínio com migração segura
```

Informe:

1. domínio raiz novo, sem esquema ou caminho, por exemplo
   `dominionovo.com`;
2. zonas Cloudflare autorizadas, normalmente o mesmo domínio raiz;
3. token novo com apenas `Zone - DNS - Edit` na zona.

O menu executa `tokens.verify` antes de gravar qualquer configuração e exige
confirmação mostrando apenas os hosts derivados. O token não vai para SQLite,
Git, Ansible, edge, relatório ou mensagem de erro.

Arquivos protegidos usados no control-plane:

```text
/etc/cdnmnus/cloudflare/api-token       root:root 0600
/etc/cdnmnus/cloudflare/zones           root:root 0644
/etc/cdnmnus/secrets/cloudflare_acme.ini root:root 0600
```

O mesmo token é sincronizado para a credencial ACME porque emissão DNS-01 e
reconciliação de registros precisam da mesma zona autorizada. O plugin
`dns-cloudflare` deve existir antes da emissão; o helper ACME recusa continuar
se o plugin, arquivo ou permissões estiverem incorretos.

## Após a confirmação do menu

O menu somente prepara o estado. Execute os gates nesta ordem:

1. `Reconciliar Cloudflare agora` e confirmar que os registros são DNS-only.
2. Processar os jobs TLS; cada tenant precisa de SAN para todos os seus hosts.
3. Visualizar os vhosts renderizados e confirmar os novos `server_name`.
4. Compilar uma release imutável.
5. Executar `preflight-edge.yml` para cada nova edge.
6. Executar `deploy-edge.yml` serialmente.
7. Executar `activate-edge.yml` serialmente.
8. Conferir `nginx -t`, units, sockets, digest e health.
9. Testar `player_api.php`, `get.php`, live, `/movie/` e `/series/`.
10. Só depois remover aliases antigos, se houver decisão operacional explícita.

Exemplo de verificação sem credenciais:

```bash
dig +short A cdn.dominionovo.com
dig +short CNAME tvbrasil.dominionovo.com
sudo nginx -t
systemctl is-active nginx cdnmnus-tenant-broker@xui-tvbrasil.service
systemctl is-active cdnmnus-vod-relay@xui-tvbrasil.service
```

## Direct source e redirects

Se o XUI entregar direct source, o app continua usando o CNAME. O broker:

1. começa na origem cadastrada do tenant;
2. valida DNS e usa um endereço global obtido naquele momento;
3. segue até cinco redirects;
4. exige esquema HTTP, porta 80, caminho absoluto seguro e ausência de
   credenciais no redirect;
5. exige que cada hostname intermediário e o destino final estejam na
   allowlist daquele tenant;
6. retorna uma location interna Nginx, nunca o `Location` do XUI.

Se qualquer salto falhar, o retorno é fail-closed. Não cadastre `0.0.0.0`,
loopback, RFC1918, link-local, multicast, IPv6 não global ou hostname de outro
tenant para forçar um teste verde.

## Cadastro de novas edges

Toda edge nova deve entrar pelo fluxo de onboarding, não por cópia manual:

```text
menu SSH -> Edges -> Cadastrar nova máquina
 -> fingerprint confirmada
 -> chave Ed25519
 -> preflight
 -> pacote imutável
 -> deploy da release
 -> ativação
 -> auditoria
 -> estado ready
```

Uma edge `ready` recebe a mesma release, snapshot de tenants, runtime broker,
relay VOD, certificados e política Nginx. O DNS só publica edges `ready` e o
reconciliador remove edges desabilitadas do pool. O novo domínio não deve ser
apontado para a edge antes de ela possuir o certificado e o vhost.

## Rollback

O rollback é seguro porque a migração é aditiva. Se a nova zona, ACME ou
release falhar:

1. não remova o domínio antigo;
2. mantenha a release anterior ativa;
3. corrija token, SAN, DNS ou edge;
4. repita a reconciliação e os gates;
5. use `rollback-edge.yml` somente para restaurar uma release já conhecida.

Se o token for exposto, revogue-o imediatamente na Cloudflare e execute a
troca novamente com um token novo. Nunca registre URL M3U, senha, token,
certificado ou chave privada.

## Critérios de aceite

- `cdn.<novo-domínio>` resolve somente para edges prontas.
- Cada tenant possui `<label>.<novo-domínio>` no vhost e no certificado.
- `player_api.php` e `get.php` respondem sem origem ou `Location` interno.
- Live/direct source funciona ou falha fechado quando o redirect não é
  autorizado.
- Filmes e séries preservam Range/If-Range e retornam `206` quando a origem
  suporta range.
- Hosts antigos continuam funcionando durante a migração.
- Nenhum tenant usa upstream, socket ou certificado de outro tenant.
- Todas as edges possuem o mesmo `release_id` e `config_digest`.
- `nginx -t`, health, testes Python e laboratório terminam com sucesso.

## Testes do repositório

```bash
cd /opt/cdnmnus
python3 -m unittest discover -s tests -p '*test.py'
bash -n lab-player/scripts/sync_playlist.sh
git diff --check
```

O teste de troca de domínio está em
`tests/domain_migration_test.py`. Ele comprova que os novos canonicals são
criados e os hosts antigos permanecem como aliases.
