# Receita executável: capacidade contínua, edges dinâmicas e failover de LBs

Data: 2026-08-31  
Escopo: transformar a topologia atual em uma rede que mede capacidade, ajusta
pesos, retira máquinas saturadas, pede novas máquinas e mantém um único
endpoint público.

Este documento é um plano de implementação. Ele não afirma que o controlador
contínuo já existe. Hoje o pacote instala edge/LB, o menu cria onboarding e a
promoção termina em `candidate`/`standby`; as seis capacidades deste documento
continuam sendo trabalho de implementação e validação.

## 1. Regra simples

1. O cliente usa somente `cdn.phpd77.com`.
2. Esse nome aponta para **um LB ativo** (preferencialmente IP flutuante).
3. O LB ativo distribui para edges saudáveis.
4. O LB standby não recebe tráfego e mantém a mesma configuração assinada.
5. Um controlador no control-plane decide; nenhum menu promove diretamente.
6. Toda decisão é registrada, bloqueada por lock/fencing, testada e reversível.

Nunca publicar DNS round-robin como se fosse failover: ele não sabe qual
servidor está vivo e não remove sessões de uma edge saturada.

## 2. Estado real de partida

| nó | função/estado | release | observação |
|---|---|---|---|
| `143.14.168.111` | LB legado/candidate | sem release gerenciada | não alterar durante o canário |
| `143.14.168.168` | edge/ready | `20260829012407-d60cfdbf` | VOD validado |
| `143.14.168.170` | edge/ready | `20260829012407-d60cfdbf` | VOD validado |
| `45.140.192.237` | LB laboratório/standby | `v0.5.0-managed-node.9` | promoção de laboratório validada |

O SQLite está íntegro e a matriz DNS local ainda lista as edges. Não fazer
alteração de produção até existir controlador, fencing e teste de failover.
As referências normativas são `STATE_REAL_2026-08-29.md`,
`PRODUCTION_MULTI_LB_MULTI_EDGE_MULTI_XUI_RUNBOOK.md`,
`LOAD_BALANCER_143_14_168_66_IMPLEMENTATION_PLAN.md` e
`CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md`.

## 3. Como descobrir se uma VPS é 1, 5 ou 10 Gbps

Não é seguro deduzir o plano apenas de um `speedtest`: o resultado depende do
servidor, rota, horário e tráfego concorrente. Use duas fontes:

1. **Capacidade contratada (autoritativa):** API/painel do provedor ou cadastro
   manual aprovado (`capacity_mbps=1000`, `5000`, `10000`).
2. **Medição controlada (verificação):** `iperf3` entre dois geradores externos,
   8–16 fluxos TCP, janela de 60–120 s, em manutenção. Repetir três vezes e
   guardar mediana, perda, retransmissões e interface usada.

Persistir `capacity_mbps`, `measured_mbps`, `confidence`, `source`,
`measured_at` e `expires_at`. A capacidade utilizável é:

```text
usable_mbps = capacity_mbps * (1 - headroom)
headroom = 0.25 (padrão; nunca menor que 0.20)
```

Se a medição ficar abaixo de 80% do contratado, marcar `capacity_suspect` e
abrir alerta; não aumentar peso para compensar. Sem fonte contratual, o nó
fica `candidate` e não entra automaticamente em produção.

## 4. Componentes a implementar

### 4.1 Banco/control-plane

Criar migração idempotente (sem copiar SQLite entre VPS) com:

* `edge_capacity_profiles(edge_id, capacity_mbps, headroom, max_connections,
  source, confidence, measured_mbps, measured_at, expires_at)`;
* `edge_capacity_samples(edge_id, sampled_at, egress_mbps, p95_ms, http5xx,
  active_sessions, cpu_pct, mem_pct, nic_errors, vod_206_ok)`;
* `edge_backend_runtime(edge_id, state, pressure, desired_weight, applied_weight,
  reason, changed_at, fencing_token)`;
* `capacity_alerts(id, severity, type, edge_id, payload_json, state,
  opened_at, acknowledged_at, resolved_at)`;
* `failover_operations(id, old_lb, new_lb, fencing_token, phase, started_at,
  completed_at, result, rollback_change_id)`.

Adicionar chaves estrangeiras, índices por `(edge_id, sampled_at)` e retenção
de amostras (por exemplo, 7 dias em 10 s; agregados por 90 dias). Cada escrita
de decisão deve gerar evento de auditoria com operador, release, digest e
fencing token.

