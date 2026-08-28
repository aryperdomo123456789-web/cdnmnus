# Auditoria de capacidade e ocultacao - 2026-08-28

## Escopo

Auditoria autorizada do caminho XUI -> cdnmnus -> aplicativo, usando uma conta de laboratorio declarada com limite de 500 conexoes. Credenciais, URLs assinadas, tokens, IPs de origem e nomes exclusivos de relays foram omitidos deste relatorio.

Foram analisados:

- playlist direta e playlist entregue pela CDN;
- canais LGBT e UFC;
- canais lineares aleatorios;
- filmes e episodios aleatorios;
- redirects, manifests HLS e segmentos MPEG-TS;
- arquivos MP4 e requisicoes parciais;
- concorrencia para um unico canal e para canais distintos;
- CPU, load average, memoria, conexoes e trafego da edge;
- configuracao carregada pelo Nginx e logs de erro.

Este foi um teste de burst executado a partir da propria edge. Ele comprova concorrencia HTTP e funcionamento do cache, mas nao substitui carga sustentada, clientes geograficamente distribuidos ou medicao do link de saida publico.

## Dados de entrada

As duas playlists apresentaram:

- 318.342 entradas;
- 1.148 entradas com extensao HLS;
- 317.194 entradas MP4;
- 406 canais classificados como ativos durante a janela de descoberta;
- 389 respostas `503`, 346 timeouts, quatro `404` e tres manifests vazios durante a varredura das 1.148 entradas.

A grande quantidade de `503` e timeout demonstra instabilidade ou limitacao no upstream. O numero de conexoes configurado na conta nao garante que todos os canais, servidores de midia e load balancers consigam sustentar esse total.

## Ocultacao antes das correcoes

### Playlist

- as 318.342 URLs da playlist CDN apontavam para o dominio publico;
- nenhuma URL da playlist CDN continha os hosts de origem conhecidos;
- a playlist direta continha o host do XUI em todas as entradas.

### Live

As amostras LGBT e UFC mantiveram o host publico no manifest e nos segmentos. Os manifests usaram caminhos relativos, e nenhum host de origem conhecido apareceu no conteudo inspecionado.

### VOD

Filmes e episodios redirecionavam o aplicativo para dois destinos externos. Isso nao revelou necessariamente o painel XUI, mas retirava o fluxo da CDN e violava o requisito de origin shield.

Foram implementados relays fixos para os destinos observados. Eles nao aceitam host arbitrario informado pelo cliente. Depois da correcao:

- tres filmes e tres episodios passaram por toda a cadeia usando somente o dominio publico;
- o MP4 final foi entregue pelo Nginx da edge;
- nenhum destino externo conhecido apareceu nos headers ou corpos inspecionados.

## Diagnostico do suposto teto de 14 conexoes

O teste original repetia o mesmo segmento HLS sem cache. Cada cliente abria uma leitura equivalente no upstream.

Resultado aproximado antes do cache:

| Concorrencia para o mesmo segmento | HTTP 200 | HTTP 503 |
| ---: | ---: | ---: |
| 10 | 9-10 | 0-1 |
| 15 | 10 | 5 |
| 25 | aproximadamente 10 | restante recusado |
| 50 | 14-39, conforme a janela | restante recusado |

O mesmo padrao apareceu acessando diretamente o upstream. A CPU da edge permaneceu praticamente ociosa. Portanto, o teto nao era causado por worker, file descriptor, CPU ou RAM da CDN.

O caminho possuia duas multiplicacoes independentes:

1. cada cliente consultava a rota curta do segmento e recebia um `302` para o LB;
2. cada cliente seguia o redirect e baixava o mesmo MPEG-TS novamente no LB.

Assim, 50 espectadores do mesmo canal podiam gerar 50 consultas de redirect e 50 downloads identicos na origem.

## Correcao implementada

Foi criado um cache HLS curto com:

