# AI-Trader Skillset — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать набор из 4 скиллов + детерминированного ядра, дающий автономному
Claude-Code-трейдеру честную перцепцию, память, дисциплину решения и жёсткий риск-гейт
(стены 3%/6%), с целью +10% к балансу.

**Architecture:** Код воспринимает и считает правду (perceive/features, risk_gate, score) —
модель интерпретирует (recall/decide/reflect). Петля каждые N минут:
perceive→gate→recall→decide→score, рефлексия на границе сессии. Стор из 3 файлов —
единственный источник правды и накопительный актив.

**Tech Stack:** Python 3, пакет `MetaTrader5`, pandas, numpy, pytest. Скиллы — `SKILL.md`
(Claude Code). Ничего кроме stdlib+эти либы.

**Design spec:** `../docs/design.md` (полный дизайн, 12 секций).

**Проектная договорённость (CLAUDE.md):** можно кодом — делай кодом; фокус-скиллы, не
монолит; все 3 слоя скилла (description/instructions/tools).

---

## File Structure (карта декомпозиции)

```
ai-trader-skillset/
├── config/trader.config.json         # рамки/цель/окна/ритм (готов)
├── trader_lib/                       # детерминированное ядро (importable)
│   ├── config.py                     # загрузка+валидация конфига
│   ├── mt5_client.py                 # тонкая обёртка MT5 + инъектируемый интерфейс
│   ├── features.py                   # фичи из баров (ATR-норм, кросс-символьно)
│   ├── account.py                    # снимок счёта + топливные датчики
│   ├── risk_gate.py                  # вердикт+бюджет, fail-closed (safety-critical)
│   ├── size_position.py              # лот из risk_usd
│   ├── journal.py                    # append decision/outcome, чтение (atomic)
│   └── score.py                      # stats.json + калибровка + бейзлайн + scorecard.md
├── scripts/                          # entrypoints петли
│   ├── perceive.py                   # snapshot → stdout JSON
│   ├── close_watch.py                # сверка закрытых с историей MT5 → outcome
│   ├── run_score.py                  # пересчёт stats.json
│   └── recall.py                     # выборка stats под контекст → stdout
├── skills/                           # → <ПК>/.claude/skills/
│   ├── trader-perceive/SKILL.md
│   ├── trader-recall/SKILL.md
│   ├── trader-decide/SKILL.md
│   └── trader-reflect/SKILL.md
├── prompts/loop_prompt.md            # оркестрация цикла (loop-промт трейдера)
└── tests/
    ├── fixtures/                     # бары + состояния счёта
    ├── test_features.py
    ├── test_risk_gate.py
    ├── test_size_position.py
    ├── test_journal.py
    └── test_score.py
```

Каждый модуль — одна ответственность, тестируется офлайн без live MT5 (MT5 инъектируется).

---

## Phase 0 — Foundation

### Task 0.1: Config loader

**Files:**
- Create: `trader_lib/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Failing test**
```python
# tests/test_config.py
from trader_lib.config import load_config

def test_load_defaults(tmp_path):
    cfg = load_config("config/trader.config.json")
    assert cfg.risk.daily_loss_limit_pct == 3.0
    assert cfg.risk.total_loss_limit_pct == 6.0
    assert cfg.risk.risk_budget_divisor_K == 3
    assert cfg.learning.min_n_for_confirmed == 20

def test_missing_key_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"risk": {}}')
    try:
        load_config(str(bad)); assert False
    except (KeyError, ValueError):
        pass
```

- [ ] **Step 2: Run — expect FAIL** (`pytest tests/test_config.py -v`) → ModuleNotFound.

- [ ] **Step 3: Implement**
```python
# trader_lib/config.py
import json, os
from dataclasses import dataclass

@dataclass(frozen=True)
class Risk:
    daily_loss_limit_pct: float; total_loss_limit_pct: float
    flatten_buffer_pct: float; risk_budget_divisor_K: int
    per_trade_risk_cap_pct: float; daily_baseline: str
    total_baseline: str; server_day_reset_hour: int

@dataclass(frozen=True)
class Perception:
    atr_period: int; momentum_bars: list; range_bars: int
    ema_fast: int; ema_slow: int; atr_pctile_lookback: int; use_closed_bars_only: bool

@dataclass(frozen=True)
class Learning:
    min_n_for_confirmed: int; reflect_every_k_closed_trades: int; reflect_on_session_end: bool

@dataclass(frozen=True)
class Config:
    account: dict; goal: dict; risk: Risk
    perception: Perception; learning: Learning; loop: dict

def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return Config(
        account=d["account"], goal=d["goal"],
        risk=Risk(**d["risk"]), perception=Perception(**d["perception"]),
        learning=Learning(**d["learning"]), loop=d["loop"],
    )

def state_dir(cfg: Config) -> str:
    return os.path.expanduser(cfg.account["state_dir"])
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(trader): config loader with strict validation`.

---

### Task 0.2: MT5 client (инъектируемый интерфейс)

**Files:**
- Create: `trader_lib/mt5_client.py`
- Test: `tests/test_mt5_client.py`

Ключ: реальный MT5 недоступен на dev-ПК → определяем **протокол** `MarketData`, реальную
реализацию поверх пакета `MetaTrader5` и фейковую для тестов.

- [ ] **Step 1: Failing test**
```python
# tests/test_mt5_client.py
from trader_lib.mt5_client import FakeMarket

