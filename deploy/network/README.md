# Linux Docker task network

This deployment creates an independent Docker bridge named `cairn-tasks`
(`br-cairn`, `172.30.0.0/24`, gateway `172.30.0.1`). Worker containers select
it through the existing `container.network_mode` setting. Do not select `host`.
The dispatcher remains trusted and may access its Cairn API and Docker socket;
only task containers join this restricted bridge.

The default policy permits UDP/TCP port 53 to the configured public DNS
servers. It permits TCP to configured public HTTPS proxy endpoints and traffic
to IPv4 addresses/CIDRs in `/etc/cairn/allowlist.txt`. All other task egress is
dropped. Allowlisted destinations permit all IPv4 protocols/ports, so scope
these entries to the authorized targets. An empty target file and empty proxy
list permit DNS only. A proxy entry opens only its IP and TCP port; configure
its CONNECT/domain/authentication policy on the proxy itself.

Before any destination allow, the policy drops RFC1918, link-local, loopback,
CGNAT, multicast/reserved and other non-routable IPv4 ranges, configured
`host_owned_networks`, and every IPv4 address currently assigned to the Docker
host. These denies also win over a broad allowlist such as `0.0.0.0/0` and over
existing task connections after a policy reload. `host_owned_networks` must
include any additional owned/routed ranges, including public management LANs
and VPN networks. Host address discovery refreshes when the service reloads;
reload after address or route changes.

Task-to-host INPUT is denied independently of forwarding, even if the host's
address is public or allowlisted. The only optional host exception is a
`host_services` tuple naming the bridge gateway, `tcp`/`udp`, and one port:

```json
"host_services": [{"address": "172.30.0.1", "protocol": "tcp", "port": 8000}]
```

That exception applies only to a service bound on the host gateway address;
it cannot open a LAN destination or another host port. Keep the list empty
unless tasks need that service. It does not override a Docker-published port
that DNATs to another private container: those packets enter FORWARD and are
still denied. Prefer keeping the API in the dispatcher rather than opening it
to tasks.

If a necessary native host service listens only on `127.0.0.1:8000`, expose
one gateway port through a trusted host-side forwarder. For example, install
`socat` on the host and set the following explicit port mapping in
`/etc/cairn/network.json`:

```json
"host_services": [{"address": "172.30.0.1", "protocol": "tcp", "port": 18000}]
```

After the bridge exists, reload the policy and start this fixed-destination
forwarder on the host (the command stays in the foreground):

```sh
sudo systemctl reload cairn-network.service
sudo socat TCP4-LISTEN:18000,bind=172.30.0.1,reuseaddr,fork,range=172.30.0.0/24 TCP4:127.0.0.1:8000
```

Tasks can then connect to `http://172.30.0.1:18000/`; other gateway ports remain
denied. The forwarder terminates the permitted host INPUT connection and opens
a separate connection to that one loopback service. It must run as a trusted
host service after `cairn-network.service` if it is made persistent. This does
not permit Docker DNAT/published-port forwarding into private containers;
those destinations remain denied by the FORWARD guard. Remove this host
exception and stop the forwarder when it is no longer needed.

All task IPv6 is dropped in both the inet and bridge families. The Docker
network also has IPv6 disabled; enable.sh disables IPv6 and router
advertisements on the bridge. A bridge-family guard drops all direct task peer
forwarding, including ARP and IPv6, independently of `br_netfilter` sysctls.
Only IPv4 and ARP can enter the host bridge. Docker's ICC option is also false.
New inbound forwarded connections to tasks are dropped; only conntrack return
traffic is accepted.

## Requirements and installation

Run on the Linux machine running the Docker daemon, using a rootful Docker
Engine with the bridge driver, Python 3.10+, `nft`, `ip` (iproute2), `sysctl`,
and systemd. Docker's iptables or native nftables firewall integration must
remain enabled, with bridge masquerading. Rootless Docker, Docker Desktop,
host/macvlan/overlay networking and arbitrary additional container networks
are outside this deployment. Choose a subnet that does not overlap local
routes/VPNs; defaults use `172.30.0.0/24`. The controller rejects TCP/SSH Docker
contexts and `DOCKER_HOST` settings: it must use a local Unix socket so the
Docker network and firewall hooks are on the same machine.

Install files from this repository checkout (normally `/opt/cairn`):

