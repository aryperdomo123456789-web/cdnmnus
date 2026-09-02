# Receita oficial: CDN DNS-only com edges 10/10

Data: 2026-09-02
Status: normativa para produção

## 1. Decisão arquitetural

O tráfego público de `cdn.phpd77.com` é distribuído diretamente por DNS entre
as edges. O `.111` e o `.237` são somente controladores de DNS, health,
capacidade e failover; não são entrada pública, proxy de mídia ou hop de
tráfego pesado.

```text
Clientes/XUIs
      |
      | DNS-only: A records das edges elegíveis
      +--> 143.14.168.168  EDGE
      +--> 143.14.168.170  EDGE
      `--> 143.14.168.78   EDGE

143.14.168.111  controlador DNS ativo
45.140.192.237  controlador DNS standby
```

HAProxy frontal, VIP e `.111` como entrada pública não fazem parte desta
arquitetura e não devem ser publicados no DNS. O laboratório de LB continua
documentado separadamente em `LOAD_BALANCER_LAB_RUNBOOK.md`.

## 2. Como o balanceamento funciona de verdade

O controlador central sonda HTTPS, SNI, `/edge-health`, latência, erros de
rede e amostras sanitizadas de capacidade. A cada reconciliação:

1. todas as edges `ready` e sem pressão impeditiva entram no RRset DNS;
2. uma edge `draining`, `saturated` ou `down` deixa de receber novas
   resoluções;
3. sessões já estabelecidas não são interrompidas pelo reconciliador;
4. uma edge volta ao pool somente depois da histerese de recuperação;
5. se nenhuma edge for elegível, a publicação falha fechada, sem apontar para
   `.111` ou `.237`.

DNS não oferece peso por conexão. Portanto, “usar o máximo” nesta opção
   significa manter todas as edges saudáveis no pool e retirar rapidamente as
   pressionadas, não prometer uma divisão matemática de cada sessão.

## 3. Capacidade BlazeHosting

As edges `.168`, `.170` e `.78` possuem perfil declarado de `10 Gbps`, com
`25%` de margem operacional e `7.5 Gbps` utilizáveis por política. Isso é uma
linha de base registrada, não uma medição independente nem confirmação
contratual do provedor. Antes de elevar qualquer limite, registrar amostras
reais de throughput, CPU, latência, erros de NIC e `HTTP 206`.

A política determinística está em `core/capacity_policy.py` e é aplicada pelo
`scripts/edge_health_controller.py`. Ela não aumenta pesos artificialmente:
pressão alta causa drenagem ou exclusão, e não sobrecarga deliberada.

## 4. Estado operacional obrigatório

| Componente | Estado oficial |
|---|---|
| `.111` | controlador DNS ativo, lease/fencing lógico vigente |
| `.237` | controlador DNS standby, sem tráfego normal |
| `.168`, `.170`, `.78` | edges de dados, `ready` quando saudáveis |
| `cdn.phpd77.com` | A records DNS-only somente para edges elegíveis |
| HAProxy frontal | inativo em produção; laboratório separado |

Não cadastrar controladores no pool de mídia, não copiar SQLite entre VPS e
não promover edge a LB sem uma decisão arquitetural nova, documentada e
aprovada.

## 5. Gates de aceite

- [ ] As três edges respondem HTTPS com SNI e `/edge-health` 200.
- [ ] Live, VOD, `HTTP 206`, `Range` e `Content-Range` passam em cada edge.
- [ ] O controlador remove uma edge falha sem publicar `.111` como fallback.
- [ ] A edge removida retorna apenas após a histerese de recuperação.
- [ ] O menu e a identidade da release são iguais em todas as VPS.
- [ ] Perfis de capacidade têm fonte, margem, operador e validade.
- [ ] Existem amostras reais antes de declarar capacidade medida 10/10.
- [ ] O failover de controlador exige isolamento comprovado e lease exclusiva.

Enquanto qualquer gate estiver pendente, a capacidade deve ser descrita como
baseline ou preparada, nunca como garantia 10/10 medida.

## 6. Referências

- `PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md`
- `CAPACITY_CONTROLLER_AND_MULTI_LB_RECIPE.md`
- `RECEITA_FAILOVER_MANUAL_SEGURO_MENU_SSH_2026-09-02.md`
- `LOAD_BALANCER_LAB_RUNBOOK.md`