def test_fake_bars_and_symbol():
    m = FakeMarket(point=0.01, digits=2, spread_points=20)
    bars = m.copy_rates("XAUUSD", "M5", 100)
    assert len(bars) == 100 and {"time","open","high","low","close"} <= set(bars.columns)
    si = m.symbol_info("XAUUSD")
    assert si["point"] == 0.01 and si["digits"] == 2
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# trader_lib/mt5_client.py
from typing import Protocol
import numpy as np, pandas as pd

class MarketData(Protocol):
    def copy_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...
    def symbol_info(self, symbol: str) -> dict: ...
    def account_info(self) -> dict: ...
    def positions(self) -> list: ...
    def history_deals(self, since_ts) -> list: ...
    def order_send(self, req: dict) -> dict: ...

class FakeMarket:
    """Детерминированный рынок для тестов/E2E. Бары можно задать явно."""
    def __init__(self, point=0.01, digits=2, spread_points=20, bars=None,
                 account=None, positions=None, deals=None):
        self._point, self._digits, self._spread = point, digits, spread_points
        self._bars = bars; self._account = account or {"balance":10000,"equity":10000}
        self._positions = positions or []; self._deals = deals or []
    def copy_rates(self, symbol, timeframe, count):
        if self._bars is not None:
            return self._bars.tail(count).reset_index(drop=True)
        rng = np.random.default_rng(42)
        px = 2600 + np.cumsum(rng.normal(0, 0.5, count))
        return pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=count, freq="5min"),
            "open": px, "high": px+0.3, "low": px-0.3, "close": px,
            "tick_volume": rng.integers(50,500,count), "spread": self._spread})
    def symbol_info(self, symbol):
        return {"point":self._point,"digits":self._digits,"spread":self._spread,
                "trade_contract_size":100,"volume_min":0.01,"volume_max":100.0,"volume_step":0.01}
    def account_info(self): return self._account
    def positions(self): return self._positions
    def history_deals(self, since_ts): return self._deals
    def order_send(self, req): return {"retcode":10009,"order":123,"price":req.get("price")}

def live_market():
    """Реальная реализация поверх пакета MetaTrader5 (только на ПК трейдера)."""
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    _TF = {"M1":mt5.TIMEFRAME_M1,"M5":mt5.TIMEFRAME_M5,"M15":mt5.TIMEFRAME_M15,
           "M30":mt5.TIMEFRAME_M30,"H1":mt5.TIMEFRAME_H1,"H4":mt5.TIMEFRAME_H4,"D1":mt5.TIMEFRAME_D1}
    class Live:
        def copy_rates(self, symbol, timeframe, count):
            r = mt5.copy_rates_from_pos(symbol, _TF[timeframe], 0, count)
            if r is None or len(r)==0: raise RuntimeError(f"no rates {symbol} {timeframe}")
            df = pd.DataFrame(r); df["time"]=pd.to_datetime(df["time"],unit="s"); return df
        def symbol_info(self, symbol):
            s = mt5.symbol_info(symbol)
            if s is None: raise RuntimeError(f"no symbol_info {symbol}")
            return {"point":s.point,"digits":s.digits,"spread":s.spread,
                    "trade_contract_size":s.trade_contract_size,"volume_min":s.volume_min,
                    "volume_max":s.volume_max,"volume_step":s.volume_step}
        def account_info(self):
            a = mt5.account_info();  return {"balance":a.balance,"equity":a.equity}
        def positions(self):
            return [p._asdict() for p in (mt5.positions_get() or [])]
        def history_deals(self, since_ts):
            import datetime as dt
            d = mt5.history_deals_get(since_ts, dt.datetime.now())
            return [x._asdict() for x in (d or [])]
        def order_send(self, req): return mt5.order_send(req)._asdict()
    return Live()
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(trader): MT5 client protocol + Fake/Live impls`.

---

### Task 0.3: Fixtures

**Files:** Create `tests/fixtures/bars_uptrend_m5.parquet`, `tests/fixtures/bars_chop_m5.parquet`,
`tests/conftest.py`.

- [ ] **Step 1:** Generate two deterministic bar sets (uptrend, chop) via a small script; save parquet.
```python
# tests/conftest.py
import numpy as np, pandas as pd, pytest

@pytest.fixture
def uptrend_bars():
    n=600; t=pd.date_range("2026-01-01",periods=n,freq="5min")
    px=2600+np.linspace(0,30,n)+np.sin(np.arange(n)/8)*1.5
    return pd.DataFrame({"time":t,"open":px,"high":px+0.6,"low":px-0.6,
                         "close":px+0.1,"tick_volume":200,"spread":20})

@pytest.fixture
def chop_bars():
    n=600; t=pd.date_range("2026-01-01",periods=n,freq="5min")
    rng=np.random.default_rng(7); px=2600+np.cumsum(rng.normal(0,0.4,n))
    return pd.DataFrame({"time":t,"open":px,"high":px+0.5,"low":px-0.5,
                         "close":px,"tick_volume":150,"spread":20})
