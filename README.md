# cdnmnus

Instalador CLI modular para transformar uma instalação limpa do Ubuntu em um **reverse proxy Nginx enxuto, previsível e adaptativo**. O projeto usa Bash, Nginx nativo do sistema e utilitários padrão do Ubuntu, sem framework ou dependência pesada.

> O objetivo é entregar uma base eficiente para VPS pequenas, sem sacrificar limites de conexão e arquivos em máquinas maiores. Não existe “número mágico” que sirva para todo hardware: o instalador calcula o perfil a partir de CPU e memória detectadas.

## Compatibilidade e arquitetura

O instalador aceita **Ubuntu 20.04, 22.04, 24.04 e versões superiores**, desde que a distribuição seja identificada como Ubuntu e a versão principal seja igual ou superior a 20. A configuração do Nginx utiliza o event loop `epoll` no Linux, `worker_processes auto`, upstream persistente com `keepalive`, buffers de proxy por faixa de memória e cabeçalhos de encaminhamento de IP real. A instalação usa a configuração principal em `/etc/nginx/nginx.conf` e preserva extensões locais em `/etc/nginx/conf.d/*.conf`.

| Componente | Responsabilidade | Alterações principais |
| --- | --- | --- |
| `install.sh` | Orquestração e CLI | Detecta Ubuntu, CPU/RAM, renderiza o template, instala pacotes, testa e reinicia o Nginx |
| `scripts/sysctl_tuning.sh` | Tuning do kernel | Ajusta `somaxconn`, `tcp_max_syn_backlog`, `tcp_tw_reuse`, `file-max`, `vm.swappiness` e `nofile` |
| `scripts/firewall_hardening.sh` | Firewall | Define entrada como deny, saída como allow e libera SSH, HTTP e HTTPS sem resetar regras existentes |
| `nginx/nginx.conf` | Template de proxy | Usa placeholders que são substituídos pelo perfil calculado do host |

## Instalação remota em um comando

A forma mais direta, usando a versão publicada na branch `main`, é:

```bash
curl -sSL https://raw.githubusercontent.com/aryperdomo123456789-web/cdnmnus/main/install.sh | sudo bash -s -- \\
  --main-ip 127.0.0.1 \\
  --main-port 3000 \\
  --domain exemplo.com
```

Para executar sem domínio específico, o `server_name` padrão é `_`:

```bash
curl -sSL https://raw.githubusercontent.com/aryperdomo123456789-web/cdnmnus/main/install.sh | sudo bash -s -- --main-port 3000
```

Antes de usar `curl | bash` em um ambiente sensível, revise o conteúdo do script ou fixe o download em um commit auditado. O modo remoto baixa os módulos `sysctl`, `firewall` e o template Nginx da mesma branch `main`.

## Instalação por clone local

```bash
git clone https://github.com/aryperdomo123456789-web/cdnmnus.git
cd cdnmnus
chmod +x install.sh scripts/*.sh
sudo ./install.sh \\
  --main-ip 127.0.0.1 \\
  --main-port 3000 \\
  --domain exemplo.com
```

Quando executado em um terminal interativo sem todos os argumentos, o instalador mostra um menu curto para escolher o backend padrão ou informar um backend personalizado. Em automações, informe os argumentos explicitamente e use `--yes` quando necessário.

## Opções do instalador

| Opção | Padrão | Descrição |
| --- | --- | --- |
| `--main-ip IP\|HOST` | `127.0.0.1` | Endereço ou host do upstream da aplicação |
| `--main-port PORTA` | `3000` | Porta TCP do upstream, entre 1 e 65535 |
| `--domain DOMÍNIO` | `_` | Valor de `server_name` no Nginx |
| `--ssh-port PORTA` | `22` | Porta SSH liberada pelo UFW |
| `--no-firewall` | desativado | Não altera o UFW |
| `--dry-run` | desativado | Valida o ambiente e imprime o plano sem alterar o sistema |
| `--yes` | desativado | Não pede confirmação no menu interativo |
| `--help` | — | Exibe a ajuda |

O backend deve estar acessível a partir da máquina onde o Nginx será instalado. Para um processo local, por exemplo, a aplicação deve escutar em `127.0.0.1:3000` ou em outra porta informada com `--main-port`.

## Perfil adaptativo

O instalador lê o número de processadores disponíveis e `MemTotal` de `/proc/meminfo`. Em seguida, calcula limites com teto para evitar que uma máquina muito grande gere valores desnecessariamente agressivos em serviços menores.

| Recurso | Perfil aplicado |
| --- | --- |
| `worker_connections` | `max(4096, min(65536, RAM_MB × 16 ÷ vCPU))` |
| `worker_rlimit_nofile` | Derivado de conexões × vCPU × 2, limitado entre `65536` e `1048576` |
| Buffers de proxy | `8k`/`4 × 8k` abaixo de 2 GB; `16k`/`8 × 16k` entre 2 e 8 GB; `32k`/`16 × 32k` a partir de 8 GB |
| `client_max_body_size` | `8m`, `16m` ou `32m`, conforme a faixa de memória |
| `net.core.somaxconn` | `4096 × vCPU`, limitado a `65535` |
| `net.ipv4.tcp_max_syn_backlog` | Duas vezes `somaxconn`, limitado a `131072` |
| `fs.file-max` | `RAM_MB × 256`, limitado entre `131072` e `8388608` |

Os limites persistentes são gravados em arquivos dedicados: `/etc/sysctl.d/99-cdnmnus.conf` e `/etc/security/limits.d/99-cdnmnus.conf`. O instalador não edita arquivos genéricos do sistema e o módulo UFW não executa `ufw reset`, preservando regras já existentes.

## Validação e operação

Após renderizar a configuração, o instalador executa `nginx -t`. Se o teste falhar, a configuração anterior é restaurada e o serviço não é reiniciado com o arquivo inválido. Em uma instalação válida, o serviço é habilitado e reiniciado automaticamente.

O endpoint local de verificação é:

```bash
curl -i http://127.0.0.1/nginx-health
```

A resposta esperada é `200 OK` com o corpo `ok`. Para conferir o estado do serviço e a configuração carregada:

```bash
sudo systemctl status nginx --no-pager
sudo nginx -T
sudo ufw status verbose
```

Se o backend não responder, investigue primeiro a aplicação e depois os logs do proxy:

```bash
sudo tail -f /var/log/nginx/error.log /var/log/nginx/access.log
```

## Firewall

Por padrão, o módulo aplica política de entrada `deny`, saída `allow` e libera TCP nas portas 22, 80 e 443. A porta do backend recebe uma regra de negação explícita para não ficar pública. A regra de loopback do UFW continua permitindo que o Nginx converse com um backend local.

Se o SSH administrativo usa outra porta, ajuste o módulo antes de instalar ou execute o módulo diretamente com a porta correta:

```bash
sudo ./scripts/firewall_hardening.sh --backend-port 3000 --ssh-port 2222
```

No instalador mestre, use a mesma opção para que a regra seja aplicada junto ao restante do deploy:

```bash
sudo ./install.sh --main-port 3000 --ssh-port 2222 --domain exemplo.com
```

Se o ambiente já possui política de firewall gerenciada por outra ferramenta, use `--no-firewall` e aplique o hardening no mecanismo oficial daquele ambiente.

## Dry-run e validação local

O dry-run não exige root quando executado no clone local:

```bash
./install.sh --dry-run --main-ip 127.0.0.1 --main-port 3000 --domain exemplo.com
./scripts/sysctl_tuning.sh --dry-run --cores 2 --memory-mb 4096 --nofile 65536
./scripts/firewall_hardening.sh --dry-run --backend-port 3000 --ssh-port 22
```

Para testar apenas a sintaxe Bash:

```bash
bash -n install.sh
bash -n scripts/sysctl_tuning.sh
bash -n scripts/firewall_hardening.sh
```

## Segurança operacional

O instalador deve ser executado com privilégio administrativo porque modifica pacotes, sysctl, limites, UFW e `/etc/nginx`. O projeto não cria usuários, não abre a porta do upstream para a Internet e não substitui certificados TLS. Para produção, coloque TLS terminando no Nginx, restrinja o acesso administrativo por rede quando possível e mantenha o sistema atualizado.

A configuração de proxy repassa `X-Real-IP`, `X-Forwarded-For` e `X-Forwarded-Proto`. Se a aplicação estiver atrás de outra camada de proxy confiável, configure a política de IP real do seu ambiente antes de usar o endereço encaminhado para decisões de autorização.

## Licença

Este repositório é distribuído conforme a licença presente em `LICENSE`, quando adicionada pelo mantenedor.

## Referências

[1]: https://nginx.org/en/docs/ Nginx Documentation — documentação oficial do Nginx.

[2]: https://manpages.ubuntu.com/manpages/jammy/en/man8/ufw.8.html Ubuntu UFW manpage — referência do utilitário de firewall.

[3]: https://man7.org/linux/man-pages/man5/sysctl.d.5.html `sysctl.d(5)` — formato e persistência de parâmetros de kernel.

## Painel de upstream HTTP autorizado

O instalador pode adicionar um painel administrativo mínimo e autenticado para apontar a VPS para um XUI autorizado por **IP ou DNS**, sempre usando HTTP na porta 80:

```bash
sudo ./install.sh --with-panel --main-port 3000 --domain vps.exemplo.com
```

