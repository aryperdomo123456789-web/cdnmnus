# Pacote universal gerenciado de nós

## Objetivo

Este é o fluxo oficial para uma VPS que fará parte da rede. Ele é separado de
`install.sh`, que permanece standalone/legado.

O pacote universal contém somente código genérico: hardening, menu, identidade,
verificador de release, broker, relay VOD e pré-requisitos de LB. Configuração
de tenants, URLs credenciadas, certificados e snapshots são entregues depois
pelo control plane em releases imutáveis.

## Cadeia de confiança

Uma instalação exige simultaneamente:

- tag Git imutável no formato `v...`;
- commit Git completo esperado;
- SHA-256 do `node-package/manifest.json`;
- hashes fechados de todos os arquivos instaláveis.

Branches como `main` são recusadas. Divergência de qualquer hash aborta antes
de instalar pacotes ou escrever identidade.

Entrada remota:

```bash
sudo ./install-managed-node-from-github.sh \
  --ref TAG_IMUTAVEL \
  --expected-commit COMMIT_SHA40 \
  --manifest-digest SHA256_MANIFESTO \
  --role edge \
  --node-id ID_NUMERICO \
  --node-name 'Edge nova' \
  --control-plane IPV4_CONTROL_PLANE
```

O script clona somente a tag, confere o commit e transfere o controle para
`node-package/install.sh`. O instalador valida Ubuntu 22.04+, 2 vCPU,
aproximadamente 4 GiB, NTP e 20% de disco livre. Um backup root-only é criado
em `/var/backups/cdnmnus-node/`; falha restaura os caminhos gerenciados.

HAProxy nunca é ativado pelo pacote. O runtime multi-tenant também não é
ativado sem snapshot: o onboarding do control plane distribui a release,
valida digest, ativa broker/relay/Nginx, audita e só então marca `ready`.

## Solicitação edge para LB

Em uma edge `ready`, o menu local mostra **Solicitar preparação para Load
Balancer**. Essa ação envia apenas uma solicitação autenticada ao control plane.
Não muda papel, DNS, Nginx ou HAProxy.

O receptor valida:

- origem SSH e IP correspondente ao node ID;
- edge `ready` no modelo topológico;
- tag, commit e digest do pacote instalado;
- ausência de outra solicitação aberta.

No menu do control plane, a aprovação exige um JSON root-only `0600` com versão
exata do HAProxy, TLS e backends. O processador drena a edge, recalcula a matriz
DNS, executa a role LB e finaliza atomicamente como `candidate` ou `standby`.
Falha restaura `ready` e registra `failed`.

Exemplo sanitizado:

```json
{
  "change_id": "lab-node-001",
  "environment": "staging",
  "haproxy_version": "VERSAO_EXATA",
  "public_hosts": ["cdn.example.com"],
  "backend_health_host": "cdn.example.com",
  "tls_fullchain_source": "/caminho/root-only/fullchain.pem",
  "tls_private_key_source": "/caminho/root-only/privkey.pem",
  "backends": [
    {"node_id": "2", "name": "edge2", "address": "IP_EDGE", "port": 443}
  ]
}
```

`active` não existe nesse processador. A role recusa `promote` sem confirmação,
lease UUID e fencing token positivo; a topologia autoritativa valida lease,
expiração, holder e exclusividade do LB ativo.

## Publicação

1. executar toda a suíte;
2. gerar `node-package/manifest.json` com a tag final;
3. executar novamente a suíte e `git diff --check`;
4. revisar segredos e arquivos de laboratório;
5. criar commit e tag sem movê-la depois;
6. publicar branch e tag;
7. homologar em VPS descartável fora de DNS;
8. se falhar, corrigir e criar uma nova tag — nunca mover a anterior.
