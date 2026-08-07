import json
import os
from dataclasses import dataclass

# `frozen=True` запрещает переприсваивание полей (cfg.risk = ...), но НЕ делает
# вложенные dict/list неизменяемыми: cfg.risk.streak[...] = ... и
# cfg.constitution.immutable_by_agent.append(...) пройдут молча. Защита от
# изменения лимитов держится не на этом, а на проверке хэша конфига на диске
# (задача 8.2). Не полагайся на frozen как на гарантию неизменности за сессию.
#
# Все блоки строятся как Block(**d["block"]): пропущенный или опечатанный ключ
# верхнего уровня падает при загрузке. Потребителям вложенных структур
# индексировать напрямую (d["key"]), а не d.get("key", default) — иначе опечатка
# тихо подменится дефолтом, и весь смысл громкого падения теряется.


@dataclass(frozen=True)
class Risk:
    daily_loss_limit_pct: float
    total_loss_limit_pct: float
    flatten_buffer_pct: float
    risk_budget_divisor_K: int
    per_trade_risk_cap_pct: float
    daily_baseline: str
    total_baseline: str
    server_day_reset_hour: int
    max_open_positions: int
    max_open_risk_pct: float
    max_new_trades_per_day: int
    max_unplanned_trades_per_day: int
    # near_wall_pct — сколько процентов лимита должно остаться, чтобы риск
    # ещё НЕ урезался; near_wall_mult — во сколько раз урезать у стены.
    # Единственные риск-параметры, которые раньше были константами в коде:
    # аудитор конституции обязан видеть правило «×0.5 у стены» здесь.
    near_wall_pct: float
    near_wall_mult: float
    ladder: dict
    streak: dict
    status_risk_mult: dict
    max_costs_R: float
    server_utc_offset_hours: int


@dataclass(frozen=True)
class Model:
    profile: str
    id: str
    profile_rules: dict


@dataclass(frozen=True)
class Alerts:
    poll_seconds: int
    max_events_per_day: int
    min_seconds_between_events: int
    # critical-события обходят max_events_per_day, но обязаны соблюдать свой
    # (короче обычного) интервал; max_events_per_minute — абсолютный потолок
    # событий в минуту, общий для normal и critical, не обходит никто (см.
    # обоснование в trader_lib/alerts.py.event_budget)
    min_seconds_between_critical_events: int
    max_events_per_minute: int
    critical_types: list
    max_silence_minutes: int


@dataclass(frozen=True)
class Session:
    trade_window_utc: list
    no_new_after_utc: str
    friday_no_new_utc: str
    friday_flat_utc: str
    swap_block_utc: list
    phases: dict


@dataclass(frozen=True)
class News:
    normal_window_min: list
    top_window_min: list
    top_events: list
    cache_max_age_hours: int
    fail_mode: str


@dataclass(frozen=True)
class Instruments:
    whitelist: list
    spread_anomaly_mult: float
    spread_median_days: int


@dataclass(frozen=True)
class Constitution:
    immutable_by_agent: list
    config_hash_check: bool


@dataclass(frozen=True)
class Perception:
    atr_period: int
    momentum_bars: list
    range_bars: int
    ema_fast: int
    ema_slow: int
    atr_pctile_lookback: int
    use_closed_bars_only: bool
    # потолок размера снимка в токенах (грубая оценка). Не эстетика: на слабой
    # модели с 32k контекста раздутый снимок вытесняет план дня, опыт и
    # открытое намерение — то, из чего принимается решение (задача 7.2)
    snapshot_token_budget: int


@dataclass(frozen=True)
class Learning:
    min_n_for_confirmed: int
    reflect_every_k_closed_trades: int
    reflect_on_session_end: bool


@dataclass(frozen=True)
class Config:
    account: dict
    goal: dict
    risk: Risk
    model: Model
    alerts: Alerts
    session: Session
    news: News
    instruments: Instruments
    constitution: Constitution
    perception: Perception
    learning: Learning
    loop: dict


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return Config(
        account=d["account"],
        goal=d["goal"],
        risk=Risk(**d["risk"]),
        model=Model(**d["model"]),
        alerts=Alerts(**d["alerts"]),
        session=Session(**d["session"]),
        news=News(**d["news"]),
        instruments=Instruments(**d["instruments"]),
        constitution=Constitution(**d["constitution"]),
        perception=Perception(**d["perception"]),
        learning=Learning(**d["learning"]),
        loop=d["loop"],
    )


def state_dir(cfg: Config) -> str:
    return os.path.expanduser(cfg.account["state_dir"])
