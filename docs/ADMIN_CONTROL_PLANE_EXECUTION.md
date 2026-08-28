# Runbook de execução do plano administrativo multi-edge

Status: procedimento operacional para a implementação presente em `core/`,
`cli/`, `web/`, `orchestrator/` e `ansible/` em 28/08/2026.

Este documento prepara o servidor de controle local, instala o Ansible em
ambiente virtual, configura SQLite em WAL, inicia o painel e o worker e fornece
validações que não alteram o data plane.

## 1. Resultado da instalação

Ao final, existirão dois processos separados:

```text
Navegador/CLI -> painel administrativo -> SQLite/WAL -> fila de deployments
                                                       |
                                                       v
                                                worker Ansible -> SSH -> edges
```

- painel web: recebe operações administrativas e enfileira deployments;
- worker: não possui porta pública e consome a fila;
- banco: `/var/lib/cdnmnus-admin/admin.db`;
- releases: `/var/lib/cdnmnus-admin/releases/`;
- chaves das edges: `/etc/cdnmnus/ssh/`;
- código: `/opt/cdnmnus`, mantido `root:root` e legível pelos serviços.

O worker não participa de requisições HLS/VOD. Sua indisponibilidade impede
novos deployments, mas não interrompe o streaming atendido pelas edges.

## 2. Pré-requisitos

- Ubuntu com Python 3, `python3-venv`, OpenSSH client e `sudo`;
- execução inicial como `root`;
- repositório em `/opt/cdnmnus`;
- acesso SSH autorizado às edges;
- fingerprint Ed25519 conferível no console do provedor;
- portas do painel limitadas por firewall à rede administrativa.

Instale os pacotes nativos:

```bash
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv openssh-client sudo ca-certificates
```

Não execute `chown -R cdn-admin /opt/cdnmnus` nem `chmod 700 /opt/cdnmnus`.
Isso transferiria o repositório, `.git` e outros arquivos para a conta de
serviço sem necessidade.

## 3. Criar usuário e diretórios

Crie uma conta de sistema sem login interativo:

```bash
if ! getent passwd cdn-admin >/dev/null; then
  useradd \
    --system \
    --create-home \
    --home-dir /var/lib/cdnmnus-admin \
    --shell /usr/sbin/nologin \
    cdn-admin
fi
```

Se o usuário já existir, não repita `useradd`. Confira:

```bash
getent passwd cdn-admin
id cdn-admin
```

Crie os diretórios graváveis:

```bash
install -d \
  -o cdn-admin \
  -g cdn-admin \
  -m 0700 \
  /var/lib/cdnmnus-admin \
  /var/lib/cdnmnus-admin/releases \
  /etc/cdnmnus/ssh

if [[ ! -e /var/lib/cdnmnus-admin/admin.db ]]; then
  install \
    -o cdn-admin \
    -g cdn-admin \
    -m 0600 \
    /dev/null \
    /var/lib/cdnmnus-admin/admin.db
fi
```

O banco fica em `/var/lib`, não em `/etc`, porque SQLite WAL precisa criar os
arquivos irmãos `admin.db-wal` e `admin.db-shm`. Não conceda escrita de todo
`/etc/cdnmnus` ao usuário do painel.

Valide:

```bash
stat -c '%U:%G %a %n' \
  /var/lib/cdnmnus-admin \
  /var/lib/cdnmnus-admin/releases \
  /var/lib/cdnmnus-admin/admin.db \
  /etc/cdnmnus/ssh
```

Resultado esperado:

```text
cdn-admin:cdn-admin 700 /var/lib/cdnmnus-admin
cdn-admin:cdn-admin 700 /var/lib/cdnmnus-admin/releases
cdn-admin:cdn-admin 600 /var/lib/cdnmnus-admin/admin.db
cdn-admin:cdn-admin 700 /etc/cdnmnus/ssh
```

## 4. Criar o ambiente Python e instalar Ansible

