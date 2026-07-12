"""Application entry point."""

from __future__ import annotations

from .core.logging_config import configure_logging


def main() -> None:
    """Start the Vacation Media Processor GUI."""
    configure_logging()
    # Import after logging is configured so GUI import-time records reach the file.
    from .gui.main.window import run_app

    raise SystemExit(run_app())


if __name__ == "__main__":
    main()
