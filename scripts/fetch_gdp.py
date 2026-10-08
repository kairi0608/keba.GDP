"""名目GDP（current US$）を取得して data/ に保存する。

優先順:
  1. World Bank API (NY.GDP.MKTP.CD) を直接取得
  2. datasets/gdp（World Bank WDI を正規化した CC-BY-4.0 ミラー）
  3. どちらも取れなければ status=DATA_PENDING を書き出す（順位は捏造しない）

使い方: python3 scripts/fetch_gdp.py
"""
import csv
import io
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = ROOT / "data" / "gdp_nominal_usd_5countries.csv"
OUT_META = ROOT / "data" / "gdp_source.json"

COUNTRIES = ["USA", "JPN", "DEU", "CHN", "FRA"]
WB_URL = ("https://api.worldbank.org/v2/country/" + ";".join(COUNTRIES)
          + "/indicator/NY.GDP.MKTP.CD?format=json&per_page=2000&date=1960:2030")
MIRROR_URL = "https://raw.githubusercontent.com/datasets/gdp/main/data/gdp.csv"


def _get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")


def from_worldbank():
    payload = json.loads(_get(WB_URL))
    rows = {}
    for rec in payload[1]:
        if rec["value"] is None:
            continue
        rows.setdefault(int(rec["date"]), {})[rec["countryiso3code"]] = float(rec["value"])
    return rows


def from_mirror():
    rows = {}
    for rec in csv.DictReader(io.StringIO(_get(MIRROR_URL))):
        if rec["Country Code"] in COUNTRIES and rec["Value"]:
            rows.setdefault(int(rec["Year"]), {})[rec["Country Code"]] = float(rec["Value"])
    return rows


def main():
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    attempts = []
    rows, source, url = None, None, None
    for name, fn, u in [("World Bank API", from_worldbank, WB_URL),
                        ("datasets/gdp mirror of World Bank WDI", from_mirror, MIRROR_URL)]:
        try:
            rows = fn()
            source, url = name, u
            attempts.append({"source": name, "ok": True})
            break
        except Exception as e:  # ネットワーク遮断などは次の手段へ
            attempts.append({"source": name, "ok": False, "error": repr(e)[:200]})

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not rows and OUT_META.exists() and json.loads(OUT_META.read_text()).get("status") == "OK":
        print("取得失敗。既存の data/ を保持します（上書きしない）", file=sys.stderr)
        return 0
    if not rows:
        OUT_META.write_text(json.dumps({"status": "DATA_PENDING", "fetched_at": fetched_at,
                                        "attempts": attempts}, ensure_ascii=False, indent=2))
        print("DATA_PENDING: GDPデータを取得できませんでした", file=sys.stderr)
        return 1

    # 5か国すべてが揃っている年だけを使う（欠けた年の順位は作らない）
    years = sorted(y for y, v in rows.items() if all(c in v for c in COUNTRIES))
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["year"] + COUNTRIES)
        for y in years:
            w.writerow([y] + [f"{rows[y][c]:.0f}" for c in COUNTRIES])
    OUT_META.write_text(json.dumps({
        "status": "OK",
        "indicator": "NY.GDP.MKTP.CD (GDP, current US$)",
        "origin": "World Bank, World Development Indicators",
        "retrieved_via": source,
        "url": url,
        "license": "CC-BY-4.0",
        "fetched_at": fetched_at,
        "years": [years[0], years[-1]],
        "attempts": attempts,
        "note": "順位はこの5か国の中での順位。World Bank の改定で値が変わることがあるため、最終版の前に再取得して確認する。",
    }, ensure_ascii=False, indent=2))
    print(f"OK via {source}: {years[0]}-{years[-1]} ({len(years)} years)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
