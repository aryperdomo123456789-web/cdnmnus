# Estado atual da entrega VOD — 28/08/2026

## Resumo executivo

Há evidência de que filmes e séries podem reproduzir pela CDN atualmente,
porque a configuração ativa da edge contém rotas `/movie/` e `/series/`, broker
VOD e uma allowlist legada. Porém, a configuração ativa não está totalmente
alinhada ao banco do novo plano de controle. Portanto, o funcionamento observado
no iBO Player Pro é real, mas não deve ser tratado como configuração
reprodutível e gerenciada pelo `mago-cdn` até essa divergência ser eliminada.

## Evidências coletadas

### Serviços

No momento da verificação, `nginx`, `cdnmnus-token-broker.service` e
`cdnmnus-admin.service` estavam ativos. O orquestrador estava em processamento
de ativação/deploy e deve ser revalidado antes de qualquer nova publicação.

### Runtime ativo

`/etc/cdnmnus/token-broker.json` contém, de forma sanitizada:

```json
{
  "public_host": "cdn.phpd77.com",
  "origin_host": "38.46.223.77",
  "load_balancers": ["38.190.176.172"],
  "vod_hosts": ["servicedovod.lat", "<destino-workers-legado>"]
}
```

O vhost ativo `/etc/nginx/conf.d/99-cdnmnus-upstream.conf` possui:

- localização pública para `/movie/` e `/series/` encaminhada ao broker;
- rotas internas VOD para resolução e retry;
- upstream para `servicedovod.lat`;
- proxy de redirects dinâmicos validados pelo broker;
- suporte a `Range`/resposta parcial no caminho VOD.

`zjo.lat` não aparece na configuração ativa verificada.

### Plano de controle

No banco administrado por `core/db.py`, o tenant `xui-principal` possui apenas:

```text
origin: 38.46.223.77:80
lb:     38.190.176.172:80
vod:    nenhum
```

O código novo suporta `kind='vod'` e `vod_hosts`, mas as fontes ainda não foram
cadastradas pelo painel nem distribuídas por uma release do novo plano.

## Fluxo efetivo da reprodução

O fluxo atualmente observado/implementado é:

```text
iBO Player Pro
  -> cdn.phpd77.com/movie ou /series
  -> Nginx da edge
  -> token broker VOD
  -> servicedovod.lat (allowlist legada)
  -> redirects HTTP limitados e validados
  -> caminho interno da CDN
  -> MP4/Ranges entregues ao player
```

O cliente não deve receber o `Location` original do fornecedor nem o endereço
interno do XUI quando a rota legada está funcionando corretamente. A proteção
não é, entretanto, equivalente a uma configuração multi-edge reproduzível,
porque o destino legado não está modelado no banco novo.

## O que foi e não foi comprovado

Comprovado nesta auditoria:

- existência das rotas VOD no Nginx ativo;
- broker VOD ativo;
- `servicedovod.lat` presente na allowlist/runtime ativo;
- configuração pública usando `cdn.phpd77.com`.

Não comprovado nesta coleta:

- identificação da sua sessão individual no iBO Player Pro;
- continuidade de uma sessão durante troca de edge;
- `zjo.lat` funcionando em produção;
- VOD configurado de forma idêntica nas três edges;
- Range/seek/refresh com uma credencial real nesta execução;
- cache compartilhado entre edges;
- failover automático.

Não foi feita tentativa de rastrear sua conexão individual. Sem um correlation
ID fornecido pelo próprio usuário, isso seria invasivo e não é necessário para
validar o caminho técnico. A coleta deve permanecer agregada e sanitizada.

## Riscos atuais

1. O runtime ativo usa configuração legada, enquanto o banco novo informa zero
   fontes VOD.
2. Uma nova publicação pelo painel pode substituir a configuração que hoje está
   funcionando e remover a allowlist legada.
3. `zjo.lat` não está liberado; séries que dependam dele podem falhar.
4. A mesma allowlist e chave de token precisam existir em cada edge antes de
   qualquer balanceamento.
5. Os A records públicos continuam sendo round-robin DNS, sem failover de
   sessão garantido.

## Ações necessárias para preservar e tornar reproduzível

1. Congelar a configuração atualmente funcional e gerar hash/manifesto sem
   incluir credenciais.
2. Cadastrar `servicedovod.lat:80` e `zjo.lat:80` como upstreams VOD no banco
   novo, após confirmar autorização e disponibilidade.
3. Gerar release e comparar o vhost renderizado com o vhost ativo.
4. Fazer deploy serial em homologação, validando filme e série com `Range`,
   seek e refresh.
5. Promover somente após `nginx -t`, health, teste de reprodução e rollback.
6. Repetir a mesma release nas três edges; não editar vhosts manualmente.
7. Só depois integrar o pool ao Load Balancer planejado.

## Conclusão

O fato de a reprodução estar funcionando é compatível com o runtime legado
ativo e constitui evidência útil, mas não prova que o novo plano de controle
esteja entregando VOD. O próximo passo seguro é alinhar a configuração legada
funcional ao modelo `vod_hosts` do painel, preservando backup e rollback antes
de alterar o DNS ou introduzir o Load Balancer `143.14.168.66`.
