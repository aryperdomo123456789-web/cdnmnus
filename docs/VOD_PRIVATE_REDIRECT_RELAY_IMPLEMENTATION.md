# Camada VOD privada para filmes e séries

## Especificação de arquitetura, segurança, implementação e operação

**Data da especificação:** 28/08/2026
**Escopo:** rotas `/movie/` e `/series/` da CDN cdnmnus
**Estado:** arquitetura alvo; não declarar concluída antes de todos os gates deste documento
**Público:** desenvolvimento, segurança, SRE, operação e homologação

---

## 1. Resultado que esta implementação deve produzir

O aplicativo do cliente deve falar somente com o domínio público da CDN. O XUI,
o domínio VOD inicial, os fornecedores descobertos por redirect, seus endereços
IP e os tokens presentes nas URLs devem permanecer internos.

Fluxo obrigatório:

```text
aplicativo
    |
    | GET /movie/... ou /series/...
    | Host: dominio-publico-da-cdn
    v
Nginx da edge
    |
    | pedido interno autenticado/isolado por tenant
    v
relay VOD
    |
    | 1. consulta o XUI
    | 2. aceita o primeiro fornecedor somente se cadastrado
    | 3. segue redirects posteriores recebidos nessa cadeia
    | 4. valida e fixa o IP de cada salto
    | 5. conecta usando Host/SNI corretos
    v
fornecedor final
    |
    | 200 OK ou 206 Partial Content
    v
relay VOD -> Nginx -> aplicativo
```

O cliente nunca pode receber:

- `Location` emitido pelo XUI ou por um fornecedor;
- hostname ou IP do XUI, de `servicedovod.lat` ou de um destino posterior;
- token, assinatura ou query string descoberta durante os redirects;
- `X-Accel-Redirect` ou nome de location interna;
- headers que identifiquem o software ou a infraestrutura da origem.

O sistema deve permitir que `servicedovod.lat` troque o fornecedor final sem
exigir cadastro manual de cada domínio posterior. Essa flexibilidade vale
somente para destinos descobertos em uma cadeia iniciada pelo XUI e ancorada em
uma fonte VOD cadastrada para o mesmo tenant.

---

## 2. Explicação simples

Pense no relay como um entregador que recebe uma instrução do XUI:

1. o app pede um filme para a CDN;
2. a CDN pergunta ao XUI onde está o filme;
3. o XUI aponta para `servicedovod.lat`;
4. `servicedovod.lat` pode apontar para outro fornecedor;
5. o relay confere se cada endereço é seguro antes de ir até ele;
6. quando encontra o arquivo, traz os bytes de volta pela CDN;
7. o app vê apenas a CDN e nunca recebe os endereços visitados.

A regra mais importante é: o app não pode escrever o endereço do destino. O
relay só segue endereços recebidos dos servidores já alcançados pela cadeia
confiável.

---

## 3. Decisão de confiança

### 3.1 Âncora cadastrada

Cada tenant/XUI possui uma lista de fontes VOD iniciais administradas, por
exemplo:

```json
{
  "tenant_id": "xui-principal",
  "vod_seeds": [
    {"host": "servicedovod.lat", "ports": [80, 443]}
  ]
}
```

`servicedovod.lat` é uma **âncora** ou **seed**. Ela não precisa ser o servidor
que contém o MP4. Ela apenas é o primeiro domínio VOD que o operador decidiu
autorizar.

### 3.2 Destino posterior desconhecido

Um hostname posterior pode ser aceito sem cadastro prévio somente quando todas
as condições abaixo forem verdadeiras:

1. a requisição pública começou em `/movie/` ou `/series/`;
2. o primeiro pedido interno foi feito ao XUI daquele tenant;
3. a transição do XUI para VOD apontou para uma seed cadastrada daquele tenant;
4. o hostname posterior veio de um `Location` recebido diretamente na conexão
   anterior, nunca do cliente;
5. esquema, porta, DNS, todos os IPs e caminho passaram pela política de rede;
6. o limite de redirects ainda não foi atingido;
7. a conexão seguinte usa exatamente um dos IPs validados naquele salto.

Essa política é denominada neste documento **confiança delegada pela cadeia**.
Ela substitui, apenas no VOD, a exigência antiga de cadastrar manualmente todo
host final. Ela não transforma a CDN em proxy aberto.

### 3.3 O que nunca é fonte de confiança

Nunca usar como destino:

- query parameter, como `?url=`, `?host=` ou `?target=`;
- header enviado pelo cliente;
- hostname embutido pelo cliente depois de um prefixo interno;
- corpo JSON enviado ao endpoint público;
- cookie do cliente;
- referer;
- cache ou mapping pertencente a outro tenant;
- `Location` recuperado antes da seed cadastrada;
- valor não associado à requisição original por um identificador interno.

---

## 4. Estado real observado em 28/08/2026

Esta seção descreve evidência local e não uma promessa de funcionamento futuro.

### 4.1 Runtime legado ativo

Foram observados ativos:

- `nginx`;
- `cdnmnus-token-broker.service`;
- painel administrativo;
- orquestrador.

O Nginx em execução ainda atende usando a última configuração aceita em memória.
Entretanto, `nginx -t` falha porque o include legado contém um upstream Workers
hardcoded que não resolve mais no DNS. Um restart nesse estado é risco de
indisponibilidade.

O include legado possui:

- interceptação de `/movie/` e `/series/` pelo broker;
- upstream fixo para `servicedovod.lat:80`;
- upstream fixo para um Workers expirado;
- locations internas fixas e de retry;
- location dinâmica que recebe hostname no caminho interno;
- `resolver 1.1.1.1 1.0.0.1` no Nginx;
- bypass de cache quando existe `Range`.

