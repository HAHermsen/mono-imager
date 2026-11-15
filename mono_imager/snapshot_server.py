"""
mono-imager: HTTP side of eMMC snapshot / restore over LAN (#29).

Extends the existing firmware server (flash_orchestrator._FirmwareHandler,
which keeps the /report endpoints the device posts its results to) with
streaming endpoints, so multi-GB images never sit in RAM on either side:

  PUT /upload?token&chunk=<i>  raw eMMC bytes in; compressed to the chunk
                               file on the PC while SHA-256 + size are taken
  PUT /verify?token&tag=<t>    raw bytes in; only hashed (read-back checks
                               when the device has no sha256sum, firmware
                               region hash)
  PUT /nor?token               read-only NOR dump -> nor.bin
  GET /image?token&k&chunk&off&len
                               decompressed range of a snapshot chunk out;
                               the PC hashes what it sent

curl on the device sends `-T -` uploads with chunked transfer encoding;
both chunked and Content-Length bodies are accepted. A random token per
session keeps stray requests from writing anything.

Author:  H.A. Hermsen
License: GPLv3
"""

import hashlib
import secrets
import threading
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mono_imager import snapshot as S
from mono_imager.flash_orchestrator import _FirmwareHandler, verbose

_BLOCK = S.MIB                    # streaming block size
TOKEN_BYTES = 16                  # session token: 128 random bits
WAIT_POLL = 0.2                   # SnapshotSession.wait() poll interval
CHUNK_LINE_MAX = 1024             # longest chunk-size / trailer line accepted
MAX_CHUNKS = 1000                 # 1000 GiB — far above any eMMC
SERVABLE_FILES = {S.NOR_ENV_FILE} # GET /file whitelist


