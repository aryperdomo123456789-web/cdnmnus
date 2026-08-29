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

## Resultado da correção do vhost raiz

O renderizador central passou a emitir uma `location = /` com a tela estática
Mago Edge. A release `20260828205036-b2a09f87` foi aplicada serialmente em
`143.14.168.168` e `143.14.168.170`; ambas agora retornam a mesma página Mago
Edge e não a página Debian “Welcome to nginx!”. `/edge-health` retorna HTTP 200
nas três pontas. A edge `143.14.168.111` permaneceu no runtime legado para
preservar a entrega atual e ainda precisa de migração controlada.

Na edge legada 111, a tela era servida pelo arquivo local
`/var/www/mago-edge/index.html` e mantinha o layout detalhado legado. Esse
arquivo foi alinhado ao mesmo layout simples emitido pelo renderizador, com o
rodapé `2026 @MagoPD todos os direitos reservados.`. As rotas de mídia não
foram alteradas. Para edges gerenciadas pela release, o texto continua sendo
controlado pela string de `location = /` em `core/render_tenants.py` e deve ser
publicado por nova release.

## Correção do deployment e resultado da reaplicação

O erro original foi reproduzido com Ansible em modo detalhado. A tarefa que
falhava era `Validar digest declarado`: o digest passado ao playbook não era o
mesmo digest do `manifest.json`. Com o valor correto
`9b729a8abaea32345c1e8b72cefead67e2c49246ab0440be1158e546260ed674`, a release
`20260828200028-06ea5480` foi sincronizada com sucesso em `lb011` e `lb02`.

Evidência remota:

```text
143.14.168.168 -> /opt/cdnmnus/releases/20260828200028-06ea5480/SYNCED
143.14.168.170 -> /opt/cdnmnus/releases/20260828200028-06ea5480/SYNCED
```

Ambas responderam HTTP 200 em `/edge-health`; Nginx e o broker por tenant estão
ativos. O registro histórico do deployment permanece `failed` porque a
execução original falhou antes da reaplicação manual; ele não deve ser editado
para aparentar sucesso. O worker precisa receber uma nova fila/release após o
diagnóstico para registrar um deployment `succeeded` de forma auditável.

A edge `143.14.168.111` não foi sobrescrita no runtime de mídia: ela permanece
no runtime legado de controle, mas sua página pública agora usa o mesmo modelo
simples das edges gerenciadas. A migração integral da 111 para o runtime novo
continua devendo ocorrer em janela controlada, com drain e rollback.

## Regra para a tela pública

O vhost novo possui `/edge-health`, `/movie/`, `/series/` e `/hls/`. A rota
raiz `location = /` em 168/170 é gerada por `core/render_tenants.py`; na 111 a
mesma resposta é servida pelo arquivo legado local. As três pontas agora
apresentam uma única tela pública simples. A validação de configuração deve
ser feita com `nginx -t` antes de qualquer reload; no host de controle atual o
teste ainda acusa um upstream externo não resolvível em
`/etc/nginx/conf.d/99-cdnmnus-upstream.conf`, falha preexistente e independente
da troca visual.

## Revalidação de latência do armazenamento

A continuação da auditoria confirmou espaço e inodes suficientes, ausência de
cache `*.stale-*` e nenhum erro registrado pelo ext4 (`errors_count=0`,
`warning_count=0`, estado `clean`). O `etime` exibido para `jbd2/vda1-8` é a
idade do thread desde o boot e não comprova, isoladamente, bloqueio contínuo.

Ainda assim, a degradação de I/O é real: um `fdatasync` de 4 KiB no volume raiz
levou aproximadamente 1,05 s, contra menos de 0,01 s em tmpfs. Em um SQLite
descartável, habilitar WAL levou 12,38 s e um commit mínimo levou 6,03 s. A
pressão `full` de I/O ficou acima de 79%. Não houve mensagem de `I/O error`,
journal abortado ou corrupção EXT4 no kernel.

O diagnóstico atual é latência severa do dispositivo virtio/host de
armazenamento, não corrupção comprovada. Não executar `fsck` online, remontar
`/` ou reiniciar a edge enquanto ela estiver no pool. A correção segura exige
incidente no provedor, snapshot consistente e drain antes de migração/reboot.
Se a latência persistir, a verificação do filesystem deve ocorrer offline pela
console de recuperação.
