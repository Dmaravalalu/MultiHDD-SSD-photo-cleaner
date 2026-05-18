"""Hard Drive Cleaner — tkinter GUI app that scans selected drives, dedupes by
MD5, and organizes images into year folders (EXIF, with mtime fallback) and
non-images into a `misc/` folder. Duplicates are isolated under `duplicates/`.

Designed to compile to a single Windows .exe via PyInstaller. See build.bat.
"""
from __future__ import annotations

import hashlib
import os
import queue
import shutil
import stat as _stat
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import psutil
from PIL import ExifTags, Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    # HEIF support is optional — app still works for .jpg/.png without it.
    pass


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

CHUNK_SIZE = 64 * 1024  # 64 KB chunks for MD5 streaming

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng",
}

VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".mts", ".m2ts", ".ts", ".vob",
}

# Hidden/system folders that exist on every Windows drive root. Never enter.
WINDOWS_SYSTEM_FOLDERS = {
    "$recycle.bin", "recycler", "system volume information", "recovery",
    "config.msi", "msocache", "$extend", "$attrdef", "documents and settings",
    "perflogs", "programdata", "$winreagent", "windowsapps",
}

_FILE_ATTRIBUTE_SYSTEM = getattr(_stat, "FILE_ATTRIBUTE_SYSTEM", 0)

# EXIF tag IDs we care about for date extraction.
EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306

QUEUE_POLL_MS = 100


# ----------------------------------------------------------------------------
# Pure helper functions
# ----------------------------------------------------------------------------

def md5_of_file(path: str) -> str:
    """Stream the file in 64KB chunks to keep memory flat for large files."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def is_system_folder_name(name: str) -> bool:
    """True if `name` is a known Windows system folder (case-insensitive)."""
    n = name.lower()
    if n in WINDOWS_SYSTEM_FOLDERS:
        return True
    # FOUND.000, FOUND.001, ... left behind by chkdsk
    if n.startswith("found.") and n[6:].isdigit():
        return True
    return False


def has_system_attribute(path: str) -> bool:
    """True if Windows marks the path with FILE_ATTRIBUTE_SYSTEM.

    Returns False on non-Windows (where st_file_attributes doesn't exist) and
    on any stat() failure.
    """
    if not _FILE_ATTRIBUTE_SYSTEM:
        return False
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_SYSTEM)


def _parse_exif_date(value: str) -> tuple[int, int] | None:
    # EXIF format is "YYYY:MM:DD HH:MM:SS"; some cameras drop the time half.
    try:
        date_part = value.split(" ", 1)[0]
        y, m, _d = date_part.split(":")
        return int(y), int(m)
    except (ValueError, AttributeError):
        return None


def get_image_date(path: str) -> tuple[int, int] | None:
    """Return (year, month) from EXIF, or None if unavailable/unparseable."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            # DateTimeOriginal is the capture time; DateTime is last-modified.
            for tag_id in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME):
                raw = exif.get(tag_id)
                if raw:
                    parsed = _parse_exif_date(str(raw))
                    if parsed:
                        return parsed
            # Some HEIC files store EXIF under IFD blocks; check the standard one.
            try:
                ifd = exif.get_ifd(ExifTags.IFD.Exif)
                raw = ifd.get(EXIF_DATETIME_ORIGINAL)
                if raw:
                    return _parse_exif_date(str(raw))
            except Exception:
                pass
    except Exception:
        return None
    return None


def get_mtime_date(path: str) -> tuple[int, int]:
    dt = datetime.fromtimestamp(os.stat(path).st_mtime)
    return dt.year, dt.month


