# Prompt documentado e plano de execução

**Data-base:** 2026-09-02
**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)

Este documento consolida o prompt recebido em uma peça operacional única.
Ele não substitui os runbooks existentes; ele os organiza e deixa explícito o
que precisa ser validado, em que ordem, e quais gates não podem ser ignorados.

## 1. Objetivo consolidado

O sistema deve permitir onboarding e operação de novos tenants/XUIs com:

- descoberta automática de CNAME externo por canonical habilitado;
- separação rígida entre alias público, canonical do tenant e origem interna;
- TLS por tenant com SAN válido e instalação transacional;
- playlists públicas sem vazamento de usuário, senha, token, IP ou host interno;
- broker interno com fail-closed, isolamento por tenant e headers sanitizados;
- testes automatizados e validação por laboratório antes de qualquer rollout.

## 2. Documentos-fonte obrigatórios

O prompt recebido é a composição operacional destes documentos:

1. [RECIPE_AUTOMATIC_CNAME_TENANT_DISCOVERY.md](RECIPE_AUTOMATIC_CNAME_TENANT_DISCOVERY.md)
2. [CNAME_DNS_ONLY_AND_LAB_RECIPE.md](CNAME_DNS_ONLY_AND_LAB_RECIPE.md)
3. [RECIPE_CERTIFICATES_OPAQUE_PLAY_TOKENS_MULTI_XUI_CACHE.md](RECIPE_CERTIFICATES_OPAQUE_PLAY_TOKENS_MULTI_XUI_CACHE.md)
4. [TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md](TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md)
5. [DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md](DOCS_INDEX_AND_OPERATIONAL_RECIPE_2026-08-29.md)
6. [RECIPE_AUTOMATIC_CNAME_TENANT_DISCOVERY_EXECUTION_2026-09-02.md](RECIPE_AUTOMATIC_CNAME_TENANT_DISCOVERY_EXECUTION_2026-09-02.md)
7. [docs/runbooks/tls_provisioner_lifecycle.md](runbooks/tls_provisioner_lifecycle.md)

## 3. Invariantes que não podem ser violadas

- O alias externo pode ser desconhecido do banco, mas só é aceito se a cadeia
  DNS terminar exatamente em um `canonical_host` habilitado.
- Alias inválido, ambíguo, privado, circular, desconhecido ou com destino IP
  deve retornar `421`.
- Nunca usar `Host`, query string ou header do cliente diretamente em
  `proxy_pass`.
- Nunca usar `sub_filter` como autorização.
- O XUI/origem permanece interno; o cliente só recebe URLs públicas da CDN.
- HTTPS só é considerado pronto quando o certificado cobre o alias no SAN ou
  a terminação TLS está comprovadamente protegida.
- Nenhum novo tenant pode ficar parcialmente publicado.
- Nenhum fluxo funcional atual pode regredir sem rollback comprovado.

## 4. Ordem de execução pretendida

1. Auditar o estado atual do código, documentação, testes, manifesto, Ansible,
   Nginx e Git.
2. Garantir o fluxo CNAME automático, incluindo resolução da cadeia,
   rejeição de IP/privado/loop e mapeamento canonical -> tenant.
3. Garantir o fluxo de instalação com manifesto, Ansible, `nginx -t`, health e
   rollback.
4. Garantir o fluxo de playlist com token opaco, expiração, revogação e
   isolamento por tenant.
5. Garantir o broker com allowlist, DNS pinning, retry limitado e remoção de
   headers internos.
6. Cobrir todos os cenários em testes unitários e de integração.
7. Executar validações locais e de laboratório antes de qualquer publicação.
8. Publicar somente se todos os gates passarem.

## 5. Estado real encontrado no repositório

Já existem, em algum nível, os seguintes componentes:

- `core/cname_discovery.py` com descoberta e fail-closed;
- `panel/cname_gateway.py` com gateway em socket, rotas permitidas e `421`;
- `panel/token_broker.py` com resolução interna e cache curto;
- `core/tls_provisioner.py` com fluxo transacional de ACME, SAN, distribuição
  e health;
- `core/m3u_transform.py` e o laboratório `lab-player/` para validação de
  playback;
- testes dedicados para CNAME, token broker, TLS, VOD, edge health e release.

Também há documentação específica cobrindo os recortes individuais. O trabalho
pendente não é "inventar um novo fluxo", e sim alinhar implementação, docs,
testes e rollout ao contrato já descrito.

## 6. Plano de execução desta frente

### Fase A

Consolidar o inventário real do que já existe e do que está apenas documentado.

### Fase B

Validar o caminho CNAME + canonical + tenant + socket, sem fallback global
para aliases desconhecidos.

### Fase C

Validar TLS por tenant, permissões de lineage, sudoers e rollback transacional.

### Fase D

Validar playlist e broker para live, HLS, filme, série e `Range`, sem vazamento
de credenciais ou origem.

### Fase E

Executar suíte de testes, `bash -n`, `git diff --check`, `nginx -t` e
laboratório antes de qualquer liberação.

## 7. Resultado desta iteração

- O prompt foi transformado em uma especificação operacional consolidada.
- O repositório já contém a maior parte das peças descritas no prompt.
- Nenhuma mudança de produção foi executada nesta iteração.
- O próximo passo é trabalhar a partir deste plano e dos runbooks citados,
  sem reescrever o contrato já documentado.

## 8. Execução do laboratório com as três fontes

As fontes fornecidas foram usadas somente em memória no laboratório. Usuários,
senhas e URLs completas não foram gravados em Git, SQLite ou nos relatórios.
Para evitar o download indefinido das playlists contínuas, foram usados nove
itens reais por fonte: três live, três filmes e três séries.

| Fonte | Autenticação | Live | VOD/Range | Resultado |
| --- | --- | --- | --- | --- |
| `tvbrasil.phpd77.com` | `active` | 6/6 com `HTTP 200` | 12/12 com `HTTP 200` e `206` | `ok` |
| `xuilab.phpd77.com` | `active` | 6/6 com `HTTP 200` | 12/12 com `HTTP 200` e `206` | `ok` |
| `turbotv.phpd77.com` | `active` | 5/6 com `HTTP 200` | 0/12; origem retornou `503` | `fail` |

O resultado `fail` do `turbotv` é uma falha observada na origem remota, não
foi convertida em sucesso pelo laboratório e não autoriza publicação dessa
fonte. Os gates locais permaneceram verdes: 72 testes automatizados passaram,
os scripts Bash passaram em `bash -n`, a compilação Python passou e
`git diff --check` não encontrou erro.
