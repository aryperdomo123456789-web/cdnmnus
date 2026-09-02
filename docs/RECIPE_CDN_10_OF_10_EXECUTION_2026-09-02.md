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
DNS-only com health check
  |
143.14.168.168 / .170 / .78  (somente edges saudáveis)
  |
múltiplos XUIs e VOD

143.14.168.111  controlador DNS/health + LB de contingência
45.140.192.237  controlador DNS/health standby
```

O IP `.66` não faz parte do ambiente atual. A máquina remota `.237` é o node
`4`; o node `1`, `143.14.168.111`, é o control plane/DNS ativo. Nenhum desses
controladores recebe tráfego de mídia.
As edges são nodes `2`, `3` e `6`.

## Ordem sem risco

1. Execute o auditor e arquive a saída sem segredos.
2. Mantenha as três edges no mesmo release ID e digest.
3. Faça backup e restore do banco antes de alterar papéis.
4. Garanta SSH pinado, console e pacote autorizado em `.237` e `.111`.
5. Execute health com SNI e `/edge-health` nas três edges.
6. Confirme perfis de capacidade e capacidade útil antes de ampliar o pool.
7. Publique `cdn.phpd77.com` somente como DNS-only para as edges saudáveis; nunca
    aponte esse hostname para `.111` ou `.237` neste modo.
8. Faça ensaio de queda de edge, drain, retirada DNS, recuperação e soak.
9. Mantenha `.237` preparada como standby de controle, sem streams.
10. Ative coleta contínua de capacidade quando o controlador dedicado estiver
    homologado; DNS não suporta pesos por conexão em tempo real.

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

O health check é executado pelo `scripts/edge_health_controller.py`, que testa
cada edge com SNI e `/edge-health`. Após a histerese configurada, ele remove ou
recoloca o A record da edge no Cloudflare. `.111` e `.237` não transportam
streams nesse desenho; eles apenas executam o controle e a reconciliação DNS.

Documentação detalhada: [runbook mestre](PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md)
e [receita de capacidade e failover](CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md).
