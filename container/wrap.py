#!/usr/bin/env python3
"""One-request curl/wget middleware; deliberately not a shell command parser."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_PROXY = "socks5h://127.0.0.1:1080"
CURL = "/usr/bin/curl"  # Never resolve through PATH: it contains our client shims.
CURL_FLAGS = {
    "silent", "show-error", "insecure", "head", "include", "fail",
    "fail-with-body", "compressed", "verbose", "globoff",
}
CURL_VALUES = {
    "output", "request", "header", "data", "data-raw", "data-binary",
    "data-urlencode", "user", "user-agent", "referer", "max-time",
    "connect-timeout", "dump-header",
}
CURL_SHORT = {
    "s": "silent", "S": "show-error", "k": "insecure", "I": "head",
    "i": "include", "f": "fail", "v": "verbose", "g": "globoff",
    "o": "output", "X": "request", "H": "header", "d": "data",
    "u": "user", "A": "user-agent", "e": "referer", "m": "max-time",
    "D": "dump-header",
}
WGET_FLAGS = {"quiet": "silent", "no-check-certificate": "insecure", "spider": "head"}
WGET_VALUES = {
    "output-document": "output", "header": "header", "post-data": "data-raw",
    "post-file": "data-binary", "user-agent": "user-agent", "timeout": "max-time",
    "connect-timeout": "connect-timeout", "method": "request",
    "body-data": "data-raw", "body-file": "data-binary",
}
WGET_SHORT = {"q": "quiet", "O": "output-document", "U": "user-agent", "T": "timeout"}
HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def validate_request_metadata(option: str, value: str) -> None:
    if option not in ("header", "request", "user-agent", "referer", "user"):
        return
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"--{option} must not contain control characters")
    if option == "request" and not HTTP_TOKEN.fullmatch(value):
        raise ValueError("--request must be one HTTP method token")
    if option == "header":
        if value.startswith("@"):
            raise ValueError("file-sourced --header values are unsupported; use a literal header")
        name, separator, _content = value.partition(":")
        if not separator and value.endswith(";"):
            name, separator = value[:-1], ";"
        if not separator or not HTTP_TOKEN.fullmatch(name):
            raise ValueError("--header requires a literal HTTP field name followed by ':' or ';'")
        if name.lower() in ("content-length", "transfer-encoding"):
            raise ValueError("custom Content-Length and Transfer-Encoding headers are unsupported; curl must frame the body")


def http_host(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("exactly one absolute http:// or https:// URL is required")
    _ = parsed.port  # Validate malformed/out-of-range ports before starting a request.
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("URL contains control characters")
    return parsed.hostname.lower().encode("idna").decode("ascii")


def proxy_uri(proxy: str) -> str:
    if not proxy:
        return proxy
    parsed = urlsplit(proxy)
    if parsed.scheme not in ("http", "https", "socks4", "socks4a", "socks5", "socks5h") or not parsed.hostname:
        raise ValueError("WRAP_PROXY must be an HTTP(S) or SOCKS proxy URI, or empty for direct access")
    _ = parsed.port
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("proxy URI must not contain a path, query, or fragment")
    return proxy


def parse_request(client: str, argv: list[str]) -> tuple[list[str], str]:
    if client not in ("curl", "wget"):
        raise ValueError("usage: wrap.sh curl|wget [supported options] URL")
    flags = CURL_FLAGS if client == "curl" else WGET_FLAGS
    values = CURL_VALUES if client == "curl" else WGET_VALUES
    shorts = CURL_SHORT if client == "curl" else WGET_SHORT
    result = [] if client == "curl" else ["--fail"]
    urls: list[str] = []
    has_output = False
    index = 0

    def option(name: str, value: str | None = None) -> None:
        nonlocal has_output
        mapped = name if client == "curl" else (WGET_FLAGS.get(name) or WGET_VALUES[name])
        result.append("--" + mapped)
        if value is not None:
            validate_request_metadata(mapped, value)
            if client == "wget" and name in ("post-file", "body-file"):
                value = "@" + value
            result.append(value)
        has_output = has_output or mapped == "output"

    while index < len(argv):
        argument = argv[index]
        index += 1
        if argument == "--":
            urls.extend(argv[index:])
            break
        if argument.startswith("--"):
            name, separator, value = argument[2:].partition("=")
            if client == "curl" and name == "url":
                if not separator:
                    if index == len(argv):
                        raise ValueError("--url requires a value")
                    value = argv[index]
                    index += 1
                urls.append(value)
            elif name in flags and not separator:
                option(name)
            elif name in values:
                if not separator:
                    if index == len(argv):
                        raise ValueError(f"--{name} requires a value")
                    value = argv[index]
                    index += 1
                option(name, value)
            else:
                raise ValueError(f"unsupported {client} option: --{name}")
        elif argument.startswith("-") and argument != "-":
            cluster = argument[1:]
            while cluster:
                short, cluster = cluster[0], cluster[1:]
                if short not in shorts:
                    raise ValueError(f"unsupported {client} option: -{short}")
                name = shorts[short]
                if name in values:
                    if cluster:
                        value, cluster = cluster, ""
                    else:
                        if index == len(argv):
                            raise ValueError(f"-{short} requires a value")
                        value = argv[index]
                        index += 1
                    option(name, value)
                else:
                    option(name)
        else:
            urls.append(argument)
    if len(urls) != 1:
        raise ValueError("exactly one HTTP URL is required; multiple transfers are unsupported")
    http_host(urls[0])
    if client == "wget" and not has_output and "--head" not in result:
        raise ValueError("wget requires -O FILE or -O - in the supported subset")
    return result, urls[0]


def settings() -> tuple[float, int, str, Path, Path]:
    rate = float(os.environ.get("WRAP_RATE", "0.5"))
    burst = int(os.environ.get("WRAP_BURST", "1"))
    if not math.isfinite(rate) or rate <= 0 or burst < 1:
        raise ValueError("WRAP_RATE must be finite and positive; WRAP_BURST must be a positive integer")
    return (
        rate, burst, proxy_uri(os.environ.get("WRAP_PROXY", DEFAULT_PROXY)),
        Path(os.environ.get("WRAP_STATE_DIR", "/var/lib/wrap")),
        Path(os.environ.get("WRAP_LOG", "/var/log/wrap/requests.log")),
    )


def take_token(host: str, rate: float, burst: int, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(host.encode()).hexdigest() + ".json"
    with (state_dir / name).open("a+", encoding="utf-8") as state_file:
        # Hold the host's lock while sleeping. Other hosts use different files.
        fcntl.flock(state_file, fcntl.LOCK_EX)
        state_file.seek(0)
        raw = state_file.read()
        now = time.monotonic()
        if raw:
            state = json.loads(raw)
            updated, tokens = float(state["updated"]), float(state["tokens"])
            if not math.isfinite(updated) or not math.isfinite(tokens):
                raise ValueError("invalid token bucket state; remove it only when no wrappers are running")
            # monotonic time resets when the host reboots.
            if updated > now:
                updated, tokens = now, float(burst)
            tokens = min(float(burst), tokens + (now - updated) * rate)
        else:
            tokens = float(burst)
        while tokens < 1:
            time.sleep((1 - tokens) / rate)
            later = time.monotonic()
            tokens = min(float(burst), tokens + (later - now) * rate)
            now = later
        state_file.seek(0)
        state_file.truncate()
        json.dump({"updated": now, "tokens": tokens - 1}, state_file)
        state_file.flush()


def execute(client: str, args: list[str], url: str, proxy: str | None = None) -> tuple[int, int]:
    rate, burst, configured_proxy, state_dir, log_path = settings()
    proxy = configured_proxy if proxy is None else proxy_uri(proxy)
    host = http_host(url)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Open before networking: unavailable audit storage must fail closed.
    with log_path.open("a", encoding="utf-8") as audit:
        take_token(host, rate, burst, state_dir)
        marker = "\n__CAIRN_WRAP_" + uuid.uuid4().hex + ":"
        command = [
            CURL, "--disable", "--globoff", "--proto", "=http,https",
            "--max-redirs", "0", "--retry", "0", "--proxy", proxy, "--noproxy", "",
            *args, "--write-out", "%{stderr}" + marker + "%{http_code}\n", "--url", url,
        ]
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        status = 0
        exit_code = 127
        process = None
        interrupted = 0
        previous_handlers = {}

        def forward_signal(signum, _frame):
            nonlocal interrupted
            interrupted = signum
            if process is not None and process.poll() is None:
                try:
                    process.send_signal(signum)
                except ProcessLookupError:
                    pass

        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[signum] = signal.signal(signum, forward_signal)
            process = subprocess.Popen(command, stderr=subprocess.PIPE)
            if interrupted:
                forward_signal(interrupted, None)
            _stdout, stderr = process.communicate()
            exit_code = 128 + interrupted if interrupted else process.returncode
            if exit_code < 0:
                exit_code = 128 - exit_code
            match = re.search(re.escape(marker.encode()) + rb"([0-9]{3})\n$", stderr)
            if match:
                status = int(match.group(1))
                sys.stderr.buffer.write(stderr[:match.start()])
            else:
                sys.stderr.buffer.write(stderr)
        except OSError as error:
            exit_code = 127
            print(f"wrap: could not execute curl: {error}", file=sys.stderr)
        finally:
            try:
                record = {
                    "timestamp": timestamp, "host": host, "url": url, "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "exit_code": exit_code, "client": client,
                }
                fcntl.flock(audit, fcntl.LOCK_EX)
                audit.write(json.dumps(record, ensure_ascii=True) + "\n")
                audit.flush()
                fcntl.flock(audit, fcntl.LOCK_UN)
            finally:
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
    return exit_code, status


def poolcheck(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        raise ValueError("usage: poolcheck.sh PROXY_FILE [HTTP_PROBE_URL]")
    target = argv[1] if len(argv) == 2 else "https://example.com/"
    http_host(target)
    timeout = float(os.environ.get("POOLCHECK_TIMEOUT", "8"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("POOLCHECK_TIMEOUT must be finite and positive")
    usable = 0
    for number, line in enumerate(Path(argv[0]).read_text(encoding="utf-8").splitlines(), 1):
        proxy = line.strip()
        if not proxy or proxy.startswith("#"):
            continue
        if "://" not in proxy:
            proxy = "socks5h://" + proxy
        try:
            proxy_uri(proxy)
        except ValueError as error:
            print(f"poolcheck: line {number}: {error}", file=sys.stderr)
            continue
        code, status = execute("curl", [
            "--silent", "--show-error", "--fail", "--output", "/dev/null",
            "--connect-timeout", str(timeout), "--max-time", str(timeout),
        ], target, proxy=proxy)
        if code in (128 + signal.SIGINT, 128 + signal.SIGTERM, 128 + signal.SIGHUP):
            return code
        if code == 0 and 200 <= status < 400:
            print(proxy, flush=True)
            usable += 1
        else:
            print(f"poolcheck: line {number}: unusable (status={status}, exit={code})", file=sys.stderr)
    return 0 if usable else 1


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "poolcheck":
            return poolcheck(argv[1:])
        if not argv:
            raise ValueError("usage: wrap.sh curl|wget [supported options] URL")
        client = argv[0]
        args, url = parse_request(client, argv[1:])
        return execute(client, args, url)[0]
    except (OSError, ValueError, KeyError) as error:
        print(f"wrap: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
