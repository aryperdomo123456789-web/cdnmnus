from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.topology import TopologyStore


ROOT = Path(__file__).parents[1]


class NodePackageTest(unittest.TestCase):
    def test_manifest_is_closed_and_verifiable(self) -> None:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            result = subprocess.run(
                [str(ROOT / "scripts/build_node_package_manifest.py"),
                 "--ref", "v9.9.9-test",
                 "--output", str(manifest)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            verified = subprocess.run(
                ["python3", str(ROOT / "node-package/verify.py"), str(ROOT),
                 str(manifest), "v9.9.9-test", commit],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            data = json.loads(manifest.read_text())
            self.assertNotIn("source_commit", data)
            self.assertIn("panel/vod_relay.py", data["files"])
            self.assertIn("panel/multi_tenant_broker.py", data["files"])
            self.assertNotIn("node-package/manifest.json", data["files"])

    def test_installer_is_pinned_and_rollbackable(self) -> None:
        installer = (ROOT / "node-package/install.sh").read_text()
        bootstrap = (ROOT / "install-managed-node-from-github.sh").read_text()
        self.assertIn("use uma tag imutável; main/branch são recusados", installer)
        self.assertIn("manifest-digest", installer)
        self.assertIn("rollback_install", installer)
        self.assertIn("systemctl disable --now haproxy", installer)
        self.assertIn("MENU_SOURCE_RELATIVE=ansible/roles/node_menu/files/node_menu.py", installer)
        self.assertIn("menu instalado diverge do manifesto autorizado", installer)
        self.assertIn("git clone --quiet --depth 1 --branch", bootstrap)
        self.assertIn("actual_commit", bootstrap)

    def test_menu_distribution_uses_the_same_source(self) -> None:
        role = (ROOT / "ansible/roles/node_menu/tasks/main.yml").read_text()
        installer = (ROOT / "node-package/install.sh").read_text()
        self.assertIn("src: node_menu.py\n    dest: /usr/local/lib/cdnmnus-node-menu.py", role)
        self.assertIn("MENU_SOURCE_RELATIVE=ansible/roles/node_menu/files/node_menu.py", installer)
        self.assertIn("MENU_TARGET=/usr/local/lib/cdnmnus-node-menu.py", installer)

    def test_menu_only_requests_and_control_plane_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "admin.db")
            database.initialize(); topology = TopologyStore(database); topology.initialize()
            database.add_edge(
                "9", "Edge laboratório", "1.1.1.1", 22, "cdn-deploy",
                "SHA256:test", "ready",
            )
            request = database.request_load_balancer_promotion(
                "9", "standby", "v1.2.3", "a" * 40, "b" * 64,
                "1.1.1.1", "homologação de promoção",
            )
            self.assertEqual(request["state"], "requested")
            self.assertEqual(topology.node("9")["role"], "edge")
            self.assertEqual(topology.node("9")["state"], "ready")
            with self.assertRaises(Exception):
                database.request_load_balancer_promotion(
                    "9", "standby", "v1.2.3", "a" * 40, "b" * 64,
                    "1.1.1.1", "duplicada",
                )
            database.add_edge(
                "10", "Backend laboratório", "8.8.8.8", 22, "cdn-deploy",
                "SHA256:backend", "ready",
            )
            database.set_promotion_request_state(request["id"], "approved")
            database.set_edge_state(
                "9", "draining", operator="test", reason="drain de teste"
            )
            database.set_promotion_request_state(request["id"], "installing")
            lb = database.finalize_load_balancer_candidate(
                request["id"], "lb-9", ["10"], "test", "candidato validado"
            )
            self.assertEqual(lb["state"], "standby")
            self.assertEqual(database.edge("9")["state"], "disabled")
            self.assertEqual(topology.node("9")["role"], "load_balancer")
            self.assertEqual(topology.node("9")["state"], "standby")
        menu = (ROOT / "ansible/roles/node_menu/files/node_menu.py").read_text()
        self.assertIn("Promover esta Edge para Load Balancer", menu)
        self.assertIn("Cadastrar nova máquina (Edge ou Load Balancer)", menu)
        self.assertIn("Nova Edge — cadastro mínimo IP/SSH", menu)
        self.assertIn('name = "edge-"', menu)
        self.assertIn('role = "edge"', menu)
        control_menu = (ROOT / "cli/mago_cdn.py").read_text()
        self.assertIn("Adicionar nova Edge (cadastro mínimo: IP/SSH)", control_menu)
        self.assertIn('role="edge"', control_menu)
        self.assertIn('name = "edge-"', control_menu)
        self.assertIn('ask("Usuário SSH inicial", "root")', control_menu)
        self.assertIn("Capacidade, consumo e saúde do cluster", menu)
        self.assertIn("Definir perfil contratado desta VPS", menu)
        self.assertIn("Failover manual do controlador DNS", menu)
        self.assertIn("cdnmnus-submit-node-onboarding", menu)
        self.assertIn("cdnmnus-submit-capacity-sample", menu)
        self.assertIn("cdnmnus-submit-capacity-profile", menu)
        self.assertIn("cdnmnus-cluster-status", menu)
        self.assertIn("Testar reprodução pelo CNAME DNS-only", menu)
        self.assertIn("Testar reprodução pelo CNAME DNS-only", menu)
        self.assertIn("cdnmnus-submit-promotion-request", menu)
        self.assertNotIn("systemctl enable haproxy", menu)
        processor = (ROOT / "scripts/process_promotion_request.py").read_text()
        self.assertNotIn('"load_balancer_action": "promote"', processor)
        self.assertIn('"load_balancer_action": "deploy"', processor)
        self.assertIn('ROOT / "venv/bin/ansible-playbook"', processor)
        self.assertIn('ansible_environment["ANSIBLE_CONFIG"]', processor)
        lb_playbook = (ROOT / "ansible/playbooks/load-balancer.yml").read_text()
        self.assertIn("Publicar identidade LB somente após candidato HAProxy válido", lb_playbook)
        self.assertIn("cdnmnus_node_role: load_balancer", lb_playbook)
        node_menu_tasks = (ROOT / "ansible/roles/node_menu/tasks/main.yml").read_text()
        node_menu_defaults = (ROOT / "ansible/roles/node_menu/defaults/main.yml").read_text()
        self.assertIn("cdnmnus-lb-candidate-preflight", node_menu_tasks)
        self.assertIn("cdnmnus-reconcile-managed-release.py", node_menu_tasks)
        production_inventory = (ROOT / "ansible/inventories/production/hosts.yml").read_text()
        self.assertIn('cdnmnus_control_plane_host: ""', node_menu_defaults)
        self.assertIn("cdnmnus_control_plane_host | length > 0", node_menu_tasks)
        self.assertIn("cdnmnus_node_role != 'load_balancer' or cdnmnus_control_plane_host != ansible_host", node_menu_tasks)
        self.assertIn("lb_candidate_237:", production_inventory)
        self.assertIn("ansible_host: 45.140.192.237", production_inventory)
        self.assertIn("cdnmnus_node_state: standby", production_inventory)
        preflight = (ROOT / "scripts/lb_candidate_preflight.py").read_text()
        preflight_entrypoint = (ROOT / "scripts/cdnmnus-lb-candidate-preflight").read_text()
        self.assertIn("promotion_allowed", preflight)
        self.assertIn("fencing externo não configurado", preflight)
        self.assertIn("external DNS could not be verified", preflight)
        self.assertIn("connection_refused", preflight)
        self.assertIn("haproxy_process_running", preflight)
        self.assertIn('run_path("/opt/cdnmnus/scripts/lb_candidate_preflight.py", run_name="__main__")', preflight_entrypoint)
        self.assertNotIn("systemctl start", preflight)
        self.assertNotIn("systemctl enable", preflight)
        self.assertNotIn("systemctl reload", preflight)
        self.assertNotIn("upsert(", preflight)
        self.assertNotIn("delete_records(", preflight)
        lb_template = (ROOT / "ansible/roles/load_balancer/templates/haproxy.cfg.j2").read_text()
        self.assertIn("{{ '\\n' }}", lb_template)
        acme_helper = (ROOT / "scripts/cdnmnus-acme-helper").read_text()
        acme_sudoers = (ROOT / "ansible/roles/node_menu/files/cdnmnus-acme.sudoers").read_text()
        self.assertIn("--dns-cloudflare-credentials", acme_helper)
        self.assertIn("--quiet", acme_helper)
        self.assertIn("hostname_re=", acme_helper)
        self.assertIn("plugin dns-cloudflare ausente", acme_helper)
        self.assertIn("sudo -u cdn-admin test -r", acme_helper)
        self.assertIn("/opt/cdnmnus/scripts/cdnmnus-acme-helper *", acme_sudoers)
        self.assertNotIn("eval ", acme_helper)
        tenant_tasks = (ROOT / "ansible/roles/cdn_tenants/tasks/main.yml").read_text()
        self.assertIn("proxy_pass http://external_alias_vod;", tenant_tasks)
        self.assertIn("{% if external_alias_has_vod | default(false) %}", tenant_tasks)
        self.assertIn("return 503;", tenant_tasks)
        self.assertNotIn("location ~ ^/(?:hls|live|movie|series)/ {\n                  proxy_pass http://origin_{{ external_alias_tenant_id }};", tenant_tasks)
        self.assertGreaterEqual(tenant_tasks.count("proxy_pass http://broker_{{ external_alias_tenant_id }};"), 2)
        service = (ROOT / "orchestrator/cdnmnus-orchestrator.service").read_text()
        self.assertNotIn("NoNewPrivileges=true", service)
        self.assertIn("/etc/letsencrypt", service)
        self.assertIn("/var/lib/letsencrypt", service)
        self.assertIn("/var/log/letsencrypt", service)


if __name__ == "__main__":
    unittest.main()
