"""Local-only proxy-chain smoke test; no account, model, or external requests."""
import hashlib
import base64
import json
import http.client
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mitmproxy.net import encoding

from app.services.timezone_proxy_service import TimezoneProxyService


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        body = encoding.decode(body, self.headers.get("Content-Encoding", "identity"))
        payload = json.loads(body)
        payload["_received_accept_language"] = self.headers.get("Accept-Language", "")
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept.decode())
        self.end_headers()
        first = self.rfile.read(2)
        size = first[1] & 127
        if size == 126:
            size = struct.unpack("!H", self.rfile.read(2))[0]
        mask = self.rfile.read(4)
        data = self.rfile.read(size)
        body = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        header = bytes([0x81, len(body)]) if len(body) < 126 else b"\x81\x7e" + struct.pack("!H", len(body))
        self.wfile.write(header + body)
        self.wfile.flush()
        self.close_connection = True


class ProxyChainIntegration(unittest.TestCase):
    def test_https_zstd_websocket_and_shared_port_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                           .public_key(key.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                           .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
                           .sign(key, hashes.SHA256()))
            cert = root / "origin.pem"
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path = root / "origin.key"
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            main_port = available_port()
            confdir = root / "certs"
            log_path = root / "proxy.log"
            with log_path.open("w+", encoding="utf-8") as log:
                args = [sys.executable, "-X", "utf8", "-c",
                        "from mitmproxy.tools.main import mitmdump; mitmdump()",
                        "--listen-host", "127.0.0.1", "--listen-port", str(main_port),
                        "--set", f"confdir={confdir}", "--set", f"ssl_verify_upstream_trusted_ca={cert}"]
                main = subprocess.Popen(args, stdout=log, stderr=log,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proxy = None
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if main.poll() is not None:
                            self.fail("main proxy exited")
                        try:
                            with socket.create_connection(("127.0.0.1", main_port), timeout=.1):
                                break
                        except OSError:
                            time.sleep(.05)
                    else:
                        self.fail("main proxy did not start")
                    with patch.object(TimezoneProxyService, "certificate_dir", return_value=confdir):
                        proxy = TimezoneProxyService("Asia/Tokyo", main_port, "ja-JP")
                        ok, error = proxy.run()
                        self.assertTrue(ok, error)
                        client_context = ssl.create_default_context(cafile=str(confdir / "mitmproxy-ca-cert.pem"))

                        def connection(port):
                            conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=8, context=client_context)
                            conn.set_tunnel("localhost", server.server_port)
                            return conn

                        payload = {"input": [{"role": "developer", "content": [{"type": "input_text",
                                   "text": "<environment_context><timezone>Asia/Shanghai</timezone></environment_context>"}]}]}
                        payload["client_metadata"] = {"locale": "zh-CN"}
                        body = json.dumps(payload).encode()
                        for port, expected in ((main_port, "Asia/Shanghai"), (proxy.config.port, "Asia/Tokyo")):
                            conn = connection(port)
                            try:
                                conn.request("POST", "/backend-api/codex/responses", encoding.encode(body, "zstd"),
                                             {"Content-Type": "application/json", "Content-Encoding": "zstd"})
                                response = conn.getresponse()
                                result = response.read().decode()
                                self.assertEqual(response.status, 200, result)
                                self.assertIn(expected, result)
                                decoded = json.loads(result)
                                if port == proxy.config.port:
                                    self.assertEqual(decoded["client_metadata"]["locale"], "ja-JP")
                                    self.assertEqual(decoded["_received_accept_language"], "ja-JP,ja;q=0.9")
                                else:
                                    self.assertEqual(decoded["client_metadata"]["locale"], "zh-CN")
                                    self.assertEqual(decoded["_received_accept_language"], "")
                            finally:
                                conn.close()
                        conn = connection(proxy.config.port)
                        try:
                            conn.request("GET", "/backend-api/codex/responses", headers={
                                "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                                "Sec-WebSocket-Key": base64.b64encode(b"timezone-smoke!!").decode()})
                            response = conn.getresponse()
                            self.assertEqual(response.status, 101)
                            payload["type"] = "response.create"
                            data = json.dumps(payload).encode()
                            mask = b"test"
                            frame = b"\x81\xfe" + struct.pack("!H", len(data)) + mask
                            frame += bytes(value ^ mask[index % 4] for index, value in enumerate(data))
                            conn.sock.sendall(frame)
                            reader = conn.sock.makefile("rb")
                            first = reader.read(2)
                            size = first[1] & 127
                            if size == 126:
                                size = struct.unpack("!H", reader.read(2))[0]
                            echoed = reader.read(size).decode()
                            self.assertIn("Asia/Tokyo", echoed)
                            self.assertEqual(json.loads(echoed)["client_metadata"]["locale"], "ja-JP")
                            reader.close()
                        finally:
                            conn.close()
                finally:
                    if proxy is not None:
                        proxy.stop()
                    main.terminate()
                    main.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
