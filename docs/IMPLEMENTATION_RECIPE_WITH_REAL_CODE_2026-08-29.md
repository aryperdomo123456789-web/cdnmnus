# Receita de implementação com encaixe no código real

**Estado real de referência:** [STATE_REAL_2026-08-29.md](STATE_REAL_2026-08-29.md)
**Mapa funcional do repositório:** [REPO_MAP_AND_STATE_2026-08-29.md](REPO_MAP_AND_STATE_2026-08-29.md)

Data-base: 2026-08-29

Este documento existe para uma finalidade muito prática: permitir que alguém
implemente o projeto seguindo o código atual sem improviso. A regra aqui é
simples:

- cada etapa precisa apontar para o arquivo real que executa a função;
- cada exemplo precisa bater com a estrutura atual do repositório;
- cada passo precisa dizer o que ler, o que editar e o que validar.

## 1. Como pensar o projeto antes de mexer nele

O projeto não é “um único painel”. Hoje ele tem cinco camadas que precisam
encaixar:

1. instalador e hardening do host;
2. painel/control plane administrativo;
3. contratos locais do nó e menu comum;
4. topologia autoritativa de nós e LBs;
5. laboratório de playback para mídia.

Se alguém começar pelo lugar errado, acaba mexendo em produção sem querer.

## 2. A forma mais segura de implementar qualquer mudança

Use sempre esta ordem:

1. descobrir qual arquivo é a fonte da regra;
2. descobrir quem lê esse arquivo;
3. descobrir quem grava esse arquivo;
4. descobrir quais testes cobrem esse caminho;
5. executar a alteração mínima;
6. validar localmente;
7. atualizar `STATE_REAL_2026-08-29.md`.

Essa ordem evita o erro comum de “corrigir o painel” e quebrar topologia,
Ansible ou o laboratório de mídia sem perceber.

## 3. Receita real do contrato do nó

### 3.1 O que o Ansible escreve

Arquivo:

- `ansible/roles/node_menu/tasks/main.yml`

O role grava três contratos locais:

- `/etc/cdnmnus/node-id`
- `/etc/cdnmnus/node-role.json`
- `/etc/cdnmnus/control-plane.conf`

Na prática, a forma atual é esta:

- `node-id` guarda só o ID numérico com uma quebra de linha;
- `node-role.json` guarda schema, identidade, papel, estado, control plane,
  release e digest;
- `control-plane.conf` guarda `KEY=VALUE` para host, porta, esquema, verify e
  modo de bootstrap.

### 3.2 Exemplo de encaixe real

Se você quiser criar um nó `edge` novo, a linha de intenção é essa:

```yaml
- name: Instalar função e estado local informativo
  copy:
    content: "{{ {...} | to_nice_json }}\n"
    dest: /etc/cdnmnus/node-role.json
```

O conteúdo exato já vem do inventário/vars do Ansible. O importante é o
contrato, não a decoração.

### 3.3 O que o cliente local lê

Arquivo:

- `ansible/roles/node_menu/files/node_menu.py`

O menu local:

- lê `node-role.json`;
- lê `node-id`;
- lê `control-plane.conf`;
- valida que os três contratos existem e batem entre si;
- não cria banco paralelo;
- não promove nó por conta própria.

Em termos operacionais, ele é read-only local e orientado ao control plane.

### 3.4 Fluxo que uma criança conseguiria seguir

1. copiar os três arquivos locais;
2. abrir o menu com `mago-cdn`;
3. ver o papel atual do nó;
4. conferir se o control plane responde;
5. não tentar editar a lógica local na mão;
6. pedir a mudança administrativa ao control plane.

## 4. Receita real da topologia autoritativa

Arquivo:

- `core/topology.py`

Aqui está o coração da promoção e do fencing.

### 4.1 O que existe hoje

O modelo cria estas tabelas:

- `nodes`
- `load_balancers`
- `lb_backends`
- `promotion_locks`
- `node_events`

### 4.2 O que isso significa em português claro

- `nodes` é a identidade autoritativa dos nós;
- `load_balancers` diz quais nós podem operar como LB;
- `lb_backends` diz quais edges servem de backend para cada LB;
- `promotion_locks` garante que a promoção só ocorre com lease válida;
- `node_events` guarda auditoria sanitizada.

### 4.3 Exemplo real de como a promoção funciona

O fluxo atual de promoção é conceitualmente este:

1. adquirir lock;
2. verificar lease;
3. verificar `fencing_token`;
4. garantir que não existe outro LB ativo;
5. ativar o LB escolhido;
6. registrar evento.

O código de promoção hoje segue esse contrato dentro de
`TopologyStore.promote_load_balancer(...)`.

### 4.4 Exemplo de uso em teste

O teste `tests/topology_model_test.py` mostra o fluxo completo:

- cria nós de laboratório;
- tenta promover sem lease e falha;
- adquire lock;
- promove com token válido;
- tenta criar segundo `active` e falha;
- faz demote;
- promove o próximo.

