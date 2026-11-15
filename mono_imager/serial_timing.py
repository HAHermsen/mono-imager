"""
mono-imager: serial timing constants, logger and verbose() shared by
serial_device.py and its mixins (serial_scripts.py, serial_boot.py).

Every value was found on real hardware; see the comments at the use sites
before changing one. Seconds unless noted. The logger keeps the name
"mono_imager.serial_device", so log lines read as before the split.

Author:  H.A. Hermsen
License: GPLv3
"""

import logging

from mono_imager.logging_setup import make_verbose

logger = logging.getLogger("mono_imager.serial_device")
verbose = make_verbose(logger)

EACCES = 13                        # errno: permission denied (tty without dialout/sudo)
DEFAULT_READ_TIMEOUT = 10.0        # pyserial read timeout of a new SerialDevice
DEFAULT_CMD_TIMEOUT = 5.0          # send_command() when no timeout is given
PORT_APPEAR_TIMEOUT = 30           # connect(): wait for the port to show up
PORT_WAIT_DEFAULT_TIMEOUT = 30.0   # wait_for_port() default
RECONNECT_PORT_TIMEOUT = 20        # _attempt_reconnect()
RETRY_PORT_TIMEOUT = 10            # _wait_for_root_shell(): per reconnect attempt
BAUD_PROBE_SETTLE = 0.5            # after poking a baud rate, before reading back
VERIFY_SHELL_TIMEOUT = 5.0
LINE_SETTLE = 0.3                  # _wait_for_line_idle(): silence that counts as idle
LINE_IDLE_MAX_WAIT = 5.0
LINE_IDLE_SHORT_WAIT = 2.0
POLL_SLEEP = 0.02                  # short poll interval, not a settling sleep
SCRIPT_WRITE_TIMEOUT = 10.0        # run_script()/launch_script() heredoc write
ECHO_BYTES_PER_S = 500             # worst-case console echo rate of a heredoc (wraps + redraws):
                                   # the post-write idle wait grows with the script size
AUTOBOOT_DEFAULT_TIMEOUT = 30
INTERRUPT_PROMPT_TIMEOUT = 5.0     # after the interrupt key, wait for "=>"
INTERRUPT_KEY_COUNT = 5            # interrupt_autoboot(): keypresses sent
INTERRUPT_KEY_GAP = 0.1
INTERRUPT_SETTLE = 0.5
DIAG_DEFAULT_TIMEOUT = 30
BOOT_POLL_TIMEOUT = 120.0          # boot_recovery()/boot_linux_staging()
BOOT_LOG_EVERY_BYTES = 2048        # debug-log the boot output every N bytes
LOGIN_STEP_TIMEOUT = 5.0           # wait for "Password:" / the shell prompt
STAGING_LOGIN_SETTLE = 1.0
STAGING_PW_SETTLE = 0.5
ARMBIAN_DEFAULT_PASSWORD = b"1234" # Armbian's documented first-boot password
BACKOFF_START = 0.5                # _wait_for_root_shell() retry back-off
BACKOFF_MAX = 5.0
ENTER_SETTLE = 0.3
PORT_POLL_SLEEP = 0.5
OUTPUT_DEFAULT_TIMEOUT = 60.0
OUTPUT_POLL_SLEEP = 0.2
MS_PER_S = 1000                    # script marker = current time in ms

# pyserial read() sizes (bytes). read(n) returns early on its timeout, so
# these only bound one read; they are kept exactly as tuned.
READ_PROMPT = 64
READ_PASSWORD = 128
READ_BOOT = 256
READ_LOGIN_BURST = 512
READ_CMD = 1024
READ_DIAG = 4096

# Characters of raw output kept in log lines
LOG_RAW_TAIL = 60
LOG_RAW_TAIL_LONG = 120
LOG_ERROR_TAIL = 500

__all__ = [
    "ARMBIAN_DEFAULT_PASSWORD",
    "AUTOBOOT_DEFAULT_TIMEOUT",
    "BACKOFF_MAX",
    "BACKOFF_START",
    "BAUD_PROBE_SETTLE",
    "BOOT_LOG_EVERY_BYTES",
    "BOOT_POLL_TIMEOUT",
    "DEFAULT_CMD_TIMEOUT",
    "DEFAULT_READ_TIMEOUT",
    "DIAG_DEFAULT_TIMEOUT",
    "EACCES",
    "ECHO_BYTES_PER_S",
    "ENTER_SETTLE",
    "INTERRUPT_KEY_COUNT",
    "INTERRUPT_KEY_GAP",
    "INTERRUPT_PROMPT_TIMEOUT",
    "INTERRUPT_SETTLE",
    "LINE_IDLE_MAX_WAIT",
    "LINE_IDLE_SHORT_WAIT",
    "LINE_SETTLE",
    "LOGIN_STEP_TIMEOUT",
    "LOG_ERROR_TAIL",
    "LOG_RAW_TAIL",
    "LOG_RAW_TAIL_LONG",
    "MS_PER_S",
    "OUTPUT_DEFAULT_TIMEOUT",
    "OUTPUT_POLL_SLEEP",
    "POLL_SLEEP",
    "PORT_APPEAR_TIMEOUT",
    "PORT_POLL_SLEEP",
    "PORT_WAIT_DEFAULT_TIMEOUT",
    "READ_BOOT",
    "READ_CMD",
    "READ_DIAG",
    "READ_LOGIN_BURST",
    "READ_PASSWORD",
    "READ_PROMPT",
    "RECONNECT_PORT_TIMEOUT",
    "RETRY_PORT_TIMEOUT",
    "SCRIPT_WRITE_TIMEOUT",
    "STAGING_LOGIN_SETTLE",
    "STAGING_PW_SETTLE",
    "VERIFY_SHELL_TIMEOUT",
    "logger",
    "verbose",
]
