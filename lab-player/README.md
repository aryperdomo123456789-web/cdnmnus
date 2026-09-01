# Lab player workspace

This directory is an isolated scratch area for playback validation artifacts.

Structure:

- `playlists/` stores fetched manifests and symlinks to the latest capture.
- `reports/` stores command output and validation notes.
- `scripts/` stores helper scripts used for controlled playback checks.

Nothing here should contain production secrets. URLs, credentials, and device
strings are supplied at runtime through environment variables.

## Ensaio do CNAME DNS-only

O teste usa o mesmo caminho de uma aplicação real, mas compara uma camada por
vez. O CNAME deve apontar para o hostname canônico, nunca diretamente para o
IP da origem:

```text
cnxt.vr766.com CNAME tvbrasil.phpd77.com DNS-only
```

Execute pelo menu SSH em `Capacidade e consumo -> Testar reprodução pelo CNAME
DNS-only`, ou diretamente:

```bash
PLAYER_BASE_CNAME=https://cnxt.vr766.com \\
PLAYER_BASE_CDN=https://tvbrasil.phpd77.com \\
PLAYER_BASE_DIRECT=http://38.46.223.77 \\
PLAYER_USERNAME='usuario-de-laboratorio' \\
PLAYER_PASSWORD='senha-de-laboratorio' \\
python3 scripts/test_playback_flow.py --cname
```

O ensaio verifica resolução do alias, handshake Xtream, amostras live e VOD,
`HTTP 200`, `HTTP 206`, `Range`, conteúdo não vazio e grava um relatório sem
senha. Use conta de laboratório com limite, nunca credenciais administrativas.

## XUI de laboratório com credencial local

Para usar um XUI de teste sem colocar usuário, senha ou M3U no repositório,
grave os valores somente em um arquivo root-only, por exemplo
`/etc/cdnmnus/lab-player/xuilab.env`, com modo `0600`:

```bash
PLAYER_USERNAME='...'
PLAYER_PASSWORD='...'
PLAYER_BASE_DIRECT='http://xui-de-lab'
PLAYER_BASE_CNAME='https://xuilab.example'
```

Execute `scripts/run_xuilab_test.sh`. O sincronizador monta a URL `get.php`
apenas em memória, baixa a playlist no diretório local ignorado e redige as
credenciais em relatórios. Esse arquivo não é cadastro de produção, não é
sincronizado entre edges e não deve ser adicionado ao SQLite ou ao Git.
