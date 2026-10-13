"""Paths, leagues and default settings."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FBOT_DATA_DIR", BASE_DIR / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "bot.db"

# Optional local folder with CSV files named like on football-data.co.uk
# (e.g. "2526/E0.csv" or "2526_E0.csv", and "fixtures.csv"). When set, nothing
# is downloaded — handy for offline use and for tests.
SOURCE_DIR = os.environ.get("FBOT_SOURCE_DIR")

BASE_URL = "https://www.football-data.co.uk"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) football-value-bot/1.0"

LEAGUES: dict[str, str] = {
    "E0": "Англия · Премьер-лига",
    "E1": "Англия · Чемпионшип",
    "E2": "Англия · Лига 1",
    "E3": "Англия · Лига 2",
    "SC0": "Шотландия · Премьершип",
    "D1": "Германия · Бундеслига",
    "D2": "Германия · 2. Бундеслига",
    "I1": "Италия · Серия A",
    "I2": "Италия · Серия B",
    "SP1": "Испания · Ла Лига",
    "SP2": "Испания · Сегунда",
    "F1": "Франция · Лига 1",
    "F2": "Франция · Лига 2",
    "N1": "Нидерланды · Эредивизи",
    "B1": "Бельгия · Про-лига",
    "P1": "Португалия · Примейра",
    "T1": "Турция · Суперлига",
    "G1": "Греция · Суперлига",
}
DEFAULT_LEAGUES = ["E0", "E1", "D1", "I1", "SP1", "F1", "N1", "P1"]

# Current season + this many previous ones are downloaded. The oldest season
# only warms the models up; the rest are used for out-of-sample evaluation.
HISTORY_SEASONS = 5

PRICE_SOURCES = {
    "avg": "Средний по рынку",
    "max": "Лучший по рынку",
    "pinnacle": "Pinnacle",
    "b365": "Bet365",
}

DEFAULT_SETTINGS: dict = {
    "bankroll": 10000.0,        # starting bankroll
    "currency": "₽",
    "kelly_fraction": 0.25,     # fraction of full Kelly
    "max_stake_pct": 0.03,      # hard cap per bet, share of current bankroll
    "min_ev": 0.05,             # minimum expected value to signal a bet
    "min_odds": 1.40,
    "max_odds": 5.00,
    # Which odds the EV is computed at. On real data (2024-2026, 18 leagues) only the best
    # market price had a positive out-of-sample backtest; average prices lost ~5% CLV.
    "price_source": "max",
    "markets": ["1X2", "OU25"],
    "leagues": DEFAULT_LEAGUES,
    "auto_update_hours": 6,
}