def resolve_collision(path: str) -> str:
    """If `path` exists, return `<stem>_N<ext>` for the smallest free N."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def _is_system_mount(mountpoint: str) -> bool:
    """True if this mount is the OS install drive — never offer as a source."""
    # Windows: %SystemDrive% is usually "C:". Compare case-insensitively.
    sys_drive = (os.environ.get("SystemDrive") or "").upper().rstrip("\\/")
    mp_upper = mountpoint.upper().rstrip("\\/")
    if sys_drive and mp_upper == sys_drive:
        return True
    # Unix: '/' is the system root; also skip common system mounts seen on macOS.
    if mountpoint == "/":
        return True
    if mountpoint.startswith(("/System", "/private", "/dev", "/usr", "/var")):
        return True
    return False


def list_drives() -> list[dict]:
    """Return mounted drives suitable for display + selection.

    Each entry: {mountpoint, fstype, label}. Pseudo-filesystems are filtered
    out by `all=False`; the system drive is filtered by `_is_system_mount`.
    """
    drives: list[dict] = []
    for part in psutil.disk_partitions(all=False):
        if _is_system_mount(part.mountpoint):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            free_str = f"{_human_bytes(usage.free)} free"
        except (PermissionError, OSError):
            free_str = "unavailable"
        label = f"{part.mountpoint}   ({part.fstype or 'unknown'}, {free_str})"
        drives.append({
            "mountpoint": part.mountpoint,
            "fstype": part.fstype,
            "label": label,
        })
    return drives


# ----------------------------------------------------------------------------
# Worker — runs on a background thread
# ----------------------------------------------------------------------------

class CleanerWorker(threading.Thread):
    def __init__(
        self,
        drives: list[str],
        dest_root: str,
        dry_run: bool,
        msg_queue: queue.Queue,
        cancel_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.drives = drives
        self.dest_root = os.path.abspath(dest_root)
        self.dry_run = dry_run
        self.queue = msg_queue
        self.cancel_event = cancel_event
        self._dest_norm = os.path.normcase(self.dest_root)
        self.hash_map: dict[str, str] = {}
        self.stats = {
            "unique": 0,
            "videos": 0,
            "duplicate": 0,
            "misc": 0,
            "skipped": 0,
            "errors": 0,
            "dirs_removed": 0,
        }

    # ---- queue helpers ----
    def _log(self, text: str) -> None:
        self.queue.put(("log", text))

    def _status(self, text: str) -> None:
        self.queue.put(("status", text))

    # ---- entry point ----
    def run(self) -> None:
        try:
            self._validate()
            for drive in self.drives:
                if self.cancel_event.is_set():
                    break
                self._process_drive(drive)
        except Exception as exc:  # pragma: no cover — last-resort safety net
            self._log(f"[FATAL] {exc}")
        finally:
            self.queue.put(("done", dict(self.stats), self.cancel_event.is_set()))

    def _validate(self) -> None:
        # Same-drive destinations are fine — _process_drive prunes the dest
        # subtree from os.walk so we never re-process our own output.
        # Only refuse the nonsensical case where dest IS a source drive root.
        for drive in self.drives:
            if os.path.normcase(os.path.abspath(drive)) == self._dest_norm:
                raise ValueError(
                    f"Destination {self.dest_root!r} is the same as source "
                    f"drive {drive!r}; pick a subfolder or different drive."
                )

    def _process_drive(self, drive: str) -> None:
        self._status(f"Scanning {drive}")
        self._log(f"[SCAN] {drive}")
        on_error = lambda err: self._log(f"[SKIP] {err}")
        for root_dir, dirs, files in os.walk(drive, onerror=on_error):
            # Prune (a) the destination subtree so we never re-process our
            # output, (b) Windows system folders like $RECYCLE.BIN.
            pruned: list[str] = []
            for d in dirs:
                full = os.path.join(root_dir, d)
                if os.path.normcase(full) == self._dest_norm:
                    continue
                if is_system_folder_name(d):
                    self._log(f"[SKIP] system folder {full}")
                    continue
                pruned.append(d)
            dirs[:] = pruned

            for name in files:
                if self.cancel_event.is_set():
                    return
                src = os.path.join(root_dir, name)
                self._handle_file(src, name, drive)
        if not self.cancel_event.is_set():
            self._cleanup_empty_dirs(drive)

    def _handle_file(self, src: str, name: str, drive: str) -> None:
        self._status(f"Processing {src}")

        # OS-marked system files (e.g. Windows install artefacts on a data
        # drive, NTFS metadata). Touching these is never the user's intent.
        if has_system_attribute(src):
            self._log(f"[SKIP] system file {src}")
            self.stats["skipped"] += 1
            return

        # Videos: skip MD5 entirely (per user — few videos, huge files, not
        # worth the I/O). Route by mtime, no dedup.
        if is_video(src):
            year, month = get_mtime_date(src)
            month_str = f"{month:02d}"
            dest = os.path.join(self.dest_root, str(year), month_str, name)
            self._log(f"[VIDEO] {name} -> {year}/{month_str}/")
            self.stats["videos"] += 1
            self._move_to(src, resolve_collision(dest))
            return

        # Everything else gets MD5'd for dedup.
        try:
            digest = md5_of_file(src)
        except (OSError, PermissionError) as exc:
            self._log(f"[ERROR] {src}: {exc}")
            self.stats["errors"] += 1
            return

        if digest in self.hash_map:
            dest = os.path.join(self.dest_root, "duplicates", name)
            self._log(f"[DUPLICATE] {name} -> duplicates/")
            self.stats["duplicate"] += 1
        elif is_image(src):
            self.hash_map[digest] = src
            year, month = get_image_date(src) or get_mtime_date(src)
            month_str = f"{month:02d}"
            dest = os.path.join(self.dest_root, str(year), month_str, name)
            self._log(f"[UNIQUE] {name} -> {year}/{month_str}/")
            self.stats["unique"] += 1
        else:
            # Misc: mirror the relative path from the source drive root so
            # folder context (e.g. projb/) is preserved under misc/.
            self.hash_map[digest] = src
            rel = os.path.relpath(src, drive)
            dest = os.path.join(self.dest_root, "misc", rel)
            rel_dir = os.path.dirname(rel) or "."
            self._log(f"[MISC] {name} -> misc/{rel_dir}/")
            self.stats["misc"] += 1

        self._move_to(src, resolve_collision(dest))

    def _move_to(self, src: str, dest: str) -> None:
        if self.dry_run:
            return
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            return
        except PermissionError:
            # Try clearing the read-only attribute (Windows: makes the file
            # writable if it was marked read-only) and retry once.
            try:
                os.chmod(src, _stat.S_IWRITE)
                shutil.move(src, dest)
                return
            except (OSError, shutil.Error):
                pass
            # Real ACL restriction, file in use, or restricted location.
            # Skip cleanly — better than crashing the run.
            self._log(
                f"[ACCESS DENIED] {src}: file is in use, ACL-protected, "
                f"or in a restricted location — skipped"
            )
            self.stats["skipped"] += 1
        except (OSError, shutil.Error) as exc:
            self._log(f"[ERROR] moving {src} -> {dest}: {exc}")
            self.stats["errors"] += 1

    def _cleanup_empty_dirs(self, drive: str) -> None:
        """Bottom-up walk: remove folders that are empty after the main pass."""
        drive_abs = os.path.abspath(drive)
        for root_dir, _dirs, _files in os.walk(drive, topdown=False):
            if self.cancel_event.is_set():
                return
            root_norm = os.path.normcase(root_dir)
            # Never touch the destination subtree.
            if root_norm == self._dest_norm:
                continue
            if root_norm.startswith(self._dest_norm + os.sep):
                continue
            # Never delete the drive root itself.
            if os.path.abspath(root_dir) == drive_abs:
                continue
            try:
                if not os.listdir(root_dir):
                    if self.dry_run:
                        self._log(f"[CLEANUP] (dry) would remove empty {root_dir}")
                    else:
                        os.rmdir(root_dir)
                        self._log(f"[CLEANUP] removed empty {root_dir}")
                        self.stats["dirs_removed"] += 1
            except OSError as exc:
                self._log(f"[ERROR] cleanup {root_dir}: {exc}")


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

class CleanerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Hard Drive Cleaner")
        self.root.geometry("780x620")
        self.root.minsize(640, 480)

        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: CleanerWorker | None = None

        self.drive_vars: dict[str, tk.BooleanVar] = {}
        self.dry_run_var = tk.BooleanVar(value=True)  # safe default
        self.dest_var = tk.StringVar()

        self._build_layout()
        self._refresh_drives()
        self.root.after(QUEUE_POLL_MS, self._drain_queue)

    # ---- layout ----
    def _build_layout(self) -> None:
        style = ttk.Style()
        # Use a clean, native-feeling theme when available.
        for candidate in ("vista", "clam", "default"):
            try:
                style.theme_use(candidate)
                break
            except tk.TclError:
                continue

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)  # log row grows

        # Drives section
        drives_frame = ttk.LabelFrame(self.root, text="Connected Drives", padding=8)
        drives_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        drives_frame.columnconfigure(0, weight=1)

        self.drives_inner = ttk.Frame(drives_frame)
        self.drives_inner.grid(row=0, column=0, sticky="ew")
        self.drives_inner.columnconfigure(0, weight=1)

        ttk.Button(
            drives_frame, text="Refresh", command=self._refresh_drives
        ).grid(row=0, column=1, sticky="ne", padx=(8, 0))

        # Destination section
        dest_frame = ttk.LabelFrame(self.root, text="Destination", padding=8)
        dest_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        dest_frame.columnconfigure(0, weight=1)
        ttk.Entry(dest_frame, textvariable=self.dest_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(dest_frame, text="Browse…", command=self._pick_dest).grid(
            row=0, column=1, padx=(8, 0)
        )

        # Controls
        controls = ttk.Frame(self.root, padding=(10, 4))
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            controls, text="Dry Run (preview only, no files moved)",
            variable=self.dry_run_var,
        ).grid(row=0, column=0, sticky="w")
        self.start_btn = ttk.Button(
            controls, text="Start Processing", command=self._on_start
        )
        self.start_btn.grid(row=0, column=1, padx=(16, 6))
        self.stop_btn = ttk.Button(
            controls, text="Stop", command=self._on_stop, state="disabled"
        )
        self.stop_btn.grid(row=0, column=2)

        # Status label
        status_frame = ttk.Frame(self.root, padding=(10, 4))
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, text="Status:").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(
            status_frame, textvariable=self.status_var,
            foreground="#0a58ca", anchor="w",
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # Log
        log_frame = ttk.LabelFrame(self.root, text="Activity Log", padding=8)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=10, pady=(4, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = ScrolledText(log_frame, height=12, state="disabled", wrap="none")
        self.log.grid(row=0, column=0, sticky="nsew")

    # ---- drive list ----
    def _refresh_drives(self) -> None:
        for child in self.drives_inner.winfo_children():
            child.destroy()
        self.drive_vars.clear()

        drives = list_drives()
        if not drives:
            ttk.Label(
                self.drives_inner, text="(no drives detected)",
                foreground="#888",
            ).grid(row=0, column=0, sticky="w")
            return

        for i, drv in enumerate(drives):
            var = tk.BooleanVar(value=False)
            self.drive_vars[drv["mountpoint"]] = var
            ttk.Checkbutton(
                self.drives_inner, text=drv["label"], variable=var,
            ).grid(row=i, column=0, sticky="w")

    def _pick_dest(self) -> None:
        chosen = filedialog.askdirectory(title="Choose destination folder")
        if chosen:
            self.dest_var.set(chosen)

    # ---- run lifecycle ----
    def _selected_drives(self) -> list[str]:
        return [mp for mp, var in self.drive_vars.items() if var.get()]

    def _on_start(self) -> None:
        drives = self._selected_drives()
        dest = self.dest_var.get().strip()

        if not drives:
            messagebox.showerror(
                "No drives selected", "Pick at least one drive to scan."
            )
            return
        if not dest:
            messagebox.showerror(
                "No destination", "Choose a destination folder."
            )
            return
        if not os.path.isdir(dest):
            messagebox.showerror(
                "Bad destination", f"{dest!r} is not an existing directory."
            )
            return
        if not self.dry_run_var.get():
            ok = messagebox.askyesno(
                "Confirm real run",
                "Dry Run is OFF. Files will be MOVED from the selected "
                "drives to the destination. Continue?",
            )
            if not ok:
                return

        self.cancel_event.clear()
        self._set_controls_running(True)
        self._append_log(
            f"=== Starting {'DRY RUN' if self.dry_run_var.get() else 'REAL RUN'} ==="
        )
        self.worker = CleanerWorker(
            drives=drives,
            dest_root=dest,
            dry_run=self.dry_run_var.get(),
            msg_queue=self.msg_queue,
            cancel_event=self.cancel_event,
        )
        self.worker.start()

    def _on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("Stopping… (finishing current file)")
            self.stop_btn.config(state="disabled")

    def _set_controls_running(self, running: bool) -> None:
        new_state = "disabled" if running else "normal"
        self.start_btn.config(state=new_state)
        self.stop_btn.config(state="normal" if running else "disabled")
        # Disable config widgets while a run is in progress.
        for child in self.drives_inner.winfo_children():
            try:
                child.config(state=new_state)
            except tk.TclError:
                pass

    # ---- queue pump ----
    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "done":
                    self._on_worker_done(stats=msg[1], cancelled=msg[2])
        except queue.Empty:
            pass
        self.root.after(QUEUE_POLL_MS, self._drain_queue)

    def _append_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_worker_done(self, stats: dict, cancelled: bool) -> None:
        self.worker = None
        self._set_controls_running(False)
        self.status_var.set("Cancelled" if cancelled else "Done")
        summary = (
            f"Images: {stats['unique']}   "
            f"Videos: {stats['videos']}   "
            f"Duplicates: {stats['duplicate']}   "
            f"Misc: {stats['misc']}   "
            f"Skipped: {stats['skipped']}   "
            f"Empty dirs removed: {stats['dirs_removed']}   "
            f"Errors: {stats['errors']}"
        )
        self._append_log("=== " + ("Cancelled" if cancelled else "Finished") + " ===")
        self._append_log(summary)
        messagebox.showinfo(
            "Processing complete" if not cancelled else "Cancelled",
            summary,
        )


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    CleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
