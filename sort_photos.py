#!/usr/bin/env python3
r"""Sort photos and videos from a source folder into Nostalgia/YYYY by date.

For each photo or video found recursively under the source folder, the capture
year is determined from embedded metadata, falling back to the file's
modification time when none is available:

  * Photos — EXIF DateTimeOriginal, then DateTimeDigitized, then DateTime.
  * Videos — container metadata via ffprobe (Apple QuickTime creationdate,
             then the standard creation_time), with a built-in MP4/MOV atom
             reader as a fallback when ffprobe is not installed.

Exact duplicates are detected by content (SHA-256) across the entire
destination: a file whose bytes already exist anywhere under ``<dest>`` is
skipped, no matter its name or which year folder holds the original. Each file
is copied into ``<dest>/YYYY/`` and is never overwritten; a remaining name
collision (same name, different content) gets a numeric suffix (``photo_1.jpg``).

By default files are copied (originals stay in the source); pass --move to
move them instead.

--source is required so the script works against any folder: a phone's DCIM,
an SD or DSLR memory card, an external drive, or an existing folder on disk.

Usage:

    # Linux (local folder, SD card, external drive):
    python3 sort_photos.py --source /home/user/Pictures
    python3 sort_photos.py --source /media/sdcard/DCIM --dest ~/Nostalgia
    python3 sort_photos.py --source ~/Pictures/Inbox --move
    python3 sort_photos.py --source /home/user/Pictures --dry-run

    # Linux (Android phone mounted via MTP, e.g. jmtpfs or GVFS):
    python3 sort_photos.py --source /mnt/phone/DCIM

    # Windows (local folder, external drive, or UNC network share):
    python sort_photos.py --source "C:\Users\User\Pictures"
    python sort_photos.py --source "D:\DCIM" --dest "D:\Organized"
    python sort_photos.py --source "\\server\share\Pictures"

    # Windows (Android phone via Windows Explorer MTP namespace):
    python sort_photos.py --source "This PC\Pixel 7\Internal shared storage\DCIM"
    python sort_photos.py --source "Pixel 7\Internal shared storage\DCIM"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

if sys.platform == "win32":
    try:
        import win32com.client  # type: ignore
        HAS_WIN32COM = True
    except ImportError:
        HAS_WIN32COM = False
else:
    HAS_WIN32COM = False

try:
    from PIL import Image, ExifTags
except ImportError:
    sys.exit("Pillow is required: install it with 'pip install Pillow'")

BANNER = r"""
███████╗ ██████╗ ██████╗ ████████╗
██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝
███████╗██║   ██║██████╔╝   ██║
╚════██║██║   ██║██╔══██╗   ██║
███████║╚██████╔╝██║  ██║   ██║
╚══════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
     photo & video sorter
