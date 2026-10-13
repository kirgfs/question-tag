"""Downloading and parsing football-data.co.uk CSV files."""
from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import config
from .markets import all_odds_columns

Log = Callable[[str], None]


def season_code(d: date) -> str:
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def season_codes(today: date, n: int) -> list[str]:
    """``n`` season codes ending with the current one, oldest first."""
    start = today.year if today.month >= 7 else today.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(start - n + 1, start + 1)]


def _local_source(rel: str) -> Path | None:
    root = Path(config.SOURCE_DIR)
    short = rel.removeprefix("mmz4281/")
    for name in (rel, short, short.replace("/", "_")):
        if (root / name).exists():
            return root / name
    return None


def fetch(rel: str, max_age_hours: float, log: Log = print) -> Path | None:
    """Return a local copy of ``BASE_URL/rel``, downloading it when stale.

    Returns ``None`` when the file does not exist on the server (for example
    a season that has not started yet). Falls back to a stale cached copy if
    the network is down.
    """
    dest = config.RAW_DIR / rel.replace("/", "_")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if config.SOURCE_DIR:
        src = _local_source(rel)
        if src is None:
            return None
        shutil.copyfile(src, dest)
        return dest

    if dest.exists() and time.time() - dest.stat().st_mtime < max_age_hours * 3600:
        return dest

    url = f"{config.BASE_URL}/{rel}"
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return dest if dest.exists() else None
        if dest.exists():
            log(f"⚠ {url}: HTTP {e.code}, беру сохранённую копию")
            return dest
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if dest.exists():
            log(f"⚠ {url}: {e}, беру сохранённую копию")
            return dest
        raise RuntimeError(f"Не удалось скачать {url}: {e}") from e

    if not body.strip() or body.lstrip()[:1] == b"<":
        # football-data returns an HTML page instead of a CSV for unknown files
        return dest if dest.exists() else None
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return dest


def read_csv(path: Path) -> pd.DataFrame:
    df = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc, on_bad_lines="skip", low_memory=False)
            break
        except UnicodeDecodeError:
            continue
    if df is None:
        return pd.DataFrame()
    df.columns = [str(c).strip().lstrip("﻿") for c in df.columns]
    return df


def normalize(raw: pd.DataFrame, played: bool) -> pd.DataFrame:
    """Turn a football-data table into the bot's tidy format."""
    need = {"Div", "Date", "HomeTeam", "AwayTeam"} | ({"FTHG", "FTAG"} if played else set())
    if raw.empty or not need.issubset(raw.columns):
        return pd.DataFrame()
    raw = raw.dropna(subset=["Div", "Date", "HomeTeam", "AwayTeam"])

    out = pd.DataFrame(index=raw.index)
    out["div"] = raw["Div"].astype(str).str.strip()
    out["home"] = raw["HomeTeam"].astype(str).str.strip()
    out["away"] = raw["AwayTeam"].astype(str).str.strip()
    dates = pd.to_datetime(
        raw["Date"].astype(str).str.strip(), dayfirst=True, format="mixed", errors="coerce"
    )
    if "Time" in raw.columns:
        times = raw["Time"].astype(str).str.strip().where(raw["Time"].notna(), "15:00")
    else:
        times = pd.Series("15:00", index=raw.index)
    hhmm = times.str.extract(r"(\d{1,2}):(\d{2})").astype(float).fillna({0: 15.0, 1: 0.0})
    local = dates + pd.to_timedelta(hhmm[0], unit="h") + pd.to_timedelta(hhmm[1], unit="m")
    # football-data publishes UK kick-off times
    kickoff = local.dt.tz_localize("Europe/London", ambiguous="NaT", nonexistent="shift_forward")
    kickoff = kickoff.fillna(local.dt.tz_localize("UTC")).dt.tz_convert("UTC")
    out["date"] = dates.dt.normalize()
    out["kickoff"] = kickoff
    if played:
        out["fthg"] = pd.to_numeric(raw["FTHG"], errors="coerce")
        out["ftag"] = pd.to_numeric(raw["FTAG"], errors="coerce")
    for col in all_odds_columns():
        out[col] = pd.to_numeric(raw[col], errors="coerce") if col in raw.columns else np.nan

    out = out[out["date"].notna()]
    if played:
        out = out[out["fthg"].notna() & out["ftag"].notna()].copy()
        out["fthg"] = out["fthg"].astype(int)
        out["ftag"] = out["ftag"].astype(int)
    out["season"] = [season_code(d) for d in out["date"].dt.date]
    out["key"] = out["div"] + "|" + out["date"].dt.strftime("%Y-%m-%d") + "|" + out["home"] + "|" + out["away"]
    return out.reset_index(drop=True)


def load_history(leagues: list[str], today: date, log: Log = print) -> pd.DataFrame:
    seasons = season_codes(today, config.HISTORY_SEASONS)
    frames = []
    for div in leagues:
        for i, s in enumerate(seasons):
            current = i == len(seasons) - 1
            path = fetch(f"mmz4281/{s}/{div}.csv", 2 if current else 24 * 30, log)
            if path is None:
                continue
            df = normalize(read_csv(path), played=True)
            df = df[df["div"] == div]
            if not df.empty:
                frames.append(df)
        log(f"История {div}: загружено")
    if not frames:
        return pd.DataFrame()
    hist = pd.concat(frames, ignore_index=True)
    hist = hist.drop_duplicates("key", keep="last")
    return hist.sort_values(["date", "div", "home"]).reset_index(drop=True)


def load_fixtures(leagues: list[str], log: Log = print) -> pd.DataFrame:
    path = fetch("fixtures.csv", 1, log)
    if path is None:
        log("⚠ fixtures.csv не найден")
        return pd.DataFrame()
    fx = normalize(read_csv(path), played=False)
    if fx.empty:
        return fx
    fx = fx[fx["div"].isin(leagues)]
    now = pd.Timestamp(datetime.now(timezone.utc))
    fx = fx[fx["kickoff"] > now - pd.Timedelta(hours=2)]
    return fx.drop_duplicates("key").sort_values("kickoff").reset_index(drop=True)
