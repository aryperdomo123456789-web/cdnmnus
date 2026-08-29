# Auditoria do upstream Nginx sem resolução DNS — 28/08/2026

## Sumário executivo

O comando `nginx -t` falha porque a configuração ativa contém o upstream:

```text
fragrant-harbor-683b.2dzncf9igp3u.workers.dev:80
```

Esse hostname não resolve no DNS (`socket.gaierror: [Errno -2] Name or service
not known`). O domínio `servicedovod.lat`, usado pela outra fonte VOD, resolve
normalmente. Portanto, o problema não é sintaxe do Nginx nem a página pública:
é uma referência de origem VOD removida, expirada ou digitada incorretamente.

## Evidências coletadas

### Teste de resolução

```text
fragrant-harbor-683b.2dzncf9igp3u.workers.dev -> Name or service not known
servicedovod.lat -> 104.21.6.220, 172.67.135.82 (+ IPv6)
cdn.phpd77.com -> 143.14.168.111, 143.14.168.168, 143.14.168.170
```

O `curl` para o Workers também falha antes de abrir conexão:

```text
curl: (6) Could not resolve host: fragrant-harbor-683b.2dzncf9igp3u.workers.dev
```

### Local exato da falha

`/etc/nginx/conf.d/99-cdnmnus-upstream.conf`, linha 47:

```nginx
upstream cdnmnus_vod_storage {
    server fragrant-harbor-683b.2dzncf9igp3u.workers.dev:80 ...;
}
```

O mesmo destino é usado nas localizações internas `/__cdnmnus_vod_1/` e
`/__cdnmnus_vod_retry_1/`.

### Origem no código

O hostname está fixado em `/opt/cdnmnus-panel/panel.py`:

- geração do bloco `upstream cdnmnus_vod_storage`;
- `proxy_set_header Host` das rotas VOD;
- lista `vod_hosts` gravada no `token-broker.json`.

Assim, qualquer aplicação do painel regenera a referência inválida, mesmo que
o restante da configuração esteja correto.

## Por que o serviço ainda está ativo?

`nginx.service` está `active (running)` desde 03:18 UTC. O processo master
continua usando a última configuração aceita em memória. O arquivo atual pode
ser inválido para uma nova inicialização sem que o processo já executando seja
interrompido.

Isso cria um risco operacional importante:

1. `nginx -t` falha;
2. um `reload` seguro é recusado;
3. um `restart` pode deixar a VPS sem Nginx;
4. o painel não consegue publicar novas alterações, pois valida com `nginx -t`;
5. somente a rota VOD que usa a fonte Workers é afetada diretamente, mas a
   configuração inválida ameaça todas as rotas em um reinício.

## Diagnóstico de causa-raiz

**Causa primária:** origem VOD Workers hardcoded e atualmente inexistente no
DNS.

**Causas contribuintes:**

- ausência de validação DNS das fontes em cada geração do include;
- fonte VOD legada mantida no código mesmo após a troca de fornecedor;
- configuração efetiva divergente do banco/controle de fontes VOD;
- o gerador trata a fonte como obrigatória, embora ela possa estar indisponível.

Não há evidência de que a troca recente da página raiz tenha causado o erro.
Também não há evidência de falha no `servicedovod.lat` nesta auditoria.

## Impacto funcional

- Filmes/séries roteados para `servicedovod.lat` continuam tecnicamente
  possíveis.
- Filmes/séries que dependam do Workers inválido falham na resolução ou no
  retry.
- O problema pode reaparecer em qualquer edge que receba essa configuração.
- O painel fica impedido de aplicar mudanças até o upstream ser corrigido ou
  removido.

## Correção recomendada (ordem segura)

1. Confirmar o novo domínio VOD autorizado e testar `A/AAAA`, TCP/80 ou TCP/443
   e resposta HTTP/Range.
2. Remover o Workers inválido da configuração, ou substituí-lo pelo domínio
   confirmado; não apontar para IP arbitrário nem criar proxy aberto.
3. Alterar o gerador (`panel.py`) e o broker para ler a lista de fontes VOD de
   configuração administrada, sem hostname hardcoded.
4. Durante a geração, resolver cada fonte e abortar antes de escrever/recarregar
   se alguma fonte obrigatória não resolver.
5. Gerar o include, executar `nginx -t`, e somente então fazer reload.
6. Validar `/edge-health`, filme, série, `Range` e refresh de token.
7. Distribuir a mesma release às três edges e registrar o hash do include.

Até a confirmação de um novo fornecedor, a ação mais segura é desativar a
fonte Workers inválida e manter apenas as fontes VOD autorizadas e resolvíveis.

## Critério de conclusão

Considerar o incidente encerrado somente quando:

```text
nginx -t                         -> syntax is ok / test is successful
systemctl reload nginx           -> sucesso
DNS de todas as fontes VOD       -> resolve em cada edge
/edge-health                     -> HTTP 200 nas três edges
filme e série                    -> reprodução + seek/Range + refresh
nova publicação do painel       -> concluída sem reintroduzir o hostname
```

Nenhuma credencial, playlist ou token foi incluído nesta documentação.

## Especificação de evolução da camada VOD

A arquitetura segura para manter a cadeia de redirects de filmes e séries
inteiramente atrás da CDN, aceitando fornecedores posteriores descobertos a
partir de uma seed administrada, está definida em
[VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md](VOD_PRIVATE_REDIRECT_RELAY_IMPLEMENTATION.md).

Essa especificação não altera o diagnóstico desta auditoria: o Workers legado
continua precisando ser removido antes de qualquer reload ou rollout.

## Atualização de remediação — 28/08/2026

O Workers legado foi removido do gerador e do include candidato, mantendo apenas
seeds VOD administradas. Foram preservados backups do include e da configuração
do broker. `nginx -t` passou, `/edge-health` respondeu `200` antes e depois, e o
reload controlado foi concluído sem alterar os blocos HLS/live.
