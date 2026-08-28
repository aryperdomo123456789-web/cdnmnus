# Multi-edge e failover

Para a especificação completa de provisionamento, inventário, modelo de dados,
playbooks, rollout e multi-XUI, consulte
[`ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md`](ANSIBLE_MULTI_EDGE_IMPLEMENTATION.md).

Cada edge deve executar a mesma versão e configuração, possuir certificado TLS
para o hostname público e alcançar o XUI/LBs pela ACL da origem. O health check
externo é `HTTPS /edge-health`: ele só retorna 200 quando Nginx alcança um
broker com configuração válida.

## Topologia mínima

- Edge A e Edge B em provedores ou zonas distintas.
- DNS com health check e TTL de 30–60 segundos, ou load balancer anycast.
- Mesmo hostname público nas duas edges.
- XUI liberando porta 80 exclusivamente aos IPs públicos das edges.
- Configuração implantada de forma idêntica, mas caches independentes.

Configuração do provedor DNS/LB:

1. cadastrar os dois IPs como origins;
2. probe HTTPS em `/edge-health`, intervalo de 15 segundos, timeout de 5;
3. retirar uma edge após três falhas consecutivas;
4. recolocar somente após cinco sucessos consecutivos;
5. preservar afinidade apenas se o aplicativo exigir; HLS não depende dela.

## Teste de desastre

Com tráfego de homologação ativo, interrompa Nginx na Edge A. Confirme que o
DNS/LB retira A, novos segmentos passam por B e não há hostname/IP de origem nas
respostas. Restaure A e espere os cinco checks antes de reinseri-la.

Uma segunda edge não pode ser criada nesta VPS: é necessário fornecer outro
servidor/IP e acesso DNS. O código e o endpoint de failover ficam prontos; a
redundância só existe depois de implantar a segunda instância.