### 4.2 Broker legado

O arquivo `panel/token_broker.py` atualmente:

- aceita apenas `/movie/` e `/series/` no modo VOD;
- consulta primeiro a origem/XUI;
- segue até cinco redirects;
- permite apenas HTTP e porta 80;
- rejeita credenciais e fragmentos no `Location`;
- valida se os endereços DNS são públicos;
- usa `Range: bytes=0-0` durante a descoberta;
- retorna uma URI interna por `X-Accel-Redirect`;
- mantém mapping em memória com TTL e singleflight.

Limitações importantes:

- HTTPS e SNI não são suportados;
- a escolha usa o primeiro endereço DNS ordenado;
- a validação acontece no Python, mas o hostname dinâmico é resolvido novamente
  pelo Nginx; isso não é pinning completo e mantém uma janela de DNS rebinding;
- o probe `bytes=0-0` e a posterior entrega são pedidos diferentes, sujeitos a
  mudança de token/destino entre eles;
- o código Python é adequado para protótipo/control path, mas não é o data path
  recomendado para arquivos grandes e alta concorrência;
- a chave atual de cache do broker não inclui explicitamente tenant e modo VOD
  no legado; a camada multi-tenant cria uma instância por tenant, mas isso deve
  continuar sendo testado como propriedade de isolamento.

### 4.3 Plano multi-tenant

O repositório novo já possui:

- tabela `tenant_upstreams` com `kind='vod'`;
- painel e CLI para adicionar, editar e remover fontes VOD por tenant;
- snapshot por release com `vod_hosts`;
- broker por tenant em Unix socket;
- vhost, cache e rotas internas derivados de `tenant_id`;
- deploy por release, digest, `nginx -t`, health e rollback Ansible.

Na revalidação realizada durante a implementação, o banco de `xui-principal`
já continha duas fontes VOD administradas, ambas resolvendo exclusivamente para
IPs públicos e aceitando TCP/80. A divergência encontrada estava no runtime
legado, que ainda acrescentava um Workers morto hardcoded além das fontes do
plano de controle. Essa referência foi removida do candidato legado antes do
reload controlado.

### 4.4 Falha específica do renderer multi-tenant atual

`core/render_tenants.py` valida o host no broker e depois gera:

```nginx
proxy_pass http://$vod_dynamic_host...;
```

O Nginx resolve novamente `$vod_dynamic_host`. O endereço usado na transferência
não é necessariamente o IP validado pelo broker. A arquitetura alvo não deve
reutilizar esse padrão.

### 4.5 Conclusão sobre maturidade

Já existe prova de conceito funcional, inclusive evidência histórica de filme e
série respondendo `206`. Ainda não existe base para declarar a solução alvo
segura e reproduzível porque:

- `nginx -t` está quebrado no runtime legado;
- há upstream morto hardcoded;
- o alinhamento entre banco, release e runtime precisa ser comprovado por
  digest em toda promoção;
- HTTPS não é seguido;
- o pinning DNS é incompleto;
- não há matriz de testes cobrindo os novos limites desta especificação.

---

## 5. Arquitetura alvo

### 5.1 Componentes

| Componente | Responsabilidade | Não deve fazer |
| --- | --- | --- |
| Nginx | TLS público, limites, encaminhamento ao socket, streaming ao cliente, remoção final de headers | resolver hostname de redirect arbitrário |
| Relay VOD | consultar XUI, seguir redirects, validar DNS/rede, fixar IP, aplicar Host/SNI, transmitir resposta | confiar em destino escolhido pelo cliente |
| Banco/control plane | guardar seeds e política por tenant | guardar token temporário de reprodução |
| Renderer | gerar vhost e snapshot determinísticos | inserir fornecedor hardcoded |
| Ansible | instalar release serialmente, validar e fazer rollback | editar vhost manualmente por edge |
| Monitor | métricas agregadas e alertas sanitizados | registrar URL ou `Location` completo |

### 5.2 Implementação recomendada

Para produção, implementar um `vod-relay` dedicado em Go ou Rust, escutando um
Unix socket por tenant ou um socket único com identidade de tenant impossível de
ser forjada externamente. O relay realiza descoberta e streaming na mesma
fronteira de segurança.

Motivos:

- conexão direta ao IP validado, sem segunda resolução;
- suporte correto a TLS/SNI com `ServerName` igual ao hostname validado;
- cancelamento quando o cliente desconecta;
- streaming com backpressure e sem carregar o filme em memória;
- pools separados por `scheme + host + port + pinned_ip`;
- limites de header, tempo e redirects controlados em código;
- menor risco que fazer Python transportar arquivos grandes.

O Python existente pode ser usado para laboratório e como referência de regras,
mas a promoção para produção deve exigir o relay compilado e testes equivalentes.

### 5.3 Por que não usar apenas `proxy_pass` dinâmico

Um `proxy_pass http://$host_recebido`:

- pode virar proxy aberto;
- faz nova resolução DNS fora da decisão de segurança;
- permite DNS rebinding entre validação e conexão;
- dificulta bloquear metadata, redes privadas e IPv6 especiais;
- não garante SNI correto no HTTPS;
- pode registrar token e hostname em access log.

Consequentemente, o Nginx nunca deve receber do cliente o hostname usado no
upstream e nunca deve resolver o destino final por conta própria.

---

## 6. Contrato público

### 6.1 Métodos

Inicialmente aceitar somente:

- `GET` para conteúdo;
- `HEAD` quando compatível com o XUI/fornecedor.

