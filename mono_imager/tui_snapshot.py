"""
mono-imager: TUI snapshot screens: take, restore, USB mount test and the done screen

Split out of tui.py. Mixed into MonoImager; module-level names are read
through mono_imager.tui (T.) so the existing patch points keep working.

Author:  H.A. Hermsen
License: GPLv3
"""

from typing import TYPE_CHECKING, Any, Optional

from mono_imager import tui as T


class TuiSnapshotMixin:
    """TUI snapshot screens: take, restore, USB mount test and the done screen."""

    if TYPE_CHECKING:  # attributes this mixin reads from MonoImager / sibling mixins
        serial_port: Optional[str]
        def print_screen_divider(self, *args: Any, **kwargs: Any) -> Any: ...
        def _select_port(self, *args: Any, **kwargs: Any) -> Any: ...

    def _snapshot_common(self, title: str):
        """Port + transfer (+ host IP and snapshot folder for LAN).
        -> (port, transfer, host_ip, store) or None (back)."""
        from mono_imager import flash_orchestrator as core
        from mono_imager import snapshot as S
        from mono_imager.config import load_config, save_config

        self.print_screen_divider()
        print(f"  {title}")
        print()
        port = self.serial_port
        if port:
            print(f"  Serial port: {port} (chosen at launch)")
        else:
            port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
            if port is None:
                return None
        print()
        print("  1) LAN — snapshots on this PC")
        print("  2) USB — snapshots on a USB stick in the Mono")
        print("  3) Back")
        c = input("Select [1-3]: ").strip()
        if c not in ("1", "2"):
            return None
        transfer = "lan" if c == "1" else "usb"
        host_ip, store = "", S.default_store()
        if transfer == "lan":
            host_ip = core.detect_host_ip()
            if not host_ip:
                print("  ❌ Could not auto-detect host IP.")
                input("  Press Enter to continue...")
                return None
            cfg = load_config()
            store = T.Path(cfg.get("snapshot_dir") or S.default_store())
            raw = input(f"  Snapshot folder on this PC [{store}]: ").strip().strip('"')
            if raw:
                store = T.Path(raw).expanduser()
            cfg["snapshot_dir"] = str(store)
            save_config(cfg)
        self.serial_port = port
        return port, transfer, host_ip, store

    def menu_snapshot(self):
        from mono_imager import snapshot_orchestrator as so
        picked = self._snapshot_common("Snapshot eMMC — the whole eMMC, bit for bit")
        if picked is None:
            self.current_state = T.MenuState.MAIN
            return
        port, transfer, host_ip, store = picked
        ok = so.run_snapshot(port=port, transfer=transfer, host_ip=host_ip,
                             http_port=self.net_http_port,
                             setup_network=self._setup_recovery_network, store=store,
                             soft_reboot=self._soft_reboot_if_possible)
        self._recovery_finish(ok)

    def menu_restore(self):
        from mono_imager import snapshot as S
        from mono_imager import snapshot_orchestrator as so
        picked = self._snapshot_common("Restore eMMC — overwrite the whole eMMC with a snapshot")
        if picked is None:
            self.current_state = T.MenuState.MAIN
            return
        port, transfer, host_ip, store = picked
        folder = manifest = None
        if transfer == "lan":
            snaps = S.list_snapshots(store)
            if not snaps:
                print(f"  No snapshots in {store}.")
                input("  Press Enter to continue...")
                self.current_state = T.MenuState.MAIN
                return
            idx = so.pick_snapshot(snaps)
            if idx is None:
                self.current_state = T.MenuState.MAIN
                return
            folder, manifest = store / snaps[idx][0], snaps[idx][1]
        auto = input("  Auto-snapshot the current eMMC first, so this can be undone? [Y/n]: ") \
            .strip().lower() != "n"
        ok = so.run_restore(port=port, transfer=transfer, host_ip=host_ip,
                            http_port=self.net_http_port,
                            setup_network=self._setup_recovery_network, store=store,
                            folder=folder, manifest=manifest, auto_snapshot=auto,
                            soft_reboot=self._soft_reboot_if_possible)
        self._recovery_finish(ok)

    def menu_test_usb_mount(self):
        """
        Verify a USB stick is connected, mountable, and (optionally)
        already staged with recognizable OS images — before starting a
        real USB flash journey. Hardware interaction, live progress, and
        the summary all live in diagnostics.test_usb(); this method
        only resolves the port (menu-navigation) and persists it once
        the mount is proven.
        """
        from mono_imager import diagnostics

        self.print_screen_divider()

        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = T.MenuState.MAIN
                return

        if diagnostics.test_usb(port):
            self.serial_port = port

        self.current_state = T.MenuState.MAIN

    def menu_done(self):
        """Flash result screen"""
        # DON'T clear screen — keep all debug output visible
        
        print()
        print("=" * T.C.DIVIDER_WIDTH)
        if self.flash_success:
            # Journey-specific (#25): OPNsense stays on NOR — telling every
            # OS to flip to eMMC sent OPNsense users into recovery Linux.
            from mono_imager.journeys import final_dip, DIP_LABELS
            dip = final_dip(self.os_name)
            print("✅ Flashing complete!")
            print()
            print("  ⚡ NEXT STEP: Set DIP Switch ⚡")
            print(f"  Move the DIP switch to: {DIP_LABELS[dip]}")
            if dip == "NOR":
                print(f"  ({self.os_name} boots via NOR's U-Boot — do NOT flip to eMMC,")
                print("   eMMC's U-Boot has factory defaults and boots recovery Linux.)")
            print()
            print("  Then power-cycle the device.")
            print("  It will boot with the new firmware.")
            print()
            print("  💡 Tip: You can watch it boot live via")
            print("     option 4 (CLI / raw serial console) from the main menu.")
        else:
            print("❌ Flashing did not complete successfully.")
            print()
            print("  Check the log output above for details.")
            print("  You can retry from the main menu.")

        print("=" * T.C.DIVIDER_WIDTH)
        print()
        if self.log_file:
            T.verbose(f"Result: {'OK' if self.flash_success else 'NOK'}")
            T.verbose(f"📄 Report saved to: {self.log_file}")
            print(f"  📄 Report: {self.log_file}")
            print()
        input("Press Enter to continue...")
        self.flash_success = False
        self.current_state = T.MenuState.MAIN
