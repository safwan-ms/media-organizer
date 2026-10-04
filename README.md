# 📸 Media Organizer

A robust Python script to automatically sort, organize, and deduplicate your photos and videos into structured, year-based folders (`YYYY/`).

```
███████╗ ██████╗ ██████╗ ████████╗
██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝
███████╗██║   ██║██████╔╝   ██║
╚════██║██║   ██║██╔══██╗   ██║
███████║╚██████╔╝██║  ██║   ██║
╚══════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝
     photo & video sorter
```

---

## ✨ Features

- **Multi-Format Support**: Automatically handles both photos and videos.
- **Smart Date Extraction**:
  - **Photos**: Extracts date from EXIF metadata (`DateTimeOriginal` ➜ `DateTimeDigitized` ➜ `DateTime`).
  - **Videos**: Probes container metadata using `ffprobe` (supports Apple QuickTime capture time or UTC conversion) with a dependency-free custom `MP4/MOV` atom reader fallback.
  - **Fallback**: Falls back gracefully to the file's modification time if metadata is missing.
- **Deduplication & Safety**:
  - Automatically compares file hashes (SHA-256) to skip exact duplicates.
  - Generates non-clobbering file names (e.g. `image_1.jpg`) if two different files share the same filename.
- **Copy or Move**: Copies files by default to keep your originals safe, with an optional `--move` mode.
- **Interactive Dashboard**: Displays a sleek, live progress bar, speed tracker, and active logs when run in a terminal.
- **Summary Reports**: Generates a final summary card and an ASCII-art bar chart showing the yearly distribution of organized media.

---

## 🛠️ Prerequisites

1. **Python 3.8+**
2. **Pillow** (Required for photo metadata):
   ```bash
   pip install Pillow
   ```
3. **FFmpeg / ffprobe** (Optional, recommended for advanced video metadata extraction):
   - **Windows**: `winget install Gyan.FFmpeg`
   - **macOS**: `brew install ffmpeg`
   - **Linux**: `sudo apt install ffmpeg`
4. **pywin32** (Optional, required on Windows only when accessing Android/MTP devices directly):
   ```bash
   pip install pywin32
   ```

---

## 🚀 Usage

Always run a **dry-run** first to preview the organization without actually writing, moving, or copying any files.

### 1. Preview changes (Dry Run)
```bash
python sort_photos.py --source "path/to/source_folder" --dry-run
```

### 2. Copy and Organize (Default)
Copies files from the source directory into a `Nostalgia/` directory in the current workspace:
```bash
python sort_photos.py --source "path/to/source_folder"
```

### 3. Move and Organize
Moves files (removing them from the source directory):
```bash
python sort_photos.py --source "path/to/source_folder" --move
```

### 4. Custom Destination Directory
```bash
python sort_photos.py --source "path/to/source" --dest "path/to/destination"
```

### 5. Organizing from an Android Phone (USB / MTP on Linux)

Android devices connect to Linux via **MTP** (Media Transfer Protocol) rather than standard USB mass storage. Storage addresses viewed in file managers (such as `mtp://[usb:001,002]/...`) are protocol URIs, not normal filesystem paths.

The script requires a normal filesystem path. To sort photos directly from your phone, first mount or expose the phone storage on your filesystem:

#### Workflow Using `jmtpfs` (Recommended):
1. **Connect & unlock your phone**: Plug in USB and set USB mode to **File Transfer / MTP**.
2. **Mount phone to a directory** (e.g. `/mnt/phone`):
   ```bash
   sudo mkdir -p /mnt/phone
   sudo chown $USER:$USER /mnt/phone
   jmtpfs /mnt/phone
   ```
3. **Run the sorter against the mounted path** (e.g. `/mnt/phone/DCIM`):
   ```bash
   python sort_photos.py --source /mnt/phone/DCIM --dest ~/Nostalgia
   ```
4. **Unmount when finished**:
   ```bash
   fusermount -u /mnt/phone
   ```

#### Alternative: Using Your Desktop File Manager (GVFS):
If your desktop environment (GNOME, XFCE, etc.) automatically mounts your phone when opened in the file manager, the storage is exposed under GVFS:
```bash
# Locate your device mount:
ls "/run/user/$UID/gvfs/"

# Run the sorter with the GVFS path:
python sort_photos.py --source "/run/user/$UID/gvfs/mtp:host=.../Internal shared storage/DCIM"
```

### 6. Windows: Local Folders, Network Shares, and Android MTP

The script natively supports Windows paths, UNC network shares, and Android MTP devices:

#### Local Folders & UNC Paths:
```powershell
# Local drive path
python sort_photos.py --source "C:\Users\User\Pictures" --dest "D:\Photos\Nostalgia"

# UNC network share
python sort_photos.py --source "\\server\share\Pictures" --dest "C:\Nostalgia"
```

#### Android Phone (USB / MTP on Windows):
On Windows, Android phones connected via USB appear under **This PC** in Windows Explorer without a drive letter.

You can organize media directly from the phone using its Explorer namespace without manually copying files first:
```powershell
# Run using the Windows Explorer path:
python sort_photos.py --source "This PC\Pixel 7\Internal shared storage\DCIM" --dest "C:\Users\User\Nostalgia"

# Or by device name:
python sort_photos.py --source "Pixel 7\Internal shared storage\DCIM"
```
*(Requires `pywin32`: install via `pip install pywin32`)*



---

## ⚙️ Command-Line Options

| Flag | Description |
| :--- | :--- |
| `--source <path>` | **Required**. Path to the directory containing files to organize. |
| `--dest <path>` | Path where organized `YYYY` directories will be created. (Default: `Nostalgia`) |
| `--move` | Move files instead of copying them. |
| `--dry-run` | Preview the actions without creating directories or altering files. |
| `--verbose` | Print log lines per file instead of showing the interactive terminal dashboard. |

---

## 📂 Supported Extensions

- **Images**: `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.webp`, `.heic`, `.cr2`, `.nef`, `.arw`, `.dng`, and more.
- **Videos**: `.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`, `.wmv`, `.3gp`, `.mpeg`, `.mts`, and more.
