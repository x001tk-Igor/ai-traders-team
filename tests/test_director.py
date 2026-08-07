"""Директорский цикл (Ф7).

ЧТО ЗДЕСЬ КОД, А ЧТО МОДЕЛЬ. Директор — модель, и решения его: какой режим он
видит, кому какие инструменты дать, как поделить бюджет. Код делает две вещи,
которые модель делать не должна: СЧИТАЕТ (режимы, экономику ТФ, кластеры) и
ПРОВЕРЯЕТ его решение на связность.

Проверка нужна не из недоверия, а потому что ошибка директора тиха. Две пары из
одного кластера, розданные разным трейдерам, выглядят как диверсификация ровно
до того дня, когда обе пойдут в одну сторону. Кластерный потолок в гейте
поймает это в момент второго входа — но правильнее не выпускать такой мандат
вовсе, чем ловить последствия.
"""
import dataclasses
import datetime as dt
import json

import numpy as np
import pytest

from trader_lib.clusters import save_clusters
from trader_lib.config import load_config
from trader_lib.director import scan_instruments, validate_allocation

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 3, 7, 30, tzinfo=UTC)

CLUSTERS = {
    "groups": [["AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF"],
               ["USDJPY"], ["XAUUSD"]],
    "threshold": 0.65, "insufficient": [], "computed_utc": NOW.isoformat()}


def _cfg(tmp_path, **instr):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if instr:
        cfg = dataclasses.replace(cfg, instruments=dataclasses.replace(
            cfg.instruments, **instr))
    return cfg


def _alloc(**traders):
    return {"server_day": "2026-08-03", "traders": traders}


def _t(instruments, share=0.3, quota=11, active=True):
    return {"instruments": list(instruments), "risk_share": share,
            "events_quota": quota, "active": active}


# --------------------------------------------------------------------------
# валидация мандатов: связность решения директора
# --------------------------------------------------------------------------

