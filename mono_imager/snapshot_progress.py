"""
mono-imager: live 0..100% progress text for one LAN snapshot chunk.

The PC sees every byte a chunk uploads (and, for a read-back without
sha256sum on the device, every byte of the read-back), so it can show a
real percentage and speed without asking the device. A step that runs
only on the device (hashing from the page cache, or the read-back hashed
by sha256sum) has no byte count on the PC; the text then names that step.
A speed that stops moving shows a stalled transfer.

Pure logic: the session and the clock are passed in (tests replace them).

Author:  H.A. Hermsen
License: GPLv3
"""

import time
from collections.abc import Callable

from mono_imager import snapshot as S

PERCENT_FULL = 100
UPLOADS = "uploads"                  # SnapshotSession counters / results: chunk uploads
VERIFY = "verify"                    # SnapshotSession counters / results: hash-only PUTs
READ_BACK_TAG = "c{}"                # verify tag of chunk i's read-back (see snapshot_scripts)
DEVICE_HASH_TEXT = "upload 100% — hashing on device…"
DEVICE_READ_BACK_TEXT = "upload 100% — read-back on device…"
WAITING_TEXT = "upload 0% — waiting for data…"


def transfer_text(what: str, done: int, total: int, seconds: float) -> str:
    """'upload 47% (481 of 1024 MiB, 41 MiB/s)'. Speed is left out until time has passed."""
    pct = min(PERCENT_FULL, done * PERCENT_FULL // total) if total > 0 else PERCENT_FULL
    text = f"{what} {pct}% ({done // S.MIB} of {total // S.MIB} MiB"
    if seconds > 0:
        text += f", {done / S.MIB / seconds:.0f} MiB/s"
    return text + ")"


class ChunkProgress:
    """Callable for Spinner(status=...): the progress text of chunk `index`
    (`total` bytes) right now. single_pass / have_sha say what follows the
    upload: a device hash (single pass, or read-back with sha256sum) or a
    read-back streamed to the PC (no sha256sum on the device)."""

    def __init__(self, sess, index: int, total: int, *, single_pass: bool, have_sha: bool,
                 clock: Callable[[], float] = time.monotonic):
        self.sess, self.index, self.total = sess, index, total
        self.single_pass, self.have_sha, self.clock = single_pass, have_sha, clock
        self._first: dict = {}    # phase -> clock time its first byte was seen

    def _since_first(self, phase: str, done: int) -> float:
        if done <= 0:
            return 0.0
        start = self._first.setdefault(phase, self.clock())
        return self.clock() - start

    def __call__(self) -> str:
        with self.sess.lock:
            uploaded = self.index in self.sess.uploads
        if not uploaded:
            done = self.sess.received(UPLOADS, self.index)
            if done <= 0:
                return WAITING_TEXT
            return transfer_text("upload", done, self.total, self._since_first(UPLOADS, done))
        if self.single_pass:
            return DEVICE_HASH_TEXT
        if self.have_sha:
            return DEVICE_READ_BACK_TEXT
        done = self.sess.received(VERIFY, READ_BACK_TAG.format(self.index))
        return "upload 100% — " + transfer_text("read-back", done, self.total,
                                                 self._since_first(VERIFY, done))