### 4.2 Coletor

Instalar `cdnmnus-capacity-collector.service` em cada edge. A cada 10 s,
coletar somente métricas (nunca conteúdo ou tokens): bytes TX da interface,
conexões, CPU, memória, erros NIC, latência e resultado de `/edge-health`.
Enviar por mTLS ao control-plane; se não houver conectividade, manter fila
local limitada e expirar amostras antigas.

### 4.3 Controlador

Criar `cdnmnus-capacity-controller.service` no control-plane. Um único líder
executa (lease de 15 s renovado a cada 5 s); os demais apenas observam. O loop
deve ser idempotente:

```text
a cada 10 s:
  ler amostras recentes e perfil contratado
  calcular pressão e estado de cada edge
  calcular peso desejado com histerese
  transacionar decisão + auditoria + fencing token
  aplicar peso somente no LB ativo (socket runtime HAProxy)
  publicar alerta/onboarding quando o headroom do conjunto for insuficiente
```

## 5. Pressão, pesos e estados

Para cada edge, calcular EWMA (janela 60 s) e:

```text
bw_ratio  = egress_mbps / usable_mbps
conn_ratio = active_sessions / max_connections
pressure  = max(bw_ratio, conn_ratio, cpu_pct/85, p95_ms/SLO, http5xx/1%)
```

Limites iniciais, ajustáveis por ambiente:

* `< 0.70`: `ready`;
* `0.70–0.85`: `pressured` (reduzir peso suavemente);
* `>= 0.85` por 3 ciclos: `draining` (peso zero para novas conexões);
* `>= 0.95`, erro de NIC ou health falho: `saturated/down` (remover);
* 5 ciclos bons consecutivos e capacidade válida: reinserir em `ready`.

Peso recomendado, limitado ao intervalo HAProxy 1–256:

```text
factor = max(0.05, (1 - min(pressure, 1.0))²)
desired = clamp(round(base_weight * factor), 1, 256)
```

Alterar no máximo 20% por ciclo; exigir diferença de 10% para aplicar, evitando
flapping. Para `draining/down`, usar `set server ... state drain` e depois
`weight 0`; aguardar o maior timeout de VOD antes de desabilitar. Cinco sucessos
não bastam se o perfil estiver expirado ou o digest divergente.

O socket runtime (`stats socket`) é o caminho de pesos frequentes; reload de
configuração só para mudança estrutural. O standby recebe snapshot assinado,
mas nunca aplica tráfego enquanto não for promovido.

## 6. Remoção automática e onboarding

Quando uma edge entra em `draining`, o controlador deve:

1. congelar novas sessões;
2. preservar conexões VOD existentes até `drain_timeout`;
3. confirmar que o LB ativo não a seleciona;
4. abrir `capacity_alert` com evidências;
5. reavaliar a cada 30 s.

Disparar alerta `capacity_pool_low` quando a soma de `usable_mbps` menos o
tráfego EWMA ficar abaixo de 25% ou quando todas as edges estiverem acima de
70%. Criar uma solicitação de onboarding **sem criar/cobrar VPS
automaticamente**, a menos que uma API de provedor esteja explicitamente
configurada e aprovada. O menu de qualquer nó então executa o fluxo já
existente: IP, root, senha em memória, host-key TOFU, tag/digest obrigatório,
preflight, backup e instalação do pacote versionado.

Uma nova máquina começa sempre como `candidate`; só vira `ready` após health,
VOD 200/206, digest idêntico e soak. Se cadastrada como LB, começa
`candidate/standby`; promoção exige solicitação ao control-plane.

## 7. Failover real entre LBs

### Endpoint

Preferência 1: IP flutuante/VIP do provedor, anexado a apenas um LB.  
Preferência 2: Cloudflare DNS **DNS-only** com TTL baixo (por exemplo 60 s),
aceitando que caches externos tornam o failover eventual, não instantâneo.

Não usar VRRP entre VPS sem domínio L2. Para Cloudflare, implementar cliente
idempotente com token mínimo `Zone DNS Edit`, segredo fora do Git, `proxied=false`,
record ID/nome/tipo fixos, leitura prévia e auditoria. O controlador publica
somente o resultado de uma eleição válida.

### Eleição e fencing

