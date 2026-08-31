# Validação VOD: contrato XCIPTV/IBO Player, segurança e carga curta

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
Os resultados desta frente devem refletir o estado real do laboratório e das
playlists atuais; atualize o arquivo de estado real após cada nova execução
relevante.

**Data:** 2026-08-28
**Escopo:** canário isolado e testes determinísticos; nenhuma mudança em produção
**Resultado:** contrato automatizado aprovado; gates reais de player, TLS e soak continuam abertos

## Resumo executivo

Foi criada uma matriz HTTP reproduzível para os padrões que importam na
compatibilidade com XCIPTV e IBO Player: abertura, `HEAD`, leitura integral,
`Range` inicial, seek intermediário, seek próximo do fim, range aberto, suffix
range e retomada com `If-Range`. A matriz usa socket Unix temporário e uma origem
falsa em memória. Não consulta fornecedores, banco, Nginx ou serviços ativos.

Os 14 testes das suítes de relay e players passaram. A carga curta realizou 32
seeks de 4 KiB, com concorrência 8, e obteve 32 respostas `206` com o tamanho
esperado. A suíte de players completa terminou em 0,75 s e atingiu RSS máximo de
40.504 KiB neste host. Esses números validam apenas o harness local; não são um
benchmark de capacidade de produção.

## O que significa “simular XCIPTV/IBO Player”

Os aplicativos são proprietários e não foram executados nesta rodada. A suíte
simula seus comportamentos HTTP relevantes e usa identificadores de teste nos
`User-Agent`; não afirma certificação pelos fabricantes nem equivalência de UI,
decoder, codecs ou reprodução audiovisual.

| Perfil | Sequência simulada | Resultado |
|---|---|---|
| XCIPTV | `GET` integral, ranges de 64 KiB em três posições, `If-Range` | PASS |
| IBO Player | `HEAD`, range aberto a partir de 4 MiB, suffix de 64 KiB | PASS |
| Ambos | filme e série, `200/206`, `Content-Range`, `Accept-Ranges` | PASS |
| Superfície inválida | multi-range, unidade inválida, whitespace ambíguo, `POST` | PASS, falha fechada |
| Concorrência curta | 32 seeks, 8 workers, 4 KiB por resposta | PASS, 32/32 |

O `User-Agent` público não é encaminhado ao fornecedor. O relay usa uma
identidade própria e estável no upstream, reduzindo variação e exposição do
cliente.

## Segurança validada

A suíte `tests/vod_relay_test.py` comprovou, sem rede externa:

- primeira seed obrigatoriamente cadastrada e isolada por tenant;
- bloqueio de DNS privado ou resposta DNS mista antes da conexão;
- rejeição de URL absoluta, traversal codificado e multi-range;
- validação de schemes, portas, IPv6 e headers/tamanhos ambíguos;
- conexão HTTPS fixada no IP validado, preservando hostname no SNI;
- `Range` e `If-Range` preservados até o destino final;
- health local sem acesso ao upstream.

A suíte HTTP adicional comprovou que `Location`, `Set-Cookie`, `Via`,
`X-Powered-By` e `X-Accel-Redirect` da origem não chegam ao cliente. O header
`Server` produzido pelo próprio servidor HTTP pode existir, mas o valor da
origem é substituído e não vaza.

## Comandos e evidências reproduzíveis

```bash
cd /opt/cdnmnus
python3 -m py_compile tests/vod_player_compatibility_test.py tests/vod_relay_test.py
python3 tests/vod_relay_test.py -v
python3 tests/vod_player_compatibility_test.py -v
git diff --check -- tests/vod_player_compatibility_test.py
```

Resultados observados nesta rodada:

```text
vod_relay_test:                 10/10 PASS
vod_player_compatibility_test:   4/4 PASS
seeks concorrentes:             32/32 HTTP 206 e 4096 bytes
py_compile:                     PASS
git diff --check:               PASS
alteração em produção:          nenhuma
rede externa durante testes:    nenhuma
```

O teste novo está em `tests/vod_player_compatibility_test.py`. Ele não contém
credenciais, URLs reais, IPs de origem ou tokens.

## Gates que esta rodada não fecha

Permanecem obrigatórios antes de aprovar produção:

1. executar XCIPTV e IBO Player reais contra uma edge de homologação, cobrindo
   play, pause, seek, retomada, troca de episódio e reconexão;
2. validar um filme e uma série reais por HTTPS, inclusive certificado, SNI,
   cadeia de redirects e rotação DNS;
3. confirmar `If-Range` válido e inválido contra o fornecedor real;
4. reproduzir conteúdo superior a três horas e medir encerramento antecipado;
5. executar carga representativa e soak mínimo de seis horas, observando RSS,
   file descriptors, latência, erros, cancelamento e backpressure;
6. ensaiar rollback da release candidata e provar health antes e depois;
7. verificar em captura/log sanitizado que nenhuma URL, credencial, token,
   hostname ou IP de origem foi persistido.

## Critério de aprovação recomendado

Não promover somente com estes testes. A release fica apta a canário real quando
as suítes continuarem verdes e o armazenamento do host estiver saudável. Ela só
fica apta ao pool após player real, TLS real, carga/soak e rollback passarem com
evidências sanitizadas. Falha em seek, resposta diferente de `200/206`, vazamento
de header/destino ou crescimento sustentado de recursos deve retirar a edge do
pool e acionar rollback.

## Atualização real de 31/08/2026

O contrato automatizado foi exercitado também contra as edges reais após a
ativação da release `20260829012407-d60cfdbf`. Em `.168` e `.170`, filme e
série passaram em Range inicial, seek intermediário, suffix e `HEAD`; uma
carga curta de 16 seeks concorrentes de 4 KiB por edge obteve 16/16 respostas
`206`. Live permaneceu `200`, TLS foi validado e `.111` continuou em `206` sem
alteração.

Isso fecha o teste HTTP real e o rollback da canária, mas não equivale a executar
XCIPTV/IBO fixados individualmente em cada IP nem fecha reprodução superior a
três horas, carga representativa ou soak de seis horas.
