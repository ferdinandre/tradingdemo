import os, pandas as pd
from datetime import datetime, timedelta, timezone
import tomllib
from src.adaptors.market_data import MarketData

SYMBOL = os.getenv("TB_SYMBOL", "SPY")
DAYS = int(os.getenv("TB_DAYS", "365"))
TIMEFRAME = os.getenv("TB_TIMEFRAME", "1Min")

def main():
    with open("config/creds.toml", "rb") as f:
        config = tomllib.load(f)
    api_key = config["alpaca"]["key_id"]
    api_secret = config["alpaca"]["secret_key"]
    md = MarketData(api_key, api_secret)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS)
    page = None; chunks = []
    while True:
        df, page = md.get_bars(SYMBOL, timeframe=TIMEFRAME, feed="iex", start=start, end=end, page_token=page)
        if not df.empty:
            chunks.append(df[["open","high","low","close","volume"]])
        if not page: break
    bars = pd.concat(chunks).sort_index().drop_duplicates()
    bars.to_parquet(f"data/cache/{SYMBOL}_{TIMEFRAME}_{DAYS}d.parquet")
    print("Saved:", bars.shape, f"→ data/cache/{SYMBOL}_{TIMEFRAME}_{DAYS}d.parquet")

if __name__ == "__main__":
    main()
