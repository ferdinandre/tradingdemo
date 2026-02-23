import pandas as pd

def summarize(trades_df: pd.DataFrame) -> dict:
    equity = trades_df["equity_after"]
    dd = (equity.cummax() - equity) / equity.cummax()
    return {
        "trades": len(trades_df),
        "win_rate": float((trades_df["pnl"] > 0).mean()),
        "total_pnl": float(trades_df["pnl"].sum()),
        "max_dd": float(dd.max()),
    }