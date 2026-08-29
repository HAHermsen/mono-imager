"""
mono-imager: Shared step-result bookkeeping.

flash_orchestrator.py and recovery_orchestrator.py each track their own
sequence of pass/fail steps for a print_report() at the end of a run.
The bookkeeping (format a PASS/FAIL line, log it, append it to a results
list, optionally auto-number it) was identical between the two modules;
only which logger to use, and whether step numbers auto-increment,
differed. That shared shape lives here as StepTracker.

Each orchestrator still owns its OWN StepTracker instance (and thus its
own `results` list) rather than sharing one — mixing the two modules'
results in a single list is exactly the stale-state bug class described
in recovery_orchestrator.py's module docstring. print_report() itself
also stays per-module: flash_orchestrator's and recovery_orchestrator's
verdict/output formatting differ in real, deliberate ways (e.g. whether
an empty result set counts as OK), not just cosmetically.

Author:  H.A. Hermsen
License: GPLv3
"""

import itertools


class StepTracker:
    """
    Accumulates (num, description, passed, reason) tuples for one
    orchestration run and logs each as it happens.

    auto_number=True: a step() call with num=0 is assigned the next
    number from an internal counter (flash_orchestrator's behaviour,
    used by journey steps that don't track their own step numbers).
    auto_number=False: num is always used as given (recovery_orchestrator's
    behaviour — every call site passes an explicit number).
    """

    def __init__(self, file_logger, console_logger, auto_number: bool = False):
        self.results: list[tuple[int, str, bool, str]] = []
        self._file_logger    = file_logger
        self._console_logger = console_logger
        self._auto_number    = auto_number
        self._step_seq       = itertools.count(1)

    def reset(self):
        """Clear accumulated results before a new attempt. Mutates the
        list in place (.clear(), not reassignment) so callers holding a
        reference to .results — including this module's own callers —
        keep seeing the same, now-empty list rather than a stale one."""
        self.results.clear()
        self._step_seq = itertools.count(1)

    def step(self, num: int, description: str, passed: bool, reason: str = "") -> bool:
        if self._auto_number and num == 0:
            num = next(self._step_seq)
        mark = "✓" if passed else "✗"

        # File gets the full technical detail: step number, PASS/FAIL, reason.
        file_msg = f"Step {num:02d}: {'✓ PASS' if passed else '✗ FAIL'} — {description}"
        if reason:
            file_msg += f" ({reason})"
        log = self._file_logger.info if passed else self._file_logger.error
        log(file_msg)

        # Console gets a short, plain line — no step numbers, no technical
        # reason strings (those are jargon like "wc -c returned unparseable
        # output" that mean nothing to someone just running the tool).
        self._console_logger.info(f"  {mark} {description}")

        self.results.append((num, description, passed, reason))
        return passed
