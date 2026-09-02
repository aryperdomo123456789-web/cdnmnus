# Estado real do projeto

Data-base: 2026-09-01

Este documento é a fotografia operacional do projeto. Ele deve ser atualizado
sempre que houver mudança real no código, nos testes, no laboratório ou na
produção observada.

Regra de ouro:

- se mudou algo no projeto, atualize este documento;
- se este documento divergir de outro arquivo, este documento é a primeira
  referência para conferir a data e o estado;
- qualquer documento operacional deve começar com um cabeçalho de rastreio que
  aponte para este arquivo.

## 1. Situação do código

- O menu local foi unificado em `mago-cdn`.
- O contrato do nó usa:
  - `/etc/cdnmnus/node-id`
  - `/etc/cdnmnus/node-role.json`
  - `/etc/cdnmnus/control-plane.conf`
- O cliente local do menu lê esses contratos em modo read-only e não cria uma
  fonte de verdade paralela.
- O instalador e o hardening UFW agora seguem política por perfil no código:
  - `edge` publica `22/80/443` e mantém as demais entradas negadas por padrão;
  - `load_balancer` publica `80/443` e mantém `22` público;
  - o instalador aplica o firewall no final do fluxo, após o bootstrap do
    runtime.
- Estado real conferido e corrigido em 2026-08-31 nos hosts atuais:
  - `143.14.168.111`, `.168` e `.170` estão com UFW ativo e habilitado no boot;
  - os três publicam somente o baseline `22/tcp`, `80/tcp` e `443/tcp`; a `.111`
    preserva a regra adicional `1455/tcp`, embora nenhum processo estivesse escutando nessa porta
    durante a validação;
  - na `.111`, a regra explícita e redundante de negação da `3000/tcp` foi
    removida; `3000` e qualquer outra porta sem exceção continuam bloqueadas
    pelo default de entrada `deny`;
  - regras SSH antigas por origem foram removidas após validar a porta 22
    pública e a autenticação da malha;
  - `8080`, `3000` e demais portas sem exceção ficam bloqueadas externamente.
- A malha SSH foi convergida e validada em todas as seis direções entre `.111`,
  `.168` e `.170` usando `cdn-deploy`, BatchMode e host key estrita:
  - cada nó possui identidade Ed25519 privada própria;
  - somente chaves públicas são distribuídas;
  - entradas externas em `authorized_keys` são preservadas fora do bloco
    gerenciado;
  - `known_hosts` é convergido para não exigir confirmação interativa;
  - o painel pode acionar somente o oneshot fixo
    `cdnmnus-ssh-mesh.service`, sem receber sudo genérico;
  - o cadastro de uma edge nova chama a convergência; falha de integração muda
    a edge para `failed`, em vez de declarar sucesso parcial.
- `PasswordAuthentication yes` e `PermitRootLogin yes` continuam ativos nos
  três hosts para não romper acessos administrativos legados. A malha CDNMNUS
  usa chave, mas o endurecimento para key-only permanece uma mudança separada.
- Em 2026-08-31 a `.111` recebeu shutdown ordenado iniciado pelo hypervisor via
  QEMU guest agent. Não houve evidência de OOM ou evento térmico no guest. Após
  o boot, o UFW foi encontrado inativo e teve persistência corrigida nos três
  nós.
- A topologia autoritativa já possui:
  - `nodes`
  - `load_balancers`
  - `lb_backends`
  - `promotion_locks`
  - `node_events`
- O modelo de topologia já impõe:
  - um único `load_balancer` ativo por vez;
  - monotonicidade estrita de `fencing_token`;
  - backend apenas com role `edge`;
  - bloqueio de promoção sem lease válido;
  - eventos sanitizados para auditoria.
- A suíte de testes foi endurecida contra vazamento de estado global.
- A última execução de `python3 -m unittest discover -s tests -p '*test.py' -v`
  executou 38 testes e todos passaram. `tests/admin_web_test.py` agora isola o
  reconciliador Cloudflare e não depende de token/zona reais do host.
- Operações de promoção, onboarding e menu exigem control-plane explícito por
  `CDNMNUS_CONTROL_PLANE`, `control-plane.conf` ou `node-role.json`; o fallback
  histórico para `.111` permanece somente para leitura compatível.