```
- [ ] **Step 2: Commit** — `test(trader): deterministic bar fixtures`.

---

## Phase 1 — Perception

### Task 1.1: features.py (ядро перцепции)

**Files:** Create `trader_lib/features.py`; Test `tests/test_features.py`.

Всё в **ATR-единицах** и raw price → кросс-символьная сопоставимость, грабли пипа золота
обойдены (пипсы не в машинном пути; спред — в пунктах брокера). Считаем на **закрытых барах**.

- [ ] **Step 1: Failing tests**
```python
# tests/test_features.py
from trader_lib.features import compute_tf_features

def test_uptrend_trend_up(uptrend_bars):
    f = compute_tf_features(uptrend_bars, point=0.01, atr_period=30,
                            momentum_bars=[5,15,30], range_bars=30,
                            ema_fast=12, ema_slow=26, atr_pctile_lookback=500)
    assert f["trend"] == "up"
    assert f["mom_mid_atr"] > 0
    assert 0.0 <= f["pos_in_range"] <= 1.0
    assert f["atr_price"] > 0 and f["atr_points"] > 0

def test_insufficient_bars_returns_null(uptrend_bars):
    f = compute_tf_features(uptrend_bars.head(5), point=0.01, atr_period=30,
                            momentum_bars=[5,15,30], range_bars=30,
                            ema_fast=12, ema_slow=26, atr_pctile_lookback=500)
    assert f["atr_price"] is None and f["reason"].startswith("insufficient")
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# trader_lib/features.py
import numpy as np, pandas as pd

