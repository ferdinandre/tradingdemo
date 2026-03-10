import pandas as pd
import matplotlib.pyplot as plt

# load csv
df = pd.read_csv("trades_stack.csv")

equity = df["equity_after"][100:200].tolist()

plt.figure(figsize=(4, 2), dpi=200)  # big + high resolution
plt.plot(equity, linewidth=1)

plt.title("Equity Curve")
plt.xlabel("Trade / Row")
plt.ylabel("Equity")
plt.grid(True, linestyle="--", alpha=0.5)

plt.tight_layout()
plt.show()