O painel escuta somente em `127.0.0.1:9090`; ele não deve ser aberto diretamente na Internet. Para acessá-lo com segurança a partir do seu computador, use um túnel SSH:

```bash
ssh -L 9090:127.0.0.1:9090 usuario@IP_DA_VPS
```

Depois abra `http://127.0.0.1:9090/`. A senha inicial é gerada aleatoriamente, exibida uma única vez pelo instalador e armazenada em `/etc/cdnmnus/panel.env` com permissão `0600`. O serviço roda como `cdnmnus-panel.service`, limitado a localhost, com `NoNewPrivileges`, `PrivateTmp` e escrita somente nos diretórios de configuração necessários.

O painel valida o host, resolve o DNS, aceita somente porta 80, grava o estado em `/etc/cdnmnus/upstream.json`, gera `/etc/nginx/conf.d/99-cdnmnus-upstream.conf`, executa `nginx -t` e somente então recarrega o Nginx. A configuração não armazena usuário ou senha de playlist, e os logs do painel removem query strings.

### Ocultação do upstream

A configuração dinâmica remove os headers `Location`, `Server`, `Via` e `X-Powered-By`, desliga `proxy_redirect` e encaminha o `Host` do upstream apenas na conexão interna entre Nginx e XUI. Para playlists textuais, o `sub_filter` reescreve referências textuais ao host configurado para o host público recebido pelo cliente.

> Isso reduz vazamentos comuns, mas não é uma garantia universal de anonimato. URLs assinadas, domínios alternativos, manifests HLS que usem hosts diferentes, conteúdo binário, mensagens de erro da aplicação e dados embutidos no próprio stream podem revelar informações. O resultado precisa ser validado no XUI autorizado e no conteúdo real que ele entrega.

O endpoint público deve ser acessado pelo domínio ou IP da VPS, nunca pelo endereço de origem. Também é necessário garantir que o DNS público da VPS não aponte para o XUI e que a porta 80 do XUI esteja protegida por firewall para aceitar somente a VPS, quando a topologia permitir.

### Testes no laboratório

Foram feitos testes passivos nos endereços de laboratório fornecidos, sem imprimir credenciais nem salvar a playlist completa. O IP e o DNS responderam `200 OK`; o endpoint de playlist começou a entregar dados, mas permaneceu contínuo e excedeu o limite de tempo após aproximadamente 1,8 MB. A página base do DNS também devolveu um header `Link` canônico referenciando o domínio de origem, confirmando que headers precisam ser ocultados ou reescritos.

No ambiente local, passaram a sintaxe Python e Bash, a validação de IP/DNS, a rejeição de comandos injetados e URLs com esquema, a geração do include Nginx e os smoke tests existentes. O sandbox não possui o binário Nginx nem permite instalar UFW, portanto a validação final de `nginx -t`, reload e regras do firewall deve ocorrer na VPS autorizada.

O painel não deve ser usado para mascarar a origem de serviços de terceiros sem autorização. Em produção, mantenha o acesso administrativo por túnel SSH/VPN, use HTTPS na camada pública quando o domínio estiver pronto e troque a credencial inicial imediatamente.

## Painel web no domínio público

Quando o domínio público já aponta para a VPS, o painel pode ser acessado no navegador em:

```text
https://cdn.phpd77.com/admin/
```

O domínio é terminado com certificado TLS do Let's Encrypt na VPS. O usuário inicial configurado neste ambiente é `mago@dono.com`. A senha inicial deve ser tratada como temporária: no primeiro login, troque-a pela aba **Trocar senha** usando uma senha com pelo menos 12 caracteres. O acesso sem autenticação continua respondendo `401`.

As configurações administrativas ficam em SQLite root-only em `/etc/cdnmnus/panel.db`, com senha armazenada por PBKDF2-HMAC-SHA256 e salt individual. O arquivo `/etc/cdnmnus/panel.env` mantém somente o usuário após o bootstrap; a senha plaintext inicial é removida. O banco contém o upstream e seus IPs resolvidos, portanto não deve ser copiado para diretórios públicos ou enviado para repositórios.

A configuração pública atual usa `cdn.phpd77.com` como `server_name`. Para mudar futuramente o domínio da VPS ou o destino do XUI, abra `/admin/`, troque a senha inicial se ainda estiver pendente e preencha novamente o host do XUI e o domínio público. O painel valida, resolve, grava, testa com `nginx -t` e recarrega somente se a configuração for válida.

O HTTPS é emitido apenas quando o DNS estiver resolvendo para a VPS e as portas 80/443 estiverem acessíveis. A renovação é mantida pelo agendamento do Certbot; após renovação, valide `nginx -t` e recarregue o Nginx.