def _atr(h, l, c, period):
    prev = c.shift(1)
    tr = pd.concat([(h-l), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_tf_features(bars, *, point, atr_period, momentum_bars, range_bars,
                        ema_fast, ema_slow, atr_pctile_lookback):
    """bars: DataFrame time,open,high,low,close,tick_volume,spread. Закрытые бары.
    Возвращает dict фич. Недостаток данных → значения None + reason (анти-галлюцинация)."""
    need = max(atr_period, max(momentum_bars), range_bars, ema_slow) + 2
    if len(bars) < need:
        return {"atr_price":None,"atr_points":None,"atr_pctile":None,"pos_in_range":None,
                "mom_short_atr":None,"mom_mid_atr":None,"mom_long_atr":None,"trend":None,
                "spread_atr":None,"spread_points":None,"dist_to_high_atr":None,
                "dist_to_low_atr":None,"reason":f"insufficient bars: {len(bars)}<{need}"}
    h,l,c = bars["high"], bars["low"], bars["close"]
    atr = _atr(h,l,c,atr_period)
    atr_price = float(atr.iloc[-1])
    hist = atr.dropna().tail(atr_pctile_lookback)
    atr_pctile = float((hist < atr_price).mean()) if len(hist) >= 20 else None
    win = bars.tail(range_bars)
    rng = float(win["high"].max() - win["low"].min())
    pos = float((c.iloc[-1] - win["low"].min()) / rng) if rng > 0 else 0.5
    ms, mm, ml = momentum_bars
    def mom(k): return float((c.iloc[-1] - c.iloc[-1-k]) / atr_price) if atr_price>0 else None
    ema_f = c.ewm(span=ema_fast, adjust=False).mean().iloc[-1]
    ema_s = c.ewm(span=ema_slow, adjust=False).mean().iloc[-1]
    slope = (ema_f - ema_s) / atr_price if atr_price>0 else 0.0
    trend = "up" if slope>0.1 else "down" if slope<-0.1 else "flat"
    spread_pts = float(bars["spread"].iloc[-1])
    spread_price = spread_pts * point
    return {
        "atr_price": round(atr_price,5), "atr_points": round(atr_price/point,1),
        "atr_pctile": None if atr_pctile is None else round(atr_pctile,2),
        "pos_in_range": round(min(max(pos,0.0),1.0),2),
        "mom_short_atr": round(mom(ms),2), "mom_mid_atr": round(mom(mm),2),
        "mom_long_atr": round(mom(ml),2), "trend": trend,
        "spread_atr": round(spread_price/atr_price,3) if atr_price>0 else None,
        "spread_points": spread_pts,
        "dist_to_high_atr": round((c.iloc[-1]-win["high"].max())/atr_price,2),
        "dist_to_low_atr": round((c.iloc[-1]-win["low"].min())/atr_price,2),
        "reason": None,
    }
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(trader): ATR-normalized feature computation`.

---

### Task 1.2: account.py — снимок счёта + топливные датчики

**Files:** Create `trader_lib/account.py`; Test `tests/test_account.py`.

- [ ] **Step 1: Failing test**
```python
# tests/test_account.py
from trader_lib.account import account_snapshot

def test_fuel_gauges():
    acc = {"balance":10000,"equity":9850}
    snap = account_snapshot(acc, day_start_equity=10000, initial_balance=10000,
                            profit_target_pct=10, daily_limit_pct=3, total_limit_pct=6,
                            positions=[])
    assert round(snap["daily_budget_remaining_pct"],2) == 1.5   # потратил 1.5% из 3%
    assert round(snap["total_budget_remaining_pct"],2) == 4.5   # −150 от 10000 → 1.5% из 6%
    assert snap["progress_to_target_pct"] < 0                   # equity ниже старта
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# trader_lib/account.py
def account_snapshot(acc, *, day_start_equity, initial_balance, profit_target_pct,
                     daily_limit_pct, total_limit_pct, positions):
    equity = acc["equity"]
    daily_loss_pct = max(0.0, (day_start_equity - equity) / day_start_equity * 100)
    total_loss_pct = max(0.0, (initial_balance - equity) / initial_balance * 100)
    progress = (equity - initial_balance) / initial_balance * 100
    return {
        "equity": equity, "balance": acc["balance"],
        "progress_to_target_pct": round(progress,2),
        "target_pct": profit_target_pct,
        "daily_budget_remaining_pct": round(daily_limit_pct - daily_loss_pct,3),
        "total_budget_remaining_pct": round(total_limit_pct - total_loss_pct,3),
        "open_positions": positions,
    }
```

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): account snapshot / fuel gauges`.

---

### Task 1.3: scripts/perceive.py (entrypoint)

**Files:** Create `scripts/perceive.py`.

Собирает snapshot по символу+ТФ, печатает JSON в stdout. Хранит `day_start_equity` в
`<state>/day_baseline.json` (сбрасывается на `server_day_reset_hour`).

- [ ] **Step 1:** Реализация (склейка features + account + day-baseline). Тест — smoke на `FakeMarket`.
```python
# scripts/perceive.py
import sys, json, argparse, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir
from trader_lib.features import compute_tf_features
from trader_lib.account import account_snapshot

def _day_baseline(sd, equity, reset_hour):
    p = Path(sd)/"day_baseline.json"; now = dt.datetime.utcnow()
    today = now.date().isoformat()
    if p.exists():
        d = json.loads(p.read_text())
        if d.get("day")==today: return d["equity"], d["initial_balance"]
    init = json.loads((Path(sd)/"account_init.json").read_text())["initial_balance"] \
        if (Path(sd)/"account_init.json").exists() else equity
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day":today,"equity":equity,"initial_balance":init}))
    return equity, init

def build_snapshot(market, cfg, symbol, timeframes):
    si = market.symbol_info(symbol); acc = market.account_info()
    sd = state_dir(cfg)
    day_eq, init_bal = _day_baseline(sd, acc["equity"], cfg.risk.server_day_reset_hour)
    P = cfg.perception
    tf = {}
    for t in timeframes:
        bars = market.copy_rates(symbol, t, P.atr_pctile_lookback + 50)
        if P.use_closed_bars_only: bars = bars.iloc[:-1]
        tf[t] = compute_tf_features(bars, point=si["point"], atr_period=P.atr_period,
            momentum_bars=P.momentum_bars, range_bars=P.range_bars,
            ema_fast=P.ema_fast, ema_slow=P.ema_slow, atr_pctile_lookback=P.atr_pctile_lookback)
    acc_snap = account_snapshot(acc, day_start_equity=day_eq, initial_balance=init_bal,
        profit_target_pct=cfg.goal["profit_target_pct"],
        daily_limit_pct=cfg.risk.daily_loss_limit_pct,
        total_limit_pct=cfg.risk.total_loss_limit_pct, positions=market.positions())
    return {"symbol":symbol,"ts":dt.datetime.utcnow().isoformat(),"tf":tf,
            "account":acc_snap}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True); ap.add_argument("--tf", default="M5,H1")
    ap.add_argument("--config", default="config/trader.config.json")
    a = ap.parse_args()
    from trader_lib.mt5_client import live_market
    cfg = load_config(a.config)
    snap = build_snapshot(live_market(), cfg, a.symbol, a.tf.split(","))
    print(json.dumps(snap, ensure_ascii=False, indent=2))
```
- [ ] **Step 2:** Тест `tests/test_perceive.py` через `FakeMarket` (import build_snapshot, assert структура). — [ ] **Step 3: Commit** — `feat(trader): perceive entrypoint`.

---

## Phase 2 — Risk core (safety-critical)

### Task 2.1: risk_gate.py

**Files:** Create `trader_lib/risk_gate.py`; Test `tests/test_risk_gate.py`.

- [ ] **Step 1: Failing tests**
```python
# tests/test_risk_gate.py
from trader_lib.risk_gate import evaluate_gate

BASE = dict(daily_limit_pct=3, total_limit_pct=6, flatten_buffer_pct=0.3, K=3, cap_pct=1.0)

def test_ok_budget():
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 100.0     # min(daily_budget/3=100, cap 1%=100)

def test_throttle_near_wall():
    v = evaluate_gate(equity=9720, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] in ("THROTTLE","HALT_NEW")

def test_force_flat_on_buffer():
    v = evaluate_gate(equity=9730, day_start_equity=10000, initial_balance=10000, **BASE)
    # дневной убыток 2.7% == стена(3%)-буфер(0.3%) → FORCE_FLAT
    assert v["verdict"] == "FORCE_FLAT"

def test_total_wall_dominates():
    v = evaluate_gate(equity=9430, day_start_equity=9500, initial_balance=10000, **BASE)
    assert v["verdict"] == "FORCE_FLAT"   # общий убыток 5.7% == 6%-0.3%
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement (fail-safe by construction)**
```python
# trader_lib/risk_gate.py
def evaluate_gate(*, equity, day_start_equity, initial_balance,
                  daily_limit_pct, total_limit_pct, flatten_buffer_pct, K, cap_pct):
    daily_loss_pct = (day_start_equity - equity) / day_start_equity * 100
    total_loss_pct = (initial_balance - equity) / initial_balance * 100
    daily_flat = daily_limit_pct - flatten_buffer_pct
    total_flat = total_limit_pct - flatten_buffer_pct

    if daily_loss_pct >= daily_flat or total_loss_pct >= total_flat:
        return _v("FORCE_FLAT", 0.0, daily_loss_pct, total_loss_pct)

    daily_budget = (daily_limit_pct - daily_loss_pct) / 100 * day_start_equity
    total_budget = (total_limit_pct - total_loss_pct) / 100 * initial_balance
    budget = max(0.0, min(daily_budget, total_budget))
    if budget <= 0:
        return _v("HALT_NEW", 0.0, daily_loss_pct, total_loss_pct)

    max_risk = min(budget / K, cap_pct / 100 * equity)
    # «близко к стене»: <1% дневного лимита осталось
    near = (daily_limit_pct - daily_loss_pct) < 1.0 or (total_limit_pct - total_loss_pct) < 1.0
    verdict = "THROTTLE" if near else "OK"
    if verdict == "THROTTLE": max_risk *= 0.5
    return _v(verdict, round(max_risk,2), daily_loss_pct, total_loss_pct,
              daily_budget=round(daily_budget,2), total_budget=round(total_budget,2))

def _v(verdict, max_risk, dlp, tlp, **extra):
    return {"verdict":verdict,"max_risk_per_trade_usd":max_risk,
            "daily_loss_pct":round(dlp,3),"total_loss_pct":round(tlp,3), **extra}

def safe_evaluate_gate(**kw):
    """Обёртка fail-closed: любая ошибка → HALT_NEW (никогда не fail-open в торговлю)."""
    try:
        return evaluate_gate(**kw)
    except Exception as e:
        return {"verdict":"HALT_NEW","max_risk_per_trade_usd":0.0,"error":str(e)}
```

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): risk gate with fail-closed wrapper`.

---

### Task 2.2: Property-тест — серия убытков не пробивает 3%

**Files:** Modify `tests/test_risk_gate.py`.

- [ ] **Step 1: Add property test**
```python
def test_loss_sequence_never_breaches_daily():
    # Симуляция: каждая сделка рискует max_risk от гейта и ВСЯ проигрывает.
    equity = 10000.0; day_start = 10000.0
    for _ in range(200):
        v = evaluate_gate(equity=equity, day_start_equity=day_start,
                          initial_balance=10000, **BASE)
        if v["verdict"] in ("FORCE_FLAT","HALT_NEW"): break
        equity -= v["max_risk_per_trade_usd"]      # полный стоп
    # реализованный дневной убыток строго меньше жёсткой стены 3%
    assert (day_start - equity) / day_start * 100 < 3.0
```
- [ ] **Step 2: Run — expect PASS** (математика budget/K гарантирует частичные суммы < бюджета).
- [ ] **Step 3: Commit** — `test(trader): property — loss run cannot breach daily wall`.

---

### Task 2.3: size_position.py

**Files:** Create `trader_lib/size_position.py`; Test `tests/test_size_position.py`.

- [ ] **Step 1: Failing test**
```python
# tests/test_size_position.py
from trader_lib.size_position import compute_lots

def test_lots_from_risk():
    si = {"point":0.01,"trade_contract_size":100,"volume_min":0.01,
          "volume_max":100.0,"volume_step":0.01}
    # риск $100, SL 3.0 цены (=300 пунктов). value_per_point_per_lot = contract*point=1.0
    # loss_per_lot = 300 * 1.0 = $300 → lots = 100/300 = 0.33
    lots = compute_lots(risk_usd=100, entry=2634.0, sl=2631.0, symbol_info=si)
    assert abs(lots - 0.33) < 1e-9

def test_respects_min_and_step():
    si = {"point":0.01,"trade_contract_size":100,"volume_min":0.01,
          "volume_max":100.0,"volume_step":0.01}
    lots = compute_lots(risk_usd=1, entry=2634.0, sl=2631.0, symbol_info=si)
    assert lots == 0.01   # округление вниз к min
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# trader_lib/size_position.py
import math

def compute_lots(*, risk_usd, entry, sl, symbol_info):
    point = symbol_info["point"]; contract = symbol_info["trade_contract_size"]
    step = symbol_info["volume_step"]; vmin = symbol_info["volume_min"]; vmax = symbol_info["volume_max"]
    sl_points = abs(entry - sl) / point
    if sl_points <= 0: return 0.0
    value_per_point_per_lot = contract * point         # прибл. для котируемых-в-USD инстр.
    loss_per_lot = sl_points * value_per_point_per_lot
    if loss_per_lot <= 0: return 0.0
    raw = risk_usd / loss_per_lot
    lots = math.floor(raw / step) * step
    lots = max(vmin, min(lots, vmax)) if raw >= vmin else 0.0
    return round(lots, 8)
```
> Заметка исполнителю: `value_per_point_per_lot` точен для инструментов, котируемых в валюте
> счёта (XAUUSD/USD-счёт). Для кросс-валют — заменить на `symbol_info_tick_value` из MT5
> (`tick_value/tick_size*point`). На старте демо (XAUUSD/USD) формула верна; вынести в TODO при мультивалюте.

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): position sizing from risk budget`.

---

## Phase 3 — Store / Journal

### Task 3.1: journal.py

**Files:** Create `trader_lib/journal.py`; Test `tests/test_journal.py`.

- [ ] **Step 1: Failing tests**
```python
# tests/test_journal.py
from trader_lib.journal import append_decision, append_outcome, read_records

def test_append_and_read(tmp_path):
    j = tmp_path/"journal.jsonl"
    append_decision(j, {"trade_id":"a1","symbol":"XAUUSD","setup_type":"s1",
                        "confidence":0.6,"action":"buy","risk_usd":40})
    append_outcome(j, {"trade_id":"a1","R":1.8,"exit_reason":"tp"})
    recs = read_records(j)
    assert len(recs)==2 and recs[0]["type"]=="decision" and recs[1]["type"]=="outcome"
    assert recs[0]["trade_id"]==recs[1]["trade_id"]
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement (atomic append)**
```python
# trader_lib/journal.py
import json, os, datetime as dt
from pathlib import Path

def _append(path, rec):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line); f.flush(); os.fsync(f.fileno())

def append_decision(path, rec):
    rec = {"type":"decision","ts":dt.datetime.utcnow().isoformat(), **rec}
    _append(path, rec); return rec

def append_outcome(path, rec):
    rec = {"type":"outcome","close_ts":dt.datetime.utcnow().isoformat(), **rec}
    _append(path, rec); return rec

def read_records(path):
    p = Path(path)
    if not p.exists(): return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
```

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): append-only journal io`.

---

### Task 3.2: scripts/close_watch.py

**Files:** Create `scripts/close_watch.py`; Test `tests/test_close_watch.py`.

Сверяет закрытые позиции с историей MT5, считает R/MFE/MAE, дописывает outcome. Идемпотентно:
не дублирует outcome по уже закрытым `trade_id` (сверяется с журналом).

- [ ] **Step 1: Failing test** (на `FakeMarket` с готовыми `deals`, где сделка `a1` закрыта):
```python
# tests/test_close_watch.py
from scripts.close_watch import reconcile