Esse teste é o melhor guia para alguém implementar um novo comportamento sem
quebrar split-brain.

## 5. Receita real do painel administrativo

Arquivo:

- `web/app.py`

Esse é o painel HTTP do control plane.

### 5.1 O que ele faz de fato

- expõe a interface web;
- autentica via Basic Auth;
- exige CSRF;
- lê o banco SQLite do control plane;
- cadastra edges, tenants, CNAMEs e fontes VOD;
- agenda deploy;
- recalcula DNS;
- testa health de edge;
- atualiza a porta do painel.

### 5.2 Exemplo de fluxo real

Na UI atual, quando o operador cadastra uma edge:

1. clica em “Ler fingerprint”;
2. o painel chama a rota de scan SSH;
3. valida a host key;
4. registra a edge;
5. deixa a edge em estado inicial;
6. o deploy/health depois ajusta o estado.

### 5.3 O que não fazer

- não colocar credencial em query string;
- não expor origem em resposta pública;
- não tratar o painel como verdade paralela de topologia;
- não contornar o control plane para mudar estado à mão.

## 6. Receita real do laboratório de mídia

Diretório:

- `lab-player/`

Scripts principais:

- `lab-player/scripts/sync_playlist.sh`
- `lab-player/scripts/test_playback_flow.py`

### 6.1 O que o sincronizador faz

O `sync_playlist.sh`:

- baixa a playlist via CDN;
- baixa a playlist via IP direto;
- guarda as cópias em `lab-player/playlists/`;
- cria symlinks `*_latest.m3u8`;
- escreve um relatório de sync.

### 6.2 O que o validador faz

O `test_playback_flow.py`:

- refresca as playlists antes de rodar;
- fixa amostras em `lab-player/reports/samples.json`;
- seleciona amostras de live, movie e series;
- compara CDN e direto;
- valida `200` e `206`;
- registra relatório local.

### 6.3 Exemplo de uso real

```bash
LAB_DIR=/opt/cdnmnus/lab-player \
PLAYER_USERNAME='...'
PLAYER_PASSWORD='...'
PLAYER_BASE_CDN='https://cdn.exemplo.com'
PLAYER_BASE_DIRECT='http://1.2.3.4:80' \
/opt/cdnmnus/lab-player/scripts/test_playback_flow.py --both
```

Isso representa o caminho prático:

- baixar;
- fixar;
- testar;
- registrar.

## 7. Receita real para implementar uma mudança sem quebrar o sistema

Suponha que você queira adicionar um novo contrato ou alterar o menu.

### Etapa 1 — localizar a fonte

Pergunte:

- isso é contrato de nó?
- isso é topologia?
- isso é painel?
- isso é laboratório?

### Etapa 2 — localizar o teste

Procure primeiro em:

- `tests/topology_model_test.py`
- `tests/panel_http_test.py`
- `tests/vod_player_compatibility_test.py`

### Etapa 3 — alterar só o necessário

Exemplo:

- se o contrato local mudou, edite `ansible/roles/node_menu/tasks/main.yml`;
- se a leitura local mudou, edite `ansible/roles/node_menu/files/node_menu.py`;
- se a promoção mudou, edite `core/topology.py`;
- se o playback mudou, edite `lab-player/scripts/test_playback_flow.py`.

### Etapa 4 — validar na ordem certa

```bash
python3 -m unittest discover -s tests -p '*test.py' -v
python3 -m py_compile web/app.py panel/*.py core/*.py cli/*.py
python3 /opt/cdnmnus/lab-player/scripts/test_playback_flow.py --both
```

### Etapa 5 — atualizar a documentação real

Depois da validação, atualize:

1. `STATE_REAL_2026-08-29.md`
2. `REPO_MAP_AND_STATE_2026-08-29.md`
3. o documento específico da frente alterada

## 8. Exemplos reais de encaixe entre arquivos

### Exemplo A: um novo edge

1. Ansible grava `node-id`, `node-role.json` e `control-plane.conf`.
2. `mago-cdn` lê esses contratos.
3. `web/app.py` registra a edge no SQLite.
4. `core/deploy.py` inclui a edge em deploy quando ela estiver pronta.
5. Os testes validam que não há vazamento de estado entre execuções.

### Exemplo B: promoção edge -> LB

1. `core/topology.py` cria lock.
2. o lock gera `lease_id` e `fencing_token`.
3. a promoção só ocorre com lease válida.
4. o LB passa para `active`.
5. qualquer segundo `active` é recusado.

### Exemplo C: validação de mídia

1. `sync_playlist.sh` baixa as playlists.
2. `test_playback_flow.py` fixa as amostras.
3. os testes consultam CDN e IP direto.
4. o relatório mostra `200/206`, `Content-Type` e `Content-Range`.
5. o ambiente passa a ter evidência real e repetível.

