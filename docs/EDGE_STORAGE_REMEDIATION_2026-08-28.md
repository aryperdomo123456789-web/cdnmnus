# Remediação de armazenamento das edges — 28/08/2026

## Resultado

As três VPS de 30 GB foram verificadas por SSH. O problema de disco cheio foi
contido sem remover banco, releases, certificados ou configurações ativas.

| Edge | Hostname | Uso após contenção | Cache Nginx | Logs |
|---|---|---:|---:|---:|
| `143.14.168.111` | `cdn` | 30% (8,2 GB usados) | 16 KB | 209 MB |
| `143.14.168.168` | `cdn2` | 15% (4,2 GB usados) | 4 KB | 76 MB |
| `143.14.168.170` | `cdn3` | 15% (4,1 GB usados) | 4 KB | 56 MB |

Na edge 111 havia aproximadamente 21 GB em
`/var/cache/nginx/cdnmnus-hls.stale-20260828111536`. Esse diretório foi
removido por ser cache HLS explicitamente marcado como stale e regenerável.
Journals antigos foram reduzidos para 200 MB; nenhum dado de configuração foi
apagado.

## Serviços observados

- Nginx: ativo nas três edges.
- Broker por tenant: ativo nas três edges.
- `cdnmnus-token-broker.service`: inativo nas 168/170, pois o runtime dessas
  máquinas usa o broker por tenant; isso deve ser mantido assim até uma release
  confirmar o contrário.
- Orquestrador no control node: ativo após correção do caminho do banco, mas o
  último deployment VOD terminou `failed` no Ansible e não deve ser repetido sem
  diagnóstico.

## Causa do incidente

O cache HLS antigo ultrapassou a capacidade do disco da edge 111. Com o sistema
em 100%, o SQLite apresentou `disk I/O error` e o worker deixou de processar a
fila. Houve também um período de `database is locked` durante a recuperação.

## Política permanente necessária

O limite de `proxy_cache_path` existente (2 GB para HLS) não basta para evitar
acúmulo de diretórios stale. A próxima release de edge deve instalar:

1. limpeza periódica de `*.stale-*` somente após confirmar que não estão ativos;
2. `max_size` por tenant e por classe de cache;
3. alerta em 70%, 80% e 90% de uso do filesystem;
4. journald com `SystemMaxUse` limitado;
5. rotação de logs e retenção definida;
6. reserva mínima de 20% do disco para SQLite, TLS e temporários;
7. bloqueio de novos caches quando a reserva for atingida.

## Próximo gate

Antes de publicar as fontes VOD nas três edges, investigar o erro detalhado do
Ansible (`lb011: failed`), executar preflight individual e gerar uma nova
release. O deployment `dep-236418bec22841bca970fe4f4e8ab007` permanece como
falho para auditoria; não foi apagado nem mascarado.

## Comandos de verificação

```bash
df -h /
du -xhd1 /var/cache/nginx /var/log /tmp 2>/dev/null | sort -h
systemctl is-active nginx cdnmnus-tenant-broker@xui-principal.service
journalctl --disk-usage
```

O espaço livre atual permite nova operação, mas o rollout só deve ser tentado
após corrigir a causa do erro Ansible e confirmar que as três edges estão no
mesmo banco de configuração e na mesma release.

## Verificação do hostname público

Durante a validação paralela, `cdn.phpd77.com` respondeu com resultados
 diferentes por edge:

- `143.14.168.111`: página controlada **Mago Edge Infrastructure**;
- `143.14.168.168` e `143.14.168.170`: página padrão **Welcome to nginx!** na
  rota `/`.

O vhost de 168/170 contém `server_name cdn.phpd77.com` e está sintaticamente
correto; a rota genérica `/` encaminha para a origem, que devolve a página
default do Nginx. Isso não é vazamento do `Location` VOD, mas é uma exposição
indesejada de fingerprint. A próxima release deve adicionar uma localização
exata `= /` com placebo controlado, preservando as rotas de API, HLS, movie e
series.

Não alterar a página por edição manual isolada: corrigir o template/renderizador,
gerar release, aplicar serialmente e validar cada edge.
