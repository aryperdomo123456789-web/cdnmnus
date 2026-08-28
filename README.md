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
