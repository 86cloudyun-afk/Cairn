"""The deployment policy is exercised without changing the host firewall or Docker."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy/network/policy.py"


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    spec = importlib.util.spec_from_file_location("cairn_network_policy", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_data():
    return {
        "network_name": "cairn-tasks",
        "bridge_name": "br-cairn",
        "subnet": "172.30.0.0/24",
        "gateway": "172.30.0.1",
        "dns_servers": ["1.1.1.1", "9.9.9.9"],
        "https_proxies": [{"address": "8.8.4.4", "port": 443}],
        "host_owned_networks": ["8.8.8.0/24"],
        "host_services": [{"address": "172.30.0.1", "protocol": "tcp", "port": 8000}],
    }


def test_allowlist_normalizes_addresses_and_rejects_non_ip_tokens(policy):
    assert policy.parse_allowlist("# target list\n8.8.8.8 # single\n8.8.8.9/24\n8.8.8.8\n") == ("8.8.8.0/24",)
    for bad in ("example.com", "8.8.8.8:443", "::/0", "8.8.8.8; accept", "8.8.8.8 1.1.1.1"):
        with pytest.raises(ValueError):
            policy.parse_allowlist(bad)


@pytest.mark.parametrize("field,value", [
    ("bridge_name", 'br-cairn"; accept'),
    ("dns_servers", ["127.0.0.1"]),
    ("dns_servers", ["192.168.0.1"]),
    ("dns_servers", ["::1"]),
    ("https_proxies", [{"address": "10.0.0.1", "port": 443}]),
    ("https_proxies", [{"address": "8.8.4.4", "port": 0}]),
    ("host_services", [{"address": "172.30.0.1", "protocol": "tcp", "port": True}]),
    ("host_services", [{"address": "127.0.0.1", "protocol": "tcp", "port": 22}]),
    ("host_services", [{"address": "172.30.0.1", "protocol": "all", "port": 22}]),
    ("gateway", "172.31.0.1"),
    ("subnet", "0.0.0.0/0"),
    ("typo_dns", []),
])
def test_invalid_configuration_is_rejected_before_firewall_changes(policy, field, value):
    data = config_data()
    data[field] = value
    with pytest.raises(ValueError):
        policy.parse_config(data)


@pytest.mark.parametrize("address", ["224.0.0.1", "239.255.255.250"])
@pytest.mark.parametrize("field", ["dns_servers", "https_proxies"])
def test_multicast_dns_and_proxy_endpoints_are_rejected(policy, field, address):
    data = config_data()
    data[field] = [address] if field == "dns_servers" else [{"address": address, "port": 443}]
    with pytest.raises(ValueError, match="public IPv4"):
        policy.parse_config(data)


def test_generated_policy_denies_host_private_ipv6_and_peers_before_allows(policy):
    config = policy.parse_config(config_data())
    rules = policy.render_rules(config, policy.parse_allowlist("0.0.0.0/0"), ["4.4.4.4"])
    assert "flush ruleset" not in rules
    assert "hook forward priority -10; policy accept" in rules
    assert "hook input priority -10; policy accept" in rules
    assert 'iifname "br-cairn" jump task_egress' in rules
    assert 'oifname "br-cairn" jump task_ingress' in rules
    assert 'meta ibrname "br-cairn" jump denied' in rules
    egress = rules.split("chain task_egress {", 1)[1].split("\n    }", 1)[0]
    assert egress.index("meta nfproto ipv6 jump denied") < egress.index("ip daddr @dns4")
    assert egress.index("ip saddr != 172.30.0.0/24 jump denied") < egress.index("ip daddr @dns4")
    assert egress.index("ip daddr @denied4 jump denied") < egress.index("ip daddr @allowed4 accept")
    assert "ct state established,related accept" not in egress
    for protected in ("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3", "8.8.8.0/24", "4.4.4.4/32"):
        assert protected in rules
    incoming_host = rules.split("chain task_input {", 1)[1].split("\n    }", 1)[0]
    assert "ip daddr 172.30.0.1 tcp dport 8000 accept" in incoming_host
    assert "@allowed4" not in incoming_host
    assert 'limit rate 6/minute burst 10 packets log prefix "cairn-drop " level warn\n' in rules
    assert rules.count("add table inet cairn_tasks") == 1
    assert rules.count("delete table inet cairn_tasks") == 1
    assert rules.count("add table bridge cairn_tasks_l2") == 1


class Host:
    """Simulates only the privileged executable boundaries, with persistent state."""

    def __init__(self):
        self.networks = {}
        self.applied = None
        self.created = 0
        self.fail_apply = False
        self.docker_endpoint = "unix:///var/run/docker.sock"
        self.interfaces = {"lo", "eth0"}
        self.commands = []

    @property
    def network(self):
        return self.networks.get("cairn-tasks")

    @network.setter
    def network(self, value):
        if value is None:
            self.networks.pop("cairn-tasks", None)
        else:
            self.networks["cairn-tasks"] = value

    def run(self, argv, *, input=None, **kwargs):
        self.commands.append((argv, input))
        output = ""
        if argv == ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]:
            output = self.docker_endpoint + "\n"
        elif argv == ["ip", "-j", "-4", "address", "show"]:
            output = json.dumps([{"addr_info": [{"family": "inet", "local": "4.4.4.4"}]}])
        elif argv == ["ip", "-j", "link", "show"]:
            output = json.dumps([{"ifname": name} for name in sorted(self.interfaces)])
        elif argv[:3] == ["docker", "network", "ls"]:
            names = list(self.networks)
            filters = [argv[index + 1] for index, value in enumerate(argv) if value == "--filter"]
            for value in filters:
                if value.startswith("name=^"):
                    names = [name for name in names if name == value[6:-1]]
            output = "\n".join(names)
        elif argv[:3] == ["docker", "network", "inspect"]:
            output = json.dumps([self.networks[name] for name in argv[3:]])
        elif argv[:3] == ["docker", "network", "create"]:
            self.created += 1
            options = dict(argv[index + 1].split("=", 1) for index, value in enumerate(argv) if value == "--opt")
            self.interfaces.add(options["com.docker.network.bridge.name"])
            self.networks[argv[-1]] = {
                "Name": argv[-1], "Driver": "bridge", "Internal": False,
                "EnableIPv6": False, "IPAM": {"Config": [{"Subnet": argv[argv.index("--subnet") + 1], "Gateway": argv[argv.index("--gateway") + 1]}]},
                "Options": options,
                "Containers": {},
            }
            output = "fake-network-id\n"
        elif argv[:3] == ["docker", "network", "rm"]:
            removed = self.networks.pop(argv[3])
            self.interfaces.discard(removed["Options"]["com.docker.network.bridge.name"])
        elif argv == ["sysctl", "-n", "net.ipv4.ip_forward"]:
            output = "1\n"
        elif argv[:2] == ["sysctl", "-w"]:
            pass
        elif argv == ["nft", "--json", "list", "ruleset"]:
            entries = []
            bridge = re.search(r'iifname "([^"]+)"', self.applied or "")
            if bridge:
                for family, table, key in (("inet", "cairn_tasks", "iifname"), ("bridge", "cairn_tasks_l2", "ibrname")):
                    entries.extend([
                        {"table": {"family": family, "name": table}},
                        {"rule": {"family": family, "table": table, "chain": "forward_guard", "expr": [
                            {"match": {"op": "==", "left": {"meta": {"key": key}}, "right": bridge.group(1)}},
                            {"jump": {"target": "task_egress"}},
                        ]}},
                    ])
            output = json.dumps({"nftables": entries})
        elif argv == ["nft", "--check", "--file", "-"]:
            assert input
        elif argv == ["nft", "--file", "-"]:
            if self.fail_apply:
                raise subprocess.CalledProcessError(1, argv, stderr="injected nft transaction failure")
            self.applied = input
        else:
            raise AssertionError(f"unexpected executable boundary: {argv!r}")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")


def setup_files(tmp_path):
    config_path = tmp_path / "network.json"
    config_path.write_text(json.dumps(config_data()))
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("8.8.8.8\n")
    daemon = tmp_path / "daemon.json"
    daemon.write_text('{"dns":["1.1.1.1","9.9.9.9"]}')
    return config_path, allowlist, daemon


@pytest.mark.parametrize("lose_runtime_state", [False, True])
def test_enable_is_idempotent_and_guards_exist_before_network_creation(policy, tmp_path, lose_runtime_state):
    files = setup_files(tmp_path)
    host = Host()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    if lose_runtime_state:
        (tmp_path / "state/network.nft").unlink()
        (tmp_path / "state").rmdir()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.created == 1
    assert (tmp_path / "state/network.nft").read_text() == host.applied
    apply = next(i for i, (argv, _) in enumerate(host.commands) if argv == ["nft", "--file", "-"])
    create = next(i for i, (argv, _) in enumerate(host.commands) if argv[:3] == ["docker", "network", "create"])
    assert apply < create
    assert not any("flush" in argv for argv, _ in host.commands)


def test_changed_network_and_bridge_cannot_replace_active_guard_after_runtime_state_loss(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    state_dir = tmp_path / "state"
    policy.enable(*files, state_dir=state_dir, run=host.run)
    host.network["Containers"] = {"task-id": {"Name": "active-task"}}
    previous_network = host.network
    previous_rules = host.applied
    for path in state_dir.iterdir():
        path.unlink()
    state_dir.rmdir()
    data = config_data()
    data.update(network_name="cairn-tasks-v2", bridge_name="br-cairn-v2")
    files[0].write_text(json.dumps(data))

    with pytest.raises(ValueError, match="identity"):
        policy.enable(*files, state_dir=state_dir, run=host.run)

    assert host.applied == previous_rules
    assert host.network is previous_network
    assert host.network["Containers"] == {"task-id": {"Name": "active-task"}}
    assert set(host.networks) == {"cairn-tasks"}
    assert host.created == 1
    assert "br-cairn-v2" not in host.interfaces
    assert not state_dir.exists()


def test_changed_network_name_cannot_disable_active_guard_after_runtime_state_loss(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    state_dir = tmp_path / "state"
    policy.enable(*files, state_dir=state_dir, run=host.run)
    host.network["Containers"] = {"task-id": {"Name": "active-task"}}
    previous_rules = host.applied
    for path in state_dir.iterdir():
        path.unlink()
    state_dir.rmdir()
    data = config_data()
    data["network_name"] = "cairn-tasks-v2"
    files[0].write_text(json.dumps(data))

    with pytest.raises(ValueError, match="identity"):
        policy.disable(files[0], state_dir=state_dir, run=host.run)

    assert host.applied == previous_rules
    assert host.network["Containers"] == {"task-id": {"Name": "active-task"}}
    assert "br-cairn" in host.interfaces
    assert not state_dir.exists()


@pytest.mark.parametrize("interface_name", ["br-cairn", "eth0"])
def test_existing_host_interface_collision_fails_before_any_mutation(policy, tmp_path, interface_name):
    files = setup_files(tmp_path)
    data = config_data()
    data["bridge_name"] = interface_name
    files[0].write_text(json.dumps(data))
    host = Host()
    host.interfaces.add(interface_name)
    with pytest.raises(ValueError, match="interface.*already exists"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.created == 0
    assert host.applied is None
    assert not (tmp_path / "state/network.nft").exists()
    assert not any(argv == ["nft", "--file", "-"] or argv[:2] == ["sysctl", "-w"] or
                   argv[:3] in (["docker", "network", "create"], ["docker", "network", "rm"])
                   for argv, _ in host.commands)


def test_enable_refuses_mismatched_existing_network_without_touching_rules(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    previous = host.applied
    host.network["EnableIPv6"] = True
    with pytest.raises(ValueError, match="IPv6"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.applied == previous


def test_failed_nft_transaction_does_not_publish_a_new_policy_or_create_network(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    host.fail_apply = True
    with pytest.raises(subprocess.CalledProcessError):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.applied is None
    assert not (tmp_path / "state/network.nft").exists()


def test_missing_or_host_loopback_daemon_dns_fails_closed(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    files[2].write_text('{"dns":["127.0.0.53"]}')
    with pytest.raises(ValueError, match="DNS"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.applied is None


def test_disable_refuses_attached_tasks_then_removes_only_owned_tables(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    previous = host.applied
    host.network["Containers"] = {"task-id": {"Name": "cairn-dispatch-project"}}
    with pytest.raises(ValueError, match="attached"):
        policy.disable(files[0], state_dir=tmp_path / "state", run=host.run)
    assert host.applied == previous
    host.network["Containers"] = {}
    policy.disable(files[0], state_dir=tmp_path / "state", run=host.run)
    policy.disable(files[0], state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.applied == "add table inet cairn_tasks\ndelete table inet cairn_tasks\nadd table bridge cairn_tasks_l2\ndelete table bridge cairn_tasks_l2\n"
    assert not (tmp_path / "state/network.nft").exists()


def test_reload_failure_preserves_previous_runtime_policy_and_firewall(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    previous = host.applied
    files[1].write_text("8.8.4.4\n")
    host.fail_apply = True
    with pytest.raises(subprocess.CalledProcessError):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.applied == previous
    assert (tmp_path / "state/network.nft").read_text() == previous


def test_invalid_reload_allowlist_never_replaces_installed_policy(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    previous = host.applied
    files[1].write_text("8.8.8.8; accept\n")
    with pytest.raises(ValueError, match="allowlist line 1"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.applied == previous
    assert (tmp_path / "state/network.nft").read_text() == previous


def test_remote_docker_context_fails_before_installing_a_local_only_guard(policy, tmp_path):
    files = setup_files(tmp_path)
    host = Host()
    host.docker_endpoint = "ssh://operator@other-host"
    with pytest.raises(ValueError, match="local"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.applied is None


def test_remote_docker_host_environment_fails_closed(policy, tmp_path, monkeypatch):
    files = setup_files(tmp_path)
    host = Host()
    monkeypatch.setenv("DOCKER_HOST", "tcp://other-host:2376")
    with pytest.raises(ValueError, match="local"):
        policy.enable(*files, state_dir=tmp_path / "state", run=host.run)
    assert host.network is None
    assert host.applied is None
