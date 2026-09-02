# Receita de failover manual seguro pelo menu SSH

Data: 2026-09-02
Escopo: failover manual entre o controlador `.111` e o standby `.237`, com
DNS direto para as edges. A receita também descreve a promoção excepcional de
uma edge para load balancer.

## 1. Regra mais importante

O failover manual não é um botão que troca `standby` por `active`. Ele é uma
operação em duas camadas:

```text
operador no menu SSH
        |
solicitação autenticada + confirmação de isolamento
        |
control plane valida lease, token e estado
        |
um único controlador ativo reconcilia DNS
        |
cdn.phpd77.com responde diretamente com edges saudáveis
```

`.111` e `.237` são controladores. Neste modo, nenhum deles deve transportar
os streams. O DNS publica somente os IPs das edges `ready`; o tráfego pesado
continua em `.168`, `.170`, `.78` e futuras edges.

## 2. O que o código já garante

| Proteção | Implementação | Regra |
|---|---|---|
| Estado de edge | `core/db.py` | `ready`, `draining`, `failed` e demais transições são auditados |
| Estado de LB | `core/topology.py` | `active` só pode vir de `promote_load_balancer` |
| Uma promoção | `load_balancers` | índice impede mais de um `active` |
| Lease | `promotion_locks` | validade, titular e token são conferidos |
| Fencing lógico | token crescente | token antigo não pode promover novamente |
| DNS direto | `core/dns_reconciler.py` | somente edges prontas viram A records |
| Health | `scripts/edge_health_controller.py` | HTTPS, SNI, `/edge-health` e histerese |
| Preparação edge -> LB | `scripts/process_promotion_request.py` | aceita preparação para `candidate`/`standby`, nunca `active` |

O banco local não comprova que um VPS remoto foi desligado. A confirmação
manual de isolamento precisa vir do painel/API do provedor ou de uma evidência
operacional equivalente. Sem isso, o fluxo deve parar.

## 3. Estados permitidos

### Operação normal

```text
.111 = controlador ativo
.237 = standby
edges = ready/failed/draining conforme health
```

### Failover manual

```text
.111 = isolado ou comprovadamente indisponível
.237 = standby -> active
```

O estado antigo não pode continuar `active`. Se não for possível provar o
isolamento, mantenha `.237` em `standby` e faça apenas diagnóstico.

## 4. Menu SSH: opção segura

### 4.1 O que existe hoje

O menu real em `cli/mago_cdn.py` já possui consulta de edges, drenagem,
marcação `ready`, desabilitação, cadastro de edge/LB e reconciliação DNS. Ele
agora possui a opção `Failover manual do controlador DNS`. A versão do menu da
tag `v0.5.0-managed-node.22` foi distribuída serialmente para `.111`, `.237`,
`.168`, `.170` e `.78`, no caminho `/usr/local/lib/cdnmnus-node-menu.py`.
Essa distribuição atualiza somente o menu; não reinstala runtime, Nginx,
HAProxy ou serviços de mídia.

O caminho atual de `core/db.py::request_load_balancer_promotion` serve para
preparar uma edge como LB. O script
`scripts/process_promotion_request.py` termina em `candidate`/`standby` e
recusa o papel `active`. A promoção de verdade permanece exclusivamente em
`core/topology.py::TopologyStore.promote_load_balancer`.

### 4.2 Opção que deve ser implementada

O menu deve oferecer esta opção somente para operador autenticado:

```text
Failover manual do controlador DNS
```

O menu deve mostrar antes da confirmação:

```text
Origem: .111 / node 1
Destino: .237 / node 4
Função: controlador DNS; sem tráfego de mídia
Edges esperadas: .168, .170, .78
```

Depois deve solicitar, em campo separado:

```text
Motivo obrigatório:
Evidência de isolamento:
Confirmação: CONFIRMO_ISOLAMENTO_DO_111
```

O texto de confirmação não deve ser aceito como prova sozinho. O operador deve
ter conferido o painel do provedor e informar a referência da ação, por
exemplo o ID da operação de desligamento ou bloqueio. Não registrar tokens,
senhas, URLs de playlist ou conteúdo de mídia.

### 4.3 Regras de implementação do menu

Implementar em uma função nova, por exemplo
`manual_controller_failover(db)`, chamada por um submenu de infraestrutura.
Não colocar essa operação dentro de `edge_action`, pois `edge_action` altera
diretamente o estado de edge e reconcilia o DNS.

A função deve seguir exatamente esta ordem:

1. carregar o estado pelo `TopologyStore`, nunca por texto digitado;
2. aceitar somente pares previamente cadastrados, neste ambiente node `1 -> 4`;
3. exigir motivo e `isolation_reference` não vazios;
4. exigir a frase exata `CONFIRMO_ISOLAMENTO_DO_111`;
5. mostrar resumo final e pedir uma segunda confirmação;
6. executar preflight somente de leitura no destino;
7. adquirir lease usando `acquire_promotion_lock`;
8. chamar `promote_load_balancer` com o `lease_id` e `fencing_token` retornados;
9. reconciliar DNS pelo reconciliador oficial;
10. executar o laboratório e registrar sucesso ou falha.

