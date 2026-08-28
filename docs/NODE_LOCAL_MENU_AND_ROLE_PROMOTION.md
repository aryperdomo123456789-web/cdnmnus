# Menu local e promoção segura de nós

## Estado atual

Hoje `cli/mago_cdn.py` é instalado no plano de controle; as edges não possuem
necessariamente o mesmo menu. O banco (`core/db.py`) impede duplicidade de
`id`, nome e IPv4, mas não possui função de nó (`edge`/`load_balancer`). Também
não existe ainda uma role Ansible de Load Balancer nem uma operação real de
promoção.

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

O bootstrap deve instalar antecipadamente:

- `mago-cdn` e dependências do menu;
- agente de health e coleta de métricas;
- cliente do control plane;
- `edge_base`, limites, firewall e NTP;
- pacote de recuperação local;
- role edge pronta, mas sem publicação DNS até `ready`.

Também deve instalar o kit de promoção LB, porém mantê-lo bloqueado até o
control plane autorizar a operação.

## Sincronismo

O código e as releases são distribuídos por Ansible serial e assinados. O banco
de configuração deve ter uma fonte autoritativa; não sincronizar SQLite por
cópia entre VPS. Em indisponibilidade do control plane, o menu local pode

- mostrar estado em cache;
- executar diagnóstico;
- iniciar recuperação com snapshot e quorum;

mas não deve promover dois nós nem alterar DNS sem lease válida.

## Implementação necessária no repositório

1. Migração DB para `nodes`, `node_events` e `promotion_locks`.
2. Role `load_balancer` e playbooks de promoção/rebaixamento.
3. Instalação do CLI em todas as edges durante `edge_base`.
4. Detecção/arquivo de função do nó e cabeçalho do menu.
5. Operações `promote`, `demote`, `drain` e `stop` com API autoritativa.
6. Controlador de health e lease/quorum.
7. Backup/restore assinado e rollback.
8. Testes de duplicidade, split-brain, falha de LB e recuperação.

Até esses itens serem implementados, o menu deve exibir promoção como
“indisponível — role LB não instalada”, nunca simular uma transformação.
