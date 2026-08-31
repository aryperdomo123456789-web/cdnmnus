# HAProxy/LB — laboratório isolado

Esta frente não autoriza acesso às edges reais, DNS ou firewall. Em
`load_balancer_environment=laboratory`, o preflight falha se qualquer backend
não for `127.0.0.1`, `::1` ou `localhost`.

## Fluxo

Use dois HTTP servers falsos locais, em portas distintas, respondendo `200` em
`/edge-health`. Defina sempre uma versão exata do pacote HAProxy.

```bash
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=preflight -e @lab-lb-vars.yml
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=deploy -e @lab-lb-vars.yml
```

`deploy` só escreve `haproxy.cfg.candidate` e executa `haproxy -c`; não altera
`haproxy.cfg` nem recarrega o serviço. Confira o digest de ambos antes de
promover. As ações abaixo mudam estado e exigem confirmação explícita:

```bash
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=promote -e load_balancer_operation_confirm=true -e @lab-lb-vars.yml
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=drain -e load_balancer_operation_confirm=true -e @lab-lb-vars.yml
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=demote -e load_balancer_operation_confirm=true -e @lab-lb-vars.yml
ansible-playbook ansible/playbooks/load-balancer.yml -l lb_lab \
  -e load_balancer_action=rollback -e load_balancer_operation_confirm=true \
  -e load_balancer_rollback_change_id=CHANGE_ANTERIOR -e @lab-lb-vars.yml
```

O promote revalida o candidato imediatamente antes de publicá-lo. Falha no
reload restaura o arquivo anterior; se ele não existia, o HAProxy fica parado.
Rollback também é validado antes da publicação. Drain usa o runtime socket e
preserva conexões existentes durante a janela configurada.

O modo deve acompanhar a ação: `active` para promote, `draining` para drain e
`standby` ou `disabled` para demote. Combinações inconsistentes falham no
preflight antes de tocar o host.

O template exige Host permitido, TLS/SNI estrito, health check em
`/edge-health`, slow-start e limites de conexão. O formato de log exclui URI,
query string, cookies e Authorization.
