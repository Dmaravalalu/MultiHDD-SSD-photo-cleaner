# Hard Drive Cleaner

A tkinter GUI app that scans selected drives, dedupes files by MD5, and organizes them into a clean destination folder:

- **Images** → `YYYY/MM/` based on EXIF capture date (falls back to file mtime)
- **Videos** → `YYYY/MM/` based on file mtime (not hashed — too large, too few)
- **Duplicates** → `duplicates/`
- **Everything else** → `misc/<original-relative-path>/`

Comes with two companion CLI scripts:

- `cleanup_destination.py` — sweeps Windows system-folder remnants (`$RECYCLE.BIN`, `System Volume Information`, etc.) and OS junk (`desktop.ini`, `Thumbs.db`, `.DS_Store`) out of a destination folder.
- `dedupe_destination.py` — re-dedupes an existing destination folder by content, in case duplicates accumulated from older runs.

## Requirements

- Python 3.10 or newer (the code uses `int | None`-style type hints)
- Tkinter (bundled with the standard python.org installer on Windows/macOS; on Linux install `python3-tk` via your package manager)
- The Python packages in `requirements.txt`:
  - `pillow` — image / EXIF reading
  - `pillow-heif` — optional HEIC/HEIF support (iPhone photos)
  - `psutil` — drive enumeration
  - `pyinstaller` — only needed if you want to build the Windows `.exe`

## Setup

### 1. Clone / download

```bash
git clone <repo-url>
cd "harddisk cleaner"
```

### 2. Create a virtual environment (recommended)

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows:**
```cmd
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the GUI

```bash
python harddisk_cleaner.py
```

## Using the app

1. **Pick source drives.** Connected drives appear at the top. The OS drive (`C:` on Windows, `/` on macOS/Linux) is filtered out and cannot be selected.
2. **Pick a destination folder.** Can be on the same drive as a source — the app prunes its own output from the scan so it never re-processes files it just moved.
3. **Leave "Dry Run" on for the first pass.** Nothing moves; the log shows what *would* happen.
4. **Uncheck "Dry Run" and click Start** to actually move files. You'll get a confirmation prompt.
5. **Stop** is safe — the current file finishes, then the worker exits. The hash map is persisted to `<dest>/.hdc_hash.json` so the next run resumes dedup memory.

## Building a Windows `.exe`

A Developer Command Prompt (or any `cmd.exe` with Python on `PATH`) and `build.bat`:

```cmd
build.bat
```

Output: `dist\HardDriveCleaner.exe` — single-file, no console window.

## Maintenance scripts

Both default to dry-run; pass `--apply` to commit.

```bash
# Remove stray system folders / junk files from an existing destination
python cleanup_destination.py "D:\Sorted"
python cleanup_destination.py "D:\Sorted" --apply

# Re-dedupe an existing destination by content
python dedupe_destination.py "D:\Sorted"
python dedupe_destination.py "D:\Sorted" --apply
```

These scripts intentionally avoid the GUI dependencies (`tkinter`, `pillow`) so they run on a stripped-down Python.

## Notes

- HEIC support is optional. If `pillow-heif` fails to import, JPEG/PNG still work fine.
- The hash state file `.hdc_hash.json` lives at the destination root. Deleting it just resets dedup memory; it does not affect already-organized files.
- On Windows, files marked with `FILE_ATTRIBUTE_SYSTEM` and folders like `$RECYCLE.BIN` are skipped regardless of whether they're at a drive root or nested.