Se qualquer passo falhar, a função deve parar. Não deve tentar novamente com
outro estado, não deve fazer SQL corretivo e não deve executar `systemctl stop`
remoto. O operador recebe apenas o próximo passo seguro.

O `isolation_reference` deve entrar no payload sanitizado do evento. Se o
modelo precisar de uma coluna própria, a migração deve ser aditiva e testada;
nunca alterar tabelas de produção manualmente.

## 5. Procedimento completo `.111 -> .237`

### Passo 0: congelar mudanças

Não execute deploy, alteração de DNS manual ou alteração de papéis em paralelo.
Faça backup do banco e salve o relatório do auditor:

```bash
cd /opt/cdnmnus
python3 scripts/cdnmnus-readiness-audit.py \
  > /var/tmp/readiness-before-manual-failover.json
```

### Passo 1: confirmar o incidente

Verifique `.111` por console ou monitor independente. Não use somente o fato
de o SSH ter falhado: SSH pode estar indisponível enquanto o serviço continua
ativo.

### Passo 2: executar fencing manual no provedor

No painel/API do provedor, desligue ou bloqueie `.111`. Guarde o ID da ação.
Se o provedor não permitir confirmar isolamento, pare aqui.

### Passo 3: iniciar a solicitação no menu

O menu deve enviar ao control plane uma solicitação com:

```yaml
source_node: "1"
target_node: "4"
operation: "manual_dns_controller_failover"
operator: "identidade-auditada"
reason: "falha confirmada no .111"
isolation_reference: "id-da-acao-do-provedor"
```

O control plane deve recusar se existir outro deployment em andamento, se o
destino não estiver `standby`, se as edges não estiverem saudáveis ou se a
referência de isolamento estiver vazia. A solicitação não é ainda uma
promoção: ela é uma intenção auditável aguardando todos os gates.

### Passo 4: validar o destino

Executar somente probes de leitura em `.237`:

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo nginx -t
sudo systemctl is-active nginx cdnmnus-admin cdnmnus-orchestrator
```

Validar os três backends com o hostname correto e SNI. Não usar
`cdn.phpd77.com` se ele estiver configurado para responder `421` diretamente;
usar o `health_host` real do tenant/lab.

### Passo 5: adquirir lease e promover

O serviço de promoção deve, na mesma operação controlada:

1. adquirir lease exclusiva para `.237`;
2. gerar fencing token maior que o anterior;
3. confirmar que não existe outro `active`;
4. executar `promote_load_balancer`;
5. registrar operador, motivo, referência de isolamento e token.

No código atual, `acquire_promotion_lock` é o lock transacional do control
plane. Ele não desliga uma VPS. Em failover manual, a prova de isolamento é a
ação conferida pelo operador no provedor; em failover automático, ela deve vir
de um adaptador externo comprovável.

Nunca altere `nodes.state` ou `load_balancers.state` com SQL manual. A função
de promoção recusa estado `active` sem lease válida e token correto.

### Passo 6: reconciliar DNS direto para edges

Depois da promoção do controlador, o reconciliador deve publicar somente as
edges `ready`:

```text
cdn.phpd77.com A -> .168
cdn.phpd77.com A -> .170
cdn.phpd77.com A -> .78
```

Não publicar `.111` nem `.237` no pool DNS de mídia. O controlador ativo
executa o health controller e remove uma edge somente após o limite de falhas;
ela só retorna depois do limite de sucessos.

### Passo 7: testar o fluxo dos aplicativos

Usar a conta exclusiva do laboratório. A aprovação exige:

- playlist HTTP `200`;
- três conteúdos live com bytes recebidos;
- filmes com `HTTP 206` e `Content-Range` válido;
- séries com `HTTP 206` e `Content-Range` válido;
- SNI correto;
- nenhum IP de origem, credencial ou hostname interno na resposta;
- relatório sanitizado com resultado `ok`.

### Passo 8: encerrar ou fazer rollback

Se os testes falharem, não reative `.111` ainda. Mantenha `.237` em `active`
somente se o DNS e os testes estiverem estáveis; caso contrário, retire a
publicação controlada e execute rollback pelo fluxo autorizado.

Para retornar ao `.111`:

1. isole `.237` no provedor;
2. registre nova referência de isolamento;
3. adquira novo token crescente;
4. promova `.111`;
5. confirme que `.237` voltou a `standby`;
6. repita os testes do laboratório.

## 6. Promover uma edge para load balancer

Isso é emergência ou expansão planejada, não o failover normal. O fluxo é:

```text
edge ready
  -> drain
  -> remover de todos os backends
  -> remover do pool DNS
  -> solicitar promoção
  -> instalar pacote aprovado
  -> configurar HAProxy e TLS
  -> preflight e laboratório
  -> candidate
  -> standby
  -> active somente com lease/fencing