Rejeitar `POST`, `PUT`, `PATCH`, `DELETE`, `CONNECT`, `TRACE` e métodos não
reconhecidos com resposta genérica.

### 6.2 Caminhos

Aceitar apenas caminhos normalizados:

```text
/movie/<componentes esperados>
/series/<componentes esperados>
```

Rejeitar:

- URL absoluta;
- authority ou esquema no request-target;
- `..` após percent-decoding canônico;
- barra invertida;
- byte NUL e controles;
- dupla codificação que mude a interpretação do caminho;
- URI maior que o limite configurado;
- prefixos internos `__cdnmnus_*` vindos do cliente.

Não decodificar e recodificar a query de forma que altere a assinatura. Validar
a estrutura, mas preservar os bytes necessários ao upstream.

### 6.3 Headers do cliente permitidos ao fornecedor final

Allowlist mínima:

- `Range`;
- `If-Range`;
- `If-Modified-Since` e `If-None-Match`, se comprovadamente necessários;
- `User-Agent`, somente se a compatibilidade exigir;
- `Accept` e `Accept-Encoding: identity`.

Não encaminhar:

- `Host` público;
- `X-Forwarded-For` com IP real do cliente;
- `Forwarded`;
- `X-Real-IP`;
- headers `X-CDN-*` internos;
- `Authorization` público sem decisão explícita;
- cookies do domínio público;
- headers hop-by-hop.

O relay constrói `Host` a partir do hostname validado de cada salto. No HTTPS,
constrói também o SNI a partir desse mesmo hostname.

### 6.4 Resposta pública

Permitir ao cliente os headers necessários à reprodução, como:

- `Content-Type`;
- `Content-Length`;
- `Content-Range`;
- `Accept-Ranges`;
- `ETag` e `Last-Modified`, após avaliação de privacidade;
- `Cache-Control` definido pela política da CDN.

Remover sempre:

- `Location`;
- `Server`;
- `Via`;
- `X-Powered-By`;
- `X-Accel-Redirect`;
- `Set-Cookie` da origem, salvo requisito formal e isolado;
- headers `X-CDN-*`, `X-Upstream-*` ou equivalentes;
- headers que contenham hostname, IP, token ou URL da cadeia.

Se o destino final devolver outro redirect depois que o streaming começou, a
resposta falha; nunca repassar esse `Location` ao cliente.

---

## 7. Máquina de estados do relay

```text
RECEIVE_PUBLIC_REQUEST
        |
        v
VALIDATE_TENANT_METHOD_PATH_HEADERS
        |
        v
REQUEST_XUI_WITH_PINNED_CONNECTION
        |
        +-- 200/206 --> STREAM_RESPONSE
        |
        +-- redirect --> VALIDATE_FIRST_VOD_SEED
                              |
                              v
                       FOLLOW_REDIRECT_CHAIN
                              |
            +-----------------+-----------------+
            |                                   |
         redirect                            200/206
            |                                   |
     VALIDATE_NEXT_HOP                    STREAM_RESPONSE
            |
      limite excedido
            |
            v
     GENERIC_FAIL_CLOSED
```

### 7.1 Passo a passo normativo

1. Identificar tenant pelo vhost Nginx e socket interno, não por campo público.
2. Validar método, caminho, tamanho e headers.
3. Resolver o XUI configurado; validar todos os endereços retornados.
4. Escolher um endereço permitido e conectar diretamente a ele.
5. Enviar ao XUI o caminho original e o `Host` esperado pelo XUI.
6. Se o XUI responder `200` ou `206`, transmitir após sanitização.
7. Se responder redirect aceito, analisar o `Location` sem torná-lo público.
8. Exigir que o primeiro hostname VOD pertença às seeds do tenant.
9. Para cada salto posterior, aceitar o host derivado do `Location`, validar a
   política completa, fixar um IP e conectar a esse IP.
10. Preservar caminho e query do `Location` sem expô-los em logs.
11. Ao receber `200` ou `206`, copiar status, headers permitidos e corpo em
    streaming.
12. Em qualquer violação ou esgotamento de limite, devolver erro genérico.

### 7.2 Status de redirect

Aceitar somente:

- `301`;
- `302`;
- `303` apenas se a política de método estiver implementada conscientemente;
- `307`;
- `308`.

Como VOD usa `GET`/`HEAD`, manter o método nos redirects, exceto semântica
explicitamente implementada para `303`. Não seguir refresh via HTML, JavaScript,
`meta refresh` ou corpo JSON.

### 7.3 Limites iniciais

Valores iniciais recomendados, ajustáveis somente com evidência:

| Limite | Valor inicial |
| --- | ---: |
| redirects totais após o XUI | 5 |
| tamanho da URI pública | 4 KiB |
| tamanho do `Location` | 8 KiB |
| headers de resposta por salto | 32 KiB |
| timeout DNS | 2 s |
| timeout de conexão | 3 s |
| timeout TLS | 3 s |
| timeout para primeiro byte | 10 s |
| idle timeout durante streaming | 30 s, renovado a cada byte |
| duração total | não usar limite curto que interrompa filmes; controlar por idle e política operacional |
| tentativas por IP no mesmo hostname | no máximo 2, sem ultrapassar deadline |
| retry completo da cadeia | 1, apenas antes de enviar headers ao cliente |

Nunca repetir automaticamente um `Range` depois que qualquer byte foi enviado ao
cliente. Isso pode corromper o conteúdo.

---

## 8. DNS, IP e proteção SSRF

### 8.1 Validação de todos os endereços

