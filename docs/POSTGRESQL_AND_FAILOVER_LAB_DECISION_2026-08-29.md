# Decisão de laboratório PostgreSQL e descoberta de failover — 2026-08-29

## Decisão provisória

O PostgreSQL autoritativo **não deve ser instalado na `.111`, `.168`, `.170`
nem na futura `.66`**. Esses nós têm papéis de dados/LB e precisam continuar
operáveis quando o plano de controle falhar. Além disso, a latência de storage
observada no control node torna inadequado hospedar ali a única cópia.

O alvo é PostgreSQL dedicado ou gerenciado, em rede administrativa, com TLS,
backup externo e restauração comprovada. A escolha final do serviço permanece
um gate de infraestrutura; nenhuma DSN de produção foi criada e o caminho
SQLite atual não foi alterado.

## Laboratório implementado

- migrações SQL versionadas em `database/postgresql/migrations`;
- importação somente para schema vazio a partir de uma leitura SQLite
  transacional e `query_only`;
- comparação por contagem e SHA-256 lógico de todas as tabelas portadas;
- DSN opt-in que recusa conexão sem TLS e sem acknowledgement explícito;
- lease com lock atômico e fencing token crescente;
- claim de worker com `FOR UPDATE SKIP LOCKED`;
- plano de `pg_dump`/`pg_restore` que usa `pg_service.conf` e não expõe DSN.

O host atual não possui cliente/servidor PostgreSQL nem runtime de container.
Portanto, os contratos offline foram testados, mas migrations, importação,
dois workers e restore ainda precisam de uma instância laboratorial dedicada.

## Gate para executar em uma instância descartável

1. provisionar banco vazio e usuário de menor privilégio;
2. configurar `pg_service.conf` fora do Git e certificado de CA;
3. exportar `CDNMNUS_POSTGRES_LAB_ACK=I_UNDERSTAND_THIS_IS_LAB`;
4. aplicar migrations sob advisory lock;
5. capturar o SQLite ativo por leitura transacional, nunca por `scp`/`rsync`;
6. importar somente no schema vazio;
7. exigir contagens e digests idênticos;
8. executar painel/worker em shadow/read-only;
9. disputar o mesmo job com dois workers e provar uma única claim;
10. executar `pg_dump`, restaurar em outra instância limpa e repetir a comparação.

## Pergunta pronta para a BlazeHosting

> A BlazeHosting oferece Floating IP ou IP failover movível por API entre duas
> VPS? Existe API documentada para mover o IP, desligar a interface de rede ou
> executar fencing (power-off forçado) de uma VPS? Precisamos saber os tempos
> máximos de propagação, autenticação/escopos da API, limites, idempotência,
> confirmação do fencing e comportamento quando a VPS antiga está isolada da
> rede de controle.

Solicitar também link da documentação, ambiente sandbox, SLA e confirmação de
que a operação impede simultaneamente os dois nós de anunciar o endpoint.

## Árvore de decisão do Passo 9

- Floating IP movível e fencing confirmável: lease PostgreSQL, fence antigo,
  mover IP e só então promover/publicar.
- Sem floating IP, mas DNS autoritativo por API e fencing confirmável: lease,
  fence antigo, alterar DNS e respeitar TTL medido.
- DNS sem fencing confirmável: inadequado para promoção automática.
- Nenhuma opção acima: serviço externo de failover ou mudança de
  provedor/topologia antes de implementar eleição de produção.

`systemctl stop` por SSH, round-robin DNS e simples expiração de lease não são
fencing e permanecem recusados.