## 9. Receita mínima para uma pessoa iniciante

Se alguém for começar do zero, eu diria:

1. leia `STATE_REAL_2026-08-29.md`;
2. leia `REPO_MAP_AND_STATE_2026-08-29.md`;
3. abra `tests/topology_model_test.py`;
4. abra `ansible/roles/node_menu/tasks/main.yml`;
5. abra `lab-player/scripts/test_playback_flow.py`;
6. só então mexa no código;
7. depois rode os testes;
8. depois atualize a documentação.

Isso é o caminho mais curto entre “entender” e “não quebrar nada”.

## 10. O que este documento não tenta fazer

Ele não tenta esconder complexidade nem fingir que tudo já está pronto em
produção.

Ele tenta fazer algo mais útil:

- mostrar o encaixe real entre os arquivos;
- dar uma ordem de implementação que funciona;
- reduzir drasticamente a chance de improviso.

## 11. Receita do instalador inteligente de promoção

Esta é a parte que resolve o cenário do print sem embolar a topologia.
O instalador externo deve agir como um intérprete do estado real do cluster,
não como um script que decide sozinho sem contexto.

### 11.1 Regra principal

O instalador nunca deve assumir que o novo edge precisa virar LB ativo.
Ele primeiro lê o cluster e só então decide entre:

- `edge`
- `load_balancer candidate`
- `load_balancer standby`
- `load_balancer active`

### 11.2 Árvore de decisão simples

1. ler o estado local do nó;
2. consultar o control plane;
3. verificar se já existe LB ativo;
4. verificar se já existe LB standby;
5. verificar se há edges saudáveis;
6. decidir o papel apropriado;
7. aplicar lock/fencing;
8. registrar o resultado;
9. só então concluir.

### 11.3 Regras exatas de comportamento

Se já existir outro load balancer ativo ou standby:

- o edge novo não tenta assumir o topo;
- ele vira `load_balancer standby` ou `candidate`;
- o operador decide a ativação depois.

Se não existir nenhum load balancer:

- o edge pode assumir `load_balancer candidate`;
- ele passa por preflight;
- se passar, pode virar o primeiro LB principal.

Se existirem outros edges, mas nenhum LB:

- o instalador escolhe um edge apto;
- não altera os outros edges além do necessário;
- evita embolar papéis;
- evita split-brain.

Se o estado estiver ambíguo:

- o instalador para;
- não promove;
- não escreve estado parcial;
- registra erro para o operador.

### 11.4 Fluxo operacional harmonizado

O fluxo recomendado fica assim:

1. operador entra por SSH no edge;
2. abre `mago-cdn`;
3. escolhe “Promover para LB”;
4. o menu chama o instalador externo versionado no GitHub;
5. o instalador lê o cluster real;
6. o instalador decide o papel correto;
7. o instalador prepara o nó;
8. o instalador valida preflight;
9. o instalador usa lock/fencing;
10. o instalador finaliza como `standby` ou `active`;
11. o operador só depois aponta Cloudflare, DNS ou R2 se for o caso.

### 11.5 O que torna esse fluxo “bonito” e seguro

- o instalador não inventa topologia;
- o instalador não pula etapas;
- o instalador não muda todo o cluster de uma vez;
- o instalador respeita o papel pré-existente;
- o instalador evita que dois LBs ativos apareçam por engano;
- o instalador sempre falha fechado quando não entende o estado.

### 11.6 Pseudo-roteiro que uma criança consegue seguir

1. ver se já existe LB;
2. se existir, deixar o novo nó como standby/candidate;
3. se não existir, preparar o novo nó como candidato;
4. checar se o nó está saudável;
5. travar a promoção com lock;
6. virar LB só se estiver tudo certo;
7. registrar o evento;
8. atualizar o estado real.

### 11.7 Como isso se conecta ao desenho do print

O print mostra o formato desejado:

- dois LBs no topo;
- um ativo e um standby;
- edges abaixo;
- XUIs/VOD atrás da camada de LB.

O instalador inteligente existe exatamente para manter esse desenho coerente
quando um novo nó entra ou quando um LB cai.

### 11.8 Implementação concreta

O plano agora está materializado em:

- `install-managed-node-from-github.sh`: clone de tag e verificação do commit;
- `node-package/install.sh` e `verify.py`: manifesto fechado, preflight,
  instalação e rollback;
- `scripts/submit_promotion_request.py`: receptor autenticado no control plane;
- `scripts/process_promotion_request.py`: drain e preparação candidate/standby;
- `ansible/roles/load_balancer`: backup, candidato HAProxy e rollback;
- `core/db.py`: fila auditável e fechamento transacional edge -> LB;
- `ansible/roles/node_menu/files/node_menu.py`: solicitação, nunca promoção local.

O fluxo `active` permanece separado e exige lease/fencing. Cloudflare e DNS
continuam posteriores à homologação do LB.
