# Architecture

VacationMediaProcessor separates a Qt-free core from the PyQt6 GUI. The core
scans media, resolves capture timestamps, builds a dry-run plan, and applies
conversions/metadata/renames; the GUI orchestrates it through worker threads.

```
vmp/
├─ main.py                  # package entry point (run.py in the repo root launches without install)
├─ core/                    # models, settings, logging, subprocess helpers, discovery
├─ timestamps/              # parsing.py · candidates.py · resolution.py
├─ metadata/                # __init__ (ExifTool read + analysis) · writing.py · gps.py
├─ tools/                   # __init__ (ExifTool write) · image.py (NConvert) · video.py (FFmpeg)
├─ pipeline/                # __init__ (facade) · shared.py · scan.py · apply.py
├─ planner.py               # dry-run plan building
├─ pair_cleanup.py          # IMG_/IMG_E detection + pixel containment
├─ manifest.py · reports.py
└─ gui/
   ├─ workers.py · settings_dialog.py
   ├─ common/               # theme, widgets, form rows, thumbnails (LRU cache),
   │                        # file transfer, plan display, backup discovery, dialogs
   ├─ main/                 # window.py (orchestrator) + mixins: scan_flow, apply_flow,
   │                        # diff_actions, overlay_flow, table_actions, status_stats,
   │                        # worker_lifecycle, window_geometry, workflow settings;
   │                        # builders: layout.py, header.py, workflow_panel.py;
   │                        # media_table.py (controller), preview_pane.py
   ├─ lasso/                # dialog.py, ui.py, map_view.py, histogram.py, thumb_strip.py,
   │                        # transfer_worker.py, trip_selection.py, geocode.py
   └─ pairs/                # dialog.py, row.py, viewer.py, worker.py
```


## Design notes

- **Layering:** `core` imports nothing above itself; `timestamps` → `core`;
  `metadata` → `timestamps`/`core`; `tools` → `metadata`/`core`; `pipeline` →
  `tools`/`planner`/`manifest`. Nothing outside `gui/` imports `gui`.
- **Facades:** `metadata`, `pipeline`, and `tools` are packages whose
  `__init__` re-exports the public API, so call sites stay stable while the
  implementation is split into focused modules.
- **MainWindow composition:** `gui/main/window.py` is a thin orchestrator
  composed of mixins (scan flow, apply flow, diff actions, overlays, table
  actions, stats, worker lifecycle, geometry) plus builder functions for the
  layout, header, and workflow sidebar.
- **Threading:** all pipeline work runs on QThread workers communicating via
  signals; previews/thumbnails decode QImages on daemon threads and marshal
  results through relays. Only the GUI thread touches widgets or QPixmaps.
- **i18n:** German is the source language; `core/i18n.py` translates at
  widget-build time from JSON catalogs in `vmp/assets/i18n/`.
  A language switch rebuilds the main window and carries the session state over
  (no rescan).