```sh
sudo install -d -m 0755 /usr/local/lib/cairn-network /etc/cairn
sudo install -m 0755 deploy/network/policy.py deploy/network/enable.sh deploy/network/disable.sh /usr/local/lib/cairn-network/
sudo install -m 0644 deploy/network/network.example.json /etc/cairn/network.json
sudo install -m 0644 deploy/network/allowlist.example.txt /etc/cairn/allowlist.txt
sudo install -m 0644 deploy/network/99-cairn-network.conf /etc/sysctl.d/99-cairn-network.conf
sudo install -m 0644 deploy/network/cairn-network.service /etc/systemd/system/
sudo sysctl --load=/etc/sysctl.d/99-cairn-network.conf
```

Edit `/etc/cairn/network.json` and `/etc/cairn/allowlist.txt` as root. Do not
make these files or the installed scripts writable by task users. Unknown
configuration keys, malformed IPs, hostnames, ports in target entries, private
DNS/proxy endpoints and IPv6 entries fail validation before firewall changes.

For an HTTPS proxy, add its actual public address and port, for example
`{"address":"<PUBLIC_IPV4>","port":443}`, to `https_proxies`. Set the matching
proxy hostname/URL in `WRAP_PROXY` for the curl/wget wrappers and in the
standard HTTP(S) proxy environment variables for other clients; see
[dispatch.example.yaml](dispatch.example.yaml). Track all current public proxy
addresses explicitly when DNS returns multiple addresses. Direct HTTPS is
still denied unless its destination is allowlisted. Set `WRAP_PROXY` to an
empty string when intentionally testing allowlisted targets directly; the
wrapper otherwise defaults to a container-local SOCKS endpoint.

