from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "Tridock"
API_PORT = 10900
DATA_DIR = Path(os.environ.get("TRIDOCK_DATA") or (Path(__file__).resolve().parents[2] / "data"))
DB_PATH = DATA_DIR / "tridock.db"
