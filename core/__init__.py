"""Plain-Python core. Never imports a workflow engine.

Every function takes plain inputs, returns plain dataclasses, and is safe to run twice.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RESULTS_DIR = REPO_ROOT / "results"
