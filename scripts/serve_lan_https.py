#!/usr/bin/env python3
"""LAN HTTPS launcher for the vdotool demo.

Stands up everything the agent needs so a second device on the local
network can open the push link and grant camera access:

  - Self-signed TLS cert covering localhost + the host's LAN IP, written
    to a temp dir.
  - HTTPS server on 0.0.0.0:8443 that:
      * serves vdo_ninja/ as static files
      * proxies POST /vdotool/frame -> 127.0.0.1:8765
  - vdotool writer subprocess on 127.0.0.1:8765 (private).

Then prints the env vars the user should export in another terminal
before running ``hermes chat`` so the plugin builds links pointing at
this server.

Self-signed cert -> the second laptop will see a "your connection is
not private" warning the first time. Click Advanced -> Proceed; the
camera will work after that.

Usage:
    ./scripts/serve_lan_https.sh
or
    python3 scripts/serve_lan_https.py
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import logging
import os
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

LOG = logging.getLogger("vdotool.lan")

REPO_DIR = Path(__file__).resolve().parent.parent
FORK_DIR = REPO_DIR / "vdo_ninja"
WRITER_SCRIPT = FORK_DIR / "vdotool" / "writer.py"

DEFAULT_HTTPS_PORT = 8443
DEFAULT_WRITER_PORT = 8765


def detect_lan_ip() -> str:
    """Return this host's primary LAN IPv4 address.

    Uses the standard "open a UDP socket to a public address" trick;
    no packet is sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def generate_self_signed_cert(out_dir: Path, lan_ip: str) -> tuple[Path, Path]:
    """Generate a self-signed cert that includes localhost + LAN IP SANs.

    Returns ``(cert_path, key_path)``.
    """
    cert_path = out_dir / "cert.pem"
    key_path = out_dir / "key.pem"

    config_path = out_dir / "openssl.cnf"
    config_path.write_text(
        f"""[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = vdotool.local

[v3_req]
subjectAltName = @alt_names
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
DNS.1 = localhost
DNS.2 = vdotool.local
IP.1 = 127.0.0.1
IP.2 = {lan_ip}
"""
    )

    LOG.info("generating self-signed cert (CN=vdotool.local, SAN includes %s)", lan_ip)
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "30",
            "-config",
            str(config_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert_path, key_path


# ---------------------------------------------------------------------------
# Combined HTTPS server: static files + reverse proxy for /vdotool/frame
# ---------------------------------------------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the fork directory and proxies frame POSTs to the writer."""

    writer_host: str = "127.0.0.1"
    writer_port: int = DEFAULT_WRITER_PORT

    # Any request path that should be proxied to the writer sidecar
    # instead of served as static files from the fork's doc-root. All
    # /vdotool/* routes are plugin-managed endpoints served by the
    # writer; the base index.html + VDO.Ninja JS are static.
    _WRITER_PREFIX = "/vdotool/"

    # Quieter access log
    def log_message(self, fmt, *args):  # noqa: D401
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self):  # noqa: N802
        if self.path.startswith(self._WRITER_PREFIX):
            self._proxy_to_writer(method="POST")
            return
        self.send_error(405, "Method not allowed")

    def do_GET(self):  # noqa: N802
        if self.path.startswith(self._WRITER_PREFIX):
            self._proxy_to_writer(method="GET")
            return
        super().do_GET()

    def _proxy_to_writer(self, method: str = "POST") -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""

            conn = http.client.HTTPConnection(self.writer_host, self.writer_port, timeout=15)
            headers = {}
            for h in ("Content-Type", "Content-Length"):
                v = self.headers.get(h)
                if v is not None:
                    headers[h] = v
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()

            self.send_response(resp.status)
            for h, v in resp.getheaders():
                if h.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(h, v)
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except OSError as e:
            LOG.warning("proxy error: %s", e)
            self.send_error(502, f"writer unreachable: {e}")


class _ThreadedHTTPS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_https(
    bind_host: str,
    bind_port: int,
    document_root: Path,
    cert: Path,
    key: Path,
    writer_host: str,
    writer_port: int,
) -> _ThreadedHTTPS:
    """Build and start an HTTPS server in a background thread."""

    handler_cls = type(
        "BoundHandler",
        (_Handler,),
        {
            "writer_host": writer_host,
            "writer_port": writer_port,
        },
    )

    # SimpleHTTPRequestHandler serves cwd by default; chdir before binding.
    os.chdir(document_root)

    httpd = _ThreadedHTTPS((bind_host, bind_port), handler_cls)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------------------
# Subprocess: writer
# ---------------------------------------------------------------------------


def start_writer(frames_dir: Path, host: str, port: int, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["VDOTOOL_FRAMES_DIR"] = str(frames_dir)
    env["VDOTOOL_WRITER_HOST"] = host
    env["VDOTOOL_WRITER_PORT"] = str(port)
    log_f = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, str(WRITER_SCRIPT)],
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    log_f.close()
    return proc