O venv pode continuar pertencendo a `root`; a conta de serviço precisa somente
ler e executar seus arquivos:

```bash
python3 -m venv /opt/cdnmnus/venv
/opt/cdnmnus/venv/bin/pip install --upgrade pip
/opt/cdnmnus/venv/bin/pip install ansible-core pexpect cryptography
```

Valide como a conta real do worker:

```bash
sudo -u cdn-admin \
  /opt/cdnmnus/venv/bin/python3 --version

sudo -u cdn-admin \
  /opt/cdnmnus/venv/bin/ansible-playbook --version
```

`pexpect` é usado pelo bootstrap SSH sem `sshpass`; `cryptography` gera as chaves
Ed25519 em memória. Não basta instalar Ansible no venv: a unit também precisa
usar o Python do venv e incluir `/opt/cdnmnus/venv/bin` no `PATH`.

## 5. Configuração compartilhada

Crie `/etc/cdnmnus/admin.env` sem colocar senha root de edge:

```bash
install -o root -g cdn-admin -m 0640 /dev/null /etc/cdnmnus/admin.env
```

Conteúdo recomendado:

```ini
CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db
CDNMNUS_ADMIN_BIND=0.0.0.0
CDNMNUS_ADMIN_PORT=8080
CDNMNUS_ADMIN_USER=admin
CDNMNUS_ADMIN_PASSWORD=SUBSTITUA_POR_UMA_SENHA_FORTE
```

Edite com um editor que não imprima a senha no terminal:

```bash
nano /etc/cdnmnus/admin.env
```

Não informe a senha administrativa diretamente na linha de comando permanente
de uma unit. Não coloque nesse arquivo senha root de edge, credencial XUI/M3U ou
token de mídia.

Para TLS, acrescente:

```ini
CDNMNUS_ADMIN_PORT=8443
CDNMNUS_ADMIN_TLS_CERT=/etc/cdnmnus/admin.crt
CDNMNUS_ADMIN_TLS_KEY=/etc/cdnmnus/admin.key
```

Ao usar HTTP sem TLS, restrinja a porta a loopback, túnel SSH ou rede
administrativa confiável. Basic Authentication sobre HTTP aberto não protege a
senha durante o transporte.

## 6. Inicializar e validar o banco sem consumir jobs

Use o mesmo caminho configurado para os dois serviços:

```bash
sudo -u cdn-admin \
  env \
    PYTHONPATH=/opt/cdnmnus \
    CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 -c \
  'from core.db import Database; db=Database(); db.initialize(); print(db.rows("SELECT id,state FROM deployments"))'
```

Saída normal em uma instalação nova:

```text
[]
```

Confirme WAL e permissões:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 -c \
  'from core.db import Database; db=Database(); db.initialize(); c=db.connect(); print(c.execute("PRAGMA journal_mode").fetchone()[0]); c.close()'

stat -c '%U:%G %a %n' /var/lib/cdnmnus-admin/admin.db*
```

O primeiro comando deve imprimir `wal`.

Não use `orchestrator/worker.py --once` como simples health check. Se existir um
job `queued`, essa opção faz claim e executa o deployment real uma vez.

## 7. Instalar as units systemd

Copie as units fornecidas:

```bash
install -o root -g root -m 0644 \
  /opt/cdnmnus/web/cdnmnus-admin.service \
  /etc/systemd/system/cdnmnus-admin.service

install -o root -g root -m 0644 \
  /opt/cdnmnus/orchestrator/cdnmnus-orchestrator.service \
  /etc/systemd/system/cdnmnus-orchestrator.service
```

Antes de ativar, ambas devem conter:

```ini
User=cdn-admin
Group=cdn-admin
WorkingDirectory=/opt/cdnmnus
EnvironmentFile=/etc/cdnmnus/admin.env
Environment="PYTHONPATH=/opt/cdnmnus"
Environment="PATH=/opt/cdnmnus/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
ReadWritePaths=/var/lib/cdnmnus-admin /etc/cdnmnus/ssh
```

E os comandos:

```ini
# Painel
ExecStart=/opt/cdnmnus/venv/bin/python3 /opt/cdnmnus/web/app.py