Lease do LB ativo: renovação 5 s, validade 15 s, `fencing_token` monotônico.
Exigir quorum do control-plane e testemunha independente (ou API do provedor).
Sem quorum, congelar promoção. Nunca promover por uma única falha de ping.

### Sequência obrigatória

1. Três probes independentes falham (TCP, TLS/SNI, `/edge-health`).
2. Controlador adquire lock transacional e incrementa fencing token.
3. Isola/fence o LB antigo (detach de VIP ou remoção Cloudflare) e confirma.
4. Executa preflight e aplica configuração assinada no standby.
5. Promove para `active`, atribui VIP/publica DNS e verifica 200/206, Host/SNI,
   Range e ausência de `Location` indevido.
6. Mantém o antigo `fenced/candidate` até diagnóstico; nunca dois `active`.
7. Registra RTO, causa, logs sanitizados e plano de rollback.

Meta inicial: detecção 15–30 s e publicação dentro do SLA do provedor; medir,
não prometer “instantâneo” com DNS.

## 8. Teste de carga próximo de 10 Gbps

Executar somente em laboratório/autorização do provedor. Uma única VPS geradora
não é suficiente: usar várias fontes (idealmente 4–10) e medir cada interface.

1. Teste de rede separado: `iperf3`, 8–16 streams, 1/2/5/8/10 Gbps em rampas.
2. Teste HTTP/VOD autorizado: arquivos sintéticos, conexões longas, seeks e
   `Range`; validar live `200`, filme/série `206`, `Content-Range` e HEAD.
3. Rampa de 5 min + sustentação de 30 min por estágio; coletar LB, edges,
   origem, NIC, CPU, memória, p95/p99, 4xx/5xx, retransmissões e drops.
4. Abortar se 5xx >1%, p99 exceder SLO, CPU >85%, perda/retransmissão anormal,
   erro NIC ou qualquer edge em hard limit.

Capacidade do conjunto é `min(capacidade do LB ativo, soma das edges,
origem/provedor)`, sempre reservando 20–30%. Aceitação: pesos convergem sem
flapping, edge saturada deixa de receber sessões, nenhuma sessão VOD quebra,
um único LB ativo e failover/rollback reproduzíveis.

## 9. Ordem de implementação segura

1. Implementar migração/tabelas e testes unitários do cálculo.
2. Implementar coletor em laboratório; validar assinatura, relógio e retenção.
3. Implementar controlador em modo `observe` (não altera pesos).
4. Ativar pesos runtime em uma edge canário; comparar com métricas reais.
5. Ativar drain automático e alertas; revisar falsos positivos por 24 h.
6. Integrar onboarding como solicitação aprovada no menu.
7. Implementar provider adapter de VIP; só então Cloudflare DNS como fallback.
8. Implementar lease/quorum/fencing e simular partição, queda e retorno.
9. Executar carga 1→10 Gbps em laboratório e registrar relatório.
10. Promover `.66`/novo LB somente após todos os gates e atualizar os runbooks.

## 10. Rollback e critérios de parada

Qualquer erro de preflight, digest, backup, lock, health, VOD ou publicação
interrompe a operação. Restaurar o último snapshot conhecido, devolver pesos
estáticos, retirar a edge/LB do pool e manter o endpoint no último LB saudável.
Toda ação precisa de `change_id`, backup verificável e comando de reversão.

Pronto para produção somente quando houver: duas execuções de failover sem
split-brain; RTO medido; restore testado; Cloudflare/VIP idempotente; soak de
seis horas; e aprovação registrada no `PRODUCTION_RISK_ACCEPTANCE`.

## 11. Checklist infantil (marque na ordem)

- [ ] Sei a capacidade contratada e medi a VPS sem tráfego de clientes.
- [ ] O nó tem tag/digest aprovados e health/VOD 200/206.
- [ ] O banco tem perfil, amostras, estado, alerta e auditoria.
- [ ] O coletor envia métricas; o controlador está em `observe`.
- [ ] O peso muda gradualmente e volta após cinco ciclos bons.
- [ ] Edge saturada drena sem cortar VOD existente.
- [ ] Nova edge é apenas solicitação `candidate`, nunca promoção direta.
- [ ] Há um único endpoint e um único LB `active`.
- [ ] VIP ou Cloudflare foi testado com fencing e rollback.
- [ ] Carga 1/5/10 Gbps foi executada com relatório e gates aprovados.

Se qualquer caixa estiver desmarcada, a rede continua em laboratório/candidate.
