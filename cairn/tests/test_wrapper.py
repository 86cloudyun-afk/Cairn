from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


CONTAINER = Path(__file__).resolve().parents[2] / "container"
WRAP = CONTAINER / "wrap.sh"
POOLCHECK = CONTAINER / "poolcheck.sh"


class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((time.monotonic(), self.path, "GET"))
        self.server.request_started.set()
        if self.path.endswith("/slow"):
            time.sleep(2)
        elif self.path == "/delayed":
            time.sleep(0.15)
        status = 418 if self.path.endswith("/teapot") else 200
        body = b"local response\n"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # The signal-forwarding test deliberately terminates curl.

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((time.monotonic(), self.path, body))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class HTTP11Handler(HTTPHandler):
    protocol_version = "HTTP/1.1"


class SocksHandler(socketserver.StreamRequestHandler):
    def handle(self):
        version, method_count = self.rfile.read(2)
        assert version == 5
        self.rfile.read(method_count)
        self.wfile.write(b"\x05\x00")
        version, command, _reserved, address_type = self.rfile.read(4)
        assert (version, command) == (5, 1)
        if address_type == 3:
            host = self.rfile.read(self.rfile.read(1)[0]).decode()
        elif address_type == 1:
            host = socket.inet_ntoa(self.rfile.read(4))
        else:
            host = socket.inet_ntop(socket.AF_INET6, self.rfile.read(16))
        port = int.from_bytes(self.rfile.read(2), "big")
        self.server.targets.append((host, port))
        self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nvia socks\n")


class SocksServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
    server.requests = []
    server.request_started = threading.Event()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.fixture
def http11_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP11Handler)
    server.requests = []
    server.request_started = threading.Event()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.fixture
def env(tmp_path):
    return {
        **os.environ,
        "WRAP_PROXY": "",
        "WRAP_RATE": "1000",
        "WRAP_BURST": "1",
        "WRAP_STATE_DIR": str(tmp_path / "state"),
        "WRAP_LOG": str(tmp_path / "requests.log"),
    }


def invoke(env, *args):
    assert WRAP.is_file(), "HTTP wrapper has not been implemented"
    return subprocess.run(["bash", str(WRAP), *args], env=env, capture_output=True, timeout=15)


def logs(env):
    return [json.loads(line) for line in Path(env["WRAP_LOG"]).read_text().splitlines()]


def url(server, path="/ok", host="127.0.0.1"):
    return f"http://{host}:{server.server_port}{path}"


def test_curl_keeps_body_and_records_status_duration_timestamp(http_server, env):
    target = url(http_server, "/ok?value=a%20b")
    result = invoke(env, "curl", "-sS", target)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"local response\n"
    [record] = logs(env)
    assert record["host"] == "127.0.0.1"
    assert record["url"] == target
    assert record["status"] == 200
    assert record["exit_code"] == 0
    assert record["duration_ms"] >= 0
    assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None


def test_http_failure_retains_curl_exit_status_and_is_audited(http_server, env):
    result = invoke(env, "curl", "-fsS", url(http_server, "/teapot"))
    assert result.returncode == 22
    assert logs(env)[0]["status"] == 418
    assert logs(env)[0]["exit_code"] == 22


def test_wget_subset_supports_stdout_output_file_and_post(http_server, env, tmp_path):
    result = invoke(env, "wget", "-qO-", url(http_server))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"local response\n"
    output = tmp_path / "payload"
    result = invoke(env, "wget", "-q", "-O", str(output), "--post-data=hello", url(http_server))
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b"hello"
    assert http_server.requests[-1][2] == b"hello"
    assert [entry["client"] for entry in logs(env)] == ["wget", "wget"]


