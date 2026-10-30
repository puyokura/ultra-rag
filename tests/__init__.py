import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (
    ROOT,
    ROOT / "secret-core",
    ROOT / "key-use",
    ROOT / "password-input",
    ROOT / "cli",
):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)