- A role Ansible `node_menu` não possui mais default de control-plane: sem valor
  explícito ela falha antes de gravar a identidade. O inventário de produção
  continua declarando o valor de forma explícita; isso evita que o `.111` seja
  herdado silenciosamente por um novo LB.
- O comando read-only `scripts/cdnmnus-lb-candidate-preflight --node
  45.140.192.237` foi implementado. Ele usa SSH estrito e `sudo -n` apenas para
  leitura, nunca chama promoção, reload/restart, escrita SQLite ou API de DNS.
- A execução read-only v2 em 2026-09-01 consultou Cloudflare sem mutação e não
  encontrou o `.237` em seus registros; confirmou também `traffic_enabled=false`
  por systemd, processo e listener. O HAProxy e o Nginx passaram na sintaxe.
  As duas edges retornaram HTTP `421` ao probe `curl --resolve` com SNI
  `cdn.phpd77.com`, indicando rejeição do hostname no vhost, não falha de TCP.
  A release instalada no `.237` é `v0.5.0-managed-node.9`, enquanto o registry
  aprova `.8`; o candidato segue bloqueado, sem capacidade, PEM referenciado ou
  fencing externo.
- O diagnóstico adicional confirmou a causa do health: as edges apresentam
  certificado `CN/SAN=cdn.phpd77.com`, mas o tenant habilitado é
  `tvbrasil.phpd77.com` com `tls_status=pending`. O vhost default rejeita
  `cdn.phpd77.com` explicitamente com `421`; portanto o renderer agora deriva
  `health_host` do canonical do tenant e não remove essa proteção. O próximo
  write permitido é a emissão/distribuição de certificado correto por pipeline,
  ainda sem promoção do `.237`.
- `pytest` ainda não está disponível neste ambiente, então a verificação foi
  feita com `unittest` e compilação sintática.
- O laboratório `lab-player/` existe e executa:
  - download de playlists;
  - fixação de amostras;
  - comparação CDN vs IP direto;
  - relatório local;
  - validação de `HTTP 200` e `HTTP 206`.
- O laboratório também persiste as amostras fixas em
  `lab-player/reports/samples.json` para repetição controlada.
- Em 2026-08-31 foi corrigida a comparação do laboratório: a rota `direct`
  agora substitui de fato o endpoint da amostra pelo XUI, preservando path e
  query, em vez de repetir a URL da CDN sob outro rótulo.
- O repositório agora possui um mapa funcional explícito em
  `docs/REPO_MAP_AND_STATE_2026-08-29.md`.

## 2. Topologia operacional atual

Estado informado e adotado como fonte de verdade para a próxima janela:

| Nó | Papel lógico | Estado | Condição operacional |
| --- | --- | --- | --- |
| `45.140.192.237` | `load_balancer` | `standby` | edge table `disabled`; release antiga de laboratório; sem capacidade, health, lease ou fencing ativo |
| `143.14.168.111` | `load_balancer` | `candidate` | ainda não é `active`; não usar como edge publicada nem promover sem aprovação |
| `143.14.168.168` | `edge` | `ready` | release atual validada, sujeita aos gates prolongados |
| `143.14.168.170` | `edge` | `ready` | release atual validada, sujeita aos gates prolongados |

Objetivo aprovado para a próxima topologia: `.237` como único LB
`PRIMARY/ACTIVE` e `.111` como LB `STANDBY`, com `.168/.170` como edges.
Antes de executar isso, o control-plane precisa ter identidade própria e não
depender implicitamente de `.111`; o modelo de papel é exclusivo por nó.

Não existe lease/fencing ativo, health recente ou capacidade declarada para o
`.237`. Portanto ele é candidato operacional, não LB de produção.

### Registro efetivo no banco autoritativo

A conferência do banco local em 2026-09-01 encontrou o registro já existente,
sem necessidade de novo `INSERT`:

| Entidade | Registro real |
| --- | --- |
| Nó LB candidato | `nodes.id=4`, IPv4 `45.140.192.237`, papel `load_balancer`, estado `standby` |
| LB | `load_balancers.id=lb-4`, `node_id=4`, modo `active_standby`, estado `standby` |
| Backend 1 | `lb_backends.lb-4 -> nodes.id=2`, IPv4 `143.14.168.168`, `enabled` |
| Backend 2 | `lb_backends.lb-4 -> nodes.id=3`, IPv4 `143.14.168.170`, `enabled` |
| Proteção | zero LB `active`, zero `promotion_locks` |

O modelo usa IDs lógicos (`4`, `2`, `3`) e guarda o IPv4 em `nodes.ipv4`; não
aceita o IPv4 como `node_id`. Além disso, os backends reais são
`143.14.168.168` e `143.14.168.170`. Os endereços `45.140.192.168` e
`45.140.192.170` não aparecem na topologia autoritativa e não devem ser
cadastrados por suposição.

O inventário agora declara `lb_candidate_237` no grupo `load_balancers`, com
`cdnmnus_node_state: standby`, control-plane explícito `.111` e sem qualquer
ação de promoção. Antes da alteração, o playbook de LB não encontrava alvo
porque esse grupo não existia; a declaração de inventário, por si só, não
inicia HAProxy nem publica tráfego.

## 3. Situação validada recentemente

- Regras UFW e persistência de boot conferidas nas máquinas `.111`, `.168` e
  `.170`; todas estão ativas.
- Regressão de HTTP diagnosticada em 2026-08-31: o role de load balancer
  removia a mesma porta 80 em que seu frontend escuta. O contrato agora mantém
  `80/443` públicos em edge e load balancer e possui teste contra recorrência.
- SSH de teste confirmado a partir do `111` para `168` e `170` após o
  endurecimento das regras.
- SSH sem senha confirmado em todas as direções `.111 <-> .168`,
  `.111 <-> .170` e `.168 <-> .170`.
- Playlist real baixada e armazenada com permissão restrita.
- Amostras reais extraídas com foco em UFC, LGBT, filmes e séries.
- Validação de reprodução executada com sucesso em CDN e IP direto.
- `Range` validado em VOD.
- A divergência VOD foi diagnosticada e corrigida em 2026-08-31. A release
  antiga `20260829154052-a11e5eed` encaminhava `/movie` e `/series` pelo broker
  legado e perdia escapes percentuais de caminhos assinados no segundo parse
  do Nginx, produzindo `403 Assinatura inválida` em `.168/.170`.
- `.168` e `.170` executam agora a release imutável
  `20260829012407-d60cfdbf`, digest
  `9e5457a1dd27609a573c0fd7cbcc80db1d84378da118d7e0dc70d70d7a534eb0`,
  com `/movie` e `/series` no relay privado por socket Unix. Nas duas edges,
  live retornou `200`; filme e série retornaram `206` em Range inicial, seek
  intermediário e suffix; `HEAD` retornou `200`; e 16 seeks concorrentes de
  4 KiB retornaram `206` com o tamanho exato.
- O rollback real foi executado na `.168`: restaurou symlink, unit antiga,
  ausência do relay e `current.json`, manteve cinco health consecutivos e
  reproduziu o `403` antigo; a candidata foi reativada e voltou a `206`.
  O playbook agora persiste contrato, units e estado anterior em
  `/var/lib/cdnmnus-edge/activation-history/<release_id>/` e recupera a
  candidata se o rollback falhar.
- `.111` não foi alterada e continuou retornando VOD Range `206`. Os três
  hosts retornaram health `200` e TLS válido após a convergência.
- Os registros legado e topológico de `.168/.170` foram movidos de
  `bootstrapping` para `ready` por transições auditadas, com release e digest
  registrados. Backups SQLite consistentes, com `integrity_check=ok` e zero
  violações de foreign key, precederam as mudanças de estado.
- O inventário versionado foi alinhado para `ready`, e
  `audit-edge-releases.yml` aprovou nas duas edges symlink, `current.json`,
  sete hashes, digest e health derivados do snapshot ativo.
- Relatório salvo em `lab-player/reports/`.
- O fluxo de sincronização baixa novamente a playlist antes de cada execução
  do validador, para evitar drift entre capturas.