@pytest.mark.parametrize("client,args", [
    ("curl", ["-L"]),
    ("curl", ["--retry", "2"]),
    ("curl", ["--config", "/dev/null"]),
    ("curl", ["--proxy", "http://elsewhere.invalid"]),
    ("curl", ["--next"]),
    ("wget", ["--recursive"]),
    ("wget", ["--input-file", "/dev/null"]),
    ("wget", ["--execute", "http_proxy=http://elsewhere.invalid"]),
])
def test_unaccountable_modes_are_rejected_before_network(http_server, env, client, args):
    result = invoke(env, client, *args, url(http_server))
    assert result.returncode == 2
    assert b"unsupported" in result.stderr.lower()
    assert http_server.requests == []
    assert not Path(env["WRAP_LOG"]).exists()


def test_multiple_urls_and_non_http_schemes_are_rejected(http_server, env):
    for args in ([url(http_server), url(http_server)], ["file:///etc/passwd"]):
        result = invoke(env, "curl", *args)
        assert result.returncode == 2
    assert http_server.requests == []


def test_header_crlf_cannot_create_unaccounted_http11_request(http11_server, env):
    injection = "X-Fuzz: y\r\n\r\nGET /unaccounted HTTP/1.1\r\nHost: 127.0.0.1\r\n"
    result = invoke(env, "curl", "-sS", "-H", injection, url(http11_server, "/accounted"))
    assert result.returncode == 2, (result.stderr, http11_server.requests)
    assert http11_server.requests == []
    assert not Path(env["WRAP_LOG"]).exists()


@pytest.mark.parametrize("client,option,prefix", [
    ("curl", "--header", "X-Test: value"),
    ("curl", "-X", "GET"),
    ("curl", "-A", "agent"),
    ("curl", "-e", "http://referer.invalid/"),
    ("curl", "-u", "user:password"),
    ("wget", "--header", "X-Test: value"),
    ("wget", "--method", "GET"),
    ("wget", "-U", "agent"),
])
@pytest.mark.parametrize("control", ["\r\n", "\t", "\x7f", "\x85"])
def test_request_metadata_controls_are_rejected_before_network(http_server, env, client, option, prefix, control):
    args = [client, "-O", "-"] if client == "wget" else [client]
    result = invoke(env, *args, option, prefix + control + "injected", url(http_server))
    assert result.returncode == 2, result.stderr
    assert http_server.requests == []
    assert not Path(env["WRAP_LOG"]).exists()


@pytest.mark.parametrize("client,option", [("curl", "-H"), ("wget", "--header")])
def test_file_sourced_headers_are_rejected_before_network(http_server, env, tmp_path, client, option):
    header_file = tmp_path / "headers.txt"
    header_file.write_text("X-Test: file-sourced\n")
    args = [client, "-O", "-"] if client == "wget" else [client]
    result = invoke(env, *args, option, "@" + str(header_file), url(http_server))
    assert result.returncode == 2, result.stderr
    assert http_server.requests == []
    assert not Path(env["WRAP_LOG"]).exists()


@pytest.mark.parametrize("header", [
    "Content-Length: 0", "Content-Length:", "content-length;",
    "Transfer-Encoding: identity", "TRANSFER-ENCODING:", "Transfer-Encoding;",
    "Content-Length : 0", " Content-Length: 0",
])
def test_framing_headers_cannot_turn_body_into_second_request(http11_server, env, header):
    body = "GET /unaccounted HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    result = invoke(env, "curl", "-sS", "--max-time", "2", "-H", header, "--data-binary", body, url(http11_server, "/accounted"))
    assert result.returncode == 2, (result.stderr, http11_server.requests)
    assert http11_server.requests == []
    assert not Path(env["WRAP_LOG"]).exists()


def test_method_must_be_one_http_token(http_server, env):
    result = invoke(env, "curl", "-X", "GET /unaccounted HTTP/1.1", url(http_server))
    assert result.returncode == 2, result.stderr
    assert http_server.requests == []


