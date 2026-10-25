Build from this directory:

```bash
docker build . -t cairn-worker-container
```

## HTTP command middleware

The image installs `wrap.sh` and `poolcheck.sh` in `/usr/local/bin`, with
`curl` and `wget` symlinks to the wrapper. Shell commands, including pipelines
and scripts, use these shims when they resolve clients through `PATH`:

```bash
wrap.sh curl -sS -H 'Accept: application/json' https://example.com/
curl -sS https://example.com/ > response.html
wget -qO- https://example.com/
wget -O response.html https://example.com/
```

The wrapper parses client arguments, not shell text. Explicit `/usr/bin/curl`,
`/usr/bin/wget`, other programs/libraries, and commands that replace `PATH` are
outside its coverage. This is cooperative command middleware, not a network
enforcement boundary. Keep the deployment's network controls enabled.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WRAP_PROXY` | `socks5h://127.0.0.1:1080` | Explicit proxy for both clients; an empty value selects direct access. |
| `WRAP_RATE` | `0.5` | Positive, finite tokens per second, per target hostname. |
| `WRAP_BURST` | `1` | Positive integer bucket capacity. |
| `WRAP_STATE_DIR` | `/var/lib/wrap` | Shared token bucket files and `flock` locks. |
| `WRAP_LOG` | `/var/log/wrap/requests.log` | JSON Lines audit destination, opened in append mode. |
| `POOLCHECK_TIMEOUT` | `8` | Positive per-proxy connect/total timeout in seconds. |

The default rate permits one request every two seconds without a burst. Host
keys ignore ports and lowercase DNS names. Independent processes share a bucket
through file locks; different hosts have separate buckets. Each container has
its own state unless containers share the same state directory on a local
filesystem with working `flock`. Use the same rate and burst settings for every
process sharing a directory. State uses Linux monotonic time and resets on host
reboot. Preserve state files while wrappers are running.

`WRAP_PROXY` accepts HTTP(S), SOCKS4/4a, and SOCKS5/5h proxy URIs. It overrides
ambient proxy variables and `NO_PROXY` for consistent routing. SOCKS5h resolves
the target hostname at the proxy. GNU wget does not support SOCKS directly, so
the wget shim translates its supported options into one real `/usr/bin/curl`
invocation. It does not invoke GNU wget.

Each supported invocation launches one curl transfer with curl-generated HTTP
framing: no automatic redirect following, retries, URL globbing, or curl
configuration files. The wrapper rejects unsupported options before networking.
A 3xx is returned and logged; issue a separate wrapped request to the chosen
Location if required.

Supported curl options (including their usual short aliases where listed):

* `-s/--silent`, `-S/--show-error`, `-k/--insecure`, `-I/--head`,
  `-i/--include`, `-f/--fail`, `--fail-with-body`, `--compressed`,
  `-v/--verbose`, `-g/--globoff`.
* `-o/--output`, `-X/--request`, `-H/--header`, `-d/--data`, `--data-raw`,
  `--data-binary`, `--data-urlencode`, `-u/--user`, `-A/--user-agent`,
  `-e/--referer`, `-m/--max-time`, `--connect-timeout`, `-D/--dump-header`.
* Exactly one URL, supplied positionally, with `--url`, or after `--`.

Supported wget options:

* `-q/--quiet`, `--no-check-certificate`, `--spider` (one HEAD request).
* `-O/--output-document`, `--header`, `--post-data`, `--post-file`,
  `-U/--user-agent`, `-T/--timeout`, `--connect-timeout`, `--method`,
  `--body-data`, `--body-file`.
* Exactly one positional URL. Downloads require `-O FILE` or `-O -`.

Short flags can be combined, for example `curl -fsS` and `wget -qO-`.
Long options accept either `--name value` or `--name=value`. Both clients use
curl exit codes (HTTP errors with `--fail` produce 22; wget enables `--fail`).
Invalid/unsupported arguments or inaccessible state/audit storage return 2.

Request metadata (`--header`, `--request`/`--method`, `--user-agent`,
`--referer`, and `--user`) must not contain control characters. Methods must
be single HTTP tokens. Headers must be literal `Name: value`, `Name:` removal,
or `Name;` empty-value forms with valid HTTP field names; `--header @file` and
`--header @-` are unsupported. Custom `Content-Length` and `Transfer-Encoding`
headers, including removal forms, are rejected so curl controls body framing.
These restrictions block metadata injection and custom framing overrides.
Binary/body data and body-file names may contain CR/LF; they remain body input,
and `--data-urlencode` file input is URL-encoded. Multipart/form options that
would put filenames into headers are outside the supported subset.

The token and audit entry cover that curl transfer. They cannot count extra
requests a server or intermediary derives by ignoring or reinterpreting the
declared body framing. For example, a server that ignores a GET body can parse
body bytes as another request. Arbitrary request-smuggling probes therefore need
separate network enforcement and capture; this wrapper does not establish their
server-side request counts.

Audit entries record UTC request start time, normalized host, original URL,
numeric HTTP status (`0` if curl cannot report a completed response status), request duration in milliseconds
(excluding the throttle wait), curl exit code, and requested client. HTTP errors
and transport failures are logged. Opening audit storage fails before sending a
request. Concurrent writers lock and append complete JSON lines. URLs can
contain secrets; protect the log and provide retention outside this wrapper.
SIGINT, SIGTERM, and SIGHUP are forwarded to curl, and the interrupted request is
logged before the wrapper exits with `128 + signal`. SIGKILL, machine failure,
or storage failure after a request starts can prevent a completed audit entry.

```json
{"timestamp":"2026-10-25T12:00:00.000+00:00","host":"example.com","url":"https://example.com/","status":200,"duration_ms":42.5,"exit_code":0,"client":"curl"}
```

## Check a proxy pool

Create a UTF-8 file with one proxy URI per line. Blank lines and full-line `#`
comments are ignored. Bare `host:port` values mean SOCKS5h. The checker performs
one wrapped, rate-limited probe per candidate and prints usable proxy URIs to
stdout; diagnostics go to stderr. A 2xx/3xx response with curl exit code zero is
usable. This establishes reachability to the probe URL, not proxy anonymity or
the trustworthiness of returned content. Exit codes: 0 if any usable proxies,
1 if none, 2 for input/configuration failures, or `128 + signal` if interrupted.
An interrupted checker stops before probing another candidate.

```bash
printf '%s\n' 'socks5h://127.0.0.1:1080' 'http://127.0.0.1:8080' > proxies.txt
poolcheck.sh proxies.txt https://example.com/ > usable-proxies.txt
```

## Reproduce local verification

From the repository root, the tests start local HTTP and SOCKS servers and use
the real installed curl. No Internet or running container is needed. The default
SOCKS proxy test binds localhost port 1080 and skips only if it is already used.

```bash
cd cairn
uv sync --group dev
uv run pytest -q tests/test_wrapper.py
uv run pytest -q
```

For a manual direct HTTP probe from the repository root:

```bash
python3 -m http.server 18080 --bind 127.0.0.1
```

In another shell:

```bash
WRAP_PROXY='' WRAP_STATE_DIR=/tmp/wrap-state WRAP_LOG=/tmp/wrap-requests.log \
  container/wrap.sh curl -sS http://127.0.0.1:18080/
cat /tmp/wrap-requests.log
```
