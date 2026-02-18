import pandas as pd

t = pd.read_csv("trades_stack.csv")
equity = t["equity_after"]
dd = (equity.cummax() - equity) / equity.cummax()
print("Max DD:", dd.max())