- zona de chaves de 32 MiB;
- limite de 2 GiB em disco;
- validade de 30 segundos para `200` e `302`;
- `proxy_cache_lock on`;
- reaproveitamento temporario em erro, timeout e atualizacao;
- cache tanto na rota `/hls/` quanto na rota real do load balancer;
- chave contendo esquema, metodo, host e URI completa;
- headers de diagnostico `X-CDN-Route` e `X-CDN-Cache`.

O cache nao foi aplicado genericamente a filmes e series. O objetivo e consolidar segmentos curtos e imutaveis de live, evitando armazenar arquivos VOD extensos.

Nota posterior: com a introducao do token broker, o mapeamento passou a usar 15 segundos e o conteudo live seis segundos. O TTL menor reduz risco de manifest stale; a renovacao e o retry ocorrem internamente sem expor o token.

A verificacao de seguranca encontrou 406 caminhos de segmento e 406 valores unicos. Nenhum caminho duplicado foi observado nessa amostra, reduzindo o risco de colisao entre canais. Essa propriedade deve continuar sendo testada a cada integracao com outro tipo de XUI.

## Validacao funcional do cache

### Segmento aquecido

- primeira leitura: `MISS`, aproximadamente 9,3 MiB, 2,8 segundos;
- segunda leitura: `HIT`, mesmo tamanho, aproximadamente 0,45 segundo;
- em outra amostra, o `HIT` completo caiu para aproximadamente 0,025 segundo.

### Cache frio com 50 clientes

Um novo segmento, ainda ausente do cache, recebeu 50 clientes simultaneos:

- 50 respostas `200`;
- um `MISS`;
- 49 `HIT`;
- todas as respostas com exatamente 2.350.000 bytes;
- zero erro;
- aproximadamente 2,65 segundos para concluir o lote.

Esse e o principal teste de `cache_lock`: somente uma requisicao buscou o objeto, enquanto as outras aguardaram e reutilizaram a mesma copia.

### Cache aquecido ate 500 clientes

Cada cliente leu 512 KiB do mesmo segmento ja armazenado:

| Clientes | Sucesso | Erros | Cache HIT | Tempo do lote |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 25 | 0 | 25 | 0,097 s |
| 50 | 50 | 0 | 50 | 0,170 s |
| 100 | 100 | 0 | 100 | 0,409 s |
| 250 | 250 | 0 | 250 | 0,883 s |
| 500 | 500 | 0 | 500 | 1,737 s |

No lote de 500:

- erro: 0%;
- latencia media observada pelo cliente local: 0,08 segundo;
- load average apos o lote: aproximadamente 0,47;
- dados amostrados: 250 MiB.

Conclusao: o teto de 10-14 conexoes para um mesmo segmento foi ultrapassado por consolidacao de requisicoes. A edge demonstrou aceitar 500 clientes HTTP simultaneos quando o objeto estava em cache.

## Teste distribuido e limite do upstream

Uma tentativa com dez canais diferentes teve nove sucessos e um `503`. A escalada foi interrompida porque a taxa de erro de 10% excedeu o criterio operacional.

Isso nao contradiz o teste de cache. Sao problemas diferentes:

- muitos espectadores no mesmo canal: resolvido pelo cache HLS;
- muitos canais diferentes: cada canal ainda exige seu proprio fluxo no upstream;
- canais offline, lentos ou recusados: nao podem ser corrigidos apenas aumentando a VPS da edge.

Para 500 clientes distribuidos em 500 canais diferentes, seriam necessarios 500 fluxos de origem ativos. Na janela auditada, somente 406 dos 1.148 canais HLS responderam como ativos, e parte deles oscilou depois.

## Regressao de vazamento apos as correcoes

Amostra aleatoria concorrente:

- live: 18/20 fluxos completos, um erro HTTP e um timeout;
- VOD: 19/20 completos e um erro HTTP;
- vazamentos conhecidos em live: zero;
- vazamentos conhecidos em VOD: zero.

