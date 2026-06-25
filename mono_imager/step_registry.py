#!/usr/bin/env python3
"""
mono-imager: Step Registry (v0.9.1)

Decorator-based step registry. Steps declare os=, transfer=,
requires=, produces=. FlowRunner resolves the correct sequence
per (os, transfer) pair automatically.

ADDING A STEP:
    @register_step(
        os=["MyOS"],
        transfer=["network", "usb"],   # or [ALL_TRANSFER]
        requires=["network_up"],
        produces=["os_flashed"],
        label="Flash image"
    )
    def step_flash_myos(ctx: StepContext) -> bool:
        ...

ADDING A JOURNEY:
    1. Tag existing steps with the new os= / transfer= values
    2. Add only steps that are genuinely new for that journey
    3. FlowRunner builds the sequence from requires/produces

Author:  H.A. Hermsen
Version: 0.9.1
License: MIT
"""

__version__ = "0.9.1"
__author__  = "H.A. Hermsen"

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinels
ALL_OS       = "__all__"
ALL_TRANSFER = "__all__"


# ============================================================================
# STEP CONTEXT
# ============================================================================

@dataclass
class StepContext:
    """
    All runtime state shared across every step in a journey.
    Adding a field here never breaks existing step signatures.
    """
    # Hardware
    device:        Any            = None   # SerialDevice

    # Journey identity
    os_name:       str            = ""
    transfer:      str            = ""     # "network" | "usb"

    # Network (used by network journeys)
    host_ip:       str            = ""
    device_ip:     str            = ""
    http_port:     int            = 8080
    device_mac:    str            = ""

    # Firmware source
    firmware_path: Optional[Path] = None   # local path (network: served; usb: on stick)
    flash_target:  str            = "/dev/mmcblk0"

    # USB (used by usb journeys)
    usb_device:    str            = "/dev/sda"   # block device on the Mono Gateway
    usb_mount:     str            = "/mnt/usb"   # where it gets mounted on-device

    # Runtime state (produced by steps, consumed by later steps)
    state:         dict           = field(default_factory=dict)

    def set(self, key: str, value: Any):
        self.state[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.state


# ============================================================================
# STEP DESCRIPTOR
# ============================================================================

@dataclass(eq=False)
class StepDescriptor:
    fn:       Callable
    os:       list[str]    # OS names or [ALL_OS]
    transfer: list[str]    # transfer methods or [ALL_TRANSFER]
    requires: list[str]    # state keys needed before this step
    produces: list[str]    # state keys set by this step on success
    label:    str


# ============================================================================
# REGISTRY
# ============================================================================

_registry: list[StepDescriptor] = []


def register_step(
    os:       list[str],
    transfer: list[str] = None,
    requires: list[str] = None,
    produces: list[str] = None,
    label:    str       = "",
):
    """
    Decorator: register a step function.

    Args:
        os:       OS names this step applies to, or [ALL_OS].
        transfer: Transfer methods ("network", "usb"), or [ALL_TRANSFER].
        requires: State keys that must exist before this step runs.
        produces: State keys this step marks present on success.
        label:    Human-readable name shown in progress output.
    """
    def decorator(fn: Callable) -> Callable:
        _registry.append(StepDescriptor(
            fn       = fn,
            os       = os,
            transfer = transfer or [ALL_TRANSFER],
            requires = requires or [],
            produces = produces or [],
            label    = label or fn.__name__,
        ))
        return fn
    return decorator


# ============================================================================
# FLOW RUNNER
# ============================================================================

class FlowRunner:
    """
    Resolves and executes the step sequence for a given (os, transfer) pair.

    Resolution:
      1. Filter registry to steps matching os + transfer
      2. Topological sort by requires/produces
      3. Execute in order; stop on first failure
    """

    def __init__(self, os_name: str, transfer: str, ctx: StepContext):
        self.os_name  = os_name
        self.transfer = transfer
        self.ctx      = ctx
        self._steps   = self._resolve()

    def _resolve(self) -> list[StepDescriptor]:
        applicable = [
            s for s in _registry
            if (ALL_OS in s.os or self.os_name in s.os)
            and (ALL_TRANSFER in s.transfer or self.transfer in s.transfer)
        ]

        # Topological sort (Kahn's algorithm)
        name_to_steps: dict[str, list[StepDescriptor]] = {}
        for s in applicable:
            for key in s.produces:
                name_to_steps.setdefault(key, []).append(s)

        in_degree: dict[StepDescriptor, int] = {s: 0 for s in applicable}
        edges: dict[StepDescriptor, list[StepDescriptor]] = {s: [] for s in applicable}

        for s in applicable:
            for req in s.requires:
                for producer in name_to_steps.get(req, []):
                    if producer is not s:
                        edges[producer].append(s)
                        in_degree[s] += 1

        queue  = [s for s in applicable if in_degree[s] == 0]
        result = []
        while queue:
            queue.sort(key=lambda s: _registry.index(s))
            current = queue.pop(0)
            result.append(current)
            for neighbor in edges[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(applicable):
            logger.error("FlowRunner: circular dependency detected")
            for s in applicable:
                if s not in result:
                    result.append(s)

        return result

    def steps_for(self) -> list[StepDescriptor]:
        return self._steps

    def run(self) -> bool:
        from mono_imager.flash_orchestrator import verbose, reset_results
        reset_results()

        verbose(f"Journey: {self.os_name} via {self.transfer} — {len(self._steps)} steps")
        verbose("=" * 60)

        for i, descriptor in enumerate(self._steps, 1):
            missing = [r for r in descriptor.requires if not self.ctx.has(r)]
            if missing:
                verbose(f"✗ '{descriptor.label}' cannot run — missing: {missing}", "error")
                return False

            verbose(f"[{i}/{len(self._steps)}] {descriptor.label}")

            try:
                ok = descriptor.fn(self.ctx)
            except Exception as e:
                logger.exception(f"Step '{descriptor.label}' raised: {e}")
                verbose(f"✗ '{descriptor.label}' raised: {e}", "error")
                ok = False

            if not ok:
                verbose(f"✗ '{descriptor.label}' FAILED — stopping journey", "error")
                return False

            for key in descriptor.produces:
                if not self.ctx.has(key):
                    self.ctx.set(key, True)

        verbose("=" * 60)
        verbose(f"✓ Journey complete — all {len(self._steps)} steps passed")
        return True


# ============================================================================
# CONVENIENCE
# ============================================================================

def run_journey(os_name: str, transfer: str, ctx: StepContext) -> bool:
    return FlowRunner(os_name, transfer, ctx).run()


def list_journey(os_name: str, transfer: str) -> list[str]:
    """Return ordered step labels without running — for preview/testing."""
    dummy_ctx = StepContext(os_name=os_name, transfer=transfer)
    return [s.label for s in FlowRunner(os_name, transfer, dummy_ctx).steps_for()]
