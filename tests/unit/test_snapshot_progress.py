"""
mono-imager: live chunk progress (snapshot_progress.py) and the Spinner status line.

No hardware, no network. Covers:
  - transfer_text: percentage, MiB, speed, 0 / full / empty chunk
  - ChunkProgress: waiting -> upload 0..100% -> device hash (single pass),
    device read-back (thorough + sha256sum), read-back % on the PC
    (thorough, no sha256sum); a frozen counter keeps the speed falling
  - SnapshotSession counters: received(), reset by begin_snapshot
  - the real HTTP server counts a streamed upload while it arrives
  - Spinner shows the status text and wipes the whole line on exit

Run: python tests/unit/test_snapshot_progress.py
"""

import http.client
import io
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mono_imager import snapshot as S
from mono_imager import snapshot_progress as P
from mono_imager.snapshot_server import SnapshotSession, start_snapshot_server
from mono_imager.spinner import Spinner, with_spinner

CHUNK = 1024 * S.MIB                 # a real 1 GiB chunk
HALF = CHUNK // 2
LOCALHOST = "127.0.0.1"
UPLOAD_BYTES = 4 * S.MIB             # streamed through the real server
PIECE = S.MIB // 4                   # body piece size in the server test
SERVER_WAIT_S = 10                   # wait for the server to count / finish
POLL_S = 0.01
SPIN_S = 0.4                         # let the spinner draw a few frames

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {label}")
        passed += 1
    else:
        print(f"  FAIL: {label}")
        failed += 1


def section(title):
    print("=" * 60)
    print(title)
    print("=" * 60)