class SnapshotSession:
    """Shared state between the HTTP handler threads and the orchestrator."""

    def __init__(self):
        self.token = secrets.token_hex(TOKEN_BYTES)
        self.lock = threading.Lock()
        self.target_dir: Path | None = None     # snapshot being written
        self.compression = S.host_compression()
        self.source_dir: Path | None = None     # snapshot being restored
        self.source_manifest: dict | None = None
        self.uploads, self.verify, self.sent = {}, {}, {}
        self.nor = None

    def begin_snapshot(self, target_dir: Path, compression: str | None = None):
        with self.lock:
            self.target_dir = Path(target_dir)
            self.compression = compression or S.host_compression()
            self.uploads, self.verify, self.nor = {}, {}, None

    def begin_restore(self, source_dir: Path, manifest: dict):
        with self.lock:
            self.source_dir, self.source_manifest = Path(source_dir), manifest
            self.sent, self.verify = {}, {}

    def wait(self, kind: str, key, timeout: float) -> dict | None:
        """Wait until a result for kind/key exists (uploads/verify/sent/nor)."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                val = self.nor if kind == "nor" else getattr(self, kind).get(key)
            if val is not None:
                return val
            time.sleep(WAIT_POLL)
        return None


def _iter_body(rfile, headers):
    """Yield the request body in pieces: chunked or Content-Length."""
    if "chunked" in (headers.get("Transfer-Encoding") or "").lower():
        while True:
            line = rfile.readline(CHUNK_LINE_MAX)
            if not line:
                raise ConnectionError("connection closed inside chunked body")
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while rfile.readline(CHUNK_LINE_MAX) not in (b"\r\n", b"\n", b""):
                    pass
                return
            left = size
            while left:
                piece = rfile.read(min(left, _BLOCK))
                if not piece:
                    raise ConnectionError("connection closed inside a chunk")
                left -= len(piece)
                yield piece
            rfile.readline(CHUNK_LINE_MAX)  # CRLF after each chunk
    else:
        left = int(headers.get("Content-Length") or 0)
        while left:
            piece = rfile.read(min(left, _BLOCK))
            if not piece:
                raise ConnectionError("connection closed early")
            left -= len(piece)
            yield piece


class _SnapshotHandler(_FirmwareHandler):
    session: SnapshotSession | None = None

    def _params(self):
        parsed = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return parsed.path, qs

    def _authorized(self, qs) -> bool:
        if self.session is None or qs.get("token") != self.session.token:
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        return True

    def _ok(self, body: bytes = b"OK\n"):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        path, qs = self._params()
        if path not in ("/upload", "/verify", "/nor"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized(qs):
            return
        sess = self.session
        h, n, err = hashlib.sha256(), 0, ""
        try:
            if path == "/upload":
                i = int(qs["chunk"])
                if not 0 <= i < MAX_CHUNKS or sess.target_dir is None:
                    raise ValueError("bad chunk / no snapshot in progress")
                name = S.chunk_file(i, sess.compression)
                with S.open_writer(sess.target_dir / name, sess.compression) as f:
                    for piece in _iter_body(self.rfile, self.headers):
                        h.update(piece)
                        n += len(piece)
                        f.write(piece)
                rec = {"bytes": n, "sha256": h.hexdigest(), "file": name,
                       "compression": sess.compression}
                with sess.lock:
                    sess.uploads[i] = rec
            elif path == "/nor":
                if sess.target_dir is None:
                    raise ValueError("no snapshot in progress")
                with open(sess.target_dir / "nor.bin", "wb") as f:
                    for piece in _iter_body(self.rfile, self.headers):
                        h.update(piece)
                        n += len(piece)
                        f.write(piece)
                with sess.lock:
                    sess.nor = {"file": "nor.bin", "bytes": n, "sha256": h.hexdigest()}
            else:
                tag = qs.get("tag", "")
                for piece in _iter_body(self.rfile, self.headers):
                    h.update(piece)
                    n += len(piece)
                with sess.lock:
                    sess.verify[tag] = {"bytes": n, "sha256": h.hexdigest()}
        except Exception as e:  # recorded, the orchestrator decides
            err = str(e)
            verbose(f"snapshot server {path}: {e}", "error")
            with sess.lock:
                if path == "/upload" and "chunk" in qs:
                    sess.uploads[int(qs["chunk"])] = {"error": err, "bytes": n}
                elif path == "/verify":
                    sess.verify[qs.get("tag", "")] = {"error": err, "bytes": n}
                elif path == "/nor":
                    sess.nor = {"error": err, "bytes": n}
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._ok()

    def do_GET(self):
        path, qs = self._params()
        if path == "/report":
            self._handle_report(urlparse(self.path))
            return
        if path == "/file":
            self._serve_file(qs)
            return
        if path != "/image":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized(qs):
            return
        sess = self.session
        try:
            k, i = int(qs["k"]), int(qs["chunk"])
            off, length = int(qs["off"]), int(qs["len"])
            chunk = next(c for c in sess.source_manifest["chunks"] if c["index"] == i)
            if off < 0 or length <= 0 or off + length > chunk["bytes"]:
                raise ValueError("range outside the chunk")
        except Exception as e:
            verbose(f"snapshot server /image: bad request {self.path}: {e}", "error")
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        h_sent, h_chunk, sent = hashlib.sha256(), hashlib.sha256(), 0
        full = off == 0 and length == chunk["bytes"]
        err = ""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            with S.open_reader(sess.source_dir / chunk["file"], chunk["compression"]) as f:
                skip = off
                while skip:
                    b = f.read(min(skip, _BLOCK))
                    if not b:
                        raise ValueError("chunk file is shorter than the manifest says")
                    skip -= len(b)
                while sent < length:
                    b = f.read(min(length - sent, _BLOCK))
                    if not b:
                        raise ValueError("chunk file is shorter than the manifest says")
                    h_sent.update(b)
                    if full:
                        h_chunk.update(b)
                    self.wfile.write(b)
                    sent += len(b)
        except Exception as e:
            err = str(e)
            verbose(f"snapshot server /image k={k}: {e}", "error")
        rec = {"bytes": sent, "sha256": h_sent.hexdigest(), "error": err}
        if full and not err:
            rec["manifest_ok"] = h_chunk.hexdigest() == chunk["sha256"]
        with sess.lock:
            sess.sent[k] = rec


    def _serve_file(self, qs):
        """GET /file?token&name — a small whitelisted file of the snapshot
        being restored (the saved NOR env), for the opt-in NOR env write."""
        if not self._authorized(qs):
            return
        sess = self.session
        name = qs.get("name", "")
        if name not in SERVABLE_FILES or sess.source_dir is None \
                or not (sess.source_dir / name).is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._ok((sess.source_dir / name).read_bytes())


def start_snapshot_server(host_ip: str, port: int, session: SnapshotSession):
    """Start the snapshot HTTP server in a daemon thread. Returns it, or None."""
    try:
        with _FirmwareHandler._reports_lock:
            _FirmwareHandler._reports.clear()
        handler = type("Handler", (_SnapshotHandler,), {"session": session, "firmware_path": None})
        server = ThreadingHTTPServer((host_ip, port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        verbose(f"✓ Snapshot server listening on http://{host_ip}:{port}/", "debug")
        return server
    except OSError as e:
        verbose(f"Failed to start snapshot server: {e}", "error")
        return None