Para cada hostname, resolver `A` e `AAAA`. Se qualquer endereço retornado cair
em classe proibida, a política mais segura é rejeitar o hostname inteiro. Não
escolher silenciosamente apenas o endereço público, pois um conjunto misto pode
indicar rebinding ou configuração perigosa.

Bloquear pelo menos:

- unspecified;
- loopback;
- redes privadas RFC 1918;
- link-local IPv4 e IPv6;
- unique-local IPv6;
- multicast;
- broadcast;
- documentation/test networks;
- benchmarking ranges;
- carrier-grade NAT, salvo exceção formal;
- endereços reservados e não globais;
- metadata de provedores cloud, especialmente `169.254.169.254` e equivalentes
  IPv6;
- redes internas do control plane, sockets e serviços locais;
- IPs das próprias edges quando isso puder criar loop.

Usar a biblioteca de classificação de IP da linguagem e complementar com uma
tabela explícita testada. Não depender apenas de comparação textual.

### 8.2 Pinning obrigatório

Depois da resolução:

```text
hostname validado -> conjunto de IPs públicos -> IP escolhido -> conexão direta
```

A conexão TCP deve usar o IP escolhido, não o hostname. Para HTTPS:

```text
TCP destination = IP validado
TLS ServerName   = hostname validado
HTTP Host        = hostname validado[:porta quando necessário]
```

Validar a cadeia TLS contra as CAs do sistema e verificar hostname. Nunca usar
`InsecureSkipVerify`, `verify=False` ou equivalente em produção.

### 8.3 TTL e nova resolução

- guardar resultado somente até o menor TTL útil, limitado por piso/teto local;
- revalidar todos os IPs após expirar;
- não reutilizar conexão além da validade definida sem política consciente;
- invalidar o pool quando o hostname mudar de conjunto de IPs;
- não compartilhar resolução entre tenants sem incluir política/tenant na chave.

### 8.4 Resolver do sistema

Preferir resolver local validado e operado pela edge, com timeout e cache
controlados. DNS público hardcoded no vhost não constitui pinning. DNSSEC pode
adicionar garantia de autenticidade quando disponível, mas não substitui a
classificação de IP nem a conexão fixada.

### 8.5 Egress

Firewall por IP é uma segunda defesa, mas destinos dinâmicos tornam allowlist
estática completa difícil. Aplicar pelo menos:

- bloqueio de saída para redes privadas, link-local, metadata e ranges internos;
- permissão somente para TCP 80/443 no processo/namespace do relay;
- bloqueio de acesso do relay ao control plane, SSH, banco e painel;
- namespace/container ou regras por usuário/cgroup quando suportado;
- DNS apenas para o resolver autorizado.

O egress não substitui a validação em aplicação; ambos são necessários.

---

## 9. HTTP, HTTPS, Host e SNI

### 9.1 Esquemas

Permitir inicialmente:

- `http` nas portas administrativamente autorizadas, normalmente 80;
- `https` nas portas administrativamente autorizadas, normalmente 443.

Rejeitar esquema vazio quando não puder ser resolvido de forma inequívoca,
`file`, `ftp`, `gopher`, `data`, `unix`, `ws`, `wss` e qualquer outro.
Redirect relativo herda esquema, host e porta do salto atual.

### 9.2 HTTPS obrigatório quando fornecido

Não reduzir `https://` para HTTP. Não aceitar certificado inválido apenas para
manter reprodução. Erro TLS deve ser fail-closed e gerar métrica sanitizada.

### 9.3 Portas

Modelo recomendado por seed:

```json
{
  "host": "servicedovod.lat",
  "allowed_schemes": ["http", "https"],
  "allowed_ports": [80, 443]
}
```

Para hosts posteriores, herdar uma política global estreita, inicialmente
somente `80` e `443`. Porta presente no `Location` precisa ser validada. Nunca
aceitar portas administrativas como 22, 2375, 3306, 5432, 6379, 8080 ou 9091
sem exceção formal e teste de ameaça.

---

## 10. Range, seek e streaming

### 10.1 Requisitos funcionais

O relay deve preservar:

- `Range` exatamente como validado;
- `If-Range` quando presente;
- status `206`;
- `Content-Range`;
- `Accept-Ranges`;
- `Content-Length` correspondente à resposta parcial.

Casos obrigatórios:

```text
Range: bytes=0-1023
Range: bytes=1048576-
Range: bytes=-65536
```

Definir explicitamente se múltiplos ranges (`bytes=0-1,4-5`) serão suportados.
A primeira versão pode rejeitá-los com `416` para reduzir complexidade, desde
que players homologados não dependam deles.

### 10.2 Não transformar Range em slice automaticamente

O renderer atual combina `slice 1m` e `proxy_set_header Range $slice_range` em
algumas locations VOD. Essa transformação não deve ser mantida sem testes
específicos. Para ocultação e compatibilidade inicial, o comportamento mais
previsível é encaminhar o Range original e não cachear respostas parciais.

### 10.3 Streaming sem buffer integral

O relay:

- não carrega o arquivo inteiro em RAM ou disco;
- usa buffers pequenos e limitados;
- respeita backpressure do cliente;
- cancela upstream ao detectar desconexão;
- limita conexões e memória por tenant;
- não tenta retomar no meio depois que bytes foram enviados.

No Nginx, iniciar com `proxy_buffering off` para VOD via relay. Otimizações de
buffer/cache entram somente após medir memória, throughput, seek e latência.

### 10.4 Cache

Política inicial segura:

- não cachear respostas com `Range`;
- não cachear redirects nem URLs assinadas;
- não usar query/token plaintext na chave persistida ou nos logs;
- se houver cache de objetos completos, isolar por tenant e asset canônico;
- nunca compartilhar bytes entre tenants sem prova de identidade do objeto e
  autorização;
