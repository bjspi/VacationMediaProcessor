"""Direct launcher — run the app from a clone without installing anything.

Usage:  python main.py        (with console)
        pythonw main.py       (no console window; ideal for a desktop shortcut)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vmp.main import main

if __name__ == "__main__":
    main()
