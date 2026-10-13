"""End-to-end tests on synthetic leagues: update pipeline, API, settlement, statistics."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fbot import data, db, engine, model, server
from fbot.markets import settle

quiet = lambda _msg: None  # noqa: E731


@pytest.fixture
def client(env):
    return TestClient(server.app)


def test_update_builds_report_and_fixtures(env):
    with db.session() as conn:
        report = db.get_meta(conn, "report")
        fixtures = [r["data"] for r in conn.execute("SELECT data FROM fixtures")]
    assert set(report["markets"]) == {"1X2", "OU25"}
    names = {m["model"] for m in report["markets"]["1X2"]["metrics"]}
    assert names == {"market", "dc", "pi", "pool", "lr", "gbm"}
    for m in report["markets"].values():
        assert m["champion"] in model.CANDIDATES
        assert all(0.3 < x["logloss"] < 1.3 for x in m["metrics"])
    assert fixtures, "upcoming fixtures were not stored"


def test_second_update_reuses_cache(env):
    with db.session() as conn:
        before = conn.execute("SELECT COUNT(*) FROM dc_cache").fetchone()[0]
    assert engine.run_update()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dc_cache").fetchone()[0] == before


def test_walk_forward_has_no_lookahead(env):
    hist = data.load_history(["E0"], date.today(), quiet)
    split = hist["date"].max() - pd.Timedelta(days=90)
    changed = hist.copy()
    changed.loc[changed["date"] >= split, "fthg"] += 3
    a = model.walk_forward(hist, set(), quiet).set_index("key")
    b = model.walk_forward(changed, set(), quiet).set_index("key")
    early = hist.loc[hist["date"] < split, "key"]
    early = [k for k in early if k in a.index and pd.notna(a.loc[k, "dc_h"])]
    assert early
    np.testing.assert_allclose(a.loc[early, model.DC_COLS], b.loc[early, model.DC_COLS])
    late = [k for k in hist.loc[hist["date"] >= split + pd.Timedelta(days=14), "key"] if pd.notna(a.loc[k, "dc_h"])]
    assert not np.allclose(a.loc[late, "dc_lh"], b.loc[late, "dc_lh"])


def test_pi_ratings_have_no_lookahead(env):
    hist = data.load_history(["E0"], date.today(), quiet)
    split = hist["date"].max() - pd.Timedelta(days=60)
    changed = hist.copy()
    changed.loc[changed["date"] >= split, "fthg"] += 3
    a = model.pi_ratings(hist, pd.DataFrame()).set_index("key")
    b = model.pi_ratings(changed, pd.DataFrame()).set_index("key")
    early = hist.loc[hist["date"] < split, "key"]
    np.testing.assert_allclose(a.loc[early, "pi_gd"], b.loc[early, "pi_gd"])


def test_bet_flow_and_stats(client):
    assert client.get("/api/status").json()["last_update"]
    signals = client.get("/api/signals").json()["signals"]
    assert signals, "expected value signals with min_ev=0 and best prices"
    tip = signals[0]
    r = client.post("/api/bets", json={
        "key": tip["key"], "market": tip["market"], "selection": tip["selection"],
        "odds": 2.5, "stake": 100, "bookmaker": "Test", "tip_id": tip["id"],
    })
    assert r.status_code == 200
    bet_id = r.json()["id"]
    fx = client.get("/api/fixtures").json()["fixtures"][0]
    own = client.post("/api/bets", json={"key": fx["key"], "market": "OU25", "selection": "O", "odds": 1.9, "stake": 50})
    assert own.status_code == 200

    bets = {b["id"]: b for b in client.get("/api/bets?status=open").json()["bets"]}
    assert bets[bet_id]["by_tip"] == 1 and bets[own.json()["id"]]["by_tip"] == 0
    assert bets[bet_id]["ev"] == pytest.approx(tip["model_prob"] * 2.5 - 1)

    assert client.post("/api/bets", json={**{"key": fx["key"], "market": "1X2", "odds": 2, "stake": 1}, "selection": "O"}).status_code == 400
    assert client.post("/api/bets", json={"key": fx["key"], "market": "1X2", "selection": "H", "odds": 1.0, "stake": 1}).status_code == 422
    assert client.post("/api/bets", json={"key": "nope", "market": "1X2", "selection": "H", "odds": 2, "stake": 1}).status_code == 404

    assert client.patch(f"/api/bets/{bet_id}", json={"status": "won"}).status_code == 200
    assert client.patch(f"/api/bets/{own.json()['id']}", json={"status": "lost"}).status_code == 200
    st = client.get("/api/stats").json()
    assert st["all"]["profit"] == pytest.approx(150 - 50)
    assert st["by_tip"]["profit"] == pytest.approx(150)
    assert st["own"]["profit"] == pytest.approx(-50)
    assert st["all"]["roi"] == pytest.approx(100 / 150)
    status = client.get("/api/status").json()
    assert status["bankroll"] == pytest.approx(10000 + 100)

    assert client.patch(f"/api/bets/{bet_id}", json={"status": "open"}).status_code == 200
    reopened = [b for b in client.get("/api/bets?status=open").json()["bets"] if b["id"] == bet_id][0]
    assert reopened["profit"] is None
    for b in client.get("/api/bets").json()["bets"]:
        assert client.delete(f"/api/bets/{b['id']}").status_code == 200
    assert client.get("/api/bets").json()["bets"] == []


def test_auto_settlement_with_clv(env):
    hist = data.load_history(["E0", "D1"], date.today(), quiet)
    row = hist.iloc[-1]
    with db.session() as conn:
        conn.execute(
            "INSERT INTO bets(created_at, key, div, kickoff, home, away, market, selection, odds, stake) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("x", row["key"], row["div"], row["kickoff"].isoformat(), row["home"], row["away"], "1X2", "H", 2.0, 10.0),
        )
        engine.settle_open(conn, hist, engine.utcnow())
        bet = db.rows(conn, "SELECT * FROM bets")[-1]
        conn.execute("DELETE FROM bets")
    assert bet["status"] == settle("1X2", "H", row["fthg"], row["ftag"])
    assert (bet["fthg"], bet["ftag"]) == (row["fthg"], row["ftag"])
    assert bet["close_prob"] is not None and bet["clv"] == pytest.approx(2.0 * bet["close_prob"] - 1)


def test_settings_validation_and_tip_refresh(client):
    base = client.get("/api/settings").json()["settings"]
    assert client.put("/api/settings", json={**base, "min_odds": 3, "max_odds": 2}).status_code == 400
    assert client.put("/api/settings", json={**base, "leagues": []}).status_code == 400
    assert client.put("/api/settings", json={**base, "price_source": "nope"}).status_code == 422
    assert client.put("/api/settings", json={**base, "min_ev": 0.9}).status_code == 200
    assert client.get("/api/signals").json()["signals"] == []
    assert client.put("/api/settings", json=base).status_code == 200
    assert client.get("/api/signals").json()["signals"]


def test_backtest(client):
    d = client.get("/api/model").json()
    bt = d["backtest"]
    assert bt["available"]
    counts = [t["n"] for t in bt["thresholds"]]
    assert counts == sorted(counts, reverse=True)
    assert {m["model"] for m in bt["by_model"]} == {"market", "dc", "pool", "lr", "gbm"}
    assert bt["summary"]["n"] > 0
    assert client.get("/").status_code == 200


def test_penaltyblog_scrapers_stay_unused(env):
    """penaltyblog's scrapers depend on wrapper-tls-requests, which downloads a native
    library from GitHub at runtime. The bot downloads CSVs itself and must never load it."""
    import sys

    assert "tls_requests" not in sys.modules


def test_champion_needs_significant_edge():
    base = {"market": "1X2", "title": "", "n": 1000, "brier": 0, "rps": 0, "accuracy": 0}
    mets = [
        {**base, "model": "market", "logloss": 1.000, "delta": 0.0, "delta_se": 0.0},
        {**base, "model": "lr", "logloss": 0.999, "delta": -0.001, "delta_se": 0.002},  # noise
    ]
    assert model.choose_champion(mets) == "market"
    mets[1].update(logloss=0.990, delta=-0.010, delta_se=0.003)
    assert model.choose_champion(mets) == "lr"
