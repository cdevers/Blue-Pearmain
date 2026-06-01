# tests/test_db_apply_legacy_metadata.py
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poller"))


def test_photos_table_has_proposed_title_column():
    from db.db import Database

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    db = Database(Path(f.name))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(photos)").fetchall()}
    assert "proposed_title" in cols
    assert "proposed_description" in cols  # sanity: existing column still there