class Clock:
    """Fake monotonic clock."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ----------------------------------------------------------------------------
section("transfer_text")
check("half a chunk after 10 s: 50%, MiB, speed",
      P.transfer_text("upload", HALF, CHUNK, 10.0) == "upload 50% (512 of 1024 MiB, 51 MiB/s)")
check("no time passed yet: no speed", P.transfer_text("upload", S.MIB, CHUNK, 0.0) == "upload 0% (1 of 1024 MiB)")
check("full chunk: 100%", P.transfer_text("upload", CHUNK, CHUNK, 1.0).startswith("upload 100% "))
check("more than the chunk: capped at 100%", P.transfer_text("upload", CHUNK + 1, CHUNK, 1.0).startswith("upload 100% "))
check("empty chunk: 100%, no division by zero", P.transfer_text("upload", 0, 0, 1.0).startswith("upload 100% "))

# ----------------------------------------------------------------------------
section("ChunkProgress: single pass")
sess, clk = SnapshotSession(), Clock()
pr = P.ChunkProgress(sess, 3, CHUNK, single_pass=True, have_sha=True, clock=clk)
check("nothing arrived: waiting", pr() == P.WAITING_TEXT)
sess._count(P.UPLOADS, 3, S.MIB)
first = pr()
check("first bytes: upload 0%, no speed yet", first == "upload 0% (1 of 1024 MiB)")
clk.t = 10.0
sess._count(P.UPLOADS, 3, HALF)
check("after 10 s: upload 50%, speed", pr() == "upload 50% (512 of 1024 MiB, 51 MiB/s)")
clk.t = 20.0
check("counter frozen: same %, speed drops (stall visible)", pr() == "upload 50% (512 of 1024 MiB, 26 MiB/s)")
sess._count(P.UPLOADS, 2, CHUNK)
check("another chunk's counter does not count", pr().startswith("upload 50% "))
sess.uploads[3] = {"bytes": CHUNK}
check("upload recorded: hashing on device", pr() == P.DEVICE_HASH_TEXT)

section("ChunkProgress: thorough verify")
sess = SnapshotSession()
pr = P.ChunkProgress(sess, 0, CHUNK, single_pass=False, have_sha=True, clock=Clock())
sess.uploads[0] = {"bytes": CHUNK}
check("sha256sum on the device: read-back on device", pr() == P.DEVICE_READ_BACK_TEXT)
sess, clk = SnapshotSession(), Clock()
pr = P.ChunkProgress(sess, 0, CHUNK, single_pass=False, have_sha=False, clock=clk)
sess.uploads[0] = {"bytes": CHUNK}
check("no sha256sum: read-back 0% on the PC", pr() == "upload 100% — read-back 0% (0 of 1024 MiB)")
sess._count(P.VERIFY, P.READ_BACK_TAG.format(0), S.MIB)
pr()
clk.t = 4.0
sess._count(P.VERIFY, P.READ_BACK_TAG.format(0), HALF)
check("no sha256sum: read-back 50%, speed", pr() == "upload 100% — read-back 50% (512 of 1024 MiB, 128 MiB/s)")

section("SnapshotSession counters")
sess = SnapshotSession()
check("unknown key: 0", sess.received(P.UPLOADS, 7) == 0)
sess._count(P.UPLOADS, 7, 123)
check("counted bytes returned", sess.received(P.UPLOADS, 7) == 123)
with tempfile.TemporaryDirectory() as tmp:
    sess.begin_snapshot(Path(tmp), "raw")
check("begin_snapshot resets the counters", sess.received(P.UPLOADS, 7) == 0)

# ----------------------------------------------------------------------------
section("real server: an upload is counted while it streams in")


def free_port():
    with socket.socket() as so:
        so.bind((LOCALHOST, 0))
        return so.getsockname()[1]


with tempfile.TemporaryDirectory() as tmp:
    sess = SnapshotSession()
    sess.begin_snapshot(Path(tmp), "raw")
    port = free_port()
    with patch("builtins.print"):
        srv = start_snapshot_server(LOCALHOST, port, sess)
    gate = threading.Event()
    seen_mid = []

    def body():
        for k in range(UPLOAD_BYTES // PIECE):
            if k == UPLOAD_BYTES // PIECE // 2:
                end = time.time() + SERVER_WAIT_S
                while sess.received(P.UPLOADS, 0) < UPLOAD_BYTES // 2 and time.time() < end:
                    time.sleep(POLL_S)
                seen_mid.append(sess.received(P.UPLOADS, 0))
            yield b"\0" * PIECE
        gate.set()

    conn = http.client.HTTPConnection(LOCALHOST, port, timeout=SERVER_WAIT_S)
    conn.request("PUT", f"/upload?token={sess.token}&chunk=0", body=body(), encode_chunked=True)
    status = conn.getresponse().status
    conn.close()
    srv.shutdown()
    check("mid-stream: server counted half the upload before the rest was sent",
          seen_mid == [UPLOAD_BYTES // 2])
    check("upload completed (HTTP 200), final count = all bytes",
          status == 200 and sess.received(P.UPLOADS, 0) == UPLOAD_BYTES
          and sess.uploads[0]["bytes"] == UPLOAD_BYTES)

# ----------------------------------------------------------------------------
section("Spinner status line")
out = io.StringIO()
texts = iter(["upload 10% (a long status text that is wider)"] + ["upload 99%"] * 1000)
with patch("sys.stdout", out):
    with Spinner("Chunk 1/30: test", status=lambda: next(texts)):
        time.sleep(SPIN_S)
drawn = out.getvalue()
check("status text drawn after the timer", "0:00  upload 10% (a long" in drawn and "0:00  upload 99%" in drawn)
last = drawn.rstrip("\x1b[?25h").split("\r")
widest = max(len(x) for x in drawn.split("\r"))
check("exit wipes at least the widest line drawn", any(set(x) == {" "} and len(x) >= widest - len("\x1b[32m\x1b[0m") for x in last))
out = io.StringIO()
with patch("sys.stdout", out):
    r, e = with_spinner(lambda a, b=0: a + b, 1, b=2, message="m", status=lambda: "st")
check("with_spinner: status not passed on to fn, result intact", r == 3 and e is None)
out = io.StringIO()
with patch("sys.stdout", out):
    r, e = with_spinner(lambda: 5, message="m")
check("with_spinner without status: unchanged", r == 5 and e is None)

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)
sys.exit(1 if failed else 0)