def test_reconcile_writes_outcome(tmp_path):
    j = tmp_path/"journal.jsonl"
    from trader_lib.journal import append_decision, read_records
    append_decision(j, {"trade_id":"a1","symbol":"XAUUSD","risk_usd":40,
                        "entry":2634.0,"sl":2631.0,"action":"buy"})
    deals = [{"position_id":"a1","profit":72.0,"price":2639.4,
              "time":1735700000,"entry":1}]   # entry=1 → выход
    n = reconcile(j, deals_by_pos={"a1": deals})
    recs = read_records(j)
    out = [r for r in recs if r["type"]=="outcome"][0]
    assert out["trade_id"]=="a1" and round(out["R"],2)==1.8   # 72/40
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# scripts/close_watch.py
import sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.journal import read_records, append_outcome

def reconcile(journal_path, deals_by_pos):
    recs = read_records(journal_path)
    decisions = {r["trade_id"]: r for r in recs if r["type"]=="decision"}
    done = {r["trade_id"] for r in recs if r["type"]=="outcome"}
    written = 0
    for tid, d in decisions.items():
        if tid in done or tid not in deals_by_pos: continue
        deals = deals_by_pos[tid]
        exits = [x for x in deals if x.get("entry")==1]
        if not exits: continue
        profit = sum(x["profit"] for x in deals)
        exit_price = exits[-1]["price"]; risk = d.get("risk_usd") or 0
        R = profit / risk if risk else 0.0
        append_outcome(journal_path, {"trade_id":tid,"exit":exit_price,
            "profit":round(profit,2),"R":round(R,3),
            "exit_reason":"closed"})
        written += 1
    return written

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--config", default="config/trader.config.json")
    a = ap.parse_args()
    from trader_lib.config import load_config, state_dir
    from trader_lib.mt5_client import live_market
    import collections
    cfg = load_config(a.config); m = live_market()
    deals = m.history_deals(__import__("datetime").datetime.now().replace(hour=0,minute=0))
    by_pos = collections.defaultdict(list)
    for x in deals: by_pos[str(x.get("position_id"))].append(x)
    print("outcomes written:", reconcile(str(Path(state_dir(cfg))/"journal.jsonl"), by_pos))
```
> Заметка: MFE/MAE требуют внутрисделочных тиков — на первой итерации пишем `mfe_R/mae_R=null`
> (анти-галлюцинация), добавим тиковый проход отдельной задачей, когда понадобится анализ выходов.

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): outcome reconciliation from MT5 history`.

---

## Phase 4 — Scoring

### Task 4.1: score.py — stats.json

**Files:** Create `trader_lib/score.py`; Test `tests/test_score.py`.

- [ ] **Step 1: Failing tests**
```python
# tests/test_score.py
from trader_lib.score import compute_stats

def _journal():
    recs=[]
    for i,(su,R,conf) in enumerate([("A",1.5,0.7),("A",-1.0,0.6),("A",2.0,0.7),
                                    ("B",-1.0,0.8),("B",-1.0,0.8)]):
        tid=f"t{i}"
        recs.append({"type":"decision","trade_id":tid,"setup_type":su,
                     "confidence":conf,"symbol":"XAUUSD"})
        recs.append({"type":"outcome","trade_id":tid,"R":R})
    return recs

def test_expectancy_per_setup():
    s = compute_stats(_journal(), min_n_for_confirmed=20)
    a = s["by_setup"]["A"]
    assert a["n"]==3 and abs(a["avg_R"]-0.833) < 0.01
    assert a["insufficient"] is True          # n<20
    b = s["by_setup"]["B"]
    assert b["avg_R"]==-1.0 and b["wr"]==0.0

def test_calibration_bucketed():
    s = compute_stats(_journal(), min_n_for_confirmed=20)
    assert "calibration" in s and isinstance(s["calibration"], list)
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement**
```python
# trader_lib/score.py
import numpy as np
from collections import defaultdict