## 4. O que está pronto

- contrato de nó;
- menu unificado;
- modelo de topologia;
- menu local read-only com orientação ao Control Plane;
- laboratório de playback;
- documentação de leitura e execução;
- mapa funcional do repositório;
- 38 de 38 testes agregados na última execução.
- relay VOD convergente em `.168/.170`, com rollback real aprovado na canária.
- onboarding futuro fail-closed: novas edges entram em `bootstrapping`, passam
  por preflight, release imutável, ativação, auditoria de broker/relay/health e
  só então recebem menu/identidade e estado `ready` nos modelos legado e
  topológico. Release e digest são registrados por eventos auditados.
- baseline para futura promoção a LB: 2 vCPU, aproximadamente 4 GiB, NTP,
  reserva de disco de 20%, `socat`, identidade comum e capacidade
  `load_balancer_candidate`; HAProxy permanece bloqueado até promoção real.
- pacote universal GitHub separado do instalador legado, com tag+commit+digest,
  manifesto fechado, backup/rollback e HAProxy desativado;
- qualquer menu de nó admitido pode enviar ao control plane um cadastro de nova
  VPS como edge ou LB. Senha segue somente por stdin SSH e não é persistida;
  a host key usa TOFU automatizado com duas capturas idênticas e pinning
  posterior. Edge recebe deployment exclusivo por `target_edge_id`; LB direto
  nasce `candidate`, nunca `active`;
- menu da edge registra solicitação de promoção no control plane e o processador
  só prepara `candidate`/`standby`. A homologação remota integral permanece
  pendente na VPS descartável.

## 5. O que ainda é gate de produção

- `.111/.170` sob carga representativa e soak prolongado;
- ensaio de reinserção do RRset Cloudflare;
- player real fixado individualmente em `.168` e `.170`, arquivo superior a
  três horas e soak mínimo de seis horas;
- PostgreSQL/failover real;
- promoção edge -> LB em produção;
- `.237` ACTIVE.
- `.111` STANDBY.
- certificado cobrindo todos os hosts publicados;
- transformação efetiva para `/play/<token>/m3u8`;
- controlador contínuo de capacidade/health;
- fencing externo e failover real.
- qualquer mudança de write em Cloudflare ou R2 sem contrato separado e sem
  restore comprovado.

## 6. Pontos de atenção do código real

- `core/topology.py` já tem compatibilidade com a assinatura antiga e a nova
  dos métodos de lock/promoção; isso é útil para migração, mas deve ser mantido
  documentado para não virar ambiguidade operacional.
- `web/app.py` é o painel HTTP administrativo principal; ele faz a orquestração
  local de edges, tenants, CNAMEs, VOD e deploy.
- A política de UFW foi amarrada ao fluxo de ativação e ao instalador para
  preservar `22/80/443` públicos em todos os nós. A exceção `1455/tcp` é específica
  da `.111` e não faz parte do baseline genérico.
- O `.111` é o control plane atual e o LB ACTIVE planejado; o `.237` é o LB
  remoto standby. Release, capacidade, health, lease e fencing ainda precisam
  ser comprovados antes da publicação. O `.111` continua `candidate` até ser
  preparado como LB ACTIVE.
- `panel/` ainda existe como superfície de broker/relay e precisa ser entendido
  como contrato de runtime, não como fonte única de verdade para operação.
- O relay Python passou a validação funcional real e carga curta, mas os gates
  de capacidade prolongada, backpressure e soak continuam abertos; `ready`
  não autoriza por si só a promoção do `.237` nem mudança de DNS.
- O laboratório `lab-player/` é agora parte da receita operacional e não deve
  ser tratado como artefato descartável.
- `docs/REPO_MAP_AND_STATE_2026-08-29.md` é a melhor visão de alto nível para
  distinguir produção, laboratório, contrato e legado.

## 7. Forma obrigatória de usar este arquivo

Antes de abrir outro documento operacional, confira se este arquivo está
atualizado.

Antes de encerrar uma mudança, atualize:

1. este arquivo;
2. o documento específico da frente afetada;
3. o índice documental, se a navegação mudar.
