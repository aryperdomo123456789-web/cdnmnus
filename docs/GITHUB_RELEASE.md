# cdnmnus: release GitHub e instalacao em Ubuntu

## Objetivo

Este documento define o procedimento oficial para manter o projeto no GitHub e instalar a CDN em servidores Ubuntu 20.04, 22.04, 24.04 e 26.04 quando a versao estiver disponivel no provedor.

O projeto usa Bash, Python 3, Nginx, systemd e UFW opcional. O painel nao deve ser publicado; ele fica em `127.0.0.1:9090`.

## Chave exclusiva do repositorio

A chave local de publicacao fica em:

```text
/opt/cdnmnus/.github-deploy/id_ed25519
```

A chave privada e root-only e o diretorio esta no `.gitignore`. Nunca cole a chave privada no GitHub, chat, issue, terminal gravado ou arquivo de configuracao.

Chave publica para cadastrar no GitHub:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP0Z50bZAvDwL3lq4TBaQH4BbdeJ5JehgkeiujlWgpnu cdnmnus-github-deploy-2026
```

Fingerprint esperado:

```text
SHA256:kU1tFH1uvv/9JYTOHZ05sN7OZCBIR2j4+Z0W5fkEzBg
```

No GitHub: Settings > Deploy keys > Add deploy key. Use um titulo identificavel e habilite escrita somente se o servidor realmente precisar fazer push. Para instalacao em outros servidores, uma chave somente-leitura e preferivel.

Teste local da chave sem exibir segredo:

```bash
sudo ssh-keygen -lf /opt/cdnmnus/.github-deploy/id_ed25519.pub -E sha256
sudo git -C /opt/cdnmnus status --short
```

## Regras de release

1. Trabalhe em branch de feature.
2. Execute os testes locais.
3. Confirme que nao ha credenciais, playlists, chaves privadas ou hosts sensiveis em diffs.
4. Faca commit assinado ou revisado.
5. Faca push para o GitHub.
6. Publique uma tag quando houver release.
7. Atualize primeiro uma VPS de homologacao.
8. So depois atualize producao.

Comandos de validacao antes do push:

```bash
cd /opt/cdnmnus
python3 -m py_compile panel/panel.py
bash -n install.sh install-from-github.sh scripts/*.sh tests/*.sh
./tests/smoke.sh
! grep -RInE 'password=|BEGIN OPENSSH PRIVATE KEY|BEGIN RSA PRIVATE KEY' . --exclude-dir=.git --exclude='*.md'
```

## Instalacao standalone/legada em VPS nova

Este procedimento instala o proxy genérico de uma máquina isolada. Ele **não é
o onboarding oficial de uma edge da rede multi-edge** e não deve marcar uma
máquina como `ready`, adicioná-la ao DNS ou substituir o deployment imutável do
control plane.

Pre-requisitos: Ubuntu 20.04+ suportado, root, DNS apontando para a VPS, Git, certificados CA e acesso SSH confirmado.

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
sudo mkdir -p /opt
sudo git clone --branch main --single-branch https://github.com/aryperdomo123456789-web/cdnmnus.git /opt/cdnmnus
cd /opt/cdnmnus
sudo chmod +x install.sh install-from-github.sh scripts/*.sh tests/*.sh
sudo ./install.sh --dry-run --yes --main-ip 127.0.0.1 --main-port 3000 --domain _
```

Instalacao real:

```bash
sudo ./install.sh --with-panel --domain cdn.example.com --ssh-port 22
```

O instalador faz backup das configuracoes antes de substitui-las, valida entradas, executa `nginx -t` antes de reload/restart e instala o painel local. Nunca use `ufw reset`. Confirme a porta SSH real antes de ativar firewall.

Bootstrap em uma VPS que ja tenha Git:

```bash
curl -fsSL https://raw.githubusercontent.com/aryperdomo123456789-web/cdnmnus/main/install-from-github.sh -o /tmp/cdnmnus-install.sh
sudo bash /tmp/cdnmnus-install.sh -- --with-panel --domain cdn.example.com --ssh-port 22
rm -f /tmp/cdnmnus-install.sh
```

O bootstrap recusa diretorio que nao seja clone Git e recusa worktree sujo. Ele encaminha as opcoes depois de `--` para o instalador principal.

## Nova edge gerenciada e harmônica

Para integrar uma edge à rede, atualize primeiro o clone Git do control plane e
use `Mago CDN -> Edges -> Adicionar edge`. O control plane confirma fingerprint,
cria a chave exclusiva e mantém o nó em `bootstrapping`. O worker é o único
fluxo autorizado a aplicar a release multi-tenant:

```text
preflight-edge.yml
-> deploy-edge.yml
-> activate-edge.yml
-> audit-edge-releases.yml
-> finalize-edge-onboarding.yml
```

Somente o sucesso de todas as etapas muda os modelos legado e topológico para
`ready`, com release/digest e eventos auditados. O `install-from-github.sh`
continua sendo a entrada de instalação standalone e não contorna esses gates.

O pacote universal gerenciado possui entrada própria:
`install-managed-node-from-github.sh`. Ela exige tag, commit e digest do
manifesto, recusa branches móveis e instala o contrato comum antes do
deployment de tenant. Consulte
[MANAGED_NODE_PACKAGE_RUNBOOK.md](MANAGED_NODE_PACKAGE_RUNBOOK.md).

## Atualizacao

No clone existente:

```bash
cd /opt/cdnmnus
sudo scripts/update.sh --dry-run
sudo scripts/update.sh
```

O atualizador faz fast-forward de `origin/main`, cria backup em `/var/backups/cdnmnus/<timestamp>`, instala o painel versionado, valida Python/Bash/Nginx e so entao recarrega servicos. Nao altera UFW, banco, senha ou estado do upstream sem uma acao especifica.

Se houver mudancas locais intencionais, pare e revise. `--allow-dirty` existe para manutencao consciente e nao deve ser rotina.

## Tunneling e operacao

Painel web local:

```bash
ssh -N -L 9090:127.0.0.1:9090 root@cdn.example.com -p 22
```

Depois abra `http://127.0.0.1:9090/` e autentique com Basic Auth.

CLI SSH:

```bash
ssh -t root@cdn.example.com -p 22 'TERM=xterm-256color mago-cdn'
```

## Checklist pos-instalacao

```bash
sudo nginx -t
sudo systemctl is-active nginx
sudo systemctl is-active cdnmnus-panel.service
sudo ss -lntp | grep -E ':(80|443|9090 )\\b'
```

Esperado: Nginx ativo em 80/443, painel somente em `127.0.0.1:9090`, e nenhuma porta de backend exposta.

## Rollback

Os backups ficam em `/var/backups/cdnmnus`. Identifique o backup, restaure apenas o arquivo afetado, execute `nginx -t` e somente depois recarregue o Nginx. Nao use `git reset --hard` nem apague banco/credenciais para tentar corrigir uma falha.

## Limites e seguranca

A CDN mascara a origem somente dentro das regras de proxy e reescrita conhecidas. Nao prometa anonimato absoluto para conteudo binario, URLs assinadas, dominios alternativos ou dados gerados por terceiros. Testes devem ser autorizados, curtos, limitados por bytes e nunca devem registrar credenciais de playlist.
