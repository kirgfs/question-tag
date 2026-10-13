from datetime import date

import pandas as pd

from fbot import data


def test_season_codes():
    assert data.season_code(date(2026, 10, 13)) == "2627"
    assert data.season_code(date(2027, 3, 1)) == "2627"
    assert data.season_codes(date(2026, 10, 13), 3) == ["2425", "2526", "2627"]


def test_read_and_normalize(tmp_path):
    csv = tmp_path / "fixtures.csv"
    csv.write_text(
        "Div,Date,Time,HomeTeam,AwayTeam,PSH,PSD,PSA,AvgH,AvgD,AvgA,P>2.5,P<2.5,AHh\n"
        "E0,15/08/2026,15:00,Arsenal,Chelsea,2.10,3.40,3.60,2.05,3.30,3.50,1.90,1.95,-0.25\n"
        "E0,15/01/2027,20:00, Man City ,Spurs,1.50,4.50,6.00,,,,,,\n"
        ",,,,,,,,,,,,,\n",
        encoding="utf-8-sig",
    )
    df = data.normalize(data.read_csv(csv), played=False)
    assert len(df) == 2
    first = df.iloc[0]
    # 15:00 UK summer time is 14:00 UTC
    assert first["kickoff"] == pd.Timestamp("2026-08-15 14:00", tz="UTC")
    assert first["key"] == "E0|2026-08-15|Arsenal|Chelsea"
    assert first["season"] == "2627"
    assert first["P>2.5"] == 1.90
    second = df.iloc[1]
    assert second["home"] == "Man City"
    # winter time: UK = UTC
    assert second["kickoff"] == pd.Timestamp("2027-01-15 20:00", tz="UTC")
    assert pd.isna(second["AvgH"])
    # columns absent from the file are still there
    assert "PSCH" in df.columns and df["PSCH"].isna().all()