# Worker
ExecStart=/opt/cdnmnus/venv/bin/python3 /opt/cdnmnus/orchestrator/worker.py
```

Se necessário, aplique as alterações com `systemctl edit --full` ou edite as
units versionadas antes de copiá-las. Depois:

```bash
systemctl daemon-reload
systemctl enable --now cdnmnus-admin.service
systemctl enable --now cdnmnus-orchestrator.service
```

## 8. Validação dos serviços

```bash
systemctl status cdnmnus-admin.service --no-pager
systemctl status cdnmnus-orchestrator.service --no-pager

journalctl -u cdnmnus-admin.service -n 50 --no-pager
journalctl -u cdnmnus-orchestrator.service -n 50 --no-pager

ss -ltnp | grep -E ':8080|:8443'
```

Teste o painel sem escrever dados:

```bash
curl -u 'admin:SENHA_CONFIGURADA' \
  -i http://127.0.0.1:8080/api/state
```

Em TLS:

```bash
curl --cacert /CAMINHO/DA/CA.crt \
  -u 'admin:SENHA_CONFIGURADA' \
  -i https://127.0.0.1:8443/api/state
```

Não use `-k` como procedimento permanente; configure a CA correta.

## 9. Alterar a porta do painel

Existem quatro formas, em ordem de precedência:

1. argumento `--port` do processo;
2. `CDNMNUS_ADMIN_PORT` em `/etc/cdnmnus/admin.env`;
3. valor `web_port` salvo no SQLite pela CLI ou navegador;
4. padrão `8080`.

Pela CLI:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 \
  /opt/cdnmnus/cli/admin_cli.py config web-port 8443
```

Pelo navegador, use **Configuração → Porta HTTP do painel**. Reinicie:

```bash
systemctl restart cdnmnus-admin.service
```

Importante: se `CDNMNUS_ADMIN_PORT` estiver definido em `admin.env`, ele vence o
valor salvo no SQLite. Para controlar a porta pelo navegador, remova essa linha
do env e reinicie o serviço.

Ao mudar a porta, atualize firewall, health check administrativo e URL de acesso
na mesma janela operacional.

## 10. Primeiro uso pela CLI

Crie um tenant:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 \
  /opt/cdnmnus/cli/admin_cli.py tenant add
```

Cadastre uma edge:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 \
  /opt/cdnmnus/cli/admin_cli.py edge add
```

O fluxo correto é:

1. ler a host key Ed25519;
2. comparar o SHA-256 com o console do provedor;
3. digitar o fingerprint completo para confirmar;
4. fornecer a senha inicial sem eco;
5. criar `cdn-deploy` e instalar a chave exclusiva;
6. validar nova conexão com `BatchMode=yes`;
7. somente depois gravar a edge como `ready`.

A senha inicial não é persistida. A chave operacional fica em
`/etc/cdnmnus/ssh/{edge_id}.ed25519`, modo `0600`.

## 11. DNS e deployment

Visualize/recalcule a matriz DNS:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 \
  /opt/cdnmnus/cli/admin_cli.py dns sync
```

Somente edges `ready` entram na matriz. A variável opcional
`CDNMNUS_DNS_SYNC_SCRIPT` pode apontar para um executável local aprovado, que
recebe a matriz JSON via stdin.

No navegador, o botão **Deploy serial** apenas cria um job `queued`. O worker o
consome e chama o playbook com inventário temporário derivado do SQLite,
fingerprint pinado e `serial: 1`.

Consulte a fila sem executá-la:

```bash
sudo -u cdn-admin \
  env PYTHONPATH=/opt/cdnmnus \
      CDNMNUS_ADMIN_DB=/var/lib/cdnmnus-admin/admin.db \
  /opt/cdnmnus/venv/bin/python3 -c \
  'from core.db import Database; print(Database().rows("SELECT id,state,release_id,error FROM deployments ORDER BY created_at DESC"))'
