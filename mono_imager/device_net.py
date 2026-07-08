"""
mono-imager: recovery-shell network resolution.

Owns the session's device-network state (DHCP-first, verified, manual
fallback) and the re-apply-on-reboot cache. This used to live in
MonoImager as _setup_recovery_network() + helpers; it's device
orchestration, not TUI, so it lives here. tui.py keeps a thin
delegating wrapper and exposes .config via its own `device_net`
property so existing readers (print_header, journeys, ctx.device_net)
are unchanged.

Author:  H.A. Hermsen
License: GPLv3
"""

import sys
import logging
from typing import Optional

from mono_imager.spinner import with_spinner

logger = logging.getLogger(__name__)


def netmask_to_prefix(value: str) -> Optional[str]:
    """
    Accept either a dotted subnet mask (255.255.255.0) or a bare CIDR
    prefix length (24) from manual entry, and return the CIDR prefix
    string `ip addr add` needs. Returns None on anything unparseable
    or a non-contiguous mask, so the caller can re-prompt rather than
    silently apply a nonsense value.
    """
    value = value.strip()
    if not value:
        return None
    if "." not in value:
        return value if value.isdigit() and 0 <= int(value) <= 32 else None
    try:
        octets = [int(o) for o in value.split(".")]
    except ValueError:
        return None
    if len(octets) != 4 or any(not 0 <= o <= 255 for o in octets):
        return None
    bits = "".join(f"{o:08b}" for o in octets)
    if "01" in bits:  # a 0 followed by a 1 means the mask isn't contiguous
        return None
    return str(bits.count("1"))


