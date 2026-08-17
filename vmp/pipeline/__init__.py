"""High-level scan, plan, and apply pipeline (facade).

The implementation lives in three focused modules and is re-exported here as
the package's public API:

* :mod:`vmp.pipeline.shared` — run ids, dirs, errors, progress
* :mod:`vmp.pipeline.scan` — scan/plan phase
* :mod:`vmp.pipeline.apply` — apply/maintenance phase
* :mod:`vmp.pipeline.gps_repair` — standalone GPS metadata repair
"""

from __future__ import annotations

# Re-exported for tests exercising single-plan behaviour.
from .apply import (  # noqa: F401  # noqa: F401
    _apply_one_plan,
    _record_outcome,
    apply_plans,
    maintain_jpegs,
)
from .gps_repair import apply_gps_assignments, gps_write_arguments  # noqa: F401
from .scan import scan_and_plan, scan_items_and_plan  # noqa: F401
from .shared import (  # noqa: F401
    ApplyItemCallback,
    CancelCallback,
    PipelineCancelled,
    PipelineError,
    ProgressCallback,
    ResultsCallback,
    VideoNotSmallerError,
    _cleanup_empty_generated_dirs,
    _resolve_required_tool,
    backup_dir,
    emit,
    make_run_id,
    raise_if_cancelled,
    work_dir,
)
