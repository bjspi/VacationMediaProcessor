"""Test package; puts the repo root on sys.path for pytest and unittest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
