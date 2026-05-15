import json
from silver_strategy.backtester import run_backtest
from data.angel_api import angel_api

# Connect to Angel One using env vars or config
angel_api.connect()

results = {}
for inst in ["SILVER", "SILVERM", "SILVERMIC"]:
    print(f"Running simulation for {inst} on 2026-05-13...")
    results[inst] = run_backtest(inst, "2026-05-13")

with open("scratch/silver_13th.json", "w") as f:
    json.dump(results, f, indent=4)
print("Saved results to scratch/silver_13th.json")