- limitar tamanho e expulsão para não encher o disco da edge.

O cache HLS existente não deve ser reutilizado automaticamente para filmes
longos. VOD tem perfil de armazenamento e concorrência diferente.

---

## 11. Isolamento multi-tenant

Todo estado deve incluir `tenant_id`:

- seed VOD;
- origem/XUI;
- política de porta/esquema;
- cache DNS;
- pool de conexões;
- singleflight;
- circuit breaker;
- métricas;
- limites de concorrência;
- chaves de cache/mapping.

Uma cadeia iniciada no tenant A nunca pode usar seed, mapping, socket, cache ou
conexão autenticada pertencente ao tenant B.

Modelo preferido:

```text
Nginx vhost tenant A -> /run/cdnmnus/vod-relay-A.sock
Nginx vhost tenant B -> /run/cdnmnus/vod-relay-B.sock
```

Um processo por tenant simplifica a fronteira. Se houver processo compartilhado,
usar credencial local/sistema para associar socket/vhost ao tenant; não confiar
somente em `X-CDN-Tenant`.

---

## 12. Configuração e modelo de dados

### 12.1 Evolução necessária

Hoje `tenant_upstreams(kind='vod')` guarda apenas host e porta. Evoluir sem
quebrar releases existentes, por migração versionada, para representar:

- seed habilitada/desabilitada;
- esquemas permitidos;
- portas permitidas;
- máximo de redirects;
- exigência de TLS;
- política de hosts posteriores;
- limites de timeout;
- data/operador da aprovação.

Exemplo lógico:

```text
tenant_vod_seeds
  id
  tenant_id
  host
  enabled
  allowed_schemes_json
  allowed_ports_json
  max_redirects
  allow_chain_derived_hosts
  created_at
  updated_at
```

### 12.2 Snapshot de release

Exemplo de snapshot, sem tokens:

```json
{
  "schema_version": 2,
  "generation": 42,
  "tenants": {
    "xui-principal": {
      "origin": {"host": "<xui-autorizado>", "port": 80, "scheme": "http"},
      "vod_policy": {
        "seeds": [
          {
            "host": "servicedovod.lat",
            "schemes": ["http", "https"],
            "ports": [80, 443]
          }
        ],
        "allow_chain_derived_hosts": true,
        "derived_host_ports": [80, 443],
        "max_redirects": 5
      }
    }
  }
}
```

### 12.3 Validação antes de gerar release

Para cada seed habilitada:

1. validar sintaxe IDNA/hostname;
2. resolver A/AAAA;
3. rejeitar qualquer endereço proibido;
4. verificar TCP nas portas configuradas sem baixar mídia;
5. verificar TLS/hostname quando HTTPS estiver habilitado;
6. registrar somente resultado agregado e hash do host nos logs operacionais;
7. abortar a release se uma seed obrigatória falhar;
8. permitir seed opcional apenas com semântica explícita, nunca por exceção
   silenciosa.

Nenhum hostname VOD deve aparecer hardcoded em `panel.py`, renderer, broker ou
template Nginx. `servicedovod.lat` deve existir apenas como dado administrado.

---

## 13. Exemplo de integração Nginx

Este bloco é referência de intenção; o renderer deve produzi-lo por tenant e os
testes devem validar o arquivo final antes do deploy:

```nginx
upstream vod_relay_xui_principal {
    server unix:/run/cdnmnus/vod-relay-xui-principal.sock;
    keepalive 32;
}

location ^~ /movie/ {
    limit_except GET HEAD { deny all; }
    proxy_pass http://vod_relay_xui_principal;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header X-CDN-Public-Host $host;
    proxy_set_header X-CDN-Request-ID $request_id;
    proxy_set_header Range $http_range;
    proxy_set_header If-Range $http_if_range;
    proxy_set_header X-Forwarded-For "";
    proxy_set_header X-Real-IP "";
    proxy_hide_header Location;
    proxy_hide_header Server;
    proxy_hide_header Via;
    proxy_hide_header X-Powered-By;
    proxy_hide_header X-Accel-Redirect;
    proxy_hide_header Set-Cookie;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 30s;
    access_log off;
}

location ^~ /series/ {
    # Mesma política da location /movie/, gerada por função/template comum.
    proxy_pass http://vod_relay_xui_principal;
    proxy_set_header X-CDN-Public-Host $host;
    proxy_set_header Range $http_range;
    proxy_set_header If-Range $http_if_range;
    proxy_hide_header Location;
    proxy_hide_header Server;
    proxy_hide_header X-Accel-Redirect;
    proxy_buffering off;
    access_log off;
}
```

Observações:

- o Unix socket e o vhost identificam o tenant;
- `$request_id` pode ser usado somente se não carregar segredo;
- `access_log off` evita URI credenciada no log padrão; métricas devem vir do
  relay em formato sanitizado;
- `proxy_read_timeout` é idle timeout, não duração máxima do filme;
- não existe location pública ou interna contendo hostname do fornecedor;
- o relay, e não o Nginx, abre a conexão ao destino validado.

---

## 14. Algoritmo de referência

Pseudocódigo obrigatório para orientar implementação e review:

