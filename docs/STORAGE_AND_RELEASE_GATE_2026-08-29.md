# Gate de storage e consolidação da release — 2026-08-29

## Decisão executiva

O control node continua **bloqueado para deployments**. O ext4 está limpo e
não há erro de journal registrado, porém a latência de persistência voltou a
ultrapassar um segundo durante a amostragem sustentada. A candidata pode ser
versionada e testada, mas não pode ser promovida nem ativada nas edges até o
storage ser estabilizado e existir snapshot externo recuperável.

Nenhum Nginx, broker, relay ou serviço de produção foi recarregado durante
esta frente.

## Evidência da Frente A

Host observado: control node atual, raiz `/dev/vda1`, ext4, volume virtual
VirtIO de 30 GiB.

Estado de capacidade na verificação:

- 19 GiB disponíveis, 33% do volume ocupado;
- 8% dos inodes ocupados;
- `Filesystem state: clean`;
- `errors_count=0` e `warning_count=0`;
- nenhum `EXT4-fs error`, `I/O error`, `Buffer I/O` ou erro `JBD2` encontrado
  no journal do kernel desde 2026-08-28.

Medição curta anterior, registrada apenas como janela transitória:

- cinco `fdatasync` de 4 KiB: aproximadamente 0,01 s cada;
- commits SQLite: 0,274 s, 0,024 s e 0,011 s.

Medição sustentada final, executada diretamente em
`/var/backups/cdnmnus`:

```text
fdatasync_4k n=30 min=0.003460s median=0.293547s p95=1.476139s max=2.823373s
sqlite_full_commit n=30 min=0.002875s median=0.025739s p95=1.189428s max=2.812908s integrity=ok
io.pressure some avg10=78.78 avg60=51.02 avg300=40.16
io.pressure full avg10=74.32 avg60=48.82 avg300=38.58
```

Conclusão: capacidade livre não é o problema imediato. O bloqueio é latência
e contenção do dispositivo/host virtual. Uma janela curta boa não demonstra
estabilidade.

## Recuperação criada

Foi criado o pacote local privado:

```text
/var/backups/cdnmnus/front-a-20260829T012118Z
```

Ele contém configuração de Nginx e CDNMenus, units instaladas, materiais TLS e
SSH e cópias consistentes de `admin.db` e `panel.db`. As duas cópias SQLite
passaram por `PRAGMA integrity_check`; 21 arquivos foram protegidos por
`SHA256SUMS`; o diretório e seu conteúdo não concedem acesso a grupo ou outros.

Esse pacote é uma recuperação de aplicação. **Não é snapshot da VPS** e está
no mesmo volume, portanto não protege contra perda do disco.

## Ação obrigatória no provedor

Abrir incidente com o texto objetivo abaixo e anexar a medição sustentada:

> Volume VirtIO ext4 de 30 GiB apresenta latência intermitente de persistência:
> fdatasync 4 KiB p95 1,476 s/máximo 2,823 s e commit SQLite FULL p95 1,189
> s/máximo 2,813 s, com PSI I/O full avg10 74,32%. Filesystem limpo, sem erros
> ext4/JBD2 e com 19 GiB livres. Solicito investigação de contenção do host,
> confirmação de IOPS/latência contratados, snapshot externo e migração do
> volume ou da VPS para host/storage saudável.

O provedor desta VPS não foi identificável pelo metadata local e não existe
LVM, ZFS ou outra camada de snapshot local. Logo, snapshot e migração exigem o
painel/API do provedor e não foram simulados nem declarados como concluídos.

## Critério mensurável para reabrir deployments

Repetir o teste durante ao menos 30 minutos e em três janelas distintas. Todas
as condições precisam ser verdadeiras:

- `fdatasync` 4 KiB: p95 menor que 100 ms e máximo menor que 500 ms;
- commit SQLite com `synchronous=FULL`: p95 menor que 250 ms e nenhum commit
  acima de 1 s;
- PSI de I/O `full avg10` abaixo de 10% na amostragem sem job extraordinário;
- zero novo erro ext4, JBD2 ou I/O no kernel;
- `PRAGMA integrity_check` igual a `ok` nas bases;
- snapshot externo concluído e restauração dele validada ou formalmente
  garantida pelo provedor;
- control node fora do caminho de dados de mídia.

Enquanto qualquer item falhar:

- não instalar Ansible neste host;
- não executar playbook de ativação;
- não promover tag candidata a release aprovada;
- não usar o pacote local como justificativa para dispensar snapshot externo.

## Frente B — evidência da candidata

Identidade congelada nesta execução:

```text
tag: v0.4.8-production-candidate.3
release_id: 20260829012407-d60cfdbf
config_digest: 9e5457a1dd27609a573c0fd7cbcc80db1d84378da118d7e0dc70d70d7a534eb0
artifact_path: /var/lib/cdnmnus-admin/releases/20260829012407-d60cfdbf
artefatos fechados no manifesto: 7
```

O verificador independente recalculou os sete SHA-256, recusou conteúdo extra
e confirmou `release_id` e `config_digest`. O bundle Git completo foi incluído
no pacote de recuperação e verificado. Não existe chave GPG secreta configurada
neste control node; por isso a tag é anotada, mas não possui assinatura
criptográfica. A aprovação operacional permanece bloqueada pelo gate de
storage e não foi falsamente registrada.

Verificações executadas antes do congelamento:

- revisão da superfície modificada de banco, renderer, deploy/rollback,
  painel, brokers, relay, units, Ansible e testes;
- `git diff --check` sem erro;
- busca por marcadores de conflito e credenciais no diff sem ocorrência;
- compilação sintática de todos os fontes Python;
- `bash -n` nos scripts de shell;
- parse de todos os YAML do Ansible;
- `systemd-analyze verify` nas duas units da release;
- testes de banco, painel, edge manager, integridade de release,
  multi-tenant broker, token broker e relay;
- contratos locais compatíveis com XCIPTV/IBO Player, incluindo `HEAD`,
  `Range`, `If-Range`, seek, suffix range e concorrência curta;
- smoke completo e `nginx -t` sobre configuração renderizada.

Importante: os testes XCIPTV/IBO são contratos HTTP locais; não substituem a
homologação com os aplicativos proprietários reais. Carga/soak de seis horas e
rollback em edge de homologação continuam gates posteriores.

## Estado do gate

| Gate | Estado |
|---|---|
| ext4 sem erros registrados | PASS |
| bases SQLite íntegras | PASS |
| pacote local de recuperação | PASS |
| fsync/SQLite estáveis | **FAIL** |
| snapshot externo disponível | **BLOCKED — provedor** |
| control node fora do data path de mídia | PASS por arquitetura observada |
| candidata testada e reproduzível | PASS, após registrar commit/tag/manifesto |
| release aprovada para ativação | **BLOCKED** |
