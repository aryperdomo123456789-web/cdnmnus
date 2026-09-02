# Receita executável da CDN 10/10

Esta receita é a porta de entrada única para a topologia atual. Ela não chama
uma máquina de `active` só porque o processo existe: cada gate precisa passar.
O auditor é somente leitura:

```bash
cd /opt/cdnmnus
python3 scripts/cdnmnus-readiness-audit.py
```

## Topologia real

```text
Clientes
  |
DNS/VIP com health e fencing
  |
LB 143.14.168.111 ACTIVE     LB 45.140.192.237 STANDBY
CONTROL PLANE + HAProxy       HAProxy sem tráfego
  |                           |
  +-------------+-------------+
                |
     143.14.168.168 / .170 / .78
                |
       múltiplos XUIs e VOD
```

O IP `.66` não faz parte do ambiente atual. A máquina remota `.237` é o node
`4`; o node `1`, `143.14.168.111`, é o control plane atual e será o LB ACTIVE.
As edges são nodes `2`, `3` e `6`.

## Ordem sem risco

1. Execute o auditor e arquive a saída sem segredos.
2. Mantenha as três edges no mesmo release ID e digest.
3. Faça backup e restore do banco antes de alterar papéis.
4. Garanta SSH pinado, console e pacote autorizado em `.237` e `.111`.
5. Execute `preflight` do role `load_balancer` com `serial: 1`.
6. Faça `deploy` em `.111` para gerar candidato HAProxy; não publique ainda.
7. Teste `haproxy -c`, TLS, `/lb-health`, `/edge-health`, live, filme, série e
   `Range` usando `--resolve`.
8. Registre os backends `.168`, `.170` e `.78` com pesos e health.
9. Adquira lease exclusiva e fencing token crescente no control plane.
10. Promova `.111` somente depois de comprovar fencing do endpoint anterior.
11. Prepare `.237` com a mesma release e deixe-o `standby`, sem lease ativa.
12. Faça ensaio de queda de edge, drain, queda do LB, promoção `.237` e retorno.
13. Só então substitua o DNS round-robin pelo endpoint controlado do LB.
14. Ative coleta de capacidade, alertas, pesos dinâmicos e soak sustentado.

## Gatilhos de parada

Pare e faça rollback se houver release/digest divergente, `nginx -t` ou
`haproxy -c` falhando, dois LBs `active`, fencing ausente, origem exposta,
`Location`/credencial vazada, `5xx` acima do SLO ou `Range` quebrado.

## Estado atual

As três edges estão convergentes e saudáveis. Existe certificado ACME válido
para `cdn.phpd77.com` no control plane, mas ele ainda não é um artefato
operacional completo para todos os aliases públicos do LB. O auditor continuará
bloqueando a nota 10 enquanto `.111` não for promovida com lease/fencing,
`.237` não for preparada como standby real, e o PostgreSQL autoritativo,
VIP/API de fencing, controlador contínuo de capacidade e teste de desastre
também são gates externos; não devem ser simulados por um script local.

Documentação detalhada: [runbook mestre](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
e [receita de capacidade e failover](CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md).