Erros e timeouts foram atribuídos a disponibilidade do upstream. Eles nao revelaram os hosts conhecidos na resposta recebida pelo cliente.

## Vazamento encontrado nos logs

O `error.log` do Nginx registrava a URI completa em falhas de upstream. Como credenciais IPTV podem existir no caminho, o arquivo podia armazenar usuario e senha.

Correcao aplicada:

- nivel do error log alterado de `warn` para `crit`;
- configuracao ativa e template versionado atualizados;
- log antigo contendo URIs foi truncado;
- permissoes do arquivo foram mantidas restritas.

Metricas sanitizadas devem substituir o uso de URI bruta para diagnostico de `502/503/504`.

## Capacidade do servidor atual

Host auditado:

- 2 vCPU;
- aproximadamente 4 GiB de RAM;
- sem swap;
- dois workers Nginx;
- banda de saida medida entre aproximadamente 426 e 843 Mbit/s;
- mediana curta proxima de 600 Mbit/s.

Usando 65% da mediana como margem, a banda util de planejamento fica proxima de 390 Mbit/s:

| Bitrate medio | Clientes conservadores |
| ---: | ---: |
| 2 Mbit/s | 195 |
| 4 Mbit/s | 97 |
| 6 Mbit/s | 65 |
| 8 Mbit/s | 48 |
| 10 Mbit/s | 39 |

O teste de 500 `HIT` ocorreu localmente e leu apenas parte do segmento. Ele demonstra capacidade de concorrencia, nao capacidade de entregar 500 streams sustentados pela Internet. Por exemplo, 500 clientes a 6 Mbit/s exigem aproximadamente 3 Gbit/s de saida, muito acima da margem medida nesta VPS.

## Projecao de upgrade

Com margem de 70% e ignorando outros gargalos:

| Link sustentado | 4 Mbit/s | 6 Mbit/s | 8 Mbit/s |
| ---: | ---: | ---: | ---: |
| 1 Gbit/s | 175 | 116 | 87 |
| 2,5 Gbit/s | 437 | 291 | 218 |
| 5 Gbit/s | 875 | 583 | 437 |
| 10 Gbit/s | 1.750 | 1.166 | 875 |

Para aproximadamente 500 espectadores a 6 Mbit/s, planejar no minimo:

- 5 Gbit/s sustentados, preferencialmente 10 Gbit/s para crescimento;
- 8-16 vCPU modernos;
- 16-32 GiB de RAM;
- NVMe e cache dimensionado pelo numero/tamanho de segmentos;
- duas edges para alta disponibilidade;
- upstream capaz de sustentar o numero de canais distintos, nao apenas o numero de usuarios da conta.

## Limitacoes e proximos testes

Antes de declarar capacidade comercial:

1. executar clientes a partir de maquinas externas;
2. sustentar cada degrau por pelo menos 15 minutos;
3. medir bitrate real por canal e stalls do player;
4. testar 50, 100, 250 e 500 clientes distribuidos entre uma mistura realista de canais;
5. confirmar no XUI o limite por conta, IP, canal e servidor de midia;
6. monitorar hits, misses, espaco do cache e tempo de resposta do upstream;
7. testar expiracao e troca de segmento sem congelamento;
8. testar Range, seek e retomada em VOD;
9. aplicar ACL na origem e confirmar bloqueio a partir de rede externa;
10. repetir a matriz de vazamento apos qualquer mudanca de XUI/LB.

## Veredito

O teto de 14 nao era capacidade maxima da VPS. Era multiplicacao de requisicoes identicas em um caminho sem cache, combinada com recusas do upstream. O cache HLS em duas camadas removeu essa multiplicacao e atingiu 500/500 no teste de concorrencia local.

A capacidade sustentada atual continua limitada principalmente pela banda disponivel e pela instabilidade/capacidade dos canais diferentes no upstream. A protecao de origem ainda exige ACL no lado do XUI para impedir acesso direto mesmo quando seu endereco for conhecido.