```text
handle_vod(request, tenant):
    validate_method_path_and_headers(request)
    policy = immutable_snapshot.for_tenant(tenant)

    current = origin_url(policy.origin, request.raw_path_and_query)
    first_vod_hop_seen = false

    for redirect_count in 0..policy.max_redirects:
        target = parse_and_normalize(current)
        require_allowed_scheme_and_port(target, policy, first_vod_hop_seen)
        require_no_userinfo_or_fragment(target)

        dns_answer = resolve_A_and_AAAA(target.hostname, deadline)
        require_all_addresses_public_and_allowed(dns_answer)
        pinned_ip = select_address_with_bounded_failover(dns_answer)

        response = request_pinned(
            ip=pinned_ip,
            port=target.port,
            tls_server_name=target.hostname when HTTPS,
            host_header=target.authority,
            method=request.method,
            path_and_query=target.raw_path_and_query,
            range=request.validated_range,
            if_range=request.validated_if_range
        )

        if response.status in [200, 206]:
            return stream_sanitized(response)

        if response.status not in [301, 302, 303, 307, 308]:
            return generic_upstream_error()

        location = bounded_location(response)
        next = resolve_relative_reference(current, location)

        if current belongs to XUI and not first_vod_hop_seen:
            require next.hostname in policy.seeds
            first_vod_hop_seen = true
        else:
            require first_vod_hop_seen
            require next was derived only from this response

        current = next

    return generic_redirect_limit_error()
```

Detalhe essencial: a verificação da seed ocorre na transição do XUI para a
camada VOD. Não basta manter `[origin, *vod_hosts]` numa lista e verificar apenas
o valor inicial, pois o valor inicial sempre é a origem.

---

## 15. Erros e fail-closed

### 15.1 Resposta ao cliente

Usar respostas genéricas, por exemplo:

| Situação interna | Resposta pública sugerida |
| --- | --- |
| entrada inválida | `400` sem detalhes |
| método proibido | `405` |
| seed/destino bloqueado | `502` genérico |
| DNS/TLS/timeout upstream | `503` genérico |
| Range inválido | `416` sem revelar tamanho quando não conhecido |
| limite local de concorrência | `503` com `Retry-After` curto |

O corpo nunca deve mencionar hostname, IP, porta, token, biblioteca, stack trace
ou posição da cadeia.

### 15.2 Retry

- retry somente antes de headers/bytes chegarem ao cliente;
- no máximo um retry completo;
- usar backoff pequeno com jitter;
- não trocar para IP não validado;
- não repetir `4xx` funcionais indiscriminadamente;
- circuit breaker por tenant/seed/hash de destino;
- singleflight apenas para descoberta idêntica, nunca para misturar streams de
  usuários ou credenciais diferentes.

---

## 16. Logs, métricas e privacidade

### 16.1 Nunca registrar

- URI pública completa;
- usuário e senha presentes no caminho;
- query string;
- `Location`;
- hostname ou IP real do fornecedor em log acessível ao operador comum;
- `Range` completo associado a identidade pessoal;
- cookies, authorization ou tokens;
- corpo de resposta.

### 16.2 Identificadores seguros

Gerar:

```text
request_id = aleatório, sem dados do cliente
target_hash = HMAC-SHA256(chave_da_edge, hostname_normalizado)
asset_hash = HMAC-SHA256(chave_da_edge, tenant || caminho_canonico_sem_segredo)
```

Não usar SHA simples para valores de pequeno espaço previsível. Rotacionar a
chave HMAC de observabilidade de forma planejada.

### 16.3 Métricas mínimas

- requisições VOD por tenant e tipo movie/series;
- status público agregado;
- `200`/`206` finais;
- quantidade de redirects por cadeia em histograma;
- DNS/TCP/TLS/TTFB em histogramas;
- bloqueios SSRF por categoria, sem destino;
- falhas de certificado;
- limite de redirect atingido;
- bytes de entrada e saída;
- streams ativos e fila por tenant;
- cancelamentos pelo cliente;
- circuit breaker aberto;
- uso de memória, FDs e sockets;
- versão/digest da política ativa.

### 16.4 Alertas

Alertar quando:

- `502/503` superar baseline;
- número médio de redirects mudar abruptamente;
- surgir nova categoria de bloqueio SSRF;
- TLS falhar para parcela relevante;
- seed deixar de resolver;
- `206` cair enquanto `200` permanece;
- streams/FDs se aproximarem do limite;
- digest divergir entre edges;
- qualquer scanner detectar `Location` ou identidade de origem na resposta.

---

## 17. Testes obrigatórios

### 17.1 Unitários do parser

- `/movie/` válido;
- `/series/` válido;
- URL absoluta rejeitada;
- traversal simples, codificado e duplamente codificado rejeitado;
- userinfo e fragmento no `Location` rejeitados;
- redirect relativo resolvido corretamente;
- porta não autorizada rejeitada;
- esquemas não HTTP(S) rejeitados;
- hostname IDNA normalizado ou rejeitado consistentemente;
- header e `Location` acima do limite rejeitados;
- loop interrompido em cinco redirects.

### 17.2 SSRF e DNS

Cobrir IPv4 e IPv6:

- `127.0.0.1`;
- `0.0.0.0`;
- RFC 1918;
- `169.254.169.254`;
- CGNAT;
- multicast;
- documentation ranges;
- `::1`;
- `::`;
- `fe80::/10`;
- `fc00::/7`;
- IPv4-mapped IPv6;
- resposta DNS mista pública + privada;
- rebinding entre duas consultas;
- CNAME encadeado para endereço proibido;
- hostname que resolve para a própria edge.

O teste de pinning deve provar que a conexão foi aberta no IP validado, mesmo se
uma resolução seguinte devolver outro IP.

### 17.3 TLS

- certificado válido e SNI correto;
- certificado expirado;
- hostname divergente;
- CA desconhecida;
- redirect HTTP para HTTPS;
- tentativa de downgrade HTTPS para HTTP conforme política;
- TLS timeout;
- porta HTTPS não autorizada.

