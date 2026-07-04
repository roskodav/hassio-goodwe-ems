"""Czech day-ahead spot electricity price (the signal Delta Green optimises on).

Fetches hourly spot prices from spotovaelektrina.cz and exposes them via
snapshot(). Fully fault tolerant: no internet -> empty snapshot, retries later.
Prices from the API are CZK/MWh; we expose CZK/kWh.
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime

URL_NOW = "https://spotovaelektrina.cz/api/v1/price/get-actual-price-json"
URL_TODAY = "https://spotovaelektrina.cz/api/v1/price/get-prices-json"
FETCH_INTERVAL = float(os.environ.get("PRICE_INTERVAL", "900"))  # 15 min
# Optional tariff adders so "buy" vs "sell" can be shown realistically (CZK/kWh).
BUY_ADDER = float(os.environ.get("PRICE_BUY_ADDER", "0") or 0)     # distribution + fees
SELL_FACTOR = float(os.environ.get("PRICE_SELL_FACTOR", "1") or 1)  # aggregator share

_lock = threading.Lock()
_PRICES = {
    "updated": None, "unit": "CZK/kWh", "now": None, "buy": None, "sell": None,
    "level": None, "today": [], "min": None, "max": None, "hour": None,
    "source": "spotovaelektrina.cz",
}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "goodwe-ems/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot():
    with _lock:
        return dict(_PRICES)


def _fetch():
    now = _get(URL_NOW)
    today = _get(URL_TODAY)
    hours = today.get("hoursToday", []) or []
    arr = []
    for h in hours:
        czk = h.get("priceCZK")
        arr.append({
            "hour": h.get("hour"),
            "czk": round(czk / 1000.0, 2) if czk is not None else None,
            "eur": h.get("priceEur"),
            "level": h.get("level"),
        })
    vals = [x["czk"] for x in arr if x["czk"] is not None]
    now_czk = now.get("priceCZK")
    spot = round(now_czk / 1000.0, 2) if now_czk is not None else None
    with _lock:
        _PRICES["now"] = spot
        _PRICES["buy"] = round(spot + BUY_ADDER, 2) if spot is not None else None
        _PRICES["sell"] = round(spot * SELL_FACTOR, 2) if spot is not None else None
        _PRICES["level"] = now.get("level")
        _PRICES["today"] = arr
        _PRICES["min"] = min(vals) if vals else None
        _PRICES["max"] = max(vals) if vals else None
        _PRICES["hour"] = datetime.now().hour
        _PRICES["updated"] = datetime.now().isoformat(timespec="seconds")


def start():
    def loop():
        while True:
            try:
                _fetch()
            except Exception:
                pass
            time.sleep(FETCH_INTERVAL)
    threading.Thread(target=loop, daemon=True, name="price").start()
