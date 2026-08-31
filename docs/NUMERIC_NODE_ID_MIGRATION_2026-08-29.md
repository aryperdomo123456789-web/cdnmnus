# Migração para IDs técnicos numéricos — 2026-08-29

## Resultado aplicado

| ID técnico | Nome | IPv4 | Papel registrado | Estado |
|---:|---|---|---|---|
| `1` | Load Balancer Principal | `143.14.168.111` | `load_balancer` | `candidate` |
| `2` | Edge 168 | `143.14.168.168` | `edge` | `bootstrapping` |
| `3` | Edge 170 | `143.14.168.170` | `edge` | `bootstrapping` |

O próximo servidor recebe automaticamente o ID `4`. A tabela
`node_id_sequence` é monotônica e a reserva ocorre em transação
`BEGIN IMMEDIATE`, impedindo que dois cadastros recebam o mesmo número. Uma
falha após a reserva pode deixar uma lacuna, mas um ID nunca é reutilizado.

## Compatibilidade preservada

`lb011` e `lb02` deixaram de ser IDs do banco. Permanecem temporariamente apenas
como aliases internos do inventário Ansible e nomes de arquivos de chave SSH.
Isso preserva fingerprints, chaves, comandos históricos e rollback. Novas
interfaces devem exibir os IDs `2` e `3`, nunca esses aliases.

A `.111` foi registrada como candidata a primeiro load balancer. Isso não a
promoveu para ACTIVE, não instalou HAProxy, não alterou DNS e não removeu seu
runtime atual. Promoção continua dependente de lock, fencing, drain e gates do
runbook de produção.

## Menu comum por SSH

Os três servidores receberam:

```text
/usr/local/bin/mago-cdn
/usr/local/lib/cdnmnus-node-menu.py
/etc/cdnmnus/node-id
/etc/cdnmnus/node-role.json
/etc/cdnmnus/control-plane.conf
```

No nó `1`, `mago-cdn` abre a Central de Operações completa e usa o banco
autoritativo. Nos nós `2` e `3`, o mesmo comando abre o cliente local sensível
ao papel, com identidade, conexão, serviços e validações. O cliente não cria
SQLite paralelo e não concede promoção/DNS sem o control plane.

## Evidências

- migração ensaiada e reaplicada em cópia antes de produção;
- backup SQLite consistente criado antes da mudança;
- `integrity_check` do backup aprovado;
- instalação do menu repetida com `changed=0` nos três nós;
- IDs e papéis confirmados remotamente;
- `nginx -t` aprovado nos três nós;
- `/edge-health` retornou HTTP 200 e TLS válido nos três IPs;
- testes do banco administrativo e topologia aprovados.

Nenhum serviço foi reiniciado ou recarregado pela migração do ID/menu.