Docker's embedded resolver `127.0.0.11` needs special care: a host-loopback
upstream can cause Docker to resolve in the host namespace, beyond this task
filter. Merge the `dns` array from
[docker-daemon-dns.example.json](docker-daemon-dns.example.json) into the host's
existing `/etc/docker/daemon.json`; preserve its other settings. Use only the
public `dns_servers` from the policy. Stop existing Cairn tasks, validate the
Docker config (`sudo dockerd --validate --config-file=/etc/docker/daemon.json`),
and restart Docker to load it before starting tasks. Recreate old task
containers so they inherit the new DNS defaults. Do not override these DNS
settings with daemon command-line flags or per-container private/host DNS.
`enable.sh` checks the file and refuses missing or non-allowlisted DNS; it
cannot prove the running daemon has reloaded that file. Verify the actual DNS
path on your Docker version before using this as an isolation boundary.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now cairn-network.service
sudo /usr/local/lib/cairn-network/enable.sh  # repeat is safe
sudo nft list table inet cairn_tasks
sudo nft list table bridge cairn_tasks_l2
```

The controller validates an existing network rather than silently changing
it. A mismatch in subnet, gateway, IPv6 or bridge options stops deployment.
Disconnect/remove old task containers and remove a mismatched network before
trying again. Creation installs the guards before creating the bridge. If
Docker creation or a subsequent check fails, the guard stays installed.

Enable and disable also inspect the installed nft guard and actual Docker
bridge ownership before changing any guard tables. Changing the configured
bridge or renaming its Docker network is rejected while the previous identity
is installed, including when `/run/cairn` has been lost. To migrate identities,
restore the previous configuration, stop and detach its tasks, run disable
with that configuration, then configure and enable the new identity. A wrong
or missing network name cannot authorize removal of the previous guard.

No Docker/global firewall rules are flushed. Each enable checks and applies a
single atomic nft transaction replacing only `inet cairn_tasks` and
`bridge cairn_tasks_l2`; a failed nft transaction leaves the old rules active.
A runtime copy of the last applied transaction is written to
`/run/cairn/network.nft`. A process lock serializes CLI updates.
`policy.py render --config ... --allowlist ...` previews the policy without
root; host addresses are added during enable on the actual Docker host. The
checked-in [cairn.nft](cairn.nft) is generated from the example configuration
for review. Use enable.sh to generate the actual host policy; do not install
the static example as a substitute for current host-address discovery.

## Dispatcher startup and persistence

Merge [dispatch.example.yaml](dispatch.example.yaml) into your full dispatcher
configuration. Tasks must join only `cairn-tasks`, with no published ports,
extra networks, host namespace, Docker socket, privileged mode, or added
NET_ADMIN/NET_RAW capabilities. Stop and remove old host-network task
containers before switching; changing configuration does not recreate existing
containers automatically. Cairn's task containers have no restart policy.
Keep that default so they cannot restart before the guard during boot.

The network unit is ordered after Docker and the initial nftables service,
and before `cairn-dispatcher.service`. It reapplies policy when restarted with
Docker. Stopping the unit deliberately leaves the guard in place. Starting
Cairn through a separate supervisor must have `Requires=cairn-network.service`
and `After=cairn-network.service`. A failed dependency must prevent dispatcher
startup.

The repository's Compose dispatcher normally has `restart: unless-stopped`.
Disable that autostart so Docker cannot launch it before this systemd guard.
The provided separate [compose.override.yaml](compose.override.yaml) and
[cairn-dispatcher.service](cairn-dispatcher.service) show a complete ordered
Compose launch without changing the repository's main Compose file. Adjust
`/opt/cairn` and `/usr/bin/docker` to your installation and put the complete
`dispatch.yaml` in that checkout. During migration, stop the old dispatcher
and tasks first, then recreate the dispatcher using the override:

```sh
docker compose stop cairn-dispatcher
# Remove old cairn-dispatch-* TASK containers after preserving anything needed.
docker compose -f docker-compose.yaml -f deploy/network/compose.override.yaml create --force-recreate cairn-dispatcher
sudo install -m 0644 deploy/network/cairn-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cairn-dispatcher.service
```

Do not run a firewall manager that later flushes these tables. If another
firewall reload flushes the ruleset, stop tasks first and restore both Docker's
rules and this guard before restarting tasks. Do not add FLOWTABLE/offload
rules for the task bridge: offloaded flows may bypass later policy reloads.
Other administrators with host/Docker privileges can change this boundary.

## Why Docker ACCEPT cannot bypass the guard

The inet input/forward base chains run at priority `-10`, before the ordinary
Docker filter priority `0`, and after destination NAT. In nftables, DROP is
terminal across base chains; an ACCEPT only ends that chain, so a later
Docker ACCEPT cannot rescue a packet already dropped by this guard. Conversely
an allowed packet can still be dropped by the host's other chains. If an
allowed connection fails, inspect Docker's NAT/forward rules and the host
firewall rather than inserting an unconditional accept rule here.

Destination NAT happens before the forward guard, so DNAT/published-port
redirection to an RFC1918/host-owned address is denied using its translated
destination. Local host access is caught by the separate INPUT hook. Same
bridge task-to-task traffic is caught by the bridge-family hook even when
`net.bridge.bridge-nf-call-iptables=0`; no global bridge sysctl is required.
`net.ipv4.ip_forward=1` is the only global sysctl needed. Bridge IPv6 sysctls
are applied after creation; nft drops are already in place before that.

## Verification on the deployment host

Use an already built/pulled worker image with `curl`, `dig`/`nslookup`, `ip`,
and `python3`; pulling the image is a host/daemon action. Run each probe in a
fresh task container, for example:

```sh
docker network inspect cairn-tasks
sudo nft list table inet cairn_tasks
sudo nft list table bridge cairn_tasks_l2
docker run --rm --network cairn-tasks --cap-drop ALL --security-opt no-new-privileges cairn-worker-container ip route
```

Verify these outcomes with time-bounded administrator probes, for example
`/usr/bin/curl --connect-timeout 3 --max-time 5 --proxy '' --noproxy '*' URL`.
These network-policy probes deliberately use the raw client to isolate the
packet filter from the wrapper's proxy and flag parsing. Normal task requests
continue to use the curl/wget wrappers. For the explicit proxy probe, use
`/usr/bin/curl --connect-timeout 3 --max-time 5 --proxy https://proxy.example.net:443 https://TARGET/`.
Use endpoints you control so an ordinary remote failure is not mistaken for a
firewall result:

| Probe from a task | Expected result |
| --- | --- |
| UDP and TCP DNS query to each configured server on port 53 | succeeds |
| Query to a different public DNS server | denied |
| Resolve a public test name via `127.0.0.11` | succeeds via configured public upstream only |
| Direct HTTPS to a controlled non-allowlisted public endpoint | denied |
| Connection to configured proxy TCP port, then permitted HTTPS CONNECT | succeeds |
| Same proxy address on a different TCP port | denied |
| Controlled allowlisted public IP/CIDR | succeeds |
| Controlled RFC1918/link-local/host public IP, including when allowlisted | denied |
| Host bridge gateway `172.30.0.1` on any unconfigured port | denied |
| Explicitly configured gateway/protocol/port host service | succeeds only for that tuple |
| Other Docker bridge or same-bridge peer container | denied |
| IPv6 literal and link-local peer | denied; bridge has IPv6 disabled |