### 17.4 Cadeia de confiança

- XUI -> seed cadastrada -> host desconhecido público -> `206`: sucesso;
- XUI -> host não cadastrado: bloqueio;
- cliente tenta fornecer host: bloqueio;
- seed -> metadata: bloqueio;
- seed -> rede privada: bloqueio;
- seed -> cinco redirects -> `206`: sucesso no limite;
- sexto redirect: bloqueio;
- tenant A tenta seed do tenant B: bloqueio;
- redirect final muda depois de expiração DNS: nova validação obrigatória.

### 17.5 Range e players

- `bytes=0-1023` retorna `206`, 1024 bytes e `Content-Range` correto;
- seek para posição intermediária;
- sufixo de arquivo;
- `If-Range` válido e inválido;
- `416`;
- cliente desconecta no meio;
- filme longo sem idle timeout falso;
- episódio de série;
- pelo menos os players realmente usados, incluindo iBO Player Pro;
- pausa, retomada, avanço e retrocesso repetidos.

### 17.6 Não vazamento

Para filme e série, inspecionar:

- status line;
- todos os headers;
- corpo de erro;
- redirects vistos pelo cliente;
- DNS feito pelo dispositivo de teste;
- logs do Nginx, relay e systemd;
- métricas exportadas.

O teste falha se encontrar qualquer hostname/IP/token da cadeia ou
`X-Accel-Redirect`.

### 17.7 Carga e resiliência

- 50 streams frios;
- 100, 250 e 500 conexões conforme capacidade contratada;
- múltiplos assets, não apenas o mesmo byte cacheado;
- seek concorrente;
- seed lenta;
- fornecedor final lento;
- DNS indisponível;
- restart do relay sem vazar redirect;
- reload Nginx durante streams;
- perda de uma edge;
- soak mínimo de seis horas e reprodução VOD superior a três horas;
- limites de FD, memória, banda e egress medidos.

---

## 18. Plano de implementação em fases

### Fase 0 — estabilizar o estado atual

1. Congelar mudanças no painel legado.
2. Fazer backup recuperável do include, config do broker e release ativa, sem
   copiar segredos para documentação ou chat.
3. Registrar hashes dos artefatos.
4. Remover do gerador e do runtime candidato o Workers inexistente.
5. Manter apenas seeds administradas que resolvam.
6. Gerar candidato em arquivo temporário.
7. Executar `nginx -t` antes de qualquer reload.
8. Só recarregar após teste bem-sucedido.
9. Não reiniciar Nginx enquanto `nginx -t` falhar.

Saída da fase: `nginx -t` verde e nenhuma referência hardcoded ao Workers morto.

### Fase 1 — alinhar control plane e runtime

1. Cadastrar `servicedovod.lat` como seed do tenant correto.
2. Gerar snapshot e vhost a partir do banco.
3. Comparar semanticamente candidato e runtime legado.
4. Garantir que toda edge receba o mesmo digest.
5. Desabilitar edição concorrente pelo painel legado.
6. Fazer deploy somente em homologação/canário.

Saída da fase: banco, snapshot, vhost e runtime dizem a mesma coisa.

### Fase 2 — implementar relay seguro

1. Criar pacote `vod-relay` em Go/Rust.
2. Implementar parser e política imutável por release.
3. Implementar resolução A/AAAA e classificação completa.
4. Implementar dial para IP fixado.
5. Implementar HTTP e HTTPS com Host/SNI.
6. Implementar redirects relativos e absolutos.
7. Implementar regra especial da primeira seed.
8. Implementar streaming, Range e cancelamento.
9. Implementar sanitização de headers e erros.
10. Implementar métricas e logs HMAC.
11. Criar systemd hardened e socket por tenant.
12. Cobrir unitários antes de integrar Nginx.

Saída da fase: testes locais passam sem Nginx resolver destino final.

### Fase 3 — integrar renderer e Ansible

1. Evoluir schema/snapshot com versão compatível.
2. Alterar `core/render_tenants.py` para apontar `/movie/` e `/series/` ao relay.
3. Remover `dynamic_vod_location` baseada em hostname.
4. Remover upstreams VOD hardcoded do legado.
5. Instalar binário, unit e sockets via role versionada.
6. Adicionar health profundo que valide configuração, sem baixar mídia real.
7. Validar digest, broker/relay, `nginx -t` e health no playbook.
8. Exercitar rollback automático.

Saída da fase: release reproduzível e rollback testado.

### Fase 4 — homologação real

1. Usar credencial autorizada sem registrá-la.
2. Testar um filme e um episódio conhecidos.
3. Confirmar `206`, seek e retomada.
4. Confirmar cadeia com fornecedor posterior não cadastrado.
5. Executar scanner de vazamento externo.
6. Executar SSRF controlado em ambiente de teste.
7. Executar carga e soak.
8. Medir banda e capacidade.

Saída da fase: relatório sanitizado com todos os gates.

### Fase 5 — rollout serial

1. Retirar canário do DNS/pool quando aplicável.
2. Implantar release pelo Ansible.
3. Validar `nginx -t`, relay, health, filme, série e Range.
4. Recolocar canário e observar métricas.
5. Repetir uma edge por vez.
6. Interromper automaticamente ao ultrapassar error budget.
7. Manter release anterior pronta para rollback.

---

## 19. Rollback

Rollback deve restaurar o conjunto coerente:

- symlink `/opt/cdnmnus/current`;
- snapshot de política;
- vhosts;
- versão do relay;
- units, se mudaram;
- digest registrado.

Procedimento:

