<div align="center">

<img src="vmp/assets/icon.png" width="112" alt="VacationMediaProcessor logo">

# VacationMediaProcessor

**Turn a chaotic pile of vacation photos & videos into a clean, correctly‑dated, space‑saving library — safely.**

Drop a folder in, preview exactly what will happen, and apply it. Originals are backed up first.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![PyQt6](https://img.shields.io/badge/GUI-PyQt6-41CD52?logo=qt&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-desktop-0078D6?logo=windows&logoColor=white)
![Dry run](https://img.shields.io/badge/dry--run-preview-informational)
![Backups](https://img.shields.io/badge/originals-backed%20up-success)
![License](https://img.shields.io/badge/license-MIT-blue)

</div>

<p align="center">
  <a href="docs/images/main-window.jpg">
    <img src="docs/images/main-window.jpg" width="100%" alt="VacationMediaProcessor main window — dry-run plan with live preview and per-file evidence">
  </a>
  <br>
  <sub><b>The main window.</b> Every file becomes a reviewable row — status, resolved capture time, timestamp source, planned action, target name, video bucket/CRF and size — next to a live preview and a per-file evidence panel. Nothing is touched until you press <b>Apply</b>.</sub>
</p>

---

## The problem

You get home from a trip with **thousands of photos and videos** from two phones, a camera, and a GoPro. The filenames are gibberish (`IMG_0042`, `20240712_...`, `VID-xyz`), the capture dates are missing, wrong, or in three different timezones, the HEIC/4K files are eating your disk, and half of them carry weird Samsung/Android junk metadata.

**VacationMediaProcessor fixes all of that in one guided pass — without ever silently touching an original.**

## What it does for you

### 📅 "Everything is named randomly and out of order"
Reads the *real* capture time from EXIF / QuickTime / GPS / filename, resolves the right local time (even across devices and timezones), and renames every file to a clean, sortable `YYYYMMDD_HHMMSS` name. Files with only a date (no real time) are held for review — never stamped with a fake `00:00:00`.

### 🗜️ "My photos and videos are way too big"
Converts HEIC/HEIF (and optionally PNG) to JPEG, and transcodes videos to modern **HEVC/x265** with per‑resolution quality (FHD/QHD/4K) — optionally capping oversized clips at Full HD. HEIC/HEIF conversion has its **own separate JPEG quality** slider, independent of the JPEG‑source quality, so you can trade size vs. fidelity differently for each. Phone "x265" clips are re‑encoded properly too, because real‑time phone encoding is far from optimal.

**One pass, per-resolution — something HandBrake and Shutter Encoder can't do.** Neither of them lets you assign a *separate CRF per source resolution*, or downscale 4K → Full HD *with its own quality target*, in a single batch pass — you'd script it or queue each resolution by hand. Here FHD/QHD/4K each carry their own CRF, oversized clips are capped to Full HD on the way through, and every clip is handled in the same run. → **[See the per-resolution buckets ↓](#video-quality)**

**Verify the compression in one click.** Wire [`video-compare`](https://github.com/pixop/video-compare) in as the video diff tool (just like Beyond Compare for images): after processing, clicking a file's **size** plays the original and the transcode **side by side**, so you can judge the before/after quality instantly.

**Portrait/depth photos are handled with care.** Optionally **keep depth‑bearing HEICs as HEIC** (still renamed/normalized) so the iPhone Portrait depth map survives, or convert them and **embed the depth map into the JPEG as Google GDepth** so later bokeh edits stay possible.

### 🌍 "The timestamps are just wrong"
A dedicated timestamp resolver cross‑checks every date source, derives the timezone offset, and even corrects Samsung clips that store the *end* of the recording in UTC. Real capture tags always win over guesses, each result carries a **confidence** rating (high/medium/low), and any conflict is flagged for you. It even **warns when your trip spans more weeks than expected**, so a single file with a wrong year stands out instead of scattering your library across the calendar.

### 🧹 "There's junk metadata everywhere"
Removes Samsung motion‑photo trailers, depth‑map cruft, and other unwanted tags — with dedicated **Samsung cleanup** and **Samsung + GPS cleanup** modes when you *only* want a cleanup and nothing else.

### 🗺️ "One giant folder — I want to split it into separate trips"
Open the **Trip Lasso**: draw a polygon on a **map** (photos plotted by GPS), and/or pick days on an interactive **date histogram** (click‑and‑drag to select date ranges). Review the matches as a **thumbnail strip**, then **move or copy** just those files into a new subfolder — whose name is even suggested via reverse geocoding. Perfect for untangling a year‑end dump into per‑trip folders.

→ **[See the Trip Lasso in action ↓](#trip-lasso)**

### 👯 "Burst mode and iPhone edits dumped duplicate pairs on me"
Yo — do you also get **HEIC files with the same capture time** (hello, burst mode) or those weird `IMG_Exxxx` **edited/cropped copies** the iPhone transfers next to every `IMG_xxxx` (a 9:16 crop you never asked for)… and just want to declutter your albums *fast*? Open the **Pair Cleanup** tool (toolbar button with a live **count badge**):

- It finds every `IMG_/IMG_E` pair that shares a capture time and tells you *which kind* it is.
- **Crop pairs** (the edit is a smaller cutout) are **pixel‑verified** to be fully contained in the original via normalized cross‑correlation, then **pre‑selected** — one click sweeps them all.
- **Portrait/blur pairs** (same size, genuinely different look — real iPhone Portrait‑mode bokeh) are shown but **never auto‑selected**; you keep the original, the edited one, or both, per pair.
- **Click a thumbnail** to compare **both full images side by side** (the original fills the frame, the crop shows exactly what was trimmed); **click the filename** to open it in your default app.
- Nothing is hard‑deleted: cleared files move into a `_VacationMediaProcessor_PairCleanup/` backup folder.

→ **[See Pair Cleanup in action ↓](#pair-cleanup)**

### 🔁 "Some photos are sideways / thumbnails are stale"
The standalone **JPG Fix** losslessly rotates JPEGs to their EXIF orientation (no re‑encode, no quality loss) and rebuilds the embedded EXIF thumbnail — independent of the full normalize pass.

**HEIC/HEIF orientation is handled carefully.** Normally a recent NConvert (via libheif) applies the stored HEIF rotation (`irot`) automatically when it decodes the file, so the JPEG comes out already upright. But we researched this and found HEIC orientation handling is notoriously *build- and version-dependent* — several upstream projects (photoprism, ImageMagick, heic-convert) have shipped bugs where the rotation is dropped or double-applied. So the app **doesn't just trust it: it verifies.** After conversion it compares the source's true display dimensions (decoded via pillow‑heif) against the produced JPEG's actual pixel size; if they're transposed — meaning the converter *didn't* rotate — it transparently **re-renders the JPEG from pillow‑heif** in the correct orientation. The Orientation tag is then normalized so viewers never rotate an already-upright image a second time. (A pure 180°/mirror flip doesn't change dimensions and so still relies on the decoder.)

### 🕒 "I want the file's own date to match the photo"
Optionally sets the real **filesystem create/modify dates** (and, on Windows, the creation date) to the resolved local capture time, so the files sort correctly even outside this app.

### 🛟 "I don't trust batch tools with my only copy"
Nothing happens blind. Every run starts as a **dry‑run plan** you can inspect row by row (with previews), originals are copied into a timestamped **backup** folder before replacement, and you can apply everything, images only, videos only, or **just the rows you picked**. For auditing, it can also write **Before/After EXIF JSON manifests** with identical keys, so you can open them side‑by‑side in your text‑diff tool and see exactly which tags changed.

### 🌐 "Ich will die Oberfläche auf Deutsch — my friend wants English"
The UI ships in **German and English**. The language is **auto-detected from the system**, can be pinned in `Settings > Misc > Sprache / Language`, and is stored in the user settings. Translations are plain **JSON language files** under `vmp/assets/i18n/` (`en.json`, …) — drop in another `<code>.json` and it becomes selectable, no code change needed.

## Screenshots

_Click any image to open it full size._

<a id="trip-lasso"></a>

### 🗺️ Trip Lasso — carve trips out of one giant folder

<table>
<tr>
<td width="50%" align="center" valign="top">
<a href="docs/images/trip-lasso-map.jpg"><img src="docs/images/trip-lasso-map.jpg" width="100%" alt="Trip Lasso — polygon selection on the map"></a>
<br><sub><b>Draw a polygon on the map.</b> Photos are plotted by GPS; media without GPS in the same time window is pulled in automatically, and a target folder name is suggested via reverse geocoding.</sub>
</td>
<td width="50%" align="center" valign="top">
<a href="docs/images/trip-lasso-histogram.jpg"><img src="docs/images/trip-lasso-histogram.jpg" width="100%" alt="Trip Lasso — day selection on the date histogram"></a>
<br><sub><b>Or pick days on the histogram.</b> Click-and-drag across the day bars to grab a date range, review the thumbnail strip, then move or copy exactly those files into a subfolder.</sub>
</td>
</tr>
</table>

<a id="pair-cleanup"></a>

### 👯 Pair Cleanup — clear iPhone/burst duplicates

<table>
<tr>
<td width="50%" align="center" valign="top">
<a href="docs/images/pair-cleanup-crops.jpg"><img src="docs/images/pair-cleanup-crops.jpg" width="100%" alt="Pair Cleanup — crop pairs pixel-verified as contained in the original"></a>
<br><sub><b>Crop pairs are pixel-verified.</b> Each <code>IMG_E</code> edit is checked to be fully contained in the original via normalized cross-correlation (NCC ≈ 1.00) and pre-selected — one click sweeps them all.</sub>
</td>
<td width="50%" align="center" valign="top">
<a href="docs/images/pair-cleanup-portraits.jpg"><img src="docs/images/pair-cleanup-portraits.jpg" width="100%" alt="Pair Cleanup — portrait/blur pairs are never auto-selected"></a>
<br><sub><b>Portrait/blur pairs are never auto-selected.</b> Same size, genuinely different look (real Portrait-mode bokeh) — you keep the original, the edit, or both, per pair.</sub>
</td>
</tr>
<tr>
<td colspan="2" align="center" valign="top">
<a href="docs/images/pair-cleanup-compare.jpg"><img src="docs/images/pair-cleanup-compare.jpg" width="82%" alt="Pair Cleanup — side-by-side full-image comparison viewer"></a>
<br><sub><b>Click a thumbnail to compare both full images side by side.</b> One button keeps only the version you pick; another opens it in your default app. Nothing is hard-deleted — cleared files move to a backup folder.</sub>
</td>
</tr>
</table>

<a id="video-quality"></a>

### 🎛️ Full control over quality &amp; metadata

<table>
<tr>
<td colspan="2" align="center" valign="top">
<a href="docs/images/video-quality-buckets.jpg"><img src="docs/images/video-quality-buckets.jpg" width="100%" alt="Per-resolution video CRF buckets shown in the plan"></a>
<br><sub><b>Per-resolution video quality.</b> Clips are bucketed FHD/QHD/4K each with its own CRF (e.g. <code>4K / CRF 28</code>, <code>x265 [4K]</code>), HEIC→JPEG keeps a separate quality, and capture metadata is preserved throughout.</sub>
</td>
</tr>
<tr>
<td width="42%" align="center" valign="top">
<a href="docs/images/workflow-settings.jpg"><img src="docs/images/workflow-settings.jpg" width="86%" alt="Workflow settings side panel with inline tooltips"></a>
<br><sub><b>The workflow panel.</b> HEIC/PNG conversion, depth handling, per-bucket CRF, Full-HD cap, apply mode, metadata checks and backups — every knob for the run, with inline tooltips.</sub>
</td>
<td width="58%" align="center" valign="top">
<a href="docs/images/column-filter.jpg"><img src="docs/images/column-filter.jpg" width="62%" alt="Excel-style column filter popup"></a>
<br><sub><b>Excel-style column filters.</b> Click any header to filter by text or a value list — e.g. show only videos, only <code>WARN</code> rows, or a single bucket.</sub>
<br><br>
<a href="docs/images/depth-map-preserve.jpg"><img src="docs/images/depth-map-preserve.jpg" width="92%" alt="Evidence panel showing a depth-bearing HEIC kept as HEIC"></a>
<br><sub><b>Depth maps are handled with care.</b> Depth-bearing HEICs can be <b>kept as HEIC</b> (renamed/normalized only) so the Portrait depth map survives — the evidence panel spells out exactly what will happen.</sub>
</td>
</tr>
</table>

### ✨ Little touches

<table>
<tr>
<td width="50%" align="center" valign="top">
<a href="docs/images/gps-google-maps.jpg"><img src="docs/images/gps-google-maps.jpg" alt="Tooltip: double-click a GPS cell to open Google Maps"></a>
<br><sub><b>GPS → Google Maps.</b> Double-click a file's GPS cell to open the exact location in Google Maps.</sub>
</td>
<td width="50%" align="center" valign="top">
<a href="docs/images/missing-exif.jpg"><img src="docs/images/missing-exif.jpg" alt="Toolbar button with badge: show files without a core EXIF date"></a>
<br><sub><b>Find missing dates.</b> A badge on the toolbar shows how many files lack a core EXIF capture date — one click lists them.</sub>
</td>
</tr>
</table>

## At a glance

- 🎯 **Confidence rating per file** — every resolved capture time is scored high/medium/low, and conflicts are flagged.
- 📆 **Trip date-span check** — warns when your dates span more weeks than expected, so a wrong-year outlier can't scatter your library.
- ♻️ **Safe to re-run** — already-processed files are marked `DONE` and never re-converted or re-transcoded on the next scan.
- 🗺️ **Trip Lasso** — carve trips out of one huge folder via map polygon + date histogram, then move/copy them into subfolders.
- 👯 **Pair Cleanup** — finds iPhone/burst `IMG_/IMG_E` duplicates by capture time, pixel‑verifies crops as contained in the original, and clears the redundant copy to a backup folder.
- 🔍 **Dry-run first, backups always** — inspect the full plan with previews before anything changes.
- 🌐 **German & English UI** — system-detected, switchable in Settings, extensible via JSON language files.

## Supported media

- **Images:** `.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`
- **Videos:** `.mp4`, `.mov`, `.m4v`

App-generated folders (`_VacationMediaProcessor_*`) and dot-directories are always excluded from scans.

## Quick start

**Requirements:** Windows · Python 3.11+ · [ExifTool](https://exiftool.org/), [FFmpeg + FFprobe](https://ffmpeg.org/) and [NConvert](https://www.xnview.com/en/nconvert/) on your `PATH` (or point the app at them later under `Settings > Settings öffnen …`).

```powershell
git clone https://github.com/bjspi/VacationMediaProcessor.git
cd VacationMediaProcessor
pip install -r requirements.txt

# run it — no package install needed:
python run.py

# or without a console window (ideal for a desktop shortcut):
pythonw run.py
```

For a double-click launcher, create a shortcut with target `pythonw run.py` and the repo folder as working directory — the app already suppresses console windows of its child tools (ExifTool/FFmpeg/NConvert) when run this way. Installing the package (`pip install .`) additionally gives you the `vmp` command.

**First run:** open or drag-and-drop a folder → **Scan** → review the dry-run plan row by row → **Apply**. Add more folders to the same session anytime; every workflow setting lives in the right sidebar, and originals are backed up before anything is replaced.

## The workflow

1. Open or drag/drop a folder (add more folders to the same session if you like).
2. Tune the right‑side workflow settings (quality, apply mode, cleanup, …).
3. **Scan** builds a dry‑run plan.
4. Review it in the sortable table — status, resolved date, timestamp source, planned action, target name, video bucket/CRF, size — with image/video preview and per‑file details.
5. **Apply** all planned work, images only, videos only, or selected rows.

Double‑click a row to open the file; double‑click the size column to launch your diff tool (when a backup exists). Right‑click a row for Explorer/open actions and backup‑aware image/video/EXIF diffs.

### Review & control

- **Built‑in preview & evidence** — image and video preview (HEIC/HEIF via libheif, video frames via FFmpeg) plus a per‑file panel showing the resolved time, its confidence, and exactly which metadata tags produced it.
- **Excel‑style column filters** — click any column header to filter by text or a value list (e.g. show only videos, only `WARN` rows, or one date).
- **Live status bar** — image and video counts with total sizes, and a **before → after** delta once files are processed, so you can see how much space you saved.
- **Process exactly what you want** — apply all, images only, videos only, or just the selected rows; ideal for handling a single problem file.
- **Standalone JPG Fix** — lossless EXIF‑orientation rotation and EXIF‑thumbnail rebuild, separate from the normalize pipeline.
- **Missing‑EXIF review** and **Excel export** for a quick overview of files without a core capture date.
- **Log panel & log file** — toggle an in‑app log dock or open the full log; every apply run also writes its own run log.

## Apply modes

| Mode | What it does |
| --- | --- |
| **Full normalize** | Image conversion (HEIC/HEIF & PNG only if their toggles are on), video transcode, metadata normalization, and timestamp rename. |
| **Rename only** | Keep encoding/extension; only rename when the timestamp is usable. |
| **Samsung cleanup only** | Remove Samsung trailer/cleanup metadata, nothing else. |
| **Samsung + GPS cleanup** | Samsung cleanup plus GPS datetime cleanup. |

## Safety model

- **Backups first.** `Run All` and the one‑by‑one buttons copy originals into `_VacationMediaProcessor_Backup/<run-id>/` before changing or replacing anything — unless you explicitly enable `Backup überspringen`.
- **No fabricated times.** Date‑only files are never renamed or stamped with a fake `00:00:00`; they're held for review, cleanup‑only.
- **One run at a time.** While a scan/apply/JPG‑Fix is running, opening a folder or starting another run is blocked, so a second run can never orphan the first (or its `ffmpeg`/`nconvert` processes).
- **Safe to re‑run.** Files already normalized by this app are marked (a small VMP comment) and shown as `DONE`, so re‑scanning a folder never re‑converts or re‑transcodes them — only idempotent cleanup is re‑applied.
- **Explicit confirmation.** Every apply/JPG‑Fix asks first and states whether backups will be created or skipped.
- **Optional filesystem dates.** Can set the real file access/modify (and, on Windows, creation) time to the resolved local capture time.
- **Full HD cap.** Videos above Full HD can get an aspect‑preserving downscale to max 1920×1080 (UI shows e.g. `4K -> FHD / CRF 25`).

## Settings

Persisted under `%LOCALAPPDATA%/VacationMediaProcessor/settings.json` (with a local `.vmp_settings/settings.json` fallback). The right panel includes recursive scan, backup skipping, JPEG quality, HEIC/HEIF‑to‑JPG and PNG‑to‑JPG toggles, **keep depth‑bearing HEICs / embed depth as GDepth**, JPG rotation/thumbnail rebuild, FHD/QHD/4K CRF and bucket thresholds, audio codec/bitrate (`copy` keeps the source track only when it is MP4‑safe, otherwise re‑encodes to AAC), Full‑HD downscale, apply mode, sanity tolerance, vacation date‑span warning, **millisecond collision naming** (`…155603.450ms.jpg` instead of `-2` when the shot has sub‑seconds), junk‑tag cleanup, filesystem‑date setting, stop‑on‑conflict, Before/After EXIF JSON export, diff‑tool templates, and table font size. Tool paths and the **Pair Cleanup parallel‑worker count** (Settings > Misc) live in `Settings > Settings öffnen …`.

## External tools

The automatic pipeline calls these command‑line tools (paths configurable in the GUI):

- **ExifTool** — metadata scan & write
- **FFmpeg** / **FFprobe** — video transcode & probing
- **NConvert** (XnConvert‑compatible CLI) — image conversion & JPG Fix

The settings dialog splits tools into **CMD Tools** (pipeline), **Difftools** (image/video/text diff templates using `$source`/`$target`), and **Standalone GUI Tools** (manual launchers for XnConvert, XnView MP, Shutter Encoder). Diff tools are backup‑aware: a media diff only appears when the original backup exists and the matching template is configured. A great pairing: [`video-compare`](https://github.com/pixop/video-compare) as the **video** diff and Beyond Compare as the **image** diff — then a single click on a processed file's size gives you an instant before/after.

## Outputs

- `_VacationMediaProcessor_Work/<run-id>/` — temporary conversion/transcode files
- `_VacationMediaProcessor_Backup/<run-id>/` — original backups (unless skipped)
- `_VacationMediaProcessor_PairCleanup/` — duplicates cleared from the Pair Cleanup tool (moved here, never hard‑deleted)
- `_VacationMediaProcessor_Manifest/<run-id>.json` — run settings, plans, and report
- `_VacationMediaProcessor_Manifest/<run-id>_before.json` / `_after.json` — optional comparable EXIF snapshots (same stable keys, ideal for left/right diffing)
- `_VacationMediaProcessor_Temp/` — temporary EXIF JSON for ad‑hoc text diffs
- `vmp_preview.xlsx` — optional Excel review export

## Performance

Apply runs use a two‑phase schedule: image plans first with bounded thread parallelism (`images.parallel_workers`), then videos strictly serial (stable ETA/progress). ExifTool reads run in configurable batches (files‑per‑batch and parallel batches), and image conversion, preview decoding, and Lasso thumbnails each have their own worker‑count setting. FFprobe results are cached per file, and workflow‑setting changes refresh only the affected table columns.

## Code layout

Qt-free core (`core/`, `timestamps/`, `metadata/`, `tools/`, `pipeline/`, planners) strictly separated from the PyQt6 GUI (`gui/common`, `gui/main`, `gui/lasso`, `gui/pairs`). The full module map and design notes live in [docs/architecture.md](docs/architecture.md).

## Tests

```powershell
python -m unittest
```
