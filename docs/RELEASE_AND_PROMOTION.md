# Releases, papéis e promoção de edge

O GitHub guarda apenas código, playbooks, testes e documentação. Inventários
reais, chaves SSH, tokens, banco SQLite e certificados ficam exclusivamente no
control node (`/etc/cdnmnus`) e nunca são commitados.

## Branches e tags

- `main`: desenvolvimento e revisão.
- `production/YYYYMMDD`: snapshot aprovado para produção.
- `edge/YYYYMMDD`: mesmo artefato da produção, usado no rollout das edges.
- `vX.Y.Z`: tag imutável do artefato que foi instalado.

O `release_id` e o `config_digest` do manifesto devem ser iguais em LB, edge
primária e edge secundária. O inventário define apenas o destino; não altera o
artefato.

## Pré-requisitos da VPS antes da senha

Antes de abrir o menu para uma nova edge, confirme no console do provedor:

- Ubuntu 20.04, 22.04 ou 24.04 64-bit, com pelo menos 1 vCPU, 1 GB RAM e 10 GB livres;
- IPv4 público dedicado e horário/NTP correto;
- SSH acessível na porta escolhida, com `root` (ou usuário com `sudo`) e senha
  inicial temporária;
- saída TCP para a origem XUI/LB e para os repositórios Ubuntu;
- portas TCP 22, 80 e 443 permitidas no firewall do provedor;
- console/VNC de recuperação disponível antes de ativar UFW.

O bootstrap não grava a senha. Ele cria `cdn-deploy`, instala a chave Ed25519,
valida o fingerprint confirmado pelo operador e então o orquestrador instala o
runtime. O certificado TLS precisa ser distribuído/emitido antes de a edge ser
publicada no DNS.

## Instalação por papel

No control node, copie o inventário local a partir do exemplo, preencha os IPs e
use a mesma tag aprovada:

```bash
git switch --detach vX.Y.Z
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/preflight-edge.yml
ansible-playbook -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/activate-edge.yml
```

O grupo `cdn_edges` pode conter `lb`, `edge_primary` e `edge_secondary`; o
playbook é serial (`serial: 1`) e falha fechado.

## Promoção secundária

Promoção não é um `git switch` no servidor e não deve depender de login manual:

1. congelar mudanças e registrar o `release_id`/digest atual;
2. confirmar cinco respostas `200` consecutivas em `HTTPS /edge-health` da
   secundária usando `--resolve`;
3. confirmar que origem/LB aceitam a secundária e que ela serve o mesmo hostname;
4. retirar a primária do pool no DNS/LB autorizado (mudança manual quando o DNS
   estiver em modo DNS-only);
5. verificar novos segmentos e VOD na secundária;
6. só depois reparar a primária e reinseri-la após cinco health checks.

Sem um controlador de DNS/LB com health check, o projeto não promete failover
automático: o operador deve executar os passos 4–6 e registrar horário, TTL e
impacto. Nunca publicar um segundo A record como se isso fosse failover.

## Rollback

Rollback é feito para o último `release_id` aprovado no próprio host, com
`nginx -t`, reload e cinco health checks. Se o digest divergir, a edge permanece
fora do pool até nova sincronização.
