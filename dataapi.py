from __future__ import annotations

import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, time as dtime
from models import Candle
import requests


Json = t.Dict[str, t.Any]


def _iso(dt: datetime) -> str:
    # Alpaca accepts RFC3339; ISO with timezone is fine.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaMarketData:
    """
    Market Data via Alpaca Stocks Data API (v2) using requests.
    Base: https://data.alpaca.markets
    """
    def __init__(self, api_key: str, api_secret: str, feed: str = "iex"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://data.alpaca.markets"
        self.feed = feed  # "iex" or "sip" (depending on your subscription)

        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        })

    def _get_latest_quote(self, symbol: str):
        url = f"{self.base}/v2/stocks/quotes/latest"
        params = {
            "symbols": symbol,
            "feed": self.feed
        }
        r = self._session.get(url, params=params)
        return r.json()

    def _get_latest_bar(self, symbol: str, timeframe: str) -> Candle:
        # /v2/stocks/bars?symbols=...&timeframe=...&limit=1&feed=...
        url = f"{self.base_url}/v2/stocks/bars"
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "limit": 1,
            "feed": self.feed,
        }
        r = self._session.get(url, params=params)
        data = r.json()

        # Shape: { "bars": { "TSLA": [ {t,o,h,l,c,v,vw,n} ] }, "next_page_token": ... }
        bar = data["bars"][symbol][0]
        ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))

        return Candle(
            symbol=symbol,
            ts=ts,
            open=float(bar["o"]),
            high=float(bar["h"]),
            low=float(bar["l"]),
            close=float(bar["c"]),
            volume=int(bar["v"]),
            vwap=float(bar["vw"]) if "vw" in bar and bar["vw"] is not None else None,
            trade_count=int(bar["n"]) if "n" in bar and bar["n"] is not None else None,
        )
    
    def get_today_open_5min_candle(self, symbol: str) -> Candle:
        """
        Returns today's FIRST 5-minute candle (09:30–09:35 ET).
        """
        today = datetime.now(timezone.utc).date()

        # 09:30 ET = 13:30 UTC (ignoring DST handling by design)
        start_utc = datetime.combine(
            today,
            dtime(hour=13, minute=30),
            tzinfo=timezone.utc,
        )
        end_utc = datetime.combine(
            today,
            dtime(hour=13, minute=35),
            tzinfo=timezone.utc,
        )

        url = f"{self.base_url}/v2/stocks/bars"
        params = {
            "symbols": symbol,
            "timeframe": "5Min",
            "start": start_utc.isoformat().replace("+00:00", "Z"),
            "end": end_utc.isoformat().replace("+00:00", "Z"),
            "limit": 1,
            "feed": self.feed,
        }

        r = self._session.get(url, params=params)
        print(r)
        bar = r.json()["bars"][symbol][0]

        ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))

        return Candle(
            symbol=symbol,
            ts=ts,
            open=float(bar["o"]),
            high=float(bar["h"]),
            low=float(bar["l"]),
            close=float(bar["c"]),
            volume=int(bar["v"]),
            vwap=float(bar["vw"]) if bar.get("vw") is not None else None,
            trade_count=int(bar["n"]) if bar.get("n") is not None else None,
        )

    def get_latest_1min_candle(self, symbol: str) -> Candle:
        return self._get_latest_bar(symbol, timeframe="1Min")

    def get_latest_5min_candle(self, symbol: str) -> Candle:
        return self._get_latest_bar(symbol, timeframe="5Min")

class AlpacaPaperTrading:
    """
    Paper Trading via Alpaca Trading API (v2) using requests.
    Base: https://paper-api.alpaca.markets
    """
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://paper-api.alpaca.markets"

        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        })

    def _post_order(self, payload: Json) -> Json:
        url = f"{self.base_url}/v2/orders"
        r = self._session.post(url, json=payload)
        return r.json()

    def _get_account(self) -> Json:
        url = f"{self.base_url}/v2/account"
        r = self._session.get(url)
        return r.json()
    
    def get_account(self):
        return self._get_account()

    def place_market_order(
        self,
        symbol: str,
        qty: float,
        long: bool = True,
        *,
        time_in_force: str = "day",
        extended_hours: bool = False,
    ) -> Json:
        """
        long=True  -> buy (open/increase long)
        long=False -> sell (open/increase short if you have margin/shorting enabled)
        """
        payload: Json = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy" if long else "sell",
            "type": "market",
            "time_in_force": time_in_force,
            "extended_hours": extended_hours,
        }
        return self._post_order(payload)

    def place_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        long: bool = True,
        *,
        time_in_force: str = "day",
        extended_hours: bool = False,
    ) -> Json:
        payload: Json = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy" if long else "sell",
            "type": "limit",
            "limit_price": str(limit_price),
            "time_in_force": time_in_force,
            "extended_hours": extended_hours,
        }
        return self._post_order(payload)

    def place_stop_order(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        long: bool = True,
        *,
        time_in_force: str = "day",
        extended_hours: bool = False,
    ) -> Json:
        """
        This is a STOP (market) order at stop_price (not stop-limit).
        For stop-limit, you'd add: type="stop_limit", stop_price=..., limit_price=...
        """
        payload: Json = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy" if long else "sell",
            "type": "stop",
            "stop_price": str(stop_price),
            "time_in_force": time_in_force,
            "extended_hours": extended_hours,
        }
        return self._post_order(payload)