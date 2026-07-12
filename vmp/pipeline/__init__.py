"""High-level scan, plan, and apply pipeline (facade).

The implementation lives in three focused modules and is re-exported here as
the package's public API:

* :mod:`vmp.pipeline.shared` — run ids, dirs, errors, progress
* :mod:`vmp.pipeline.scan` — scan/plan phase
* :mod:`vmp.pipeline.apply` — apply/maintenance phase
"""

from __future__ import annotations

from .apply import (  # noqa: F401
    apply_plans,
    maintain_jpegs,
)
from .shared import (  # noqa: F401
    ApplyItemCallback,
    PipelineError,
    ProgressCallback,
    ResultsCallback,
    VideoNotSmallerError,
    _cleanup_empty_generated_dirs,
    _resolve_required_tool,
    backup_dir,
    emit,
    make_run_id,
    work_dir,
)
from .scan import scan_and_plan  # noqa: F401

# Re-exported for tests exercising single-plan behaviour.
from .apply import _apply_one_plan, _record_outcome  # noqa: F401
