# Menu unificado operacional

O comando `mago-cdn` oferece uma única central em português do Brasil. O menu
anterior não é mais aberto como uma segunda interface: suas funções de perfil
XUI foram incorporadas ao menu principal e continuam usando a implementação
estável existente em `/opt/cdnmnus-panel/panel.py`.

## Organização

```text
Mago CDN — Central de Operações
├── Visão geral e situação do ambiente
├── Infraestrutura e distribuição
│   ├── Edges — cadastro, nomes, estado e drenagem
│   ├── DNS e matriz de distribuição
│   └── Implantações, versões e execução sequencial
├── XUIs, domínios e conteúdo
│   ├── Nova arquitetura: XUIs, tenants e domínios
│   ├── Fontes VOD isoladas por XUI
│   └── Configuração XUI atual
│       ├── Visão geral e consulta
│       ├── Cadastrar, editar e excluir perfil
│       └── Validar e recarregar o proxy atual
└── Operação e acesso
    ├── Serviços, diagnóstico e Nginx
    └── Acesso ao painel web e porta local
```

## Compatibilidade e segurança

- IDs técnicos são numéricos e monotônicos; aliases Ansible/SSH históricos são
  preservados internamente para não quebrar automações.
- Salvar/excluir perfil XUI ainda chama as funções estáveis do painel atual.
- O recarregamento do proxy continua condicionado a `nginx -t` aprovado.
- Ações de estado, implantação e exclusão continuam exigindo confirmação.
- O arquivo `/usr/local/bin/mago-cdn-legacy` foi preservado como contingência;
  o fluxo normal não precisa mais abri-lo.

Os termos apresentados ao operador foram padronizados em português do Brasil.
Identificadores internos como `ready`, `bootstrapping` e `deployment` continuam
inalterados no banco para não quebrar contratos existentes, mas aparecem como
“pronta”, “em preparação” e “implantação” na interface.
