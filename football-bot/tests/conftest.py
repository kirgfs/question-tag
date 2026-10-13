from datetime import date

import pytest

from fbot import config, db, engine
from tests.synth import generate


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Two synthetic leagues, fully updated, isolated data directory (one per test module)."""
    root = tmp_path_factory.mktemp("bot")
    generate(root / "src", date.today(), leagues=("E0", "D1"), n_teams=14, seed=1)
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "SOURCE_DIR", str(root / "src"))
    mp.setattr(config, "DATA_DIR", root / "data")
    mp.setattr(config, "RAW_DIR", root / "data" / "raw")
    mp.setattr(config, "DB_PATH", root / "data" / "bot.db")
    db.init()
    with db.session() as conn:
        db.save_settings(conn, {"leagues": ["E0", "D1"], "price_source": "max", "min_ev": 0.0})
    assert engine.run_update(), engine.STATUS["error"]
    yield root
    mp.undo()
