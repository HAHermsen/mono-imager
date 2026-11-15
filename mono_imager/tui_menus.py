"""
mono-imager: TUI menus: main menu, flash, network and update screens, serial and LAN tests

Split out of tui.py. Mixed into MonoImager; module-level names are read
through mono_imager.tui (T.) so the existing patch points keep working.

Author:  H.A. Hermsen
License: GPLv3
"""

from typing import TYPE_CHECKING, Any, Optional

from mono_imager import tui as T


class TuiMenusMixin:
    """TUI menus: main menu, flash, network and update screens, serial and LAN tests."""

    if TYPE_CHECKING:  # attributes this mixin reads from MonoImager / sibling mixins
        serial_port: Optional[str]
        def safe_input(self, prompt: str) -> str | None: ...
        def _validate_firmware_path(self, *args: Any, **kwargs: Any) -> Any: ...
        _net: Any
        def _select_port(self, *args: Any, **kwargs: Any) -> Any: ...
        def print_screen_divider(self, *args: Any, **kwargs: Any) -> Any: ...

    def menu_main(self):
        """Main menu — action first. See print_screen_divider()."""
        self.print_screen_divider()
        print("What would you like to do?")
        print()
        print("  1) Flash OS")
        print("  2) Update eMMC firmware")
        print("  3) Update NOR firmware")
        print("  4) CLI only (serial)")
        print("  5) Test Serial connection")
        print("  6) Test LAN connection")
        print("  7) Test USB stick")
        print("  8) Show Device Stats")
        print("  9) Snapshot eMMC")
        print("  10) Restore eMMC")
        print("  11) Exit")
        print()

        choice = input("Select [1-11]: ").strip()

        if choice == "1":
            self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL
        elif choice == "2":
            self.current_state = T.MenuState.UPDATE_EMMC
        elif choice == "3":
            self.current_state = T.MenuState.UPDATE_NOR
        elif choice == "4":
            self.current_state = T.MenuState.CLI_CONSOLE
        elif choice == "5":
            self.menu_test_serial()
        elif choice == "6":
            self.menu_test_lan()
        elif choice == "7":
            self.menu_test_usb_mount()
        elif choice == "8":
            self.current_state = T.MenuState.DEVICE_STATS
        elif choice == "9":
            self.current_state = T.MenuState.SNAPSHOT
        elif choice == "10":
            self.current_state = T.MenuState.RESTORE
        elif choice == "11":
            T.sys.exit(0)
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.MAIN

    def menu_flash_auto_or_manual(self):
        self.print_screen_divider()
        print("  1) Fully Auto — flash via LAN or USB")
        print("  2) Back")
        print()

        choice = input("Select [1-2]: ").strip()

        if choice == "1":
            self.current_state = T.MenuState.NETWORK_AUTO_CONFIG
        elif choice == "2":
            self.current_state = T.MenuState.MAIN
        else:
            print("  Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL

    def menu_network_auto_config(self):
        """
        Fully-auto path. Only the firmware file is asked; the serial
        port is the one chosen at launch (self.serial_port). If none
        was chosen this session, the normal port picker runs: it
        auto-selects a single port and asks when there are several
        (#27 — never just the first port in the list). Host IP is
        auto-detected, device IP is auto-derived from it, HTTP port
        defaults to 8080, flash target defaults to /dev/mmcblk0.
        """
        from mono_imager import flash_orchestrator as core

        self.print_screen_divider()
        print("  Fully Auto")
        print()

        port = self.serial_port
        if port:
            print(f"  Serial port: {port} (chosen at launch)")
        else:
            port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
            if port is None:
                self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL
                return
        print()
        print("  ℹ️  Images over ~3GB auto-switch to streaming flash mode (slower, no local copy).")
        print()

        # OS selection — built dynamically from discovered journey files
        picked = self._auto_pick_journey()
        if picked is None:
            return
        os_name, transfer = picked

        self.transfer_method = transfer

        # Flash target is owned by the journeys package.
        from mono_imager.journeys import _FLASH_TARGETS
        flash_target = _FLASH_TARGETS.get(os_name, T.C.EMMC_DEV)

        self.os_name = os_name

        # Any journey whose step list includes "Device network ready"
        # needs the device's OWN Ethernet connection — either for the
        # LAN flash transfer itself, or for a post-flash step that needs
        # real internet access (OpenWRT/OPNsense's firmware update).
        # Driven by the step registry rather than a hardcoded os_name
        # check, so this stays correct as journeys are added/changed.
        from mono_imager.step_registry import list_journey
        if "Device network ready" in list_journey(os_name, transfer):
            self._print_cable_notice()

        fw = self._auto_firmware(os_name, transfer)
        if fw is None:
            return
        firmware_path, firmware_display = fw

        host_ip = core.detect_host_ip()
        if not host_ip:
            print("  ❌ Could not auto-detect host IP.")
            print("  Use Manual mode instead to set it yourself.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL
            return

        # The device's own IP comes from _startup_network_setup() (already
        # run once, before the main menu) or gets (re-)resolved fresh once
        # this journey actually bootstraps into the recovery shell — see
        # menu_network_flashing(). Nothing to derive here; just preview
        # whatever's already known.
        if self.device_net:
            device_ip_preview = f"{self.device_net['ip']} ({self.device_net['source']})"
        else:
            device_ip_preview = "resolved via DHCP once the device is connected"

        confirmed = self._show_flash_confirmation(
            os_name=os_name,
            port=port,
            firmware_path=firmware_display,
            flash_target=flash_target,
            host_ip=host_ip,
            device_ip=device_ip_preview,
        )
        if confirmed is None:
            return
        if not confirmed:
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.MAIN
            return

        self.serial_port      = port
        self.net_host_ip      = host_ip
        self.net_http_port    = T.C.HTTP_PORT
        self.custom_fw_path   = firmware_path
        self.net_flash_target = flash_target
        self.current_state    = T.MenuState.NETWORK_FLASHING

    def _auto_pick_journey(self):
        """Ask which (OS, transfer) journey to run; None when backing out or invalid
        (the next state is already set)."""
        from mono_imager.journeys import discovered_journeys

        journeys = discovered_journeys()  # [(os_name, transfer), ...]

        print("  Select OS and flash method:")
        from mono_imager.journeys import get_transfer_label
        for i, (os_name, transfer) in enumerate(journeys, 1):
            print(f"    {i}) {os_name} via {get_transfer_label(os_name, transfer)}")
        back_idx = len(journeys) + 1
        print(f"    {back_idx}) Back")
        print("  (Type 'exit!' at any prompt to escape to main menu)")
        print()
        os_choice = self.safe_input(f"  Select [1-{back_idx}]: ")
        if os_choice is None:
            return None

        if os_choice == str(back_idx):
            self.current_state = T.MenuState.FLASH_AUTO_OR_MANUAL
            return None

        try:
            os_name, transfer = journeys[int(os_choice) - 1]
        except (ValueError, IndexError):
            print("  ❌ Invalid selection.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.NETWORK_AUTO_CONFIG
            return None
        return os_name, transfer

    @staticmethod
    def _print_cable_notice():
        """The device needs its own Ethernet connection for this journey."""
        print()
        print("  +-----------------------------------------------------+")
        print("  | ETHERNET CABLE REQUIRED                             |")
        print("  |                                                     |")
        print("  | The device needs its own network connection -       |")
        print("  | both for the flash transfer itself, and for any     |")
        print("  | post-flash 'firmware update' step, which needs      |")
        print("  | direct internet access.                             |")
        print("  | Connect an Ethernet cable to a router/switch that   |")
        print("  | provides DHCP and internet access - the active      |")
        print("  | port is auto-detected, no specific jack required.   |")
        print("  | If no DHCP response comes back, you'll be           |")
        print("  | prompted to enter the network settings manually.    |")
        print("  +-----------------------------------------------------+")
        print()
        input("  Press Enter once the cable is plugged in...")

    def _auto_firmware(self, os_name: str, transfer: str):
        """(firmware path, display text); None when cancelled or invalid (state set)."""
        print()
        if transfer == "usb":
            print("  Image will be auto-detected from USB stick.")
            print("  Recommended: 16 GB minimum (holds all three OS images simultaneously).")
            print()
            firmware_path     = T.Path(".")
            firmware_display  = "auto-detected from USB stick (16 GB min. recommended)"
        else:
            from mono_imager.journeys import get_firmware_prompt, get_journey_notice
            notice = get_journey_notice(os_name, transfer)
            if notice:
                print(notice)
                print()
            firmware_prompt = get_firmware_prompt(os_name, transfer)
            firmware_raw = self.safe_input(f"  {firmware_prompt} ")
            if firmware_raw is None:
                return None
            firmware_path, error = self._validate_firmware_path(firmware_raw)
            if error:
                print(f"  ❌ {error}")
                input("  Press Enter to continue...")
                self.current_state = T.MenuState.NETWORK_AUTO_CONFIG
                return None
            firmware_display = str(firmware_path)

            # Guard against an image whose real format does not match the
            # chosen OS journey (e.g. a bzip2 OPNsense image via OpenWRT's
            # raw dd) - that flashes "OK" but leaves an unbootable device.
            from mono_imager.journeys import check_image_matches_os
            _img_ok, _img_why = check_image_matches_os(os_name, firmware_path)
            if not _img_ok:
                print()
                print(f"  ⚠ Image/OS mismatch: {_img_why}")
                if self.safe_input("  Flash anyway? [y/N]: ") not in ("y", "Y"):
                    print("  Cancelled.")
                    input("  Press Enter to continue...")
                    self.current_state = T.MenuState.NETWORK_AUTO_CONFIG
                    return None
        return firmware_path, firmware_display

    def menu_network_flashing(self):
        """
        Run the actual flash journey. Bootstrap, U-Boot steps, recovery
        boot, network setup, and journey.run() all live in
        flash_orchestrator.run_flash_journey() now — this method only
        calls it and updates flash_success/current_state, restoring
        the get_journey()+.run() contract JOURNEYS.md documents for
        tui.py.

        flash_success is only touched when run_flash_journey() returns
        non-None (i.e. it got far enough to actually call
        print_report()) — matching the original's behavior of leaving
        flash_success at its previous value on an early bootstrap/
        U-Boot-steps/network-setup failure.
        """
        # DON'T clear screen here — keep all output visible for debugging

        from mono_imager import flash_orchestrator as core

        result = core.run_flash_journey(
            self.serial_port,
            self.os_name,
            getattr(self, "transfer_method", "lan"),
            self.net_host_ip,
            self.net_http_port,
            self.custom_fw_path,
            lambda: self.device_net,
            self._setup_recovery_network,
        )
        if result is not None:
            self.flash_success = result
        self.current_state = T.MenuState.DONE

    def _setup_recovery_network(self, d) -> bool:
        """
        Resolve+verify the recovery shell's network. DHCP-first,
        real-reachability-verified, manual fallback; caches on the
        session RecoveryNetwork and re-applies (not re-prompts) on
        each fresh recovery boot. See mono_imager.device_net for the
        full rationale and the hardware bugs this guards against.
        """
        return self._net.resolve(d)

    def menu_update_emmc(self):
        """
        Flash eMMC firmware only. Device must be in NOR recovery (DIP RIGHT).

        Bootstrap, modern/legacy detection, flashing, and legacy
        fallback all live in recovery_orchestrator.run_emmc_update()
        now (it used to be duplicated almost verbatim here and in
        menu_update_nor()). This method only owns the menu chrome
        (instructions, port selection, confirmation) and the
        MenuState/flash_success bookkeeping via _recovery_finish().
        """
        from mono_imager import recovery_orchestrator as rec

        self.print_screen_divider()
        print("Update eMMC Firmware")
        print()
        print("  +-----------------------------------------------+")
        print("  | START HERE: DIP switch -> RIGHT (NOR)         |")
        print("  |                                               |")
        print("  | Booting from NOR recovery ensures 'firmware   |")
        print("  | update' targets eMMC automatically.           |")
        print("  |                                               |")
        print("  | If DIP is LEFT (eMMC), flip it RIGHT and      |")
        print("  | power-cycle before continuing.                |")
        print("  +-----------------------------------------------+")
        print()

        port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
        if port is None:
            self.current_state = T.MenuState.MAIN
            return

        print()
        print("⚠️  This writes firmware to eMMC.")
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.MAIN
            return

        rec.run_emmc_update(
            port, self._soft_reboot_if_possible, self._setup_recovery_network,
            on_output=self._show_firmware_output,
        )

        success = rec.print_report()
        if success:
            print()
            print("=" * T.C.DIVIDER_WIDTH)
            print("  ⚡ FLIP DIP SWITCH TO LEFT (eMMC), THEN POWER-CYCLE ⚡")
            print("=" * T.C.DIVIDER_WIDTH)
            print()
        self._recovery_finish(success)

    def menu_update_nor(self):
        """
        Flash NOR firmware only. Device must be in eMMC recovery (DIP LEFT).
        Requires eMMC to already have the official Mono firmware with recovery.

        Bootstrap, modern/legacy detection, flashing, and legacy
        fallback all live in recovery_orchestrator.run_nor_update()
        now. This method only owns the menu chrome and MenuState/
        flash_success bookkeeping. No DIP-flip-back or boot
        verification is prompted after flashing — the tool leaves the
        device as-is once the write is confirmed.
        """
        from mono_imager import recovery_orchestrator as rec

        self.print_screen_divider()
        print("Update NOR Firmware")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  START HERE: DIP switch → LEFT (eMMC)            │")
        print("  │                                                   │")
        print("  │  Booting from eMMC recovery ensures 'firmware    │")
        print("  │  update' targets NOR automatically.              │")
        print("  │                                                   │")
        print("  │  ⚠️  eMMC must already have the official Mono     │")
        print("  │  Gateway firmware (with recovery partition).      │")
        print("  └─────────────────────────────────────────────────┘")
        print()

        port = self._select_port(auto_select_single=True, allow_back=True, save_on_select=True)
        if port is None:
            self.current_state = T.MenuState.MAIN
            return

        print()
        print("⚠️  This writes firmware to NOR flash (bootloader + recovery).")
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            input("  Press Enter to continue...")
            self.current_state = T.MenuState.MAIN
            return

        rec.run_nor_update(
            port, self._soft_reboot_if_possible, self._setup_recovery_network,
            on_output=self._show_firmware_output,
        )

        self._recovery_finish(rec.print_report())

    def menu_test_serial(self):
        """
        Run the serial connection test. Hardware interaction, live
        progress, and the pass/fail summary all live in
        diagnostics.test_serial() now; this method only resolves the
        port (a menu-navigation decision) and drives MenuState.

        If self.serial_port is already set (user connected earlier in
        this session), it is used automatically. Otherwise the user is
        prompted to pick a port first.
        """
        from mono_imager import diagnostics

        self.print_screen_divider()
        print("  Test Serial Connection")
        print("  " + "─" * T.C.BOX_RULE_WIDTH)
        print()

        # Resolve port
        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = T.MenuState.MAIN
                return

        if diagnostics.test_serial(port):
            self.serial_port = port

        self.current_state = T.MenuState.MAIN

    def menu_test_lan(self):
        """
        Full end-to-end LAN test — boots device into recovery, sets up
        networking, and confirms the device can reach the host HTTP
        server. Hardware interaction, live progress, and the summary
        all live in diagnostics.test_lan(); this method only resolves
        the port (menu-navigation), passes through the session-scoped
        callables (soft reboot, device-network resolve/read — both
        owned by MonoImager so every caller shares the same cache),
        and persists whatever diagnostics.test_lan() says is safe to
        keep.
        """
        from mono_imager import diagnostics

        self.print_screen_divider()

        port = self.serial_port
        if not port:
            port = self._select_port(auto_select_single=True, allow_back=False)
            if port is None:
                self.current_state = T.MenuState.MAIN
                return

        saved = diagnostics.test_lan(
            port,
            self.net_host_ip,
            self._soft_reboot_if_possible,
            self._setup_recovery_network,
            lambda: self.device_net,
        )
        if saved:
            self.serial_port   = saved["serial_port"]
            self.net_host_ip   = saved["host_ip"]
            self.net_device_ip = saved["device_ip"]

        self.current_state = T.MenuState.MAIN