"""

# Status glyphs keyed by event kind: (symbol, ansi color).
_GLYPHS = {
    "INFO": ("►", "36"),   # cyan   — scanning / info
    "OK": ("✔", "32"),     # green  — success
    "COPY": ("✔", "32"),   # green  — file copied
    "MOVE": ("➜", "34"),   # blue   — file moved
    "PROC": ("➜", "34"),   # blue   — processing
    "SKIP": ("⚠", "33"),   # yellow — duplicate / warning
    "ERROR": ("✖", "31"),  # red    — error
}
_RESET = "\033[0m"


def _color_enabled(stream) -> bool:
    # Honor the NO_COLOR convention and only color real terminals.
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def status(kind: str, *, err: bool = False) -> str:
    """Return a colored status glyph for the given event kind."""
    symbol, code = _GLYPHS[kind]
    stream = sys.stderr if err else sys.stdout
    if _color_enabled(stream):
        return f"\033[{code}m{symbol}{_RESET}"
    return symbol


def paint(text: str, code: str, stream=None) -> str:
    """Wrap text in an ANSI color when the target stream is a color TTY."""
    if code and _color_enabled(stream or sys.stdout):
        return f"\033[{code}m{text}{_RESET}"
    return text


def render_box(title: str, rows: list[tuple[str, str]]) -> list[str]:
    """Build a double-line box: a centered title, a rule, then label/value rows."""
    label_w = max(len(label) for label, _ in rows)
    body = [f"{label:<{label_w}} : {value}" for label, value in rows]
    inner = max(len(title) + 2, max(len(line) for line in body) + 2)
    lines = ["╔" + "═" * inner + "╗",
             "║" + title.center(inner) + "║",
             "╠" + "═" * inner + "╣"]
    lines += ["║ " + line.ljust(inner - 1) + "║" for line in body]
    lines.append("╚" + "═" * inner + "╝")
    return lines


def render_distribution(per_year: "Counter[str]", width: int) -> list[str]:
    """Build a horizontal bar chart of file counts per year, scaled to fit."""
    peak = max(per_year.values())
    max_bar = 40
    scale = 1.0 if peak <= max_bar else max_bar / peak
    bars = {y: "█" * max(1, round(per_year[y] * scale)) for y in per_year}
    bar_w = max(len(b) for b in bars.values())
    lines = ["Directory Distribution", "─" * width]
    for year in sorted(per_year):
        lines.append(f"{year}  {bars[year].ljust(bar_w)} {per_year[year]}")
    return lines


def boot_sequence() -> None:
    """Print a faux boot log to set the mood before sorting begins.

    Animates line-by-line on a color-capable TTY; on a pipe it prints instantly
    so logs stay clean and scripted runs aren't slowed.
    """
    animate = _color_enabled(sys.stdout)
    pause = 0.15 if animate else 0.0

    for msg in ("Initializing filesystem...",
                "Loading EXIF parser...",
                "Probing video containers...",
                "Connecting to media database..."):
        print(paint(msg, "32"))
        if pause:
            time.sleep(pause)

    checks = ["Hash cache", "Metadata engine", "Duplicate detector"]
    width = max(len(c) for c in checks) + 7  # dot-leader column width
    for label in checks:
        dots = "." * (width - len(label))
        if animate:
            sys.stdout.write(paint(label + dots, "32"))
            sys.stdout.flush()
            time.sleep(pause)
            print(paint("OK", "1;32"))
        else:
            print(paint(f"{label}{dots}OK", "32"))

    print()
    print(paint("Mission Started...", "1;32"))
    if pause:
        time.sleep(pause)


# File extensions treated as images.
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".cr2", ".nef", ".arw", ".dng", ".orf",
    ".rw2", ".raf", ".sr2",
}

# File extensions treated as videos.
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".qt", ".avi", ".mkv", ".webm", ".wmv",
    ".flv", ".f4v", ".3gp", ".3g2", ".mpg", ".mpeg", ".m2v", ".mts",
    ".m2ts", ".ts", ".vob", ".ogv", ".mxf", ".asf", ".divx",
}

# Everything we are willing to scan and sort.
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# ffprobe (part of ffmpeg) reads creation dates from any container it supports;
# resolved once at import. None when ffmpeg isn't installed -> atom/mtime path.
_FFPROBE = shutil.which("ffprobe")

# EXIF tag ids for date fields, in order of preference.
_TAG_BY_NAME = {name: tag for tag, name in ExifTags.TAGS.items()}
EXIF_DATE_TAGS = [
    _TAG_BY_NAME["DateTimeOriginal"],
    _TAG_BY_NAME["DateTimeDigitized"],
    _TAG_BY_NAME["DateTime"],
]


def get_exif_date(path: Path) -> datetime | None:
    """Return the capture datetime from image EXIF, or None if unavailable."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
        for tag in EXIF_DATE_TAGS:
            raw = exif.get(tag)
            if not raw:
                continue
            # EXIF dates look like "2021:05:14 15:53:59".
            try:
                return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    except Exception:
        # Unreadable / corrupt / not really an image -> caller falls back.
        pass
    return None


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp out of container metadata.

    Tolerates a trailing ``Z``, fractional seconds, and numeric UTC offsets
    (with or without a colon). Returns None for unparseable values or the
    QuickTime "unset" sentinel (anything at/below the 1904 epoch)."""
    raw = raw.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(raw)  # Py3.11+ handles 'Z' and ±HHMM
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None or dt.year <= 1904:
        return None
    return dt


def _ffprobe_tag_dicts(path: Path) -> list[dict]:
    """Return every tag dictionary (format + per-stream) ffprobe reports.

    Empty when ffprobe is unavailable, errors, or the file has no tags."""
    if _FFPROBE is None:
        return []
    try:
        proc = subprocess.run(
            [_FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return []
    dicts = []
    fmt_tags = data.get("format", {}).get("tags")
    if isinstance(fmt_tags, dict):
        dicts.append(fmt_tags)
    for stream in data.get("streams", []):
        tags = stream.get("tags")
        if isinstance(tags, dict):
            dicts.append(tags)
    return dicts


def _iter_boxes(f, end: int):
    """Yield (type, body_start, body_end) for ISO-BMFF boxes up to ``end``."""
    while True:
        pos = f.tell()
        if pos + 8 > end:
            return
        header = f.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        if size == 1:                       # 64-bit "largesize" follows the type
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            header_len = 16
        elif size == 0:                     # box runs to the end of the file
            yield box_type, f.tell(), end
            return
        else:
            header_len = 8
        body_end = pos + size
        if size < header_len or body_end > end:
            return
        yield box_type, pos + header_len, body_end
        f.seek(body_end)


def _mvhd_creation_date(path: Path) -> datetime | None:
    """Dependency-free fallback: read the creation time from an MP4/MOV ``mvhd``
    atom (the QuickTime / ISO base-media format used by phones and cameras).

    Used when ffprobe is unavailable or yields nothing. Returns local time, or
    None. Only covers the ISO-BMFF family (mp4, m4v, mov, 3gp); other containers
    fall back to mtime."""
    qt_epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            moov = next((b for b in _iter_boxes(f, size) if b[0] == b"moov"), None)
            if moov is None:
                return None
            f.seek(moov[1])
            mvhd = next((b for b in _iter_boxes(f, moov[2]) if b[0] == b"mvhd"), None)
            if mvhd is None:
                return None
            f.seek(mvhd[1])
            version = f.read(1)
            f.read(3)  # flags
            field = f.read(8 if version == b"\x01" else 4)
            seconds = int.from_bytes(field, "big")
            if seconds == 0:
                return None
            return (qt_epoch + timedelta(seconds=seconds)).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def get_video_date(path: Path) -> datetime | None:
    """Return the original creation datetime for a video, or None.

    Prefers ffprobe metadata: Apple's capture-local ``creationdate`` first
    (kept as written, since it already reflects where it was shot), then the
    standard UTC ``creation_time`` (converted to local time). Falls back to a
    built-in MP4/MOV atom reader when ffprobe is unavailable or finds nothing."""
    tag_dicts = _ffprobe_tag_dicts(path)
    # Apple stores the capture-local wall clock -> use its components as-is.
    for tags in tag_dicts:
        raw = tags.get("com.apple.quicktime.creationdate")
        if raw:
            dt = _parse_iso_datetime(raw)
            if dt:
                return dt.replace(tzinfo=None)
    # creation_time is UTC -> convert to local so the bucketed year is local.
    for tags in tag_dicts:
        raw = tags.get("creation_time")
        if raw:
            dt = _parse_iso_datetime(raw)
            if dt:
                return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    return _mvhd_creation_date(path)


def get_capture_date(path: Path | Any) -> tuple[datetime, str]:
    """Return (capture datetime, source) where source is 'exif', 'video' or 'mtime'.

    Photos use EXIF; videos use container metadata (ffprobe, then a built-in
    MP4/MOV atom reader). Both fall back to the file's modification time when no
    embedded date is found."""
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        captured = get_video_date(path)
        if captured is not None:
            return captured, "video"
    else:
        captured = get_exif_date(path)
        if captured is not None:
            return captured, "exif"
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def sha256(path: Path | Any, chunk_size: int = 1 << 20) -> str:
    if hasattr(path, "content_hash") and path.content_hash is not None:
        return path.content_hash
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if hasattr(path, "content_hash"):
        path.content_hash = digest
    return digest


class DedupIndex:
    """Exact-content index of every media file under the destination, so identical
    bytes are never copied twice — regardless of filename or which year folder
    holds the original.

    Detection is by SHA-256. To avoid hashing an entire existing library on every
    run, files are bucketed by byte size first and a hash is computed only when
    two files share a size (files of different sizes can't be byte-identical).
    Each file's hash is memoized, so it is read at most once.
    """

    def __init__(self) -> None:
        # size -> list of [path, hash_or_None]; the hash is filled in lazily the
        # first time that file must be compared against another of the same size.
        self._by_size: dict[int, list[list]] = {}

    def _hash(self, entry: list) -> str:
        if entry[1] is None:
            entry[1] = sha256(entry[0])
        return entry[1]

    def add_existing(self, path: Path) -> None:
        """Index a file already present on disk under the destination."""
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._by_size.setdefault(size, []).append([path, None])

    def find_duplicate(self, src: Path | Any, size: int) -> tuple[Path | None, str | None]:
        """Look for a byte-identical file already indexed.

        Returns (match, src_hash): ``match`` is the existing path when ``src`` is
        a duplicate, else None. ``src_hash`` is src's SHA-256 when it had to be
        computed (some indexed file shares its size), else None — pass it back to
        ``register`` to avoid re-hashing.
        """
        bucket = self._by_size.get(size)
        if not bucket:
            return None, None
        src_hash = sha256(src)
        for entry in bucket:
            if self._hash(entry) == src_hash:
                return entry[0], src_hash
        return None, src_hash

    def register(self, path: Path | Any, size: int, content_hash: str | None = None) -> None:
        """Record a freshly placed file so later files dedupe against it too.

        ``path`` must exist by the time any later same-size comparison forces its
        hash — pass the copy/move target, or the source path in a dry run.
        """
        self._by_size.setdefault(size, []).append([path, content_hash])


def unique_destination(src: Path | Any, dest_dir: Path) -> Path:
    """Return a non-clobbering path for ``src`` inside ``dest_dir``.

    Content-level deduplication is handled up front by :class:`DedupIndex`, so a
    name collision here is always a *different* file — we just append a numeric
    suffix (``photo_1.jpg``, ``photo_2.jpg``, …) until the name is free. Nothing
    is ever overwritten.
    """
    target = dest_dir / src.name
    if not target.exists():
        return target
    stem, suffix = src.stem, src.suffix
    counter = 1
    while target.exists():
        target = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def iter_media(source: Path | Any, exclude: Path | None = None) -> Iterator[Any]:
    if hasattr(source, "iter_media"):
        yield from source.iter_media(exclude=exclude)
    else:
        for path in sorted(source.rglob("*")):
            if exclude is not None and path.is_relative_to(exclude):
                continue
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                yield path


def block_bar(frac: float, width: int = 30) -> str:
    """Render a solid-block progress bar with 1/8-block partials for smoothness."""
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    filled = frac * width
    full = int(filled)
    bar = "█" * full
    if full < width:
        eighths = int((filled - full) * 8)
        if eighths:
            bar += "▏▎▍▌▋▊▉"[eighths - 1]
        bar = bar.ljust(width, "░")
    return bar


def fmt_eta(secs: float) -> str:
    secs = int(secs)
    if secs >= 3600:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


def _fit(text: str, width: int) -> str:
    """Truncate to ``width`` columns, appending an ellipsis when cut."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:max(0, width)]
    return text[:width - 1] + "…"


class Dashboard:
    """A live, in-place panel: a fixed-height scrolling log inside a box, with a
    progress bar and counters pinned beneath it.

    The log keeps only the most recent ``log_rows`` entries — older ones scroll
    off the top while the bar stays put. Renders on stderr via ANSI cursor moves,
    redrawing the whole panel each update, so it expects a TTY.
    """

    def __init__(self, title: str, source, total: int, *, log_rows: int = 7):
        cols = shutil.get_terminal_size((80, 24)).columns
        self.inner = max(30, min(cols - 2, 64))  # interior width between borders
        self.title = title
        self.source = str(source)
        self.total = total
        self.log_rows = log_rows
        self.entries: "deque[tuple[str, str]]" = deque(maxlen=log_rows)
        self.start = time.monotonic()
        self._drawn = False
        self._nlines = 0

    def _c(self, text: str, code: str) -> str:
        return paint(text, code, stream=sys.stderr)

    def log(self, text: str, code: str = "") -> None:
        self.entries.append((text, code))

    def _row(self, text: str, code: str = "") -> str:
        border = self._c("│", "32")
        interior = (" " + _fit(text, self.inner - 2)).ljust(self.inner)
        return border + self._c(interior, code) + border

    def _blank(self) -> str:
        border = self._c("│", "32")
        return border + " " * self.inner + border

    def _panel(self, done: int, copied: int, errors: int) -> list[str]:
        inner = self.inner
        label = f" {self.title} "
        fill = max(0, inner - len(label))
        left, right = fill // 2, fill - fill // 2
        top = (self._c("┌" + "─" * left, "32") + self._c(label, "1;32")
               + self._c("─" * right + "┐", "32"))
        bottom = self._c("└" + "─" * inner + "┘", "32")

        lines = [top, self._row(f"Scanning: {self.source}"), self._blank()]
        ents = list(self.entries)
        for i in range(self.log_rows):
            lines.append(self._row(*ents[i]) if i < len(ents) else self._blank())
        lines.append(bottom)

        # Progress bar + counters, pinned below the box.
        frac = done / self.total if self.total else 1.0
        elapsed = time.monotonic() - self.start
        speed = done / elapsed if elapsed > 0 else 0.0
        remaining = (self.total - done) / speed if speed > 0 else 0.0
        bar = block_bar(frac, max(20, inner - 6))
        lines += [
            "",
            self._c("Progress", "1;32"),
            self._c(bar, "32") + f" {frac * 100:.0f}%",
            "",
            f"{'Copied':<9} : {copied}",
            f"{'Errors':<9} : {errors}",
            f"{'Speed':<9} : {speed:.0f} files/sec",
            f"{'Remaining':<9} : {fmt_eta(remaining)}",
        ]
        return lines

    def update(self, done: int, copied: int, errors: int) -> None:
        lines = self._panel(done, copied, errors)
        out = f"\033[{self._nlines - 1}A\r" if self._drawn else ""
        out += "\n".join("\033[2K" + ln for ln in lines)  # 2K = clear whole line
        sys.stderr.write(out)
        sys.stderr.flush()
        self._drawn = True
        self._nlines = len(lines)

    def finish(self, done: int, copied: int, errors: int) -> None:
        self.update(done, copied, errors)
        sys.stderr.write("\n")
        sys.stderr.flush()
        self._drawn = False


class MTPFileItem(os.PathLike):
    """Path-like abstraction representing a media file on an MTP device."""

    def __init__(self, shell: Any, folder_item: Any, staging_dir: Path):
        self._shell = shell
        self._item = folder_item
        self._staging_dir = staging_dir
        self.name: str = str(folder_item.Name)
        p = Path(self.name)
        self.stem: str = p.stem
        self.suffix: str = p.suffix
        self.content_hash: str | None = None
        self._local_path: Path | None = None

        # Size in bytes
        try:
            self._size: int = int(folder_item.Size)
        except Exception:
            self._size = 0

        # Modification time timestamp
        try:
            mdate = folder_item.ModifyDate
            if hasattr(mdate, "timestamp"):
                self._mtime: float = float(mdate.timestamp())
            elif isinstance(mdate, (int, float)):
                self._mtime = float(mdate)
            else:
                self._mtime = datetime.now().timestamp()
        except Exception:
            self._mtime = datetime.now().timestamp()

    def stat(self):
        class _Stat:
            def __init__(self, size: int, mtime: float):
                self.st_size = size
                self.st_mtime = mtime
        return _Stat(self._size, self._mtime)

    def is_file(self) -> bool:
        return True

    def is_relative_to(self, other: Any) -> bool:
        return False

    def ensure_local(self) -> Path:
        """Download/stage the file from MTP into staging_dir on demand."""
        if self._local_path is not None and self._local_path.exists():
            return self._local_path

        target_file = self._staging_dir / self.name
        if target_file.exists():
            try:
                target_file.unlink()
            except OSError:
                pass

        try:
            dest_ns = self._shell.NameSpace(str(self._staging_dir.resolve()))
            if dest_ns is None:
                raise OSError(f"Cannot access staging folder: {self._staging_dir}")
            # 4: do not show progress dialog, 16: Yes to all, 512: no new dir confirm, 1024: no error UI
            dest_ns.CopyHere(self._item, 4 | 16 | 512 | 1024)
        except Exception as e:
            raise OSError(f"MTP device copy failed (device may have disconnected): {e}")

        # Poll for completion
        start_time = time.monotonic()
        timeout = 180.0
        last_size = -1
        stable_count = 0

        while time.monotonic() - start_time < timeout:
            if target_file.exists():
                try:
                    curr_size = target_file.stat().st_size
                    if self._size > 0 and curr_size >= self._size:
                        self._local_path = target_file
                        return target_file
                    if curr_size == last_size and curr_size > 0:
                        stable_count += 1
                        if stable_count >= 3:
                            self._local_path = target_file
                            return target_file
                    else:
                        stable_count = 0
                        last_size = curr_size
                except OSError:
                    pass
            time.sleep(0.1)

        if not target_file.exists():
            raise TimeoutError(f"Transfer timed out copying '{self.name}' from MTP device.")

        self._local_path = target_file
        return target_file

    def __fspath__(self) -> str:
        return str(self.ensure_local())

    def __str__(self) -> str:
        return str(self.ensure_local())

    def cleanup(self) -> None:
        """Clean up the locally staged temporary file to free disk space."""
        if self._local_path is not None and self._local_path.exists():
            try:
                self._local_path.unlink()
            except OSError:
                pass
            self._local_path = None


class WindowsMTPSource:
    """Media source wrapping a Windows Portable Device / MTP folder."""

    def __init__(self, shell: Any, folder: Any, display_name: str):
        self._shell = shell
        self._folder = folder
        self.display_name = display_name
        self._staging_dir_obj = tempfile.TemporaryDirectory(prefix="media_sorter_mtp_")
        self.staging_dir = Path(self._staging_dir_obj.name)

    def iter_media(self, exclude: Path | None = None) -> Iterator[MTPFileItem]:
        stack = [self._folder]
        while stack:
            curr = stack.pop()
            try:
                items = curr.Items()
            except Exception as e:
                raise OSError(f"Failed to read folder on MTP device (device may have disconnected): {e}")

            for item in items:
                try:
                    if item.IsFolder:
                        stack.append(item.GetFolder)
                    else:
                        name = item.Name
                        suffix = Path(name).suffix.lower()
                        if suffix in MEDIA_EXTENSIONS:
                            yield MTPFileItem(self._shell, item, self.staging_dir)
                except Exception as e:
                    raise OSError(f"Error reading item from MTP device (device may have disconnected): {e}")

    def __str__(self) -> str:
        return f"This PC\\{self.display_name}"


def is_explicit_mtp_or_uri(raw: str) -> bool:
    """Check if the source string explicitly specifies an MTP or URI scheme."""
    s = raw.strip()
    s_lower = s.lower()
    if s_lower.startswith(("mtp:", "mtp:/", "mtp://", "mtp:\\", "gphoto2:", "gphoto2://", "ptp:", "camera:")):
        return True
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        return True
    if s_lower.startswith(("this pc\\", "this pc/", "computer\\", "computer/", "my computer\\", "my computer/")):
        return True
    return False


def parse_windows_mtp_path(raw: str) -> list[str]:
    """Parse and normalize a Windows Explorer MTP path into parts."""
    s = raw.strip()
    for prefix in ("mtp://", "mtp:\\", "mtp:/", "mtp:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.replace("/", "\\")
    parts = [p.strip() for p in s.split("\\") if p.strip()]
    if parts and parts[0].lower() in ("this pc", "computer", "my computer"):
        parts = parts[1:]
    return parts


def resolve_windows_mtp_source(raw_source: str) -> WindowsMTPSource:
    """Resolve an Android/MTP source under 'This PC' in Windows Explorer."""
    if not HAS_WIN32COM:
        sys.exit(
            "Error: pywin32 is required to access Android/MTP devices directly on Windows.\n"
            "Please install it using: pip install pywin32"
        )

    parts = parse_windows_mtp_path(raw_source)
    if not parts:
        sys.exit(f"Error: Invalid MTP source path: '{raw_source}'")

    device_name = parts[0]
    subpath = parts[1:]

    try:
        shell = win32com.client.Dispatch("Shell.Application")
        this_pc = shell.NameSpace(17)  # ssfDRIVES = 17 (This PC / Computer)
        if this_pc is None:
            sys.exit("Error: Failed to access Windows Shell 'This PC' namespace.")
    except Exception as e:
        sys.exit(f"Error initializing Windows Shell COM: {e}")

    # Enumerate devices in This PC
    device_item = None
    available_devices: list[str] = []
    try:
        for item in this_pc.Items():
            name = str(item.Name)
            available_devices.append(name)
            if name.lower() == device_name.lower():
                device_item = item
                break
    except Exception as e:
        sys.exit(f"Error accessing devices in 'This PC' (device may have disconnected): {e}")

    if device_item is None:
        portable_candidates = [d for d in available_devices if not (len(d) >= 3 and d[-2:] == ":)")]
        dev_list = ", ".join(f"'{d}'" for d in portable_candidates)
        hint = f"\nAvailable portable devices in 'This PC': {dev_list}" if dev_list else "\nNo portable/MTP devices detected in 'This PC'."
        sys.exit(
            f"Error: Unsupported or disconnected MTP device '{device_name}'.{hint}\n"
            "Ensure the Android device is connected via USB, unlocked, and 'File Transfer / MTP' mode is enabled."
        )

    try:
        current_folder = device_item.GetFolder
        if current_folder is None:
            sys.exit(
                f"Error: Permission denied or device locked: Unable to access storage on '{device_name}'.\n"
                "Please unlock your phone screen and allow USB file transfer access."
            )
    except Exception as e:
        sys.exit(f"Error accessing MTP device '{device_name}': {e}. Device may have been disconnected.")

    # Navigate subpath
    navigated = [device_item.Name]
    for part in subpath:
        found_child = None
        available_children: list[str] = []
        try:
            for child in current_folder.Items():
                if child.IsFolder:
                    available_children.append(str(child.Name))
                    if child.Name.lower() == part.lower():
                        found_child = child
                        break
        except Exception as e:
            sys.exit(f"Error: MTP device disconnected or unresponsive while reading '{'/'.join(navigated)}': {e}")

        if found_child is None:
            avail_str = ", ".join(f"'{c}'" for c in available_children) or "None"
            sys.exit(
                f"Error: Folder '{part}' not found in '{'/'.join(navigated)}'.\n"
                f"Available folders: {avail_str}"
            )
        try:
            current_folder = found_child.GetFolder
            navigated.append(found_child.Name)
        except Exception as e:
            sys.exit(f"Error opening folder '{found_child.Name}' on MTP device: {e}")

    return WindowsMTPSource(shell, current_folder, "\\".join(navigated))


def resolve_source(raw_source: str, dest_root: Path) -> Path | WindowsMTPSource:
    """Resolve and validate the source into either a filesystem Path or an MTP source."""
    raw = raw_source.strip()
    is_windows = sys.platform == "win32"
    is_linux = sys.platform.startswith("linux")
    is_darwin = sys.platform == "darwin"

    if not (is_windows or is_linux or is_darwin):
        sys.exit(f"Error: Unsupported platform '{sys.platform}'. Supported platforms are Linux, Windows, and macOS.")

    # 1. Check if the string explicitly specifies an MTP device / URI
    if is_explicit_mtp_or_uri(raw):
        if is_windows:
            return resolve_windows_mtp_source(raw)
        else:
            sys.exit(
                f"Error: '{raw_source}' is not a normal filesystem path.\n\n"
                "Android phones connected via USB/MTP cannot be accessed directly via MTP URIs\n"
                "or file manager addresses on Linux.\n"
                "The device storage must first be mounted or exposed as a normal filesystem directory.\n\n"
                "Example workflow using a mounted path (/mnt/phone/DCIM):\n"
                "  1. Unlock your phone and select 'File Transfer / MTP' under USB options.\n"
                "  2. Mount the phone to a local directory using jmtpfs (or check your file manager's GVFS mount):\n"
                "       sudo mkdir -p /mnt/phone\n"
                "       sudo chown $USER:$USER /mnt/phone\n"
                "       jmtpfs /mnt/phone\n"
                "     (Or locate your desktop file manager's GVFS mount under /run/user/$UID/gvfs/)\n"
                "  3. Run the sorter against the mounted path:\n"
                "       python3 sort_photos.py --source /mnt/phone/DCIM\n"
                "  4. Safely unmount when finished:\n"
                "       fusermount -u /mnt/phone"
            )

    # 2. Check if it is a local filesystem path
    fs_path = Path(raw).expanduser().resolve()

    if fs_path.is_dir():
        # Safety check: source == destination
        try:
            if fs_path.samefile(dest_root):
                sys.exit(f"Error: Source and destination cannot be the same directory ({fs_path}).")
        except (FileNotFoundError, OSError):
            if fs_path == dest_root:
                sys.exit(f"Error: Source and destination cannot be the same directory ({fs_path}).")

        # Permission check
        try:
            next(fs_path.iterdir(), None)
        except PermissionError:
            sys.exit(f"Error: Permission denied accessing source directory: {fs_path}")
        except OSError as e:
            sys.exit(f"Error: Access failure on source directory: {fs_path} ({e})")

        return fs_path

    # If it exists on filesystem but is not a directory
    if fs_path.exists() and not fs_path.is_dir():
        sys.exit(f"Error: Source path is not a directory: {fs_path}")

    # 3. Path does not exist on filesystem.
    # On Windows, could it be an MTP device name without the 'This PC\' prefix?
    if is_windows:
        is_drive_or_unc = bool(re.match(r"^[a-zA-Z]:[\\/]", raw)) or raw.startswith(("\\\\", "//"))
        if not is_drive_or_unc:
            try:
                return resolve_windows_mtp_source(raw)
            except SystemExit:
                raise
            except Exception as e:
                sys.exit(f"Error resolving source as MTP device: {e}")

    # Missing path error reporting
    if any(k in str(fs_path).lower() for k in ("phone", "mtp", "android")):
        if is_windows:
            sys.exit(
                f"Error: Source folder or portable device not found: '{raw_source}'.\n"
                "Ensure your phone is connected via USB, unlocked, and 'File Transfer / MTP' mode is enabled."
            )
        else:
            sys.exit(
                f"Error: Source folder not found: {fs_path}\n\n"
                "If you are organizing from an Android phone connected via USB/MTP on Linux,\n"
                "ensure the device is unlocked and mounted as a filesystem path first\n"
                "(e.g. sudo mkdir -p /mnt/phone && jmtpfs /mnt/phone, then pass --source /mnt/phone/DCIM)."
            )

    sys.exit(f"Error: Source folder not found: {fs_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True,
                        help="Folder to scan recursively:\n"
                             "  Linux:   Local folder or mounted phone (e.g. /home/user/Pictures, /mnt/phone/DCIM)\n"
                             "  Windows: Local folder, UNC path, or MTP device (e.g. 'C:\\Pictures', "
                             "'This PC\\Phone\\Internal shared storage\\DCIM')")
    parser.add_argument("--dest", default="Nostalgia",
                        help="Destination root for YYYY folders (default: Nostalgia)")
    parser.add_argument("--move", action="store_true",
                        help="Move files instead of copying (removes them from source)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without changing anything")
    parser.add_argument("--verbose", action="store_true",
                        help="Print a line per file instead of the live progress bar")
    args = parser.parse_args()

    if is_explicit_mtp_or_uri(args.dest):
        sys.exit("Error: Destination must be a local filesystem directory, not an MTP device.")
    dest_root = Path(args.dest).expanduser().resolve()
    source = resolve_source(args.source, dest_root)

    print(BANNER)
    boot_sequence()

    scanned = moved = skipped_dup = errors = 0
    exif_count = video_count = mtime_count = 0
    per_year: Counter[str] = Counter()

    # Collect up front so we know the total and can show a percentage.
    op_start = time.monotonic()
    print(f"{status('INFO')} Scanning {source}", flush=True)
    media = list(iter_media(source, dest_root))
    total = len(media)
    n_img = sum(1 for p in media if p.suffix.lower() in IMAGE_EXTENSIONS)
    n_vid = total - n_img
    print(f"{status('OK')} {total} media file{'s' if total != 1 else ''} detected "
          f"({n_img} photo{'s' if n_img != 1 else ''}, "
          f"{n_vid} video{'s' if n_vid != 1 else ''})")
    if total == 0:
        return 0

    # Index what's already under dest so byte-identical files are never copied
    # again — across every year folder, not just on a name clash. Sizes are read
    # now; hashing is deferred until two files actually share a size.
    print(f"{status('INFO')} Indexing existing media under {dest_root}", flush=True)
    index = DedupIndex()
    indexed = 0
    if dest_root.is_dir():
        for path in dest_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                index.add_existing(path)
                indexed += 1
    print(f"{status('OK')} Indexed {indexed} existing media file"
          f"{'s' if indexed != 1 else ''}")

    # Live dashboard by default on a TTY; fall back to per-line output when
    # --verbose is set or stderr is not a terminal (e.g. piped to a file).
    live = not args.verbose and sys.stderr.isatty()
    dash = Dashboard("Media Organizer", source, total) if live else None
    if not live:
        print(f"{status('PROC')} Processing {total} file{'s' if total != 1 else ''}...")

    src_colors = {"exif": "35", "video": "36", "mtime": "90"}
    for item in media:
        scanned += 1
        try:
            size = item.stat().st_size
            dup_path, content_hash = index.find_duplicate(item, size)
            if dup_path is not None:
                skipped_dup += 1
                if live:
                    dash.log(f"⚠ {item.name}  (dup of {dup_path.name})", "33")
                elif args.verbose:
                    print(f"{status('SKIP')} Duplicate: {item.name}  "
                          f"(identical to {dup_path.parent.name}/{dup_path.name})")
                continue

            captured, source_kind = get_capture_date(item)
            year = str(captured.year)
            dest_dir = dest_root / year

            if not args.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)

            target = unique_destination(item, dest_dir)
            renamed = target.name != item.name
            if args.verbose:
                action = "MOVE" if args.move else "COPY"
                verb = "Moved" if args.move else "Copied"
                src = paint(source_kind, src_colors.get(source_kind, "90"))
                prefix = "[dry-run] " if args.dry_run else ""
                renamed_note = "  [renamed]" if renamed else ""
                print(f"{prefix}{status(action)} {verb} {item.name}  → {dest_dir}/  ({src}){renamed_note}")

            if not args.dry_run:
                if args.move:
                    shutil.move(str(item), str(target))
                else:
                    shutil.copy2(str(item), str(target))

            # Register the placed file (its source in a dry run, where target
            # isn't written) so later identical files are caught this run too.
            index.register(item if args.dry_run else target, size, content_hash)

            moved += 1
            per_year[year] += 1
            if source_kind == "exif":
                exif_count += 1
            elif source_kind == "video":
                video_count += 1
            else:
                mtime_count += 1
            if live:
                glyph, code = ("➜", "34") if args.move else ("✔", "32")
                dash.log(f"{glyph} {item.name}  → {year}", code)
        except Exception as exc:
            errors += 1
            if live:
                dash.log(f"✖ {item.name}: {exc}", "31")
            else:
                print(f"{status('ERROR', err=True)} Error processing {item.name}: {exc}",
                      file=sys.stderr)
        finally:
            if hasattr(item, "cleanup"):
                item.cleanup()
            if live:
                dash.update(scanned, moved, errors)

    if live:
        dash.finish(scanned, moved, errors)

    elapsed = time.monotonic() - op_start
    title = "OPERATION COMPLETE" + (" (DRY RUN)" if args.dry_run else "")
    rows = [
        ("Files scanned", str(scanned)),
        ("Successfully moved" if args.move else "Successfully copied", str(moved)),
        ("Duplicates ignored", str(skipped_dup)),
        ("Errors", str(errors)),
        ("Photo EXIF dates", str(exif_count)),
        ("Video metadata dates", str(video_count)),
        ("Filesystem fallback", str(mtime_count)),
    ]
    box = render_box(title, rows)
    box_width = len(box[0])  # outer width, including corner glyphs

    print()
    for i, line in enumerate(box):
        print(paint(line, "1;32" if i == 1 else "32"))  # title row bold

    if per_year:
        print()
        chart = render_distribution(per_year, box_width)
        print(paint(chart[0], "1;32"))   # bold section header
        for line in chart[1:]:
            print(paint(line, "32"))

    print()
    print(paint(f"Elapsed : {elapsed:.2f} sec", "32"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