class RecoveryNetwork:
    """
    Session-scoped device-network resolver for the recovery shell.

    config:   {"ip","prefix","gateway","dns","source","iface"} once
              resolved, else None. Recovery Linux forgets its network
              config every reboot, so resolve() re-applies the cached
              values on each fresh shell — but doesn't re-prompt.
    verified: True once real internet reachability (not just local
              config) has been proven for `config` at least once this
              session; lets later recovery boots skip the several-second
              ping re-check on an unchanged path.
    """

    def __init__(self):
        self.config: Optional[dict] = None
        self.verified: bool = False

    def resolve(self, d) -> bool:
        """
        Bring up Ethernet, then resolve a reachable device network:
        reuse cached config if still reachable, else DHCP, else manual
        entry (with a retry loop). Returns True once the device can
        reach the internet, False on give-up.
        """
        from mono_imager import recovery_orchestrator as rec
        from mono_imager import flash_orchestrator as core

        print()
        print("  Network setup — REQUIRED before 'firmware update' will work.")
        print("  'firmware update' needs the device to reach the internet directly.")
        print()

        # Bring up every eth* port, then auto-detect which one actually
        # has a cable (LOWER_UP) instead of assuming one physical jack.
        # Recovery Linux boots all eth ports administratively DOWN, so
        # LOWER_UP is never set until each candidate is brought up first.
        # Combined into a single round trip — each costs real seconds.
        try:
            bring_up_cmd = "; ".join(f"ip link set eth{n} up 2>/dev/null" for n in range(5))
            d.run_script(bring_up_cmd, marker="recovery_eth_up", exec_timeout=10)
        except Exception as e:
            print(f"  ❌ Failed to bring up Ethernet ports: {e}")
            return False

        try:
            # Poll for LOWER_UP instead of a blind sleep 2: returns as
            # soon as carrier appears (common case), still caps at ~2s
            # for a genuinely slow link, so worst case is unchanged.
            ip_output, _eth_err = with_spinner(
                d.run_script,
                "for i in 1 2 3 4; do ip link show | grep -q LOWER_UP && break; sleep 0.5; done; ip link show",
                marker="recovery_eth_check", exec_timeout=10,
                message="Detecting active Ethernet port..."
            )
            if _eth_err:
                raise _eth_err
            ifaces = core.parse_active_eth_ifaces(ip_output)
            if not ifaces:
                print("  ❌ No Ethernet port has a cable plugged in.")
                print("     Plug an Ethernet cable into any RJ-45 jack (not the SFP+ cages).")
                print()
                input("  Press Enter once the cable is plugged in...")
                ip_output = d.run_script("ip link show", marker="recovery_eth_check_retry", exec_timeout=5)
                ifaces = core.parse_active_eth_ifaces(ip_output)
                if not ifaces:
                    print("  ❌ Still no Ethernet port with a cable detected.")
                    return False
            print(f"  Port(s) reporting a link: {', '.join(ifaces)}")
        except Exception as e:
            print(f"  ❌ Failed to check Ethernet carrier: {e}")
            return False

        # LOWER_UP is unreliable on this board: several internal ports
        # (SerDes/SFP side) report a link with NO external cable, so a
        # LOWER_UP count > 1 does NOT mean multiple cables (#19 false
        # positive). Find the real uplink by which candidate actually
        # obtains a working DHCP lease; only treat it as a genuine
        # multi-uplink (and prompt) if more than one truly reaches the net.

        # Cached config: re-apply on whichever candidate still reaches the
        # net - the working port can differ from the previous boot.
        if self.config:
            net = self.config
            order = ([net["iface"]] if net.get("iface") in ifaces else []) + \
                    [c for c in ifaces if c != net.get("iface")]
            for cand in order:
                print(f"  Re-applying known config on {cand}: {net['ip']}/{net['prefix']} via {net['gateway']}...")
                if self._apply(d, cand, net) and self._verify(d, net["gateway"]):
                    self.verified = True
                    self.config = {**net, "iface": cand}
                    print(f"  ✓ Internet reachable via {cand} - network is ready.")
                    return True
            print("  ⚠ Previously-working config no longer reachable - re-resolving...")
            self.config = None
            self.verified = False

        # First time (or cache invalidated): DHCP-probe each candidate.
        working = []  # [(iface, lease)]
        for cand in ifaces:
            lease, _dhcp_err = with_spinner(
                rec.try_dhcp, d, cand,
                message=f"Attempting DHCP on {cand}..."
            )
            if _dhcp_err or not lease:
                continue
            if self._verify(d, lease["gateway"]):
                working.append((cand, lease))

        if len(working) > 1:
            # Genuinely multiple working uplinks - now the #19 prompt is real.
            chosen = self._select_iface(d, [w[0] for w in working])
            if chosen is None:
                return False
            working = [w for w in working if w[0] == chosen]

        if working:
            iface, lease = working[0]
            dns_note = f", DNS {lease['dns']}" if lease["dns"] else ""
            print(f"  DHCP lease on {iface}: {lease['ip']}/{lease['prefix']} via {lease['gateway']}{dns_note}")
            print(f"  ✓ Internet reachable via {iface} - network is ready.")
            self.config = {**lease, "source": "dhcp", "iface": iface}
            self.verified = True
            return True

        print()
        print("  ❌ No Ethernet port obtained a working DHCP lease.")
        print("  Falling back to manual network entry.")

        while True:
            net = self._prompt_manual()
            if net is None:
                return False
            # Try the static config on each candidate; use whichever reaches.
            applied = False
            for cand in ifaces:
                print(f"  Configuring {cand} = {net['ip']}/{net['prefix']}, gateway {net['gateway']}...")
                if self._apply(d, cand, net) and self._verify(d, net["gateway"]):
                    print(f"  ✓ Internet reachable via {cand} - network is ready.")
                    self.config = {**net, "source": "manual", "iface": cand}
                    self.verified = True
                    return True
            print("  ❌ None of the live ports reached the internet with that config.")
            print("     Check the gateway IP, cable, and network configuration.")
            retry = input("  Try entering the network settings again? [Y/n]: ").strip().lower()
            if retry == "n":
                return False

    def _apply(self, d, iface: str, net: dict) -> bool:
        """
        Statically (re-)apply a known ip/prefix/gateway/dns to iface.
        Used for cache-reuse (config lost on reboot) and manual entry —
        NOT for a fresh DHCP lease, which udhcpc's own bound script
        already applies. Flushes any existing address/route first so
        this is safe to call again in the same boot after a failed
        attempt (retry loop), not just once.
        """
        dns_cmd = f" && echo nameserver {net['dns']} > /etc/resolv.conf" if net.get("dns") else ""
        net_cmd = (
            f"ip addr flush dev {iface} 2>/dev/null; "
            f"ip link set {iface} up && "
            f"ip addr add {net['ip']}/{net['prefix']} dev {iface} && "
            f"ip route replace default via {net['gateway']} dev {iface}"
            f"{dns_cmd}; echo RC=$?"
        )
        try:
            output = d.run_script(net_cmd, marker="recovery_net_setup", exec_timeout=20)
        except RuntimeError as e:
            print(f"  ❌ Network setup failed on {iface}: {e}")
            return False

        if "RC=0" not in output:
            print(f"  ❌ Network setup did not report success on {iface}.")
            return False
        return True

    def _verify(self, d, gateway: str) -> bool:
        from mono_imager import recovery_orchestrator as rec
        result, error = with_spinner(
            rec.check_internet_reachable, d, gateway=gateway,
            message="Verifying real connectivity..."
        )
        if error is not None:
            return False
        return bool(result)

    def _select_iface(self, d, ifaces: list):
        """
        Multiple Ethernet ports have a live cable - a complex topology
        the tool does not support automatically (issue #19). Only one
        port can serve as the WAN uplink for 'firmware update', so ask
        the user which one, then set every OTHER eth* port 'link down'
        so nothing downstream (DHCP, routing) can pick the wrong port.

        Returns the chosen iface name, or None if the user aborts.
        """
        print()
        print("  ⚠ Multiple Ethernet ports have a live link:")
        for i, name in enumerate(ifaces, 1):
            print(f"      {i}) {name}")
        print()
        print("    This is a complex topology the tool cannot resolve on its")
        print("    own - only one port can be the WAN uplink. The others will")
        print("    be set 'link down'.")
        print()
        chosen = None
        while chosen is None:
            raw = input(f"  Select the WAN port [1-{len(ifaces)}] (or 'q' to quit): ").strip().lower()
            if raw == "q":
                print("  Quitting - remove the non-essential cables and re-run mono-imager.")
                sys.exit(0)
            try:
                chosen = ifaces[int(raw) - 1]
            except (ValueError, IndexError):
                print("  Invalid selection.")
                chosen = None
        # Set every other eth* port down (not just the live ones) so no
        # ambiguous carrier or route can survive downstream.
        others = [f"eth{n}" for n in range(5) if f"eth{n}" != chosen]
        down_cmd = "; ".join(f"ip link set {n} down 2>/dev/null" for n in others)
        try:
            d.run_script(down_cmd, marker="recovery_eth_down_unused", exec_timeout=10)
        except Exception as e:
            print(f"  ⚠ Could not set the other ports down: {e}")
        print(f"  Using {chosen}; the other Ethernet ports were set down.")
        return chosen

    def _prompt_manual(self) -> Optional[dict]:
        """
        Prompt for Device IP, subnet mask, gateway, and DNS. Returns
        None (caller should abort) if the user leaves a required field
        blank — DNS is optional since some networks resolve fine
        without one being explicitly set.
        """
        device_ip = input(
            "  Pick an unused IP address for the device on that same network "
            "(e.g. 192.168.1.50). Check your own machine's network adapter "
            "settings first if you're unsure of the IP/subnet/gateway on that "
            "network.\n  Device IP to assign: "
        ).strip()
        if not device_ip:
            print("  ❌ Device IP is required.")
            return None

        # Accept "IP/CIDR" (e.g. 10.1.9.32/24) in the address field: take
        # the prefix from it and skip the subnet-mask prompt. Without this
        # the "/24" stayed part of the IP and _apply() built an invalid
        # "10.1.9.32/24/24" (#17).
        prefix = None
        if "/" in device_ip:
            device_ip, _, cidr = device_ip.partition("/")
            device_ip = device_ip.strip()
            cidr = cidr.strip()
            if not device_ip:
                print("  ❌ Device IP is required.")
                return None
            if not (cidr.isdigit() and 0 <= int(cidr) <= 32):
                print(f"  ❌ Invalid CIDR prefix '/{cidr}': expected /0 to /32.")
                return None
            prefix = cidr
            print(f"  Using /{prefix} from the address you entered (skipping subnet mask).")

        if prefix is None:
            mask_raw = input("  Subnet mask (e.g. 255.255.255.0) [255.255.255.0]: ").strip() or "255.255.255.0"
            prefix = netmask_to_prefix(mask_raw)
            if prefix is None:
                print(f"  ❌ Invalid subnet mask: {mask_raw}")
                return None

        gateway = input("  Gateway (your router's IP on that network, e.g. 192.168.1.1): ").strip()
        if not gateway:
            print("  ❌ Gateway is required.")
            return None

        dns = input("  DNS server [8.8.8.8]: ").strip() or "8.8.8.8"

        return {"ip": device_ip, "prefix": prefix, "gateway": gateway, "dns": dns}
