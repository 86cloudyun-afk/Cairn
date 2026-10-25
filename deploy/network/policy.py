#!/usr/bin/env python3
"""Cairn's Linux Docker network guard. Requires only Python's standard library.

The generated nft transaction owns only inet/cairn_tasks and
bridge/cairn_tasks_l2. It never changes Docker's chains or flushes the ruleset.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


DENIED_NETWORKS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/3",
)
REMOVE_TABLES = (
    "add table inet cairn_tasks\ndelete table inet cairn_tasks\n"
    "add table bridge cairn_tasks_l2\ndelete table bridge cairn_tasks_l2\n"
)


@dataclass(frozen=True)
class Endpoint:
    address: str
    port: int
    protocol: str = "tcp"


@dataclass(frozen=True)
class Config:
    network_name: str
    bridge_name: str
    subnet: str
    gateway: str
    dns_servers: tuple[str, ...]
    https_proxies: tuple[Endpoint, ...]
    host_owned_networks: tuple[str, ...]
    host_services: tuple[Endpoint, ...]


def ipv4(value: str, *, public: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("an IPv4 address must be a string")
    address = ipaddress.IPv4Address(value)
    # is_global includes multicast; it does not mean publicly routable unicast.
    # DNS/proxy endpoints must also avoid multicast, which the guard denies.
    if public and (not address.is_global or address.is_multicast):
        raise ValueError(f"DNS/proxy address must be public IPv4: {value}")
    return str(address)


def networks(values) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("networks must be a list of IPv4 addresses or CIDRs")
    parsed = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("networks must contain strings")
        parsed.append(ipaddress.IPv4Network(value, strict=False))
    return tuple(str(network) for network in ipaddress.collapse_addresses(parsed))


def parse_allowlist(text: str) -> tuple[str, ...]:
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        entry = line.partition("#")[0].strip()
        if entry:
            try:
                entries.extend(networks([entry]))
            except ValueError as exc:
                raise ValueError(f"allowlist line {number}: {exc}") from exc
    return networks(entries)


def endpoint(value, *, gateway: str | None = None) -> Endpoint:
    expected = {"address", "port"} if gateway is None else {"address", "port", "protocol"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"endpoint must have exactly {sorted(expected)}")
    address = ipv4(value["address"], public=gateway is None)
    port = value["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("endpoint port must be an integer between 1 and 65535")
    protocol = value.get("protocol", "tcp")
    if protocol not in ("tcp", "udp"):
        raise ValueError("host service protocol must be tcp or udp")
    if gateway is not None and address != gateway:
        raise ValueError("host services must bind to the task bridge gateway address")
    return Endpoint(address, port, protocol)


def parse_config(data) -> Config:
    fields = set(Config.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError(f"network configuration must have exactly {sorted(fields)}")
    for name, maximum in (("network_name", 63), ("bridge_name", 15)):
        value = data[name]
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value) or len(value) > maximum:
            raise ValueError(f"invalid {name}")
    subnet = ipaddress.IPv4Network(data["subnet"], strict=True)
    private_blocks = tuple(ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if subnet.prefixlen > 29 or not any(subnet.subnet_of(block) for block in private_blocks):
        raise ValueError("task subnet must be an RFC1918 IPv4 network with at least eight addresses")
    gateway = ipv4(data["gateway"])
    if ipaddress.IPv4Address(gateway) not in subnet or gateway in (str(subnet.network_address), str(subnet.broadcast_address)):
        raise ValueError("gateway must be a usable address in the task subnet")
    if not isinstance(data["dns_servers"], list):
        raise ValueError("dns_servers must be a list")
    dns = tuple(sorted({ipv4(value, public=True) for value in data["dns_servers"]}))
    if not dns:
        raise ValueError("at least one public DNS server is required")
    for key in ("https_proxies", "host_services"):
        if not isinstance(data[key], list):
            raise ValueError(f"{key} must be a list")
    return Config(
        data["network_name"], data["bridge_name"], str(subnet), gateway, dns,
        tuple(endpoint(value) for value in data["https_proxies"]),
        networks(data["host_owned_networks"]),
        tuple(endpoint(value, gateway=gateway) for value in data["host_services"]),
    )


def address_set(name: str, values: tuple[str, ...], *, interval=False) -> str:
    flags = " flags interval;" if interval else ""
    elements = " elements = { " + ", ".join(values) + " };" if values else ""
    return f"    set {name} {{ type ipv4_addr;{flags}{elements} }}"


def render_rules(config: Config, allowlist: tuple[str, ...], host_addresses) -> str:
    denied = networks([*DENIED_NETWORKS, *config.host_owned_networks, *host_addresses])
    proxy_rules = "\n".join(f"        ip daddr {item.address} tcp dport {item.port} accept" for item in config.https_proxies)
    host_rules = "\n".join(f"        ip daddr {item.address} {item.protocol} dport {item.port} accept" for item in config.host_services)
    # ACCEPT at this priority only permits traversal of later Docker/host chains;
    # DROP is terminal across all base chains, including Docker's later ACCEPTs.
    return f'''# Generated by Cairn; apply as ONE nft transaction.
{REMOVE_TABLES}table inet cairn_tasks {{
{address_set("denied4", denied, interval=True)}
{address_set("dns4", config.dns_servers)}
{address_set("allowed4", allowlist, interval=True)}
    chain denied {{
        limit rate 6/minute burst 10 packets log prefix "cairn-drop " level warn
        counter drop
    }}
    chain task_egress {{
        meta nfproto ipv6 jump denied
        ip saddr != {config.subnet} jump denied
        ct state invalid jump denied
        ip daddr @denied4 jump denied
        ip daddr @dns4 udp dport 53 accept
        ip daddr @dns4 tcp dport 53 accept
{proxy_rules}
        ip daddr @allowed4 accept
        jump denied
    }}
    chain task_input {{
        meta nfproto ipv6 jump denied
        ip saddr != {config.subnet} jump denied
        ct state invalid jump denied
{host_rules}
        jump denied
    }}
    chain task_ingress {{
        meta nfproto ipv6 jump denied
        ct state established,related accept
        jump denied
    }}
    chain forward_guard {{
        type filter hook forward priority -10; policy accept;
        iifname "{config.bridge_name}" jump task_egress
        oifname "{config.bridge_name}" jump task_ingress
    }}
    chain input_guard {{
        type filter hook input priority -10; policy accept;
        iifname "{config.bridge_name}" jump task_input
    }}
}}
table bridge cairn_tasks_l2 {{
    chain denied {{
        limit rate 6/minute burst 10 packets log prefix "cairn-l2-drop " level warn
        counter drop
    }}
    chain bridge_input {{
        type filter hook input priority -200; policy accept;
        meta ibrname "{config.bridge_name}" ether type ip6 jump denied
        meta ibrname "{config.bridge_name}" ether type != {{ ip, arp }} jump denied
    }}
    chain bridge_forward {{
        type filter hook forward priority -200; policy accept;
        meta ibrname "{config.bridge_name}" jump denied
        meta obrname "{config.bridge_name}" jump denied
    }}
}}
'''


def command(run, argv, *, input=None) -> str:
    return run(argv, input=input, text=True, capture_output=True, check=True).stdout


def validate_local_docker(run) -> None:
    # The packet hooks and IP discovery run on this machine. A remote Docker
    # context would otherwise create an entirely unguarded task network there.
    selected_host = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    endpoint = selected_host or command(run, ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]).strip()
    if not endpoint.startswith("unix:///"):
        raise ValueError("Docker must use a local Unix socket on the firewall host")


def inspect_network(config: Config, run):
    names = command(run, ["docker", "network", "ls", "--filter", f"name=^{config.network_name}$", "--format", "{{.Name}}"])
    if config.network_name not in names.splitlines():
        return None
    result = json.loads(command(run, ["docker", "network", "inspect", config.network_name]))
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError("Docker did not return one network")
    return result[0]


def validate_guard_identity(config: Config, run) -> None:
    """Do not orphan an installed guard when configuration identity changes.

    Kernel rules and Docker bridge ownership survive loss of /run state. Read
    those authorities before replacing or deleting our fixed-name tables.
    """
    data = json.loads(command(run, ["nft", "--json", "list", "ruleset"]))
    if not isinstance(data, dict) or not isinstance(data.get("nftables"), list):
        raise ValueError("cannot establish installed network guard identity")
    tables = {("inet", "cairn_tasks"), ("bridge", "cairn_tasks_l2")}
    present = set()
    bridges = {table: set() for table in tables}
    for item in data["nftables"]:
        table = item.get("table", {})
        identity = (table.get("family"), table.get("name"))
        if identity in tables:
            present.add(identity)
        rule = item.get("rule", {})
        identity = (rule.get("family"), rule.get("table"))
        if identity not in tables:
            continue
        for expression in rule.get("expr", []):
            match = expression.get("match", {})
            key = match.get("left", {}).get("meta", {}).get("key")
            if key in ("iifname", "oifname", "ibrname", "obrname"):
                value = match.get("right")
                if not isinstance(value, str):
                    raise ValueError("cannot establish installed network guard identity")
                bridges[identity].add(value)
    if any(bridges[table] != {config.bridge_name} for table in present):
        raise ValueError("installed network guard identity differs; restore the previous configuration before teardown")

    names = command(run, ["docker", "network", "ls", "--filter", "driver=bridge", "--format", "{{.Name}}"]).splitlines()
    if names:
        networks = json.loads(command(run, ["docker", "network", "inspect", *names]))
        for network in networks:
            bridge = network.get("Options", {}).get("com.docker.network.bridge.name")
            if bridge == config.bridge_name and network.get("Name") != config.network_name:
                raise ValueError("Docker bridge network identity differs; restore the previous configuration before teardown")


def validate_network(config: Config, network) -> None:
    if network.get("Name") != config.network_name or network.get("Driver") != "bridge" or network.get("Internal"):
        raise ValueError("existing network must be the configured independent Docker bridge")
    if network.get("EnableIPv6") is not False:
        raise ValueError("existing task network must have IPv6 disabled")
    if network.get("IPAM", {}).get("Config") != [{"Subnet": config.subnet, "Gateway": config.gateway}]:
        raise ValueError("existing task network subnet/gateway differs from configuration")
    expected = {
        "com.docker.network.bridge.name": config.bridge_name,
        "com.docker.network.bridge.enable_icc": "false",
        "com.docker.network.bridge.enable_ip_masquerade": "true",
    }
    if any(network.get("Options", {}).get(key) != value for key, value in expected.items()):
        raise ValueError("existing task network bridge options differ from configuration")


def host_addresses(run) -> tuple[str, ...]:
    interfaces = json.loads(command(run, ["ip", "-j", "-4", "address", "show"]))
    return tuple(ipv4(item["local"]) for interface in interfaces for item in interface.get("addr_info", []) if item.get("family") == "inet")


def validate_daemon_dns(config: Config, path: Path) -> None:
    data = json.loads(Path(path).read_text())
    configured = data.get("dns") if isinstance(data, dict) else None
    if not isinstance(configured, list) or not configured:
        raise ValueError("Docker daemon DNS must explicitly list allowed public DNS servers")
    for address in configured:
        if ipv4(address, public=True) not in config.dns_servers:
            raise ValueError("Docker daemon DNS must use only policy dns_servers")


def apply_rules(rules: str, run) -> None:
    command(run, ["nft", "--check", "--file", "-"], input=rules)
    command(run, ["nft", "--file", "-"], input=rules)


def save_policy(state_dir: Path, rules: str) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".policy-", dir=state_dir)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(rules)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, state_dir / "network.nft")
    finally:
        Path(name).unlink(missing_ok=True)


def enable(config_path, allowlist_path, daemon_path, *, state_dir, run=subprocess.run) -> None:
    config = parse_config(json.loads(Path(config_path).read_text()))
    allowed = parse_allowlist(Path(allowlist_path).read_text())
    validate_daemon_dns(config, Path(daemon_path))
    validate_local_docker(run)
    validate_guard_identity(config, run)
    network = inspect_network(config, run)
    if network is not None:
        validate_network(config, network)
    else:
        interfaces = json.loads(command(run, ["ip", "-j", "link", "show"]))
        if any(item.get("ifname") == config.bridge_name for item in interfaces):
            raise ValueError(f"task bridge interface {config.bridge_name} already exists without the configured Docker network")
    rules = render_rules(config, allowed, host_addresses(run))
    # Protect the interface name before Docker creates or attaches any endpoint.
    apply_rules(rules, run)
    save_policy(Path(state_dir), rules)
    if network is None:
        command(run, [
            "docker", "network", "create", "--driver", "bridge",
            "--subnet", config.subnet, "--gateway", config.gateway,
            "--opt", f"com.docker.network.bridge.name={config.bridge_name}",
            "--opt", "com.docker.network.bridge.enable_icc=false",
            "--opt", "com.docker.network.bridge.enable_ip_masquerade=true", config.network_name,
        ])
        validate_network(config, inspect_network(config, run))
    if command(run, ["sysctl", "-n", "net.ipv4.ip_forward"]).strip() != "1":
        raise ValueError("net.ipv4.ip_forward must be 1; guards remain installed")
    command(run, ["sysctl", "-w", f"net.ipv6.conf.{config.bridge_name}.disable_ipv6=1"])
    command(run, ["sysctl", "-w", f"net.ipv6.conf.{config.bridge_name}.accept_ra=0"])


def disable(config_path, *, state_dir, run=subprocess.run) -> None:
    config = parse_config(json.loads(Path(config_path).read_text()))
    validate_local_docker(run)
    validate_guard_identity(config, run)
    network = inspect_network(config, run)
    if network is not None:
        validate_network(config, network)
        if network.get("Containers"):
            raise ValueError("task containers are still attached; remove/detach them before disabling")
        command(run, ["docker", "network", "rm", config.network_name])
    # Never remove the guard while a task network can still carry traffic.
    apply_rules(REMOVE_TABLES, run)
    (Path(state_dir) / "network.nft").unlink(missing_ok=True)


@contextmanager
def exclusive_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "policy.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "enable", "disable"))
    parser.add_argument("--config", type=Path, default=Path("/etc/cairn/network.json"))
    parser.add_argument("--allowlist", type=Path, default=Path("/etc/cairn/allowlist.txt"))
    parser.add_argument("--daemon-config", type=Path, default=Path("/etc/docker/daemon.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("/run/cairn"))
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            config = parse_config(json.loads(args.config.read_text()))
            print(render_rules(config, parse_allowlist(args.allowlist.read_text()), ()), end="")
            return 0
        if os.geteuid() != 0:
            raise ValueError("enable/disable must run as root on the Linux Docker host")
        with exclusive_lock(args.state_dir):
            if args.action == "enable":
                enable(args.config, args.allowlist, args.daemon_config, state_dir=args.state_dir)
            else:
                disable(args.config, state_dir=args.state_dir)
        print(f"Cairn task network guard: {args.action} complete")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        print(f"Cairn task network guard: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