```

Nesta etapa, o playbook sincroniza e valida o manifesto da release. Ele não
ativa automaticamente os novos vhosts multi-tenant: essa trava evita quebrar o
data plane enquanto o token broker por socket/tenant não estiver implantado e
validado.

## 12. Diagnóstico

### `sudo: unknown user cdn-admin`

```bash
getent passwd cdn-admin || echo 'usuário ausente'
```

Execute a Seção 3 se estiver ausente.

### `attempt to write a readonly database`

Confirme que o banco está em `/var/lib/cdnmnus-admin`, não `/etc`, e valide:

```bash
namei -l /var/lib/cdnmnus-admin/admin.db
stat -c '%U:%G %a %n' /var/lib/cdnmnus-admin /var/lib/cdnmnus-admin/admin.db*
```

### `ansible-playbook não está instalado`

```bash
sudo -u cdn-admin \
  env PATH=/opt/cdnmnus/venv/bin:/usr/bin \
  ansible-playbook --version
```

Confira `Environment=PATH=...` na unit do worker.

### Playbook não encontrado

```bash
systemctl show cdnmnus-orchestrator.service -p WorkingDirectory -p ExecStart
test -f /opt/cdnmnus/ansible/playbooks/deploy-edge.yml
```

O `WorkingDirectory` deve ser `/opt/cdnmnus`, ou o playbook deve ser passado com
caminho absoluto.

### Worker ativo, job permanece `queued`

```bash
journalctl -u cdnmnus-orchestrator.service -n 100 --no-pager
systemctl show cdnmnus-orchestrator.service -p Environment
```

Confirme que painel e worker usam exatamente o mesmo `CDNMNUS_ADMIN_DB`.

## 13. Parada e rollback administrativo

Parar o plano de controle não para o data plane:

```bash
systemctl stop cdnmnus-admin.service cdnmnus-orchestrator.service
```

Faça backup consistente do SQLite:

```bash
sudo -u cdn-admin \
  python3 -c \
  'import sqlite3; src=sqlite3.connect("/var/lib/cdnmnus-admin/admin.db"); dst=sqlite3.connect("/var/lib/cdnmnus-admin/admin.backup.db"); src.backup(dst); dst.close(); src.close()'

chmod 0600 /var/lib/cdnmnus-admin/admin.backup.db
```

Não copie somente `admin.db` enquanto os serviços estão ativos ignorando os
arquivos WAL/SHM. Use a API de backup do SQLite ou pare os dois serviços antes.

Para remover apenas a ativação automática das units:

```bash
systemctl disable --now cdnmnus-admin.service cdnmnus-orchestrator.service
```

Isso não remove banco, releases, chaves nem configurações das edges.

## 14. Checklist de aceite

- [ ] `cdn-admin` é uma conta de sistema sem shell interativo;
- [ ] repositório continua sem propriedade recursiva de `cdn-admin`;
- [ ] banco e releases pertencem a `cdn-admin` e estão em `/var/lib`;
- [ ] SQLite reporta `wal`;
- [ ] `/etc/cdnmnus/ssh` está em modo `0700`;
- [ ] Ansible funciona sob `sudo -u cdn-admin`;
- [ ] painel e worker usam o mesmo caminho de banco;
- [ ] painel exige autenticação;
- [ ] acesso remoto ao painel usa TLS ou túnel/rede administrativa;
- [ ] mudança de porta foi aplicada após restart e firewall atualizado;
- [ ] validação inicial não consumiu acidentalmente job `queued`;
- [ ] senha root de edge não aparece em DB, env, argv, arquivo ou journal;
- [ ] worker não abre porta pública;
- [ ] data plane continuou servindo durante instalação do control plane.
