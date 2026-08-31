# Menu local e promoção segura de nós

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
Atualize esse arquivo sempre que o contrato de nó, menu ou promoção mudar.

## Estado atual

O plano de controle possui modelo topológico de nós, eventos, locks e fencing,
role Ansible de Load Balancer e menu local fino. O onboarding gerenciado passou
a instalar a identidade e o menu em toda nova edge somente depois de preflight,
release imutável, ativação e auditoria. A promoção real continua bloqueada até
existirem lease/quorum, backends saudáveis, certificados e confirmação
operacional.

Não se deve adicionar botões que apenas alterem um texto: sem instalar HAProxy/
Nginx LB, health controller e lock, a promoção seria falsa e poderia criar
split-brain.

## Comportamento alvo

Toda VPS recebe, durante o bootstrap, o mesmo pacote:

```text
/usr/local/bin/mago-cdn
/etc/cdnmnus/node-role.json
/etc/cdnmnus/node-id
/etc/cdnmnus/control-plane.conf
```

Ao abrir `TERM=xterm-256color mago-cdn`, o cabeçalho deve mostrar claramente:

```text
Nó: edge-168
Função: EDGE
Estado: READY
Control plane: conectado
```

ou:

```text
Nó: lb-primary
Função: LOAD BALANCER
Estado: ACTIVE
Backends saudáveis: 2/3
```

O menu local é um cliente fino. Ele consulta o control plane; não mantém uma
segunda verdade local nem edita Nginx diretamente.

## Operações do menu

### Promover esta edge para Load Balancer

Pré-condições obrigatórias:

- lock/quorum de promoção adquirido;
- nenhum outro LB ativo sem lease válida;
- edge em `ready` e sem deployment pendente;
- backup assinado disponível;
- certificados e chaves de token presentes;
- pelo menos um backend saudável além do nó promovido.

Fluxo:

```text
confirmar impacto -> drain da edge -> instalar role load_balancer
-> restaurar snapshot -> validar configuração -> iniciar LB
-> health dos backends -> registrar promoção
```

### Rebaixar Load Balancer para Edge

Somente se existir outro LB ativo ou rota de recuperação aprovada:

```text
promover/confirmar substituto -> retirar DNS/pool
-> drenar LB -> instalar role edge -> validar mídia
-> registrar rebaixamento
```

### Parar Edge ou Load Balancer

“Parar” nunca deve ser um `systemctl stop` imediato:

1. confirmar impacto e exigir motivo;
2. marcar `draining` no control plane;
3. aguardar conexões novas zerarem ou atingir timeout;
4. remover do pool;
5. parar somente os serviços da função;
6. manter SSH e recuperação disponíveis;
7. registrar evento e operador.

## Estado e prevenção de duplicatas

Adicionar ao banco:

```text
nodes(id, ipv4, role, state, lease_id, release_id, updated_at)
node_events(...)
promotion_locks(...)
```

Restrições obrigatórias:

- IPv4, nome e node ID únicos;
- no máximo um LB `active` por serviço/tenant;
- lease com expiração e renovação;
- promoção rejeitada se houver lock/quorum ausente;
- cadastro de edge duplicado retorna erro explícito e sugere o registro
  existente;
- um nó não pode ser edge e LB ativo simultaneamente.

## Instalação de uma nova edge

O fluxo oficial parte de **Mago CDN → Edges → Adicionar edge** no control plane.
O clone obtido do GitHub fornece o código versionado ao control plane; não se
executa `install.sh` diretamente na edge para transformá-la em edge gerenciada.

Após confirmar o fingerprint, a senha inicial serve apenas para criar o usuário
`cdn-deploy` e instalar uma chave exclusiva. A máquina permanece
`bootstrapping`. O worker então executa, serialmente e sem publicação DNS:

```text
preflight -> release imutável/digest -> edge_base -> broker/relay/Nginx
-> health público e privado -> auditoria integral -> identidade/menu -> ready
```

O preflight recusa menos de 2 vCPU, aproximadamente 4 GiB, reserva de disco
inferior a 20%, relógio sem NTP, origem inacessível ou LB upstream inacessível.
A auditoria recusa hash divergente, symlink/estado divergente, broker parado,
relay VOD parado, socket VOD sem health ou hostname sem health `200`.

O bootstrap instala antecipadamente:

- `mago-cdn` e dependências do menu;
- agente de health e coleta de métricas;
- cliente do control plane;
- `edge_base`, limites, firewall e NTP;
- pacote de recuperação local;
- role edge pronta, mas sem publicação DNS até `ready`.

Também grava a capacidade `load_balancer_candidate` e instala os pré-requisitos
comuns. HAProxy não é iniciado numa edge: a role central de LB permanece
bloqueada até o control plane autorizar drain, mudança de papel e promoção.

Cadastro e mudança de estado são transacionais nos modelos legado e topológico.
Falha em qualquer gate deixa a máquina fora de `ready` e, portanto, fora da
matriz DNS. A transição bem-sucedida registra a mesma release e digest nos dois
modelos com eventos auditáveis.

## Sincronismo

O código e as releases são distribuídos por Ansible serial e assinados. O banco
de configuração deve ter uma fonte autoritativa; não sincronizar SQLite por
cópia entre VPS. Em indisponibilidade do control plane, o menu local pode

- mostrar estado em cache;
- executar diagnóstico;
- iniciar recuperação com snapshot e quorum;

mas não deve promover dois nós nem alterar DNS sem lease válida.

## Implementação e gates remanescentes

1. [x] Migração DB para `nodes`, `node_events` e `promotion_locks`.
2. [x] Role `load_balancer` com preflight, deploy, drain, promoção e rollback.
3. [x] Instalação do menu/identidade em toda edge após auditoria.
4. [x] Cadastro e estados legado/topológico na mesma transação.
5. [x] Release, VOD relay, rollback e auditoria fail-closed.
6. [ ] Controlador externo de lease/quorum e fencing de produção.
7. [ ] Pacote de recuperação assinado e restore completo comprovado.
8. [ ] Promoção/rebaixamento real pelo menu após os gates de produção.

Enquanto os três itens abertos não forem comprovados, o menu deve manter a
promoção indisponível; possuir capacidade de candidato não simula mudança de
papel nem inicia HAProxy.