def _join(records):
    dec = {r["trade_id"]: r for r in records if r["type"]=="decision"}
    out = {r["trade_id"]: r for r in records if r["type"]=="outcome"}
    rows=[]
    for tid, d in dec.items():
        if tid in out:
            rows.append({**d, "R": out[tid]["R"]})
    return rows

def _agg(rows):
    n=len(rows)
    if n==0: return {"n":0,"wr":None,"avg_R":None,"sum_R":0.0}
    Rs=np.array([r["R"] for r in rows], float)
    wins=(Rs>0).sum()
    return {"n":n,"wr":round(wins/n,3),"avg_R":round(float(Rs.mean()),3),
            "sum_R":round(float(Rs.sum()),3)}

def _bootstrap_gt0(Rs, iters=2000, seed=0):
    if len(Rs)<5: return None
    rng=np.random.default_rng(seed); n=len(Rs)
    means=[rng.choice(Rs,n,replace=True).mean() for _ in range(iters)]
    lo=np.percentile(means,2.5)
    return bool(lo>0)   # 95% CI на avg R не пересекает 0 снизу

def compute_stats(records, *, min_n_for_confirmed):
    rows=_join(records)
    overall=_agg(rows)
    by_setup={}
    grp=defaultdict(list)
    for r in rows: grp[r.get("setup_type","?")].append(r)
    for su, rs in grp.items():
        a=_agg(rs); Rs=np.array([x["R"] for x in rs],float)
        a["insufficient"]=a["n"]<min_n_for_confirmed
        a["edge_significant"]=_bootstrap_gt0(Rs)
        by_setup[su]=a
    # калибровка: бакеты уверенности
    calib=[]
    cb=defaultdict(list)
    for r in rows:
        c=r.get("confidence")
        if c is None: continue
        cb[round(min(max(c,0),0.99)*10)//1] .append(1 if r["R"]>0 else 0)
    for bucket in sorted(cb):
        vals=cb[bucket]
        calib.append({"conf_bucket":f"{bucket/10:.1f}-{bucket/10+0.1:.1f}",
                      "n":len(vals),"realized_wr":round(sum(vals)/len(vals),3)})
    # сегменты (пример: символ) — расширяемо
    seg=defaultdict(list)
    for r in rows: seg[r.get("symbol","?")].append(r)
    by_symbol={k:_agg(v) for k,v in seg.items()}
    return {"overall":overall,"by_setup":by_setup,"by_symbol":by_symbol,
            "calibration":calib}
```

- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): stats/expectancy/calibration/edge-significance`.

---

### Task 4.2: scorecard.md renderer + run_score.py

**Files:** Modify `trader_lib/score.py` (add `render_scorecard`); Create `scripts/run_score.py`.

- [ ] **Step 1: Failing test**
```python
def test_scorecard_renders():
    from trader_lib.score import compute_stats, render_scorecard
    s=compute_stats(_journal(), min_n_for_confirmed=20)
    md=render_scorecard(s, progress_to_target_pct=1.2, target_pct=10)
    assert "Прогресс к цели" in md and "Матожидание" in md
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement**
```python
# добавить в trader_lib/score.py
def render_scorecard(stats, *, progress_to_target_pct, target_pct):
    o=stats["overall"]
    lines=[f"# Scorecard",
           f"**Прогресс к цели:** {progress_to_target_pct:.2f}% из {target_pct}%",
           f"**Матожидание (avg R):** {o['avg_R']}  ·  n={o['n']}  ·  WR={o['wr']}  ·  ΣR={o['sum_R']}",
           "", "## Сетапы"]
    for su,a in sorted(stats["by_setup"].items(), key=lambda kv:-(kv[1]['sum_R'] or 0)):
        flag="⚠ мало данных" if a.get("insufficient") else ("✅ edge" if a.get("edge_significant") else "—")
        lines.append(f"- **{su}**: n={a['n']} WR={a['wr']} avgR={a['avg_R']} {flag}")
    lines+=["","## Калибровка (заявленная уверенность → реальный WR)"]
    for c in stats["calibration"]:
        lines.append(f"- {c['conf_bucket']}: n={c['n']} реальный WR={c['realized_wr']}")
    return "\n".join(lines)
```
`scripts/run_score.py`: читает journal → `compute_stats` → пишет `stats.json`; на ритме
reflect — `render_scorecard` → `scorecard.md`.
- [ ] **Step 4: Run — expect PASS.** — [ ] **Step 5: Commit** — `feat(trader): scorecard renderer + run_score`.

---

### Task 4.3: scripts/recall.py

**Files:** Create `scripts/recall.py`.

Детерминированная выборка из `stats.json` под контекст (символ + список сетапов-кандидатов),
печать компактного JSON для `decide`.
- [ ] **Step 1: Implement**
```python
# scripts/recall.py
import sys, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir

def pull(stats, symbol, setups):
    return {"overall":stats.get("overall"),
            "symbol":stats.get("by_symbol",{}).get(symbol),
            "setups":{s:stats.get("by_setup",{}).get(s) for s in setups},
            "calibration":stats.get("calibration")}

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--symbol",required=True)
    ap.add_argument("--setups",default=""); ap.add_argument("--config",default="config/trader.config.json")
    a=ap.parse_args(); cfg=load_config(a.config)
    sp=Path(state_dir(cfg))/"stats.json"
    stats=json.loads(sp.read_text()) if sp.exists() else {}
    print(json.dumps(pull(stats,a.symbol,[s for s in a.setups.split(",") if s]),
                     ensure_ascii=False, indent=2))
```
- [ ] **Step 2: Commit** — `feat(trader): recall — context-scoped stats pull`.

---

## Phase 5 — Skills (SKILL.md — интерфейс модели)

Полное содержимое всех 4 `SKILL.md` — в отдельном файле `docs/skill_contents.md` этого
плана (Task 5.1–5.4 создают файлы из него). Ключевые контракты:

- **trader-perceive** — «Каждый цикл вызови `perceive.py --symbol S --tf ...`. Читай
  снимок КАК ЕСТЬ. `null` = неизвестно, НЕ додумывай. Не решай без снимка.»
- **trader-recall** — «Перед решением вызови `recall.py`. Смотри n/avg_R/insufficient по
  сетапу и калибровку. insufficient → доверяй слабо.»
- **trader-decide** — 4 рельса процесса (SL обязателен; размер через `size_position.py`,
  вверх не переопределять; тезис+уверенность+setup_type обязательны; вердикт гейта
  исполняется). Всё остальное свободно.
- **trader-reflect** — переходы статусов сетапов (порог n), схлопывание синонимов,
  калибровочный haircut, рост `playbook.md`; не повышать сетап на n<порог.

- [ ] **Task 5.1–5.4:** Создать 4 `SKILL.md` из `skill_contents.md`; smoke-проверка —
  `description` каждого точно описывает «когда брать» (Правило 1). Commit после каждого.

---

## Phase 6 — Orchestration + E2E

### Task 6.1: prompts/loop_prompt.md

**Files:** Create `prompts/loop_prompt.md` — точная последовательность цикла (§3 дизайна),
которую трейдер выполняет каждый тик (детерминированные скрипты + вызовы скиллов), и
условия рефлексии. Содержимое — в `docs/skill_contents.md` (раздел Loop).
- [ ] Commit — `feat(trader): loop orchestration prompt`.

### Task 6.2: E2E dry-run harness

**Files:** Create `tests/test_e2e_dryrun.py`.

Прогон исторических баров через build_snapshot→gate→(симулированное решение)→journal→score
на `FakeMarket` в paper-режиме (order_send мокнут). Проверки:
- журнал получает decision+outcome, stats.json считается;
- **на adversarial-серии убытков гейт ни разу не даёт реализованному дневному убытку ≥3%.**
- [ ] **Step 1:** Написать harness (переиспользует property-логику Task 2.2 в петле).
- [ ] **Step 2: Run — expect PASS.** — [ ] **Step 3: Commit** — `test(trader): E2E dry-run + wall safety`.

---

## Self-Review (пройден при написании плана)

- **Покрытие спеки:** perceive §4→T1.1-1.3; стор §5→T3.1,T4; гейт+decide §6→T2,T5.3;
  score+reflect §7→T4,T5.4; холодный старт §8→loop/decide-промт; ошибки §10→fail-closed
  (T2.1 `safe_evaluate_gate`, perceive skip, close_watch reconcile); тесты §11→property T2.2,
  E2E T6.2. Развёртывание §9 → `DEPLOY.md`.
- **Плейсхолдеры:** MFE/MAE и мультивалютный tick_value явно помечены как отложенные с
  причиной и `null`-поведением (не «TODO без плана»).
- **Согласованность типов:** `trade_id`/`R`/`setup_type`/`risk_usd`/`max_risk_per_trade_usd`
  единообразны между journal/score/gate/size.

---

## Execution Handoff
Порядок фаз строгий: 0→6 (перцепция и гейт до скиллов; скиллы до оркестрации). Каждая фаза
даёт тестируемый результат.
