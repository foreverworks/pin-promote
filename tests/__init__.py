"""The tests run from a checkout, against src/, with the standard library's unittest."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
