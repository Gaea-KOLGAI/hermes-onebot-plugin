import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERMES_SRC = Path.home() / ".hermes" / "hermes-agent"

for path in (ROOT, HERMES_SRC):
    if path.exists():
        sys.path.insert(0, str(path))

os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))

collect_ignore = ["test_full_chain.py"]
