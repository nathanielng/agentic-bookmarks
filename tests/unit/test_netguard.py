import http.server
import threading

import httpx
import pytest

from bmk.enrich import netguard
from bmk.enrich.fetch import FetchError, Fetcher


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1%en0",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "not an ip",
    ],
)
def test_non_public_addresses_are_rejected(address):
    assert not netguard.is_public(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert netguard.is_public(address)


def test_check_url_rejects_schemes_credentials_and_private_hosts():
    resolve = lambda host, port: {"inside.test": ["10.0.0.5"]}.get(host, ["93.184.216.34"])  # noqa: E731
    netguard.check_url("https://example.com/a", resolve)
    for url in ("file:///etc/passwd", "ftp://example.com/", "https://user:pw@example.com/", "http://inside.test/"):
        with pytest.raises(netguard.BlockedAddress):
            netguard.check_url(url, resolve)


def test_any_private_answer_blocks_the_host():
    with pytest.raises(netguard.BlockedAddress):
        netguard.vet_host("mixed.test", 443, lambda h, p: ["93.184.216.34", "127.0.0.1"])


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>secret</title>")

    def log_message(self, *args):
        pass


def test_connections_are_vetted_at_connect_time():
    """Even if the URL check were fooled (DNS rebinding), the connection itself refuses a local address."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])  # public for the check, local for the connect
    fetcher = Fetcher(resolver=lambda host, p: next(answers))
    try:
        with pytest.raises(FetchError) as exc:
            fetcher.get(f"http://rebind.test:{port}/")
        assert exc.value.kind == "blocked"
    finally:
        fetcher.close()
        server.shutdown()


def test_local_server_is_reachable_without_the_guard():
    """Control for the test above: the server really answers, so the block came from the guard."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert httpx.get(f"http://127.0.0.1:{server.server_address[1]}/").text == "<title>secret</title>"
    finally:
        server.shutdown()