def wait_for_writer(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", "/vdotool/healthz")
            r = conn.getresponse()
            r.read()
            conn.close()
            if r.status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="LAN HTTPS launcher for vdotool.")
    p.add_argument("--https-port", type=int, default=DEFAULT_HTTPS_PORT)
    p.add_argument("--writer-port", type=int, default=DEFAULT_WRITER_PORT)
    p.add_argument(
        "--frames-dir",
        default=None,
        help="Frames root. Default: a fresh tempdir, removed on exit.",
    )
    p.add_argument(
        "--keep-frames",
        action="store_true",
        help="Keep frames dir on exit (only meaningful with --frames-dir).",
    )
    p.add_argument(
        "--lan-ip",
        default=None,
        help="Override LAN IP detection.",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not WRITER_SCRIPT.is_file():
        print(f"ERROR: writer script not found at {WRITER_SCRIPT}", file=sys.stderr)
        return 2

    if shutil.which("openssl") is None:
        print("ERROR: openssl is not installed; cannot make a self-signed cert", file=sys.stderr)
        return 2

    lan_ip = args.lan_ip or detect_lan_ip()

    # Frames dir
    if args.frames_dir:
        frames_dir = Path(args.frames_dir).resolve()
        frames_dir.mkdir(parents=True, exist_ok=True)
        cleanup_frames = False
    else:
        frames_dir = Path(tempfile.mkdtemp(prefix="cp-lan-frames-"))
        cleanup_frames = not args.keep_frames

    # Cert dir (always temp)
    cert_dir = Path(tempfile.mkdtemp(prefix="cp-lan-cert-"))
    cert_path, key_path = generate_self_signed_cert(cert_dir, lan_ip)

    # Writer log + HTTPS access log directory (kept post-mortem)
    log_dir = Path(tempfile.mkdtemp(prefix="cp-lan-logs-"))
    writer_log = log_dir / "writer.log"
    access_log = log_dir / "https-access.log"

    # Tee Python logging to a file too, so we have a record of HTTPS hits.
    file_handler = logging.FileHandler(access_log)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    LOG.info("HTTPS access log: %s", access_log)

    # Start writer
    LOG.info("starting writer on 127.0.0.1:%d (frames -> %s)", args.writer_port, frames_dir)
    writer_proc = start_writer(frames_dir, "127.0.0.1", args.writer_port, writer_log)
    if not wait_for_writer("127.0.0.1", args.writer_port):
        LOG.error("writer failed to come up; tail of log:\n%s", writer_log.read_text())
        writer_proc.terminate()
        return 3

    # Start HTTPS
    LOG.info("serving fork on https://0.0.0.0:%d (TLS, self-signed)", args.https_port)
    httpd = serve_https(
        bind_host="0.0.0.0",
        bind_port=args.https_port,
        document_root=FORK_DIR,
        cert=cert_path,
        key=key_path,
        writer_host="127.0.0.1",
        writer_port=args.writer_port,
    )

    base_url = f"https://{lan_ip}:{args.https_port}"
    bar = "=" * 70

    # Drop a marker file so other tools (and me) can find the active demo
    # without parsing terminal output.
    marker = Path("/tmp/vdotool-lan-demo.json")
    try:
        import json as _json
        marker.write_text(_json.dumps({
            "base_url": base_url,
            "lan_ip": lan_ip,
            "https_port": args.https_port,
            "writer_port": args.writer_port,
            "frames_dir": str(frames_dir),
            "writer_log": str(writer_log),
            "access_log": str(access_log),
            "log_dir": str(log_dir),
            "started_at": int(time.time()),
            "pid": os.getpid(),
        }, indent=2))
    except OSError:
        pass

    msg = f"""
{bar}
vdotool LAN demo is up.

   public URL (LAN):  {base_url}
   frames dir:        {frames_dir}
   writer log:        {writer_log}
   https access log:  {access_log}
   marker file:       {marker}

In ANOTHER terminal, run:

    export VDOTOOL_FRAMES_DIR={frames_dir}
    export VDOTOOL_VDO_BASE_URL={base_url}
    hermes chat

Then ask: "let's cook something simple together."
The agent will hand you a push_link. Open that link on your second
laptop's browser. It will warn about the self-signed cert -- click
Advanced -> Proceed -- then grant camera access.

Frames will land in {frames_dir}/<session_id>/
{bar}
"""
    print(msg, flush=True)

    # Wait for ctrl-c
    stop = threading.Event()

    def handle_sig(signum, frame):
        LOG.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        while not stop.is_set():
            time.sleep(0.5)
            if writer_proc.poll() is not None:
                LOG.error("writer subprocess exited unexpectedly (rc=%d)", writer_proc.returncode)
                stop.set()
    finally:
        LOG.info("stopping HTTPS server")
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        LOG.info("stopping writer")
        try:
            writer_proc.terminate()
            writer_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            writer_proc.kill()
        except ProcessLookupError:
            pass

        if cleanup_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
        shutil.rmtree(cert_dir, ignore_errors=True)
        # Leave the log dir so users can inspect post-mortem.
        try:
            marker.unlink()
        except (OSError, NameError):
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
