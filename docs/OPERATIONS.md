# cdnmnus: operacao, instalacao e atualizacao

## Modelo de seguranca

- O painel escuta somente em `127.0.0.1:9090`.
- A administracao deve usar SSH com tunel local; nenhuma porta adicional e aberta.
- O upstream usa HTTP/80 neste perfil.
- O proxy nao deve registrar query strings ou caminhos que possam conter credenciais.
- O endereco do upstream nao deve ser colocado em mensagens, headers publicos ou relatorios.
- Nunca execute testes destrutivos nem baixe playlists continuas sem limite.

A ocultacao de origem e uma politica de reescrita, nao anonimato absoluto. URLs assinadas, dominios alternativos, manifests incomuns e conteudo binario podem exigir regras adicionais.

## Servidor novo

1. Confirme Ubuntu 20.04+ e acesso root por SSH.
2. Detecte a porta SSH real antes do firewall:

```bash
sudo ss -lntp
```

3. Clone e valide o projeto:

```bash
sudo mkdir -p /opt
sudo git clone https://github.com/aryperdomo123456789-web/cdnmnus.git /opt/cdnmnus
cd /opt/cdnmnus
chmod +x install.sh scripts/*.sh tests/*.sh
./install.sh --dry-run --yes --main-ip 127.0.0.1 --main-port 3000 --domain _
```

4. Instale usando o dominio publico e a porta SSH confirmada:

```bash
sudo ./install.sh --with-panel --domain cdn.example.com --ssh-port 22
```

Use `--no-firewall` apenas quando outro sistema gerenciar o firewall. O instalador deve fazer backup antes de substituir configuracoes e nunca usar `ufw reset`.

## Acesso ao painel

O painel nao e publico. A partir do computador administrador:

```bash
ssh -N -L 9090:127.0.0.1:9090 root@cdn.example.com -p 22
```

Abra `http://127.0.0.1:9090/`. A autenticacao e Basic Auth; nunca coloque a senha em scripts, URLs, tickets ou logs.

A CLI administrativa opcional fica em `/usr/local/bin/mago-cdn` e pode ser executada por SSH:

```bash
ssh -t root@cdn.example.com -p 22 'TERM=xterm-256color mago-cdn'
```

## Configuracao de upstream

No painel, informe o host do XUI sem credenciais, a porta 80, o dominio publico e os LBs HTTP, um por linha. O fluxo de salvamento deve ser:

1. Validar sintaxe do host.
2. Resolver DNS.
3. Escrever estado com permissao root-only.
4. Gerar include Nginx.
5. Executar `nginx -t`.
6. Somente depois recarregar o Nginx.
7. Em falha, restaurar a configuracao anterior.

## Atualizacao pelo GitHub

O atualizador versionado e `/opt/cdnmnus/scripts/update.sh`. Primeiro execute o dry-run:

```bash
cd /opt/cdnmnus
sudo scripts/update.sh --dry-run
```

Depois, com uma janela de manutencao:

```bash
sudo scripts/update.sh
```

O script:

- Busca `origin/main` com `git fetch`.
- Aplica somente fast-forward.
- Recusa worktree sujo por padrao.
- Cria backup em `/var/backups/cdnmnus/<timestamp>`.
- Atualiza o painel instalado.
- Valida Python, Bash e Nginx.
- Reinicia somente o servico do painel e recarrega o Nginx apos teste valido.
- Nao altera UFW nem credenciais.

Se houver alteracoes locais conscientemente revisadas:

```bash
sudo scripts/update.sh --allow-dirty
```

Essa opcao nao deve ser usada como procedimento normal, pois permite que arquivos locais nao versionados interfiram na atualizacao.

## Verificacao pos-atualizacao

```bash
python3 -m py_compile panel/panel.py
bash -n install.sh scripts/*.sh tests/*.sh
python3 tests/panel_http_test.py
./tests/smoke.sh
sudo nginx -t
sudo systemctl is-active nginx
sudo systemctl is-active cdnmnus-panel.service
sudo ss -lntp | grep -E ':(80|443|9090 )\\b'
```

Para testes publicos, use somente o dominio da VPS, limite de bytes e timeout curto. Registre apenas status, tipo e contagens sanitizadas; nunca imprima credenciais, query strings completas, URLs assinadas, playlists ou o host da origem.

## Rollback operacional

1. Pare e avalie o servico afetado.
2. Escolha o backup correto em `/var/backups/cdnmnus/<timestamp>`.
3. Restaure somente o arquivo necessario, preservando credenciais e estado atuais quando possivel.
4. Execute `sudo nginx -t`.
5. So entao execute `sudo systemctl reload nginx`.
6. Confirme os dois servicos e o bind local do painel.

Nao use `git reset --hard`, `ufw reset` ou comandos destrutivos como rollback automatico.