def test_binary_body_and_its_filename_can_contain_controls_without_header_injection(http11_server, env, tmp_path):
    body = b"GET /unaccounted HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n\x00\xff"
    body_file = tmp_path / "payload\r\nX-Name: value.bin"
    body_file.write_bytes(body)
    result = invoke(
        env, "curl", "-sS", "-X", "POST", "-A", "wrapper-test", "-e", "http://referer.invalid/",
        "-u", "user:password", "-H", "X-Test: value", "--data-binary", "@" + str(body_file),
        url(http11_server, "/accounted"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == body
    assert len(http11_server.requests) == 1
    assert http11_server.requests[0][1:] == ("/accounted", body)
    assert len(logs(env)) == 1


def test_data_urlencode_file_is_encoded_body_not_request_metadata(http11_server, env, tmp_path):
    body_file = tmp_path / "value\r\nX-Name: injected.bin"
    body_file.write_bytes(b"line1\r\nline2")
    result = invoke(env, "curl", "-sS", "--data-urlencode", "value@" + str(body_file), url(http11_server))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"value=line1%0D%0Aline2"
    assert len(http11_server.requests) == 1
    assert len(logs(env)) == 1


def test_concurrent_processes_share_host_bucket_and_complete_log_lines(http_server, env):
    env["WRAP_RATE"] = "5"
    assert WRAP.is_file(), "HTTP wrapper has not been implemented"
    processes = [subprocess.Popen(
        ["bash", str(WRAP), "curl", "-sS", url(http_server)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for _ in range(4)]
    for process in processes:
        _stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
    times = sorted(item[0] for item in http_server.requests)
    assert len(times) == 4
    assert all(later - earlier >= 0.16 for earlier, later in zip(times, times[1:]))
    assert len(logs(env)) == 4


def test_separate_hosts_have_independent_buckets(http_server, env):
    env["WRAP_RATE"] = "0.5"
    started = time.monotonic()
    first = invoke(env, "curl", "-sS", url(http_server))
    second = invoke(env, "curl", "-sS", url(http_server, host="localhost"))
    assert first.returncode == second.returncode == 0
    assert time.monotonic() - started < 1.5
    assert {record["host"] for record in logs(env)} == {"localhost", "127.0.0.1"}


def test_burst_capacity_allows_two_immediate_requests(http_server, env):
    env.update(WRAP_RATE="4", WRAP_BURST="2")
    for _ in range(3):
        result = invoke(env, "curl", "-sS", url(http_server))
        assert result.returncode == 0, result.stderr
    times = [item[0] for item in http_server.requests]
    assert times[1] - times[0] < 0.2
    assert times[2] - times[0] >= 0.20


def test_default_rate_waits_two_seconds_between_host_requests(http_server, env):
    env.pop("WRAP_RATE")
    for _ in range(2):
        result = invoke(env, "curl", "-sS", url(http_server))
        assert result.returncode == 0, result.stderr
    assert http_server.requests[1][0] - http_server.requests[0][0] >= 1.9


def test_audit_duration_measures_actual_network_time(http_server, env):
    result = invoke(env, "curl", "-sS", url(http_server, "/delayed"))
    assert result.returncode == 0, result.stderr
    assert logs(env)[0]["duration_ms"] >= 130


@pytest.mark.parametrize("client,args", [("curl", ["-sS"]), ("wget", ["-qO-"])])
def test_explicit_http_proxy_wins_over_ambient_proxy_and_no_proxy(http_server, env, client, args):
    env.update(WRAP_PROXY=url(http_server, ""), NO_PROXY="*", no_proxy="*", ALL_PROXY="http://127.0.0.1:1")
    target = "http://target.invalid/through-proxy"
    result = invoke(env, client, *args, target)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"local response\n"
    assert http_server.requests[0][1] == target
    assert logs(env)[0]["host"] == "target.invalid"


def test_default_socks5h_proxy_routes_curl_and_wget_with_remote_dns(env):
    try:
        server = SocksServer(("127.0.0.1", 1080), SocksHandler)
    except OSError:
        pytest.skip("default SOCKS port 1080 is already in use")
    server.targets = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env.pop("WRAP_PROXY")
    env["NO_PROXY"] = "*"
    try:
        for client, args in (("curl", ["-sS"]), ("wget", ["-qO-"])):
            result = invoke(env, client, *args, "http://remote-dns.invalid/ok")
            assert result.returncode == 0, result.stderr
            assert result.stdout == b"via socks\n"
        assert server.targets == [("remote-dns.invalid", 80), ("remote-dns.invalid", 80)]
        assert len(logs(env)) == 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_configured_socks5h_proxy_routes_both_clients_with_remote_dns(env):
    server = SocksServer(("127.0.0.1", 0), SocksHandler)
    server.targets = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env["WRAP_PROXY"] = f"socks5h://127.0.0.1:{server.server_address[1]}"
    try:
        for client, args in (("curl", ["-sS"]), ("wget", ["-qO-"])):
            result = invoke(env, client, *args, "http://remote-dns.invalid/ok")
            assert result.returncode == 0, result.stderr
            assert result.stdout == b"via socks\n"
        assert server.targets == [("remote-dns.invalid", 80), ("remote-dns.invalid", 80)]
        assert len(logs(env)) == 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_forwards_signal_and_audits_started_request(http_server, env, signum):
    assert WRAP.is_file(), "HTTP wrapper has not been implemented"
    process = subprocess.Popen(
        ["bash", str(WRAP), "curl", "-sS", url(http_server, "/slow")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert http_server.request_started.wait(timeout=5)
        process.send_signal(signum)
        _stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 128 + signum, stderr
        [record] = logs(env)
        assert record["status"] == 0
        assert record["exit_code"] == 128 + signum
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_transport_failure_is_audited_with_zero_status(http_server, env):
    env["WRAP_PROXY"] = "http://127.0.0.1:1"
    result = invoke(env, "curl", "-sS", "--max-time", "1", url(http_server))
    assert result.returncode != 0
    [record] = logs(env)
    assert record["status"] == 0
    assert record["exit_code"] == result.returncode
    assert http_server.requests == []


def test_unwritable_audit_destination_fails_before_http_request(http_server, env, tmp_path):
    env["WRAP_LOG"] = str(tmp_path)
    result = invoke(env, "curl", "-sS", url(http_server))
    assert result.returncode != 0
    assert http_server.requests == []


def test_path_shims_dispatch_without_recursion(http_server, env, tmp_path):
    assert WRAP.is_file(), "HTTP wrapper has not been implemented"
    shim = tmp_path / "bin"
    shim.mkdir()
    for client in ("curl", "wget"):
        (shim / client).symlink_to(WRAP)
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    result = subprocess.run(
        ["bash", "-c", 'curl -sS "$1" && wget -qO- "$1"', "wrapper-test", url(http_server)],
        env=env, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"local response\nlocal response\n"
    assert [entry["client"] for entry in logs(env)] == ["curl", "wget"]


def test_poolcheck_outputs_only_working_proxies(http_server, env, tmp_path):
    assert POOLCHECK.is_file(), "Proxy pool checker has not been implemented"
    good_proxy = url(http_server, "")
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text(f"# candidates\n\nhttp://127.0.0.1:1\n{good_proxy}\n")
    result = subprocess.run(
        ["bash", str(POOLCHECK), str(proxy_file), "http://pool-target.invalid/ok"],
        env={**env, "POOLCHECK_TIMEOUT": "1"}, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().splitlines() == [good_proxy]
    assert [entry["status"] for entry in logs(env)] == [0, 200]


def test_poolcheck_cancellation_stops_before_next_proxy(http_server, env, tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text(f"{url(http_server, '')}\n{url(http_server, '')}\n")
    process = subprocess.Popen(
        ["bash", str(POOLCHECK), str(proxy_file), "http://pool-target.invalid/slow"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert http_server.request_started.wait(timeout=5)
        process.terminate()
        _stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 143, stderr
        assert len(http_server.requests) == 1
        assert len(logs(env)) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("variable,value", [("WRAP_RATE", "0"), ("WRAP_RATE", "nan"), ("WRAP_BURST", "0"), ("WRAP_PROXY", "file:///tmp/proxy")])
def test_invalid_runtime_settings_fail_before_network(http_server, env, variable, value):
    env[variable] = value
    result = invoke(env, "curl", "-sS", url(http_server))
    assert result.returncode == 2
    assert http_server.requests == []
