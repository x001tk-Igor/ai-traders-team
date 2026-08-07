import pytest

from trader_lib.config import load_config


def test_load_defaults():
    cfg = load_config("config/trader.config.json")
    assert cfg.risk.daily_loss_limit_pct == 3.0
    assert cfg.risk.total_loss_limit_pct == 6.0
    assert cfg.risk.risk_budget_divisor_K == 3
    assert cfg.learning.min_n_for_confirmed == 20


def test_missing_key_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"risk": {}}')
    with pytest.raises((KeyError, ValueError, TypeError)):
        load_config(str(bad))


def test_v2_blocks_load():
    cfg = load_config("config/trader.config.json")

    # risk: v2 additions alongside the pre-existing keys
    assert cfg.risk.max_open_positions == 3
    assert cfg.risk.max_open_risk_pct == 1.5
    assert cfg.risk.max_new_trades_per_day == 10
    assert cfg.risk.per_trade_risk_cap_pct == 0.5
    assert cfg.risk.max_unplanned_trades_per_day == 1
    assert cfg.risk.near_wall_pct == 1.0
    assert cfg.risk.near_wall_mult == 0.5
    assert cfg.risk.ladder["daily"][0]["loss_pct"] == 1.5
    assert cfg.risk.ladder["total"][1]["require_status"] == "confirmed"
    assert cfg.risk.streak["three_losses"]["pause_minutes"] == 60
    assert cfg.risk.status_risk_mult["подтверждён"] == 1.0
    assert cfg.risk.status_risk_mult["unknown"] == 0.2
    assert cfg.risk.max_costs_R == 0.10
    assert cfg.risk.server_utc_offset_hours == 3

    # model
    assert cfg.model.profile == "strong"
    assert cfg.model.id == "claude-opus-5"
    assert cfg.model.profile_rules["weak"]["require_status"] == "confirmed"

    # alerts
    assert cfg.alerts.poll_seconds == 1
    assert cfg.alerts.max_events_per_day == 100
    assert cfg.alerts.min_seconds_between_events == 60
    assert cfg.alerts.min_seconds_between_critical_events == 15
    assert cfg.alerts.max_events_per_minute == 6
    assert "wall_breach" in cfg.alerts.critical_types
    assert cfg.alerts.max_silence_minutes == 180

    # session
    assert cfg.session.trade_window_utc == ["07:00", "20:00"]
    assert cfg.session.no_new_after_utc == "19:00"
    assert cfg.session.phases["LONDON"] == ["07:00", "11:00"]

    # news
    assert cfg.news.top_events[0] == "FOMC"
    assert cfg.news.fail_mode == "halt_new"
    assert cfg.news.cache_max_age_hours == 24

    # instruments
    assert "XAUUSD" in cfg.instruments.whitelist
    assert cfg.instruments.spread_anomaly_mult == 1.5

    # constitution
    assert "risk" in cfg.constitution.immutable_by_agent
    assert cfg.constitution.config_hash_check is True
