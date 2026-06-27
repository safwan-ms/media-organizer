#!/usr/bin/env python3
"""Sort images from an inbox folder into Photos/YYYY folders by capture date.

For each image found recursively under the inbox folder, the capture year is
determined from EXIF (DateTimeOriginal, then DateTimeDigitized, then DateTime),
falling back to the file's modification time when no usable EXIF date exists.
Each image is copied into ``<dest>/YYYY/`` and is never overwritten: if a file
with the same name already exists, an identical file (same SHA-256) is skipped,
otherwise a numeric suffix is appended (e.g. ``photo_1.jpg``).

By default images are copied (originals stay in the inbox); pass --move to
move them instead.

--source is required so the script works against any folder: a phone's DCIM,
an SD or DSLR memory card, an external drive, or an existing folder on disk.

Usage:
    python3 sort_photos.py --source /media/sdcard/DCIM         # copy -> Photos
    python3 sort_photos.py --source ~/Pictures/Inbox --move    # move instead
    python3 sort_photos.py --source /mnt/usb --dest ~/Photos   # custom dest
    python3 sort_photos.py --source /media/sdcard --dry-run    # preview only
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

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
        photo sorter
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
                "Connecting to image database..."):
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

# EXIF tag ids for date fields, in order of preference.
_TAG_BY_NAME = {name: tag for tag, name in ExifTags.TAGS.items()}
EXIF_DATE_TAGS = [
    _TAG_BY_NAME["DateTimeOriginal"],
    _TAG_BY_NAME["DateTimeDigitized"],
    _TAG_BY_NAME["DateTime"],
]


def get_capture_date(path: Path) -> tuple[datetime, str]:
    """Return (capture datetime, source) where source is 'exif' or 'mtime'."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
        for tag in EXIF_DATE_TAGS:
            raw = exif.get(tag)
            if not raw:
                continue
            # EXIF dates look like "2021:05:14 15:53:59".
            try:
                return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S"), "exif"
            except ValueError:
                continue
    except Exception:
        # Unreadable / corrupt / not really an image -> fall back to mtime.
        pass
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_destination(src: Path, dest_dir: Path) -> tuple[Path | None, bool]:
    """Pick a non-clobbering destination path for ``src`` inside ``dest_dir``.

    Returns (target, is_duplicate). target is None and is_duplicate is True when
    an identical file (same content) already exists -> caller should skip.
    """
    target = dest_dir / src.name
    if not target.exists():
        return target, False

    src_hash = sha256(src)
    stem, suffix = src.stem, src.suffix
    counter = 1
    while target.exists():
        if target.is_file() and sha256(target) == src_hash:
            return None, True  # exact duplicate already present
        target = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return target, False


def iter_images(source: Path):
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True,
                        help="Folder to scan recursively (e.g. a phone DCIM, "
                             "SD/DSLR card mount, external drive, or any folder)")
    parser.add_argument("--dest", default="Photos",
                        help="Destination root for YYYY folders (default: Photos)")
    parser.add_argument("--move", action="store_true",
                        help="Move files instead of copying (removes them from source)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without changing anything")
    parser.add_argument("--verbose", action="store_true",
                        help="Print a line per file instead of the live progress bar")
    args = parser.parse_args()

    print(BANNER)
    boot_sequence()

    source = Path(args.source)
    dest_root = Path(args.dest)

    if not source.is_dir():
        sys.exit(f"Source folder not found: {source}")

    scanned = moved = skipped_dup = errors = 0
    exif_count = mtime_count = 0
    per_year: Counter[str] = Counter()

    # Collect up front so we know the total and can show a percentage.
    op_start = time.monotonic()
    print(f"{status('INFO')} Scanning {source}", flush=True)
    images = list(iter_images(source))
    total = len(images)
    print(f"{status('OK')} {total} image{'s' if total != 1 else ''} detected")
    if total == 0:
        return 0

    # Live dashboard by default on a TTY; fall back to per-line output when
    # --verbose is set or stderr is not a terminal (e.g. piped to a file).
    live = not args.verbose and sys.stderr.isatty()
    dash = Dashboard("Photo Organizer", source, total) if live else None
    if not live:
        print(f"{status('PROC')} Processing {total} file{'s' if total != 1 else ''}...")

    for img in images:
        scanned += 1
        try:
            captured, source_kind = get_capture_date(img)
            year = str(captured.year)
            dest_dir = dest_root / year

            if not args.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)

            target, is_dup = unique_destination(img, dest_dir)

            if is_dup:
                skipped_dup += 1
                if live:
                    dash.log(f"⚠ {img.name}  (duplicate)", "33")
                elif args.verbose:
                    print(f"{status('SKIP')} Duplicate: {img.name}  (already in {year}/)")
                continue

            renamed = target.name != img.name
            if args.verbose:
                action = "MOVE" if args.move else "COPY"
                verb = "Moved" if args.move else "Copied"
                src = paint("exif", "35") if source_kind == "exif" else paint("mtime", "90")
                prefix = "[dry-run] " if args.dry_run else ""
                renamed_note = "  [renamed]" if renamed else ""
                print(f"{prefix}{status(action)} {verb} {img.name}  → {dest_dir}/  ({src}){renamed_note}")

            if not args.dry_run:
                if args.move:
                    shutil.move(str(img), str(target))
                else:
                    shutil.copy2(str(img), str(target))

            moved += 1
            per_year[year] += 1
            if source_kind == "exif":
                exif_count += 1
            else:
                mtime_count += 1
            if live:
                glyph, code = ("➜", "34") if args.move else ("✔", "32")
                dash.log(f"{glyph} {img.name}  → {year}", code)
        except Exception as exc:
            errors += 1
            if live:
                dash.log(f"✖ {img.name}: {exc}", "31")
            else:
                print(f"{status('ERROR', err=True)} Error processing {img.name}: {exc}",
                      file=sys.stderr)
        finally:
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
        ("EXIF timestamps", str(exif_count)),
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
