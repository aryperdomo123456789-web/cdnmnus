# Registro de continuidade sem GPG e sem snapshot — 2026-08-29

> Atualização posterior: os nomes históricos `lb011` e `lb02` correspondem às
> edges operacionais `edge1` (`.168`) e `edge2` (`.170`). Os IDs técnicos são
> `2` e `3`; os Load Balancers são `.111` e `.237`.

## Decisão do operador

O operador decidiu continuar o plano sem assinatura GPG e sem snapshot de
volume da BlazeHosting. Esses dois controles deixam de bloquear desenvolvimento,
versionamento, preflight e homologação isolada.

Esta decisão **não elimina** os riscos nem transforma o backup local em snapshot:

- a autoria da tag não possui assinatura criptográfica;
- uma perda integral de `/dev/vda1` também elimina o backup local;
- o storage do control node ainda apresentou latência sustentada acima do SLO.

Por isso, o waiver autoriza seguir com GitHub, testes e operações somente leitura.
Ele não autoriza uma ativação cega em todas as edges.

## Proteções compensatórias implementadas

- branch `production/2026-08-28` publicada no GitHub;
- tag anotada `v0.4.8-production-candidate.3` publicada;
- commit da candidata `3fefe8bf5ae69ba2bc7773c618e420457f6a4f8d`;
- `release_id` `20260829012407-d60cfdbf`;
- `config_digest`
  `9e5457a1dd27609a573c0fd7cbcc80db1d84378da118d7e0dc70d70d7a534eb0`;
- lista fechada de sete artefatos com SHA-256 recalculado;
- bundle Git completo no pacote local de recuperação;
- `.gitignore` ampliado para bancos, ambientes, chaves, certificados, M3U e
  arquivos criptografados;
- workflow GitHub com permissão somente de leitura, concorrência controlada,
  busca de segredos, compilação Python, testes funcionais, smoke e validação
  sintática dos playbooks com `ansible-core==2.17.14`.

Backups, bancos, certificados, chaves SSH e playlists não foram enviados ao
GitHub.

## Validação Ansible fora do host persistente

O pacote Ubuntu 22.04 oferece `ansible-core 2.12`, que não reconhece
`ansible.builtin.systemd_service` usado pelos roles. Ele foi recusado para a
execução da candidata.

Foi criado um ambiente descartável em RAM com `ansible-core 2.17.14`. Com
`ANSIBLE_CONFIG=/opt/cdnmnus/ansible/ansible.cfg`, todos os playbooks passaram
em `--syntax-check`:

- `preflight-edge.yml`;
- `deploy-edge.yml`;
- `activate-edge.yml`;
- `deploy-and-activate-edge.yml`;
- `audit-edge-releases.yml`.

Nenhum pacote Ansible foi instalado permanentemente no control node nesta
etapa.

## Preflight somente leitura das edges

### `.168` / edge operacional `edge1` / ID técnico `2`

- fingerprint conhecida e `StrictHostKeyChecking=yes`;
- chave SSH local modo `0600`;
- Ubuntu 22.04, 2 vCPU e 3.940 MiB de RAM;
- Nginx ativo e `nginx -t` aprovado;
- raiz de 30 GiB, 24 GiB livres, 15% ocupada;
- release ativa `20260828205549-34caf01e`;
- `current.json` ainda ausente;
- banco do control plane ainda registra estado `bootstrapping`.

### `.170` / edge operacional `edge2` / ID técnico `3`

- fingerprint conhecida e `StrictHostKeyChecking=yes`;
- chave SSH local modo `0600`;
- Ubuntu 22.04, 2 vCPU e 3.940 MiB de RAM;
- Nginx ativo e `nginx -t` aprovado;
- raiz de 30 GiB, 24 GiB livres, 15% ocupada;
- mesma release ativa `20260828205549-34caf01e`;
- `current.json` ainda ausente;
- zero conexões TCP estabelecidas em 80/443 no instante da amostragem; isso
  não é prova de capacidade nem garantia de tráfego futuro;
- banco do control plane ainda registra estado `bootstrapping`.

Nenhum arquivo, serviço, pool, Nginx ou release das edges foi alterado pelo
preflight.

## Próximo gate seguro

Antes de ativar a candidata na `.168`:

1. instalar `ansible-core 2.17.14` no venv dedicado do control node, ou mover o
   worker para um control node saudável;
2. corrigir explicitamente os estados de inventário somente depois de confirmar
   o contrato de bootstrap;
3. identificar e testar o mecanismo real para retirar `.168` do pool;
4. medir capacidade de `.111/.170` sob carga representativa, pois uma amostra
   de conexões zero não prova capacidade;
5. executar backup externo criptografado e teste de restauração assim que o
   destino estiver disponível;
6. executar sincronização sem ativação, auditar os sete hashes na `.168` e só
   então autorizar o bloco de ativação/rollback.

## Evolução planejada: DNS e backup externos isolados

A automação futura fica subordinada a
`CLOUDFLARE_DNS_R2_PRODUCTION_RUNBOOK.md`: Conta Cloudflare A limitada a DNS-only
com `proxied=false`, Conta Cloudflare B limitada a um bucket R2 privado, backup
criptografado antes do upload, Bucket Lock e restore comprovado. Essa receita
não altera o waiver atual nem declara backup externo existente; implementação,
credenciais, upload e restore continuam pendentes até seus gates passarem.