For a concrete allow/deny check, set `CAIRN_TEST_ALLOWED_IP` to a public IPv4
address you control, with an HTTP service returning success on port 80. Select
an already available worker image, append only that authorized target, and
reload before the test:

```sh
export CAIRN_TEST_ALLOWED_IP="REPLACE_WITH_AUTHORIZED_PUBLIC_IPV4"
export CAIRN_TEST_IMAGE="cairn-worker-container"
printf '%s\n' "$CAIRN_TEST_ALLOWED_IP" | sudo tee -a /etc/cairn/allowlist.txt >/dev/null
sudo systemctl reload cairn-network.service

docker run --rm --network cairn-tasks --cap-drop ALL --security-opt no-new-privileges "$CAIRN_TEST_IMAGE" /usr/bin/curl --fail --show-error --connect-timeout 3 --max-time 5 --proxy '' --noproxy '*' --output /dev/null "http://${CAIRN_TEST_ALLOWED_IP}/"
```

The allowlisted request must succeed. The following RFC1918 request must fail;
compare the deny-chain counter before and after to establish that Cairn's
filter caused the denial:

```sh
sudo nft list chain inet cairn_tasks denied
if docker run --rm --network cairn-tasks --cap-drop ALL --security-opt no-new-privileges "$CAIRN_TEST_IMAGE" /usr/bin/curl --fail --show-error --connect-timeout 3 --max-time 5 --proxy '' --noproxy '*' --output /dev/null http://10.0.0.1/; then
    printf '%s\n' 'FAIL: RFC1918 destination was reachable'
else
    printf '%s\n' 'Connection failed as expected; confirm the deny counter increased'
fi
sudo nft list chain inet cairn_tasks denied
sudo nft list chain bridge cairn_tasks_l2 denied
sudo journalctl -k --since '-5 minutes' --grep 'cairn-(l2-)?drop'
```

A timeout alone does not prove filtering; the counter/log establishes the
packet path. After testing, remove the temporary authorized target from the
allowlist and reload the service. If testing the optional loopback forwarder
above, use the same fresh-container command with `http://172.30.0.1:18000/`;
it succeeds only while the matching host-service exception and forwarder are
both active.

`127.0.0.1` inside the container is its own loopback, not the host. Test host
loopback exposure using host gateway/public-address listeners instead. Inspect
`journalctl -k --grep=cairn-` and nft counters after denied probes: logs are
limited to six per minute with an initial burst of ten, separately for inet
and bridge drops, while every denied packet increments a counter.

For the embedded DNS check, capture on the host (`tcpdump -ni br-cairn port 53`
and the uplink) during queries to `127.0.0.11`. Confirm public upstream DNS
traffic crosses `br-cairn` from the task namespace; no host-loopback resolver
is contacted. Change the allowed upstream to a controlled unreachable address
in a test installation and verify embedded resolution fails rather than
falling back to another resolver. Exercise TCP as well as UDP. If your Docker
version forwards embedded DNS outside this filtered path, do not deploy it
until its resolver path is explicitly isolated.

Test refresh by adding a controlled public target to the allowlist and running
`sudo systemctl reload cairn-network`. Confirm it becomes reachable, remove it,
reload, and confirm new and already-open connections are blocked. Repeating
enable must not duplicate tables/chains. Confirm an unrelated firewall table
is unchanged. Reboot the test host and restart Docker, then repeat the deny
and allow probes before enabling production workloads.

## Teardown and local tests

Stop the dispatcher and all task containers, preserve required output, then
remove/detach the task containers from this network. Run:

```sh
sudo systemctl disable --now cairn-dispatcher.service
sudo systemctl disable --now cairn-network.service
sudo /usr/local/lib/cairn-network/disable.sh
```

Disable refuses while any container is still attached, removes the Docker
network before its guard, and then atomically deletes only its two dedicated
tables. Repeating disable is safe. It does not reset global sysctls, edit the
Docker daemon config, or change unrelated firewall rules.

The repository tests run the real parser/generator/controller against
controlled executable boundaries; they do not need root or modify Docker:

```sh
uv run --project cairn --group dev pytest cairn/tests/test_network_deployment.py
```

They cover validation, normalization, deny precedence, host/IPv6/peer guards,
transaction failure, repeated enable/disable and refusing unsafe teardown.
Host packet routing, Docker DNS forwarding, kernel nft syntax support,
systemd boot ordering and live connectivity require the deployment-host checks
above; unit tests do not establish those properties.
