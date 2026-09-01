# Transição auditada e rollback explícito do canário

O ID técnico atual da `.168` é `2`. O nome operacional é `edge1`; o parâmetro
`--limit edge1` usa o alias Ansible atual e não representa o ID do nó. A `.170`
é `edge2`. Os Load Balancers são `.111` e `.237`.
Estes controles não alteram DNS. Execute sempre com `--limit edge1`, depois de
confirmar que `.168` está drenada e que `.111/.170` suportam o tráfego.

## Estado `bootstrapping` para `ready`

`Database.set_edge_state()` preserva os três argumentos posicionais antigos,
mas agora valida o grafo de estados e grava `edge_events` na mesma transação.
Novos chamadores devem fornecer `operator`, `reason` e somente payload sem
segredos. Chaves com nomes de segredo são removidas e URLs perdem query string.

Exemplo local (não executar antes dos gates de preflight, mídia e rollback):

```python
db.set_edge_state(
    "2", "ready",
    operator="operador-identificado",
    reason="preflight, hashes e health aprovados",
    payload={"release_id": "RELEASE_APROVADA", "change_id": "CHG-..."},
)
```

## Rollback explícito

Registre primeiro o symlink atual, o ID/digest da release anterior e os tenants.
O comando exige o ID atual esperado para impedir rollback baseado em estado
obsoleto. A release de destino deve estar sincronizada e passar pelo verificador.

```bash
ANSIBLE_CONFIG=/opt/cdnmnus/ansible/ansible.cfg \
/opt/cdnmnus/venv/bin/ansible-playbook \
  -i ansible/inventories/production/hosts.yml \
  ansible/playbooks/rollback-edge.yml --limit edge1 \
  --extra-vars @/caminho/0600/rollback-edge1.json
```

O JSON `0600` contém `expected_current_release_id`, `release_id` (destino),
`config_digest`, `canonical_health_host`, `tenant_ids`, `vod_tenant_ids` e
`tenant_health_hosts`. Não inclua senha, token ou URL de mídia.

O playbook falha fechado se o estado ativo mudou, o manifesto diverge, a release
é a mesma ou qualquer health falha. A ativação reutilizada preserva symlink,
units, serviços e `current.json` anteriores e os restaura automaticamente em
caso de falha. Só considere o rollback aprovado após `nginx -t`, cinco health
checks e reprodução/seek reais, mantendo a edge fora do pool durante o ensaio.
