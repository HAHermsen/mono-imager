# Project Engineering Standards & Architecture Constitution

## 1. Core Philosophy: Zero "Vibe Coding"
- **Plan First, Code Second:** Never write implementation code until the architecture, interfaces, and file structure for the current task are explicitly approved.
- **No Shortcuts or Placeholders:** Never use `// TODO`, mock logic, dummy data, or temporary hacks unless explicitly instructed. Every commit must be production-grade. Mocks and fake data live only inside tests.
- **Ask Before Assuming:** If any requirement in the specification is ambiguous, stop and ask clarifying questions before writing code. Never fill a gap silently.
- **Research First:** Research online before answering, and read the actual repo code before changing it.
- **Explicit Go Only:** Touch files only when explicitly asked. Never commit or push unless asked.
- **No Scope Creep:** Do exactly what was asked. Keep answers short.
- **Uncommitted Work Comes First:** Build on the user's local changes, never overwrite them. Check file modification times before writing a file back.

## 2. Clean Code & Zero Magic Numbers
- **Strict Ban on Magic Numbers & Strings:** Every literal number (other than `0`, `1`, `-1` in standard loop/index contexts) and repeated string must be extracted into a named constant, enum, or configuration file.
- **Centralized Configuration:** Environment variables, thresholds, timeouts, and business rules belong in dedicated config/constants modules—never hardcoded inside business logic. Shared constants live in `mono_imager/constants.py` (imported as `C`); module-only constants sit at the top of the module that owns them (e.g. `snapshot.py`).
- **Self-Documenting Naming:** Use explicit, unambiguous names for variables, functions, and classes. Avoid single-letter variables or cryptic abbreviations.
- **Exception (decided):** device shell scripts use short names (`$i`, `$off`, `$n`, `rd`, `put`, `rep`, `fsha`) on purpose, to keep lines short on the serial console (section 6). Allowed, with a comment in the code saying so

## 3. Modularity & Extensibility (SOLID)
- **Single Responsibility Principle:** Each file, class, and function must do one thing well. Keep files small and focused (see File & Function Size).
- **Dependency Injection:** High-level modules must not depend on low-level modules directly. Pass dependencies (services, repositories, configs) via constructors or function parameters so components can be swapped or mocked easily. As practised here: functions take the device, session and `ask` callables as parameters, which the tests replace.
- **Interface-Driven Design:** Define types, interfaces, and data contracts *before* writing implementation logic.
- **Strict Layer Separation:** Keep core domain/business logic completely decoupled from UI, database, network, and third-party API code. Current layers: device shell scripts are built by pure functions (no I/O) in `snapshot.py`; orchestration in `*_orchestrator.py`; serial I/O in `serial_device.py`; UI in `tui.py`.
- **Known gaps (2026-11-15, after refactor phases 1–4):** 14,399 lines in 53 files. No file over 1,000 lines (largest `snapshot_orchestrator.py` 829); no function over 100 lines; duplicated functions were consolidated in phase 1. Still open: magic-number naming, and the lint baseline (ruff 152 findings, mypy 79).
- **File & Function Size:** soft targets, hard ceilings (decided 2026-11-15):
  - **Files:** target 200–400 lines; above 400 needs a stated reason; 1,000 lines is the hard ceiling.
  - **Functions:** target 50 lines or fewer; 100 lines is the hard ceiling.
  - **Why these numbers:**
    - *Fewer bugs per line in this range.* Hatton's data puts the defect-density sweet spot at 200–400 logical lines (about 400–800 physical lines); a large Ada study found its lowest defect rate at 159–251 lines (optimum ~225). The evidence is contested (other studies find no clear optimum), so these are targets, not laws.
    - *Tools agree on the ceilings.* Pylint warns above 1,000 lines per module by default; Rust's Clippy flags functions above 100 lines.
    - *AI-assisted work.* Coding agents read big files in parts (e.g. Cursor: 250 lines, 750 in Max mode). A file that fits whole in context gets read completely, edited precisely, and costs fewer tokens; parts missed in a big file cause wrong edits.
    - *Review and responsibility.* A short file gives a diff you can check in one go; a file that keeps growing usually does more than one thing (SRP).
  - **Note:** Anthropic's "under 200 lines" advice is about the CLAUDE.md file itself, not about source files.

## 4. Execution Workflow
When given a feature or module specification, follow this exact sequence:
1. **Analyze & Map:** Identify which modules, interfaces, and files will be created or modified.
2. **Scaffold Contracts:** Write or update the types, interfaces, and constants first. Pause for review if creating a new domain boundary.
3. **Implement Modularly:** Build one isolated module at a time, adhering strictly to the defined interfaces.
4. **Verify & Lint:** Run the project's type-checker, linter, and test suite. Fix all warnings and errors before considering the task complete:
   - `python tests/run_tests.py --unit-only` (all files must pass)
   - `ruff check mono_imager` (line length 100)
   - `mypy mono_imager`