def test_clean_allocation_passes(tmp_path):
    res = validate_allocation(
        _alloc(trend=_t(["XAUUSD"]), fade=_t(["EURUSD"]), range=_t(["USDJPY"])),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is True, res["problems"]


def test_two_traders_in_one_cluster_are_refused(tmp_path):
    """ГЛАВНАЯ ПРОВЕРКА. EURUSD и USDCAD — один фактор риска (corr −0.67).
    Разные символы, разные трейдеры, а ставка одна. По символам это не видно —
    видно только по кластеру."""
    res = validate_allocation(
        _alloc(fade=_t(["EURUSD"]), range=_t(["USDCAD"])),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is False
    assert any("кластер" in p for p in res["problems"]), res["problems"]


def test_same_instrument_to_two_traders_is_refused(tmp_path):
    res = validate_allocation(
        _alloc(a=_t(["XAUUSD"]), b=_t(["XAUUSD"])),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is False
    assert any("дважды" in p or "двум" in p for p in res["problems"]), res["problems"]


def test_instrument_outside_whitelist_is_refused(tmp_path):
    """Директор не может выдать мандат на инструмент, которого нет в
    конституции: whitelist — решение человека, а не оркестратора."""
    res = validate_allocation(
        _alloc(trend=_t(["BTCUSD"])),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is False
    assert any("whitelist" in p for p in res["problems"]), res["problems"]


def test_shares_above_one_are_refused(tmp_path):
    res = validate_allocation(
        _alloc(a=_t(["XAUUSD"], share=0.7), b=_t(["USDJPY"], share=0.6)),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is False
    assert any("долей" in p for p in res["problems"]), res["problems"]


def test_event_quotas_must_leave_a_reserve(tmp_path):
    """Квоты, выбирающие бюджет подчистую, оставили бы директорские эскалации и
    стоп-кран без места.

    Числа берутся ОТ конституции, а не вписываются: лимит событий — параметр,
    который человек меняет (2026-08-01 поднят 40 → 100 под троих), и тест,
    закрепляющий старое число, ломался бы при каждой такой правке, ничего при
    этом не проверяя по существу. Проверяется ПРАВИЛО: резерв обязан остаться.
    """
    cfg = _cfg(tmp_path)
    budget = cfg.alerts.max_events_per_day
    res = validate_allocation(
        _alloc(a=_t(["XAUUSD"], quota=budget // 2),
               b=_t(["USDJPY"], quota=budget // 2)),
        cfg=cfg, clusters=CLUSTERS, now=NOW)
    assert res["ok"] is False
    assert any("резерв" in p for p in res["problems"]), res["problems"]


def test_benched_trader_does_not_occupy_a_cluster(tmp_path):
    """Снятый с торговли трейдер не должен блокировать кластер для остальных:
    он сегодня не торгует, значит и фактор риска не занимает."""
    res = validate_allocation(
        _alloc(fade=_t(["EURUSD"], active=False), range=_t(["USDCAD"])),
        cfg=_cfg(tmp_path), clusters=CLUSTERS, now=NOW)
    assert res["ok"] is True, res["problems"]


def test_unknown_cluster_is_reported_not_ignored(tmp_path):
    """Инструмент вне карты кластеров: про его корреляции ничего не известно.
    Молчаливый пропуск читался бы как «риски независимы»."""
    res = validate_allocation(
        _alloc(trend=_t(["USDCHF"])), cfg=_cfg(tmp_path),
        clusters={"groups": [["XAUUSD"]], "threshold": 0.65, "insufficient": [],
                  "computed_utc": NOW.isoformat()}, now=NOW)
    assert res["ok"] is False
    assert any("карте кластеров" in p for p in res["problems"]), res["problems"]


# --------------------------------------------------------------------------
# скан: числа, на которых директор принимает решение
# --------------------------------------------------------------------------

class _Market:
    """Ровный ряд с заданной волатильностью и спредом."""

    def __init__(self, *, spread=20, step=0.5, point=0.01):
        self.spread, self.step, self.point = spread, step, point

    def symbol_info(self, symbol):
        return {"point": self.point, "spread": self.spread, "digits": 2,
                "trade_contract_size": 100.0, "volume_min": 0.01,
                "volume_step": 0.01, "volume_max": 500.0, "filling_mode": 3}

    def copy_rates(self, symbol, tf, count):
        import pandas as pd
        n = max(count, 60)
        close = 2400.0 + np.arange(n) * self.step
        return pd.DataFrame({
            "time": pd.date_range("2026-07-20", periods=n, freq="5min"),
            "open": close, "high": close + self.step, "low": close - self.step,
            "close": close, "tick_volume": 200, "spread": self.spread})


def test_scan_reports_every_whitelisted_instrument(tmp_path):
    cfg = _cfg(tmp_path, whitelist=["XAUUSD", "EURUSD"])
    rows = scan_instruments(_Market(), cfg, now=NOW)
    assert {r["symbol"] for r in rows} == {"XAUUSD", "EURUSD"}


def test_scan_measures_costs_at_the_honest_stop_not_the_minimum(tmp_path):
    """ДЕФЕКТ, НАЙДЕННЫЙ 2026-08-01 В ЭТОЙ ЖЕ ФУНКЦИИ.

    Первая версия мерила «существует ли стоп, укладывающийся в лимит издержек»,
    и отвечала «да» там, где такой стоп стоял бы ВНУТРИ шума таймфрейма — то
    есть не защищал бы вовсе (урок 27.07, стоил $37). Честный стоп обязан
    удовлетворять обоим условиям сразу: не дешевле лимита издержек И не уже
    1.5 ATR.
    """
    cfg = _cfg(tmp_path, whitelist=["XAUUSD"])
    row = scan_instruments(_Market(spread=2, step=0.5), cfg, now=NOW)[0]
    tf = row["tf"]["M5"]
    assert tf["honest_stop_atr"] >= 1.5, "стоп обязан стоять вне шума"
    assert tf["costs_R"] == pytest.approx(row["spread_usd"] / tf["honest_stop"],
                                          abs=1e-4)


def test_scan_refuses_a_timeframe_that_only_just_fits_the_cost_limit(tmp_path):
    """Касание лимита не годится: спред расширяется в самый неподходящий момент
    (на золоте за неделю 27–31.07 он уходил до ×9.8 от медианы). Годен ТФ, где
    честный стоп занимает ДОЛЮ лимита, а не весь."""
    cfg = _cfg(tmp_path, whitelist=["XAUUSD"])
    limit = cfg.risk.max_costs_R

    cheap = scan_instruments(_Market(spread=2), cfg, now=NOW)[0]["tf"]["M5"]
    assert cheap["viable"] is True and cheap["costs_R"] < limit * 0.8

    # спред подобран так, что честный стоп упирается ровно в лимит издержек
    tight = scan_instruments(_Market(spread=120, step=0.4), cfg, now=NOW)[0]["tf"]["M5"]
    assert tight["costs_R"] == pytest.approx(limit, abs=1e-6)
    assert tight["viable"] is False, "работа впритык к лимиту не считается годной"


def test_scan_survives_a_broken_symbol(tmp_path):
    """Один недоступный инструмент не должен лишать директора всей картины."""
    class Broken(_Market):
        def copy_rates(self, symbol, tf, count):
            if symbol == "EURUSD":
                raise RuntimeError("нет истории")
            return super().copy_rates(symbol, tf, count)

    cfg = _cfg(tmp_path, whitelist=["XAUUSD", "EURUSD"])
    rows = scan_instruments(Broken(), cfg, now=NOW)
    by = {r["symbol"]: r for r in rows}
    assert by["XAUUSD"]["tf"], "исправный символ обязан быть посчитан"
    assert by["EURUSD"]["reason"], "сломанный обязан назвать причину, а не исчезнуть"


# --------------------------------------------------------------------------
# CLI директора: scan / validate / review
# --------------------------------------------------------------------------

def test_cli_scan_prints_json(tmp_path, capsys, monkeypatch):
    import scripts.director as d

    monkeypatch.setattr(d, "live_market", lambda: _Market())
    monkeypatch.setattr(d, "load_config", lambda p: _cfg(tmp_path, whitelist=["XAUUSD"]))
    assert d.main(["scan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["symbol"] == "XAUUSD"
    assert "H1" in out[0]["tf"]


def test_cli_validate_refuses_an_incoherent_allocation(tmp_path, capsys, monkeypatch):
    """Валидация обязана возвращать НЕнулевой код: директор запускает её
    скриптом, и «проверил, но не заметил» не должно выглядеть как успех."""
    import scripts.director as d

    save_clusters(tmp_path / "clusters.json", CLUSTERS)
    (tmp_path / "allocation.json").write_text(json.dumps(
        _alloc(fade=_t(["EURUSD"]), range=_t(["USDCAD"]))), encoding="utf-8")
    monkeypatch.setattr(d, "load_config", lambda p: _cfg(tmp_path))

    assert d.main(["validate"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert any("кластер" in p for p in out["problems"])


def test_cli_validate_accepts_a_clean_allocation(tmp_path, capsys, monkeypatch):
    import scripts.director as d

    save_clusters(tmp_path / "clusters.json", CLUSTERS)
    (tmp_path / "allocation.json").write_text(json.dumps(
        _alloc(trend=_t(["XAUUSD"]), fade=_t(["EURUSD"]), range=_t(["USDJPY"]))),
        encoding="utf-8")
    monkeypatch.setattr(d, "load_config", lambda p: _cfg(tmp_path))

    assert d.main(["validate"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_scan_warns_when_the_live_spread_is_far_above_its_median(tmp_path):
    """ЗАЩИТА ОТ РЕШЕНИЯ ПО ЗАКРЫТОМУ РЫНКУ. 2026-08-01 (суббота) живой спред
    золота был 79 против будничной медианы 19 — вчетверо. Скан по таким числам
    объявил бы негодными четыре инструмента из семи, и директор снял бы их с
    понедельника без всякой причины.

    Скан обязан сказать, что смотрит на нерепрезентативный рынок, а не молча
    выдать пессимистичную картину как факт.
    """
    class Wide(_Market):
        """В барах спред узкий (будни), живой — вчетверо шире (рынок закрыт)."""

        def symbol_info(self, symbol):
            return {**super().symbol_info(symbol), "spread": 80}

        def copy_rates(self, symbol, tf, count):
            df = super().copy_rates(symbol, tf, count)
            df["spread"] = 20
            return df

    cfg = _cfg(tmp_path, whitelist=["XAUUSD"])
    row = scan_instruments(Wide(), cfg, now=NOW)[0]
    assert row["spread_vs_median"] == pytest.approx(4.0, abs=0.1)
    assert row["stale_market"] is True
    assert "медиан" in (row["reason"] or "")


def test_scan_does_not_warn_on_a_normal_market(tmp_path):
    cfg = _cfg(tmp_path, whitelist=["XAUUSD"])
    row = scan_instruments(_Market(spread=20), cfg, now=NOW)[0]
    assert row["stale_market"] is False


# --------------------------------------------------------------------------
# НОВОСТИ (Ф9): владелец календаря — директор, а не каждый трейдер
# --------------------------------------------------------------------------

def _news_cache(tmp_path, *events):
    (tmp_path / "news_cache.json").write_text(json.dumps({
        "fetched_utc": NOW.isoformat(), "events": list(events)}), encoding="utf-8")


def _ev(title, currency, at, impact="high"):
    return {"title": title, "currency": currency, "impact": impact,
            "ts_utc": at.isoformat(), "time_known": True}


def test_director_arms_a_warning_before_every_top_event(tmp_path):
    """Сейчас трейдер узнаёт о новости в момент ОТКАЗА гейта — то есть когда
    уже собрал вход и потратил на него решение. Предупреждать обязан директор:
    новости это факт о МИРЕ, общий для всех, как кластеры и медианы спреда.
    Трое, следящих за одним календарём, — тройная работа и тройной расход
    событий на одно и то же."""
    from trader_lib.director import news_alerts

    _news_cache(tmp_path,
                _ev("Non-Farm Employment Change", "USD", NOW + dt.timedelta(hours=3)),
                _ev("Official Bank Rate", "GBP", NOW + dt.timedelta(hours=5)))
    alerts = news_alerts(_cfg(tmp_path), tmp_path, now=NOW, minutes_before=30)

    assert len(alerts) == 2
    a = alerts[0]
    assert a["type"] == "news_window_opens" and a["minutes_before"] == 30
    assert a["once"] is True, "разовое предупреждение, а не звонок каждую минуту"
    assert "USD" in a["note"]


def test_past_events_are_not_armed(tmp_path):
    """Событие, которое уже прошло, будильником быть не может."""
    from trader_lib.director import news_alerts

    _news_cache(tmp_path, _ev("вчерашнее", "USD", NOW - dt.timedelta(hours=2)))
    assert news_alerts(_cfg(tmp_path), tmp_path, now=NOW, minutes_before=30) == []


def test_low_impact_events_are_ignored(tmp_path):
    """Будить команду на событие, которое даже гейт не считает окном, —
    расход без решения."""
    from trader_lib.director import news_alerts

    _news_cache(tmp_path, _ev("мелочь", "USD", NOW + dt.timedelta(hours=2),
                              impact="low"))
    assert news_alerts(_cfg(tmp_path), tmp_path, now=NOW, minutes_before=30) == []


def test_affected_traders_are_named_by_their_instruments(tmp_path):
    """Предупреждение адресное: событие по USD задевает того, у кого доллар в
    паре, а не всех подряд. Иначе трейдер по кроссу без доллара просыпается
    впустую."""
    from trader_lib.director import affected_traders

    alloc = _alloc(gold=_t(["XAUUSD"]), fx=_t(["EURUSD"]), yen=_t(["USDJPY"]))
    assert affected_traders(alloc, {"USD"}) == ["fx", "gold", "yen"]
    assert affected_traders(alloc, {"JPY"}) == ["yen"]
    assert affected_traders(alloc, {"CHF"}) == []


def test_benched_trader_is_not_warned(tmp_path):
    from trader_lib.director import affected_traders

    alloc = _alloc(gold=_t(["XAUUSD"], active=False), yen=_t(["USDJPY"]))
    assert affected_traders(alloc, {"USD"}) == ["yen"]