```

Antes de drenar, o control plane deve confirmar que restarão pelo menos duas
edges `ready` ou que existe capacidade aprovada para manter o serviço. A
função `change_role` deve recusar a operação enquanto a edge estiver em
backends. A função `finalize_load_balancer_candidate` deve ser usada para
fechar a mudança de papel; não faça `UPDATE` direto.

Uma edge convertida para LB deixa de ser edge. Portanto, ela não pode aparecer
simultaneamente como backend, edge DNS e LB ativo.

## 7. O que o menu nunca pode fazer

```text
menu -> UPDATE state='active'
menu -> systemctl stop remoto como fencing
menu -> publicar .111/.237 no DNS de mídia
menu -> promover sem evidência de isolamento
menu -> aceitar senha/token no motivo ou log
menu -> converter edge em LB sem drain e capacidade
```

## 7.1 Testes obrigatórios da nova opção

Antes de liberar a opção no menu, criar testes offline para todos estes casos:

```text
sem autenticação                 -> recusado
frase de confirmação errada      -> recusado
isolation_reference vazio        -> recusado
origem diferente de .111         -> recusado
destino diferente de standby     -> recusado
outro active existente            -> recusado
lease expirada                    -> recusado
fencing token repetido            -> recusado
preflight inválido                -> recusado
DNS sem edge ready                -> recusado
fluxo completo                    -> evento auditado e resultado explícito
```

O teste de sucesso deve usar um banco temporário, mocks do provedor DNS e
probes locais. Nunca deve chamar Cloudflare real, mudar DNS público, reiniciar
HAProxy ou desligar uma VPS. O teste de promoção existente em
`tests/topology_model_test.py` deve continuar passando.

## 8. Auditoria mínima obrigatória

Cada tentativa deve registrar, sem segredos:

```yaml
event: manual_failover
operator: identidade
source_node: "1"
target_node: "4"
reason: texto sanitizado
isolation_reference: id externo
lease_id: id da lease
fencing_token: inteiro
from_state: standby/candidate/active
to_state: active/standby/failed
dns_result: succeeded/failed
lab_result: ok/failed
created_at: UTC
```

Se qualquer etapa falhar, o evento deve ficar como `failed` com erro
sanitizado. O menu deve mostrar o próximo passo seguro, nunca sugerir edição
manual do banco.

## 9. Critérios de pronto

- [ ] `.111` e `.237` possuem release e configuração compatíveis.
- [ ] `.237` permanece sem tráfego normal enquanto `standby`.
- [ ] O provedor fornece evidência de isolamento manual.
- [ ] A lease é exclusiva e o token é crescente.
- [ ] Nunca existem dois LBs `active`.
- [ ] `cdn.phpd77.com` publica apenas edges saudáveis.
- [ ] `.111` e `.237` não aparecem como A records de mídia.
- [ ] O laboratório passa live, filmes, séries, SNI e Range.
- [ ] O rollback foi ensaiado sem alterar o fluxo público atual.

## 10. Contrato de aceitação da implementação

A implementação do menu só está pronta quando todos os itens forem verdadeiros:

1. a opção aparece somente em ambiente com control plane autoritativo;
2. uma edge não consegue chamar a promoção diretamente;
3. a operação exige duas confirmações e referência de isolamento;
4. nenhum segredo é armazenado nos eventos;
5. a operação é idempotente: repetir a mesma solicitação não cria segundo active;
6. a promoção usa somente as funções transacionais do `TopologyStore`;
7. o DNS continua DNS-only e publica apenas edges `ready`;
8. uma falha no meio deixa o sistema em estado conhecido e auditado;
9. o rollback é possível sem editar SQLite à mão;
10. testes offline e laboratório aprovado estão arquivados.

## 11. Estado real desta receita

O checkout atual possui as invariantes de promoção, a preparação edge ->
`candidate`/`standby` e a ação de menu que executa este procedimento inteiro.
As VPS auditadas ainda executam menus das releases anteriores e, portanto,
não possuem essa ação em produção. Até a nova release ser publicada e
homologada, o operador deve usar o fluxo existente de solicitação/preparação
e não tentar promover por SQL, SSH remoto ou edição de DNS. A confirmação
humana continua sendo evidência operacional de isolamento; ela não é fencing
automático por si só.

Referências:

- `core/topology.py`
- `core/db.py`
- `core/dns_reconciler.py`
- `scripts/edge_health_controller.py`
- `scripts/process_promotion_request.py`
- `scripts/lb_candidate_preflight.py`
- `docs/RECIPE_CDN_10_OF_10_EXECUTION_2026-09-02.md`
- `docs/RECEITA_5_FRENTES_LB_10_DE_10_2026-09-02.md`
- `docs/RELEASE_AND_PROMOTION.md`
- `docs/POSTGRESQL_AND_FAILOVER_LAB_DECISION_2026-08-29.md`
