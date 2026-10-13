import numpy as np
import penaltyblog as pb
import pytest

from fbot.markets import ah_win_lose, kelly_fraction, profit_for, settle, shin_probs


@pytest.mark.parametrize("k", [2, 3])
def test_shin_matches_penaltyblog(k):
    rng = np.random.default_rng(k)
    odds = 1 / (rng.dirichlet([3] * k, size=50) * rng.uniform(1.02, 1.12, size=(50, 1)))
    mine = shin_probs(odds)
    ref = np.array([pb.implied.calculate_implied(list(r), method="shin").probabilities for r in odds])
    assert np.abs(mine - ref).max() < 1e-9


def test_shin_handles_missing_prices():
    p = shin_probs(np.array([[2.0, 3.5, np.nan], [1.9, 3.6, 4.2]]))
    assert np.isnan(p[0]).all()
    assert p[1].sum() == pytest.approx(1)


@pytest.mark.parametrize("line", [-1.75, -1.25, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 1.5])
def test_asian_handicap_matches_penaltyblog(line):
    model = pb.models.DixonColesGoalModel(
        [1, 2, 0, 3, 1, 0, 2, 1], [0, 1, 0, 1, 2, 1, 2, 3],
        ["A", "B", "C", "A", "B", "C", "A", "B"], ["B", "C", "A", "C", "A", "B", "C", "A"],
    )
    model.fit()
    g = model.predict("A", "B", max_goals=10)
    for side in ("home", "away"):
        ref = g.asian_handicap_probs(side, line)
        win, lose = ah_win_lose(np.asarray(g.grid), side, line)
        assert win == pytest.approx(ref["win"])
        assert lose == pytest.approx(ref["lose"])


def test_settlement_and_profit():
    assert settle("1X2", "H", 2, 1) == "won"
    assert settle("1X2", "D", 2, 1) == "lost"
    assert settle("1X2", "D", 1, 1) == "won"
    assert settle("OU25", "O", 2, 1) == "won"
    assert settle("OU25", "U", 2, 1) == "lost"
    assert settle("OU25", "U", 1, 1) == "won"
    assert profit_for("won", 2.5, 100) == 150
    assert profit_for("lost", 2.5, 100) == -100
    assert profit_for("void", 2.5, 100) == 0


def test_kelly():
    assert kelly_fraction(0.5, 2.2) == pytest.approx((0.5 * 2.2 - 1) / 1.2)
    assert kelly_fraction(0.4, 2.0) == 0.0