1. retirar a edge afetada do pool;
2. apontar `current` para a release anterior;
3. reiniciar/recarregar somente os componentes necessários;
4. executar `nginx -t`;
5. recarregar Nginx;
6. validar health, filme, série e Range;
7. recolocar no pool somente após sucesso;
8. preservar logs sanitizados e artefatos para análise.

Nunca “corrigir” produção editando diretamente um vhost em uma das edges. A
correção precisa nascer no gerador e virar nova release.

---

## 20. Checklist de code review

- [ ] nenhum destino vem do cliente;
- [ ] primeira transição VOD exige seed do tenant;
- [ ] hosts posteriores só vêm de `Location` da conexão anterior;
- [ ] todos os A/AAAA são classificados;
- [ ] conjunto DNS misto público/privado é rejeitado;
- [ ] conexão usa IP validado;
- [ ] HTTPS usa SNI/Host e valida certificado;
- [ ] somente portas 80/443 são aceitas por padrão;
- [ ] redirect, header e tempo possuem limites;
- [ ] Range e If-Range são preservados;
- [ ] não há buffer integral do filme;
- [ ] não há retry após início da resposta pública;
- [ ] `Location` e headers internos nunca saem;
- [ ] logs não contêm URI, token, hostname ou IP real;
- [ ] estado e caches incluem tenant;
- [ ] Nginx não resolve hostname dinâmico final;
- [ ] não existe fornecedor hardcoded no código;
- [ ] testes SSRF IPv4/IPv6 e rebinding passam;
- [ ] teste de vazamento externo passa;
- [ ] deploy valida digest e `nginx -t`;
- [ ] rollback foi exercitado.

---

## 21. Critérios de aceite de produção

Todos são obrigatórios:

```text
nginx -t em todas as edges                     PASS
nenhum hostname VOD hardcoded no código        PASS
seed administrada por tenant                   PASS
XUI -> seed -> host desconhecido -> 206         PASS
HTTP e HTTPS com pinning + SNI                  PASS
SSRF IPv4/IPv6/rebinding                        BLOQUEADO
Location público                               AUSENTE
host/IP/token público                          AUSENTE
Range/seek/If-Range filme                       PASS
Range/seek/If-Range série                       PASS
isolamento entre tenants                        PASS
logs e métricas sanitizados                     PASS
load/soak dentro do SLO                         PASS
digest igual em todas as edges                  PASS
rollback serial exercitado                      PASS
```

Enquanto qualquer item falhar, o estado correto é **homologação incompleta**.

---

## 22. Arquivos do projeto afetados pela futura implementação

| Arquivo/área | Mudança esperada |
| --- | --- |
| `core/db.py` | migração e política de seeds VOD |
| `core/render_tenants.py` | remover proxy dinâmico por hostname e apontar ao relay |
| `core/deploy.py` | incluir binário/política no manifesto e digest |
| `panel/token_broker.py` | deixar de ser data path VOD ou servir como compatibilidade controlada |
| `panel/multi_tenant_broker.py` | separar HLS do novo relay VOD |
| `web/app.py` e CLI | administrar seed e política sem expor destinos descobertos |
| `ansible/roles/cdn_runtime` | instalar relay/unit/socket hardened |
| `ansible/roles/cdn_tenants` | ativar por tenant, health e rollback |
| `tests/` | parser, SSRF, TLS, pinning, Range, isolamento e vazamento |
| scripts de monitor/soak | métricas sem URL e validação VOD longa |

Antes de editar, procurar também cópias implantadas em `/opt/cdnmnus-panel` e
`/etc/nginx`; nesta auditoria os fontes legado/versionado tinham hashes iguais,
mas isso deve ser revalidado em cada release.

---

## 23. Relação com os documentos existentes

Esta especificação foi cruzada com:

- `NGINX_UPSTREAM_RESOLUTION_AUDIT_2026-08-28.md`;
- `VOD_DELIVERY_CURRENT_STATE_2026-08-28.md`;
- `TOKEN_LIFECYCLE_AND_ORIGIN_SHIELD.md`;
- `PRODUCTION_SECURITY_AND_CAPACITY.md`;
- `CDN_5_OF_10_EXECUTION_AUDIT.md`;
- `ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md`;
- documentos de operação, release, failover, capacidade, edges e control plane;
- código legado, renderer multi-tenant, banco, deploy, Ansible e testes.

Quando houver conflito, aplicar estas regras:

1. para HLS/live, permanece a política própria de allowlist e token lifecycle;
2. para VOD, hosts posteriores podem ser desconhecidos apenas sob **confiança
   delegada pela cadeia**;
3. a regra antiga “todo host final precisa de cadastro” continua válida para
   qualquer destino que não tenha sido derivado da seed conforme esta máquina
   de estados;
4. segurança comprovada pelo runtime e testes prevalece sobre comentário ou
   documento histórico;
5. nenhuma evidência histórica substitui os gates atuais.

---

## 24. Definição de pronto

A camada estará pronta somente quando uma criança consiga seguir o runbook de
homologação sem tomar decisões de segurança improvisadas, e um especialista
consiga provar tecnicamente que:

- a cadeia sempre nasce no tenant correto;
- `servicedovod.lat` é uma seed administrada, não código fixo;
- qualquer fornecedor posterior foi descoberto exclusivamente por redirect;
- cada conexão usou exatamente um IP público previamente validado;
- HTTPS preservou Host/SNI e validou certificado;
- o app recebeu apenas bytes e metadados necessários à reprodução;
- nenhum segredo ou endereço da origem vazou;
- falhas foram fechadas, observáveis e reversíveis.

Até lá, a implementação deve ser descrita como **experimental/canário**, nunca
como ocultação garantida em produção.
