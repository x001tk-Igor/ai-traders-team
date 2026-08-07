import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.size_position import compute_lots  # noqa: E402


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk_usd", type=float, required=True)
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True)
    ap.add_argument("--symbol", required=True)
    a = ap.parse_args()
    from trader_lib.mt5_client import live_market
    si = live_market().symbol_info(a.symbol)
    lots = compute_lots(risk_usd=a.risk_usd, entry=a.entry, sl=a.sl, symbol_info=si)
    print(json.dumps({"lots": lots, "symbol": a.symbol}, ensure_ascii=False))