- **Version Control:** No branches: commit on `main`. The user commits with their own git identity; Claude prepares the change and checks it with `git diff` / `git show --stat`. Every commit Claude helped with says so: it ends with the trailer `Co-Authored-By: Claude <noreply@anthropic.com>` (model name may be added, e.g. `Claude Opus 5.5`).
- **Line Endings:** Files are CRLF in the working tree (git stores LF). Keep CRLF, and never leave a lone CR: git then treats the file as binary (happened to `serial_device.py`, fixed in `5f5c014`).
- **Versioning:** `__version__` in `mono_imager/__init__.py` is the single source; `pyproject.toml` is the only build file. Do not bump the version unless asked.

## 5. Error Handling & Testing
- **logging** logs shall be mandatory in the defacto standard available and will be done in files with size of 1 MByte: a log is split into a series of 1 MB files, never one growing file. **Known gap:** today `logging_setup.py` writes one file per run to `logs/`, with no 1 MB split.
- **Explicit Error Handling:** Never swallow errors silently (`catch (e) {}`). Use typed errors or structured result objects, and log meaningful context. **Known gap:** 102 catch-all `except Exception` and 10 `except: pass` (ruff BLE001 / S110).
- **Unit Test Coverage:** Every new business logic module must include unit tests covering both the happy path and edge cases. Unit tests need no hardware: fake disks and a fake serial line. Coverage (coverage.py, 2026-11-15): 87% overall; lowest: diagnostics 36%, console 56%, flash_orchestrator 70%, recovery_orchestrator 74%, config 74%, snapshot_orchestrator 78%.
- **Regression Safety:** Do not modify existing public interfaces without checking all dependents across the codebase. Always run the whole suite, not only the file that changed.
- **Shell-Script Tests:** Generated device scripts are run for real under `/bin/sh` and BusyBox `ash` (when installed). On Windows they need `sh` from Git for Windows; without it those parts skip with a message.

## 6. Project-Specific Context
- **Done (2026-11-15):** the test limits are named in a block in `test_snapshot_scripts.py` (`HASH_FIFO` / `HASH_OUT` were removed with the tee/FIFO single pass, 2026-11-21). Literals elsewhere in the code are handled in refactor phase 4.
- **Known gaps (baseline locked 2026-11-15, after phase 4):** ruff 152 findings (was 355); mypy 79 errors (was 157). Locked by `tests/unit/test_lint_baseline.py`; counts may only go down.
- **Folder Structure:** Folder /logs contains logging
- **Language / Runtime:** Python >= 3.10 (`pyproject.toml`). The user runs 3.14 on Windows; Claude tests on Linux with 3.13.
- **Framework:** none. CLI/TUI on `pyserial` (optional `zstandard` below Python 3.14).
- **Linter / Formatter:** `ruff` (line length 100), `mypy`.
- **Test Runner:** plain scripts via `tests/run_tests.py` (`pytest` is listed in dev dependencies but not used).
- **Repo:** `C:\Users\user\Downloads\UgreenDrive\MyWorkfolder\mono-imager`; GitHub `HAHermsen/mono-imager` (public). Claude can read it, not push.
- **Run:** `venv\Scripts\activate`, then `mono-imager` (or `python -m mono_imager.cli`). If the folder moves, recreate the venv.
- **Hardware (Mono Gateway):** NXP LS1046A, 4x Cortex-A72, 8 GB RAM, 32 GB eMMC (`/dev/mmcblk0`), 64 MB NOR. Serial COM5, 115200 baud (FTDI). DIP switch LEFT = boot from eMMC.
- **Recovery Linux:** runs from RAM; tools may be BusyBox (`od` has no `-A`/`-t`, `dd` has no `status=progress`). Probe for a tool before relying on it.
- **Measured in recovery (2026-11-15):** eMMC read ~54 MiB/s; `openssl dgst -sha256` ~740 MiB/s (CPU SHA instructions); `sha256sum`/`sha1sum` ~35 MiB/s; LAN >= 1 Gbit/s.
- **Device Shell Scripts (sent over serial):** POSIX `sh` that also runs under BusyBox `ash`. Keep the whole script small (a 13 KB one broke on hardware; today ~2.3 KB) and lines short (longest today 89 chars). Loop over a plan file, never one block per chunk. Manual console use: paste one line at a time.
- **Snapshot:** LAN = 30 chunks of ~1 GiB for 32 GB, each SHA-256-checked against what the PC received. Default single pass: read once from the eMMC and upload, then hash the chunk again from the page cache (openssl, else `sha256sum`); no `tee`/FIFO (both hardware runs with them stalled in chunk 0). "Thorough verify" (LAN only) re-reads from the eMMC (~2x slower). Missing tools fall back to the read-back with a warning. NOR dump is read-only, never restored. USB snapshot/restore unchanged.
- **Known cosmetic issues:** a snapshot ends with "Flash completed successfully"; the report's step numbers restart at 01 partway through.

Use code with caution.
