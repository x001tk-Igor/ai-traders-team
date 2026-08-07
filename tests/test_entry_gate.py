"""Единый предвходовой гейт (задача 5.4). Всё офлайн.

Этот модуль — оркестратор: своих правил у него нет, он вызывает восемь уже
написанных проверок в фиксированном порядке и складывает их вердикты. Поэтому
главные тесты здесь — не «правильно ли считает спред» (это тесты spread_gate),
а:

  * КАЖДАЯ проверка умеет запретить вход (параметризованный тест по всем
    восьми) — оркестратор, который забыл посмотреть на одну из них, выглядит
    работающим ровно до того дня, когда именно она должна была сработать;
  * ЛЮБАЯ внутренняя ошибка = запрет (fail-closed): исключение внутри проверки
    не имеет права стать «проверка промолчала, значит можно»;
  * причины пригодны для журнала — их читает человек через месяц.
"""
import dataclasses
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trader_lib.config import load_config
from trader_lib.constitution import HASH_FILE, write_ack
from trader_lib.model_session import SESSION_FILE, declare
from trader_lib.entry_gate import CHECKS, check_entry
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
# понедельник 13:00 UTC — фаза NY, торговое окно открыто
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
SERVER_OFFSET_H = 3


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _bars(n=400, spread=20):
    end = (NOW + dt.timedelta(hours=SERVER_OFFSET_H)).replace(tzinfo=None)
    t = pd.date_range(end=end, periods=n, freq="5min")
    c = 2400.0 + np.arange(n) * 0.01
    return pd.DataFrame({"time": t, "open": c, "high": c + 0.5, "low": c - 0.5,
                         "close": c, "tick_volume": 200, "spread": spread})


class Market(FakeMarket):
    def __init__(self, *, spread=20, equity=10000.0, positions=None):
        super().__init__(bars=_bars(spread=spread),
                         account={"balance": 10000.0, "equity": equity},
                         positions=list(positions or []))
        self._spread = spread

    def symbol_info(self, symbol):
        return {**super().symbol_info(symbol), "spread": self._spread}


def _state(tmp_path, *, heartbeat_age_s=5, news=None, medians=True, ack=True):
    """Файлы состояния, которые читает гейт: подтверждение конституции, пульс
    датчика, кэш новостей, медианы спреда, базы equity."""
    if ack:
        # конституция подтверждена: без этого гейт запрещает вход первым же
        # шагом (задача 8.2), и остальные проверки не проверялись бы вовсе
        write_ack(tmp_path / HASH_FILE,
                  config_dict=json.loads(
                      Path("config/trader.config.json").read_text(encoding="utf-8")),
                  now=NOW)
        # модель объявлена этим сеансом: иначе гейт запрещает вход вторым шагом
        # (идентичность, 2026-07-27) — неверный model_id портит калибровку
        declare(tmp_path, model_id="claude-opus-5", profile="strong", now=NOW,
                session_id=None)
    (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
        "ts": (NOW - dt.timedelta(seconds=heartbeat_age_s)).isoformat(),
        "walls_checked": True, "pending_undelivered": 0}), encoding="utf-8")
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-27", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    (tmp_path / "account_init.json").write_text(json.dumps({"initial_balance": 10000.0}),
                                                encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps({
        "fetched_utc": NOW.isoformat(), "events": news or []}), encoding="utf-8")
    if medians:
        (tmp_path / "spread_median.json").write_text(json.dumps({
            "computed_utc": NOW.isoformat(), "medians": {"XAUUSD": 20.0},
            "excluded": {}, "samples": {}, "source": {"XAUUSD": "bars"}}),
            encoding="utf-8")


def _call(tmp_path, market=None, cfg=None, **over):
    kw = {"symbol": "XAUUSD", "side": "buy", "entry": 2400.0, "sl": 2395.0,
          "rr": 2.4, "setup_status": "подтверждён", "p_win_journal": None,
          "planned": True, "now": NOW}
    kw.update(over)
    return check_entry(market=market or Market(), cfg=cfg or _cfg(tmp_path), **kw)


# --------------------------------------------------------------------------
# зелёный путь
# --------------------------------------------------------------------------

def test_all_green_allows(tmp_path):
    _state(tmp_path)
    res = _call(tmp_path)
    assert res["allow"] is True, res["reasons"]
    assert res["max_risk_usd"] > 0
    assert set(res["checks"]) == set(CHECKS), "в отчёте обязаны быть ВСЕ проверки"
    assert all(c["ok"] for c in res["checks"].values())


def test_result_carries_journal_fields(tmp_path):
    """Гейт — источник механических полей записи решения: enter.py берёт их
    отсюда, а не считает заново."""
    _state(tmp_path)
    res = _call(tmp_path)
    for field in ("session_phase", "news_check", "spread_at_entry",
                  "correlation_check", "daily_risk_remaining_usd", "verdict"):
        assert field in res, field
    assert res["session_phase"] == "NY" and res["spread_at_entry"] == 20.0


# --------------------------------------------------------------------------
# каждая проверка умеет запретить
# --------------------------------------------------------------------------

def _deny_setup(name, tmp_path):
    """Готовит мир так, чтобы запретила ровно проверка name."""
    _state(tmp_path)
    if name == "constitution":
        # подтверждения нет вовсе — первый запуск на новом ПК
        (tmp_path / HASH_FILE).unlink()
        return {}, {}, {}
    if name == "identity":
        # модель не объявилась: записи подписывались бы значением из конфига,
        # и статистика двух моделей смешалась бы молча
        (tmp_path / SESSION_FILE).unlink()
        return {}, {}, {}
    if name == "session":
        return {}, {}, {"now": dt.datetime(2026, 8, 1, 13, 0, tzinfo=UTC)}  # суббота
    if name == "instrument":
        return {}, {}, {"symbol": "EURJPY"}                       # вне whitelist
    if name == "spread":
        return {"spread": 40}, {}, {}                             # ×2 от медианы
    if name == "news":
        (tmp_path / "news_cache.json").write_text(json.dumps({
            "fetched_utc": NOW.isoformat(),
            "events": [{"title": "Non-Farm Employment Change", "currency": "USD",
                        "impact": "high", "ts_utc": NOW.isoformat(),
                        "time_known": True}]}), encoding="utf-8")
        return {}, {}, {}
    if name == "risk":
        return {"equity": 9700.0}, {}, {}                         # стена дня пробита
    if name == "exposure":
        positions = [{"ticket": t, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
                      "price_open": 2400.0, "sl": 2395.0, "tp": 0.0,
                      "price_current": 2400.0, "profit": 0.0, "magic": 0}
                     for t in (1, 2, 3)]
        return {"positions": positions}, {}, {}                   # max_open_positions
    if name == "mandate":
        # директор выдал trend мандат только на XAUUSD; вход по EURUSD — уход
        # на чужой инструмент, ровно та болезнь человеческих команд
        (tmp_path / "allocation.json").write_text(json.dumps({
            "server_day": "2026-07-27",
            "traders": {"trend": {"instruments": ["XAUUSD"], "risk_share": 0.4,
                                  "active": True}}}, ensure_ascii=False),
            encoding="utf-8")
        return {}, {}, {"symbol": "EURUSD", "trader": "trend"}
    if name == "cluster":
        # BTCUSD уже открыт; вход по ETHUSD — тот же кластер риска (+0.865),
        # то есть одна ставка под видом двух
        from trader_lib.clusters import save_clusters
        save_clusters(tmp_path / "clusters.json", {
            "groups": [["AUDUSD", "EURUSD", "GBPUSD", "USDCHF"],
                       ["BTCUSD", "ETHUSD"], ["USDCAD"], ["USDJPY"], ["XAUUSD"]],
            "threshold": 0.7, "insufficient": [], "computed_utc": NOW.isoformat()})
        held = [{"ticket": 1, "symbol": "BTCUSD", "type": 0, "volume": 0.1,
                 "price_open": 2400.0, "sl": 2395.0, "tp": 0.0,
                 "price_current": 2400.0, "profit": 0.0, "magic": 0}]
        return {"positions": held}, {}, {"symbol": "ETHUSD"}
    if name == "quality":
        return {}, {}, {"rr": 0.05}                               # безубыток недостижим
    if name == "heartbeat":
        _state(tmp_path, heartbeat_age_s=600)                     # датчик слеп
        return {}, {}, {}
    raise AssertionError(f"нет сценария для проверки {name}")


@pytest.mark.parametrize("check", CHECKS)
def test_each_check_can_deny(check, tmp_path):
    """Оркестратор, забывший посмотреть на одну проверку, выглядит рабочим
    ровно до того дня, когда именно она должна была сработать."""
    market_kw, cfg_kw, call_kw = _deny_setup(check, tmp_path)
    res = _call(tmp_path, market=Market(**market_kw),
                cfg=_cfg(tmp_path, **cfg_kw) if cfg_kw else None, **call_kw)
    assert res["allow"] is False, f"{check} не запретила: {res['reasons']}"
    assert res["checks"][check]["ok"] is False, f"запретила не {check}: {res['checks']}"


# --------------------------------------------------------------------------
# fail-closed
# --------------------------------------------------------------------------

def test_exception_fails_closed(tmp_path, monkeypatch):
    """Исключение внутри проверки не имеет права стать «промолчала, значит
    можно».

    Ломается ИМЕННО та проверка, у которой нет собственного обработчика:
    сломать symbol_info недостаточно — spread_gate поймает это сам и вернёт
    честный запрет, и тест прошёл бы, даже если общий обработчик entry_gate
    превращал ошибку в разрешение (так и было: мутация G2 выживала).
    """
    import trader_lib.entry_gate as eg

    _state(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("календарь взорвался")

    monkeypatch.setattr(eg, "_check_news", boom)
    res = _call(tmp_path)
    assert res["allow"] is False
    assert res["checks"]["news"]["ok"] is False
    assert any("календарь взорвался" in r for r in res["reasons"])


def test_missing_heartbeat_denies(tmp_path):
    """Всё остальное зелено, но датчик пробуждения не запущен: без стоп-крана
    входить нельзя. Общий тест «нет файлов состояния» это не покрывал — там
    запрещали другие проверки."""
    _state(tmp_path)
    (tmp_path / "watch_heartbeat.json").unlink()
    res = _call(tmp_path)
    assert res["allow"] is False and res["checks"]["heartbeat"]["ok"] is False
    assert any("датчик" in r for r in res["reasons"])


def test_denied_entry_gives_zero_risk(tmp_path):
    """При запрете риск обязан быть нулевым: вызывающий, который прочитает
    max_risk_usd и проигнорирует allow, не должен получить из ответа
    разрешение."""
    _state(tmp_path)
    res = _call(tmp_path, market=Market(spread=40))   # аномальный спред
    assert res["allow"] is False and res["max_risk_usd"] == 0.0


def test_unplanned_denied_when_daily_limit_spent(tmp_path):
    """Лимит внеплановых входов на день исчерпан: гейт возвращает
    planned_only, и внеплановый вход обязан быть отклонён именно здесь —
    сам риск-гейт торговлю при этом не останавливает."""
    _state(tmp_path)
    (tmp_path / "journal.jsonl").write_text(json.dumps({
        "type": "decision", "ts": (NOW - dt.timedelta(hours=2)).isoformat(),
        "trade_id": "1", "symbol": "XAUUSD", "side": "buy", "risk_usd": 50.0,
        "planned": False, "confidence": 0.5, "setup_type": "s",
        "model_id": "claude-opus-5"}) + "\n", encoding="utf-8")

    planned = _call(tmp_path, planned=True)
    assert planned["allow"] is True, planned["reasons"]

    unplanned = _call(tmp_path, planned=False)
    assert unplanned["allow"] is False
    assert any("запланированные" in r for r in unplanned["reasons"])


def test_gate_never_touches_network(tmp_path, monkeypatch):
    """Вход не имеет права ждать сетевого таймаута: календарь обновляет цикл
    восприятия, а гейт читает только кэш. Кэш здесь СПЕЦИАЛЬНО устаревший —
    на свежем этот тест был бы слеп (загрузчик и так не вызывается)."""
    import trader_lib.news as news

    _state(tmp_path)
    (tmp_path / "news_cache.json").write_text(json.dumps({
        "fetched_utc": (NOW - dt.timedelta(days=3)).isoformat(), "events": []}),
        encoding="utf-8")

    # факт вызова фиксируем списком, а не исключением: load_windows глотает
    # ЛЮБУЮ ошибку загрузчика и трактует её как «нет сети», поэтому
    # AssertionError внутри него был бы не виден — на этом мутация выживала
    calls = []

    def forbidden(*a, **k):
        calls.append(1)
        raise RuntimeError("нет сети")

    monkeypatch.setattr(news, "_fetch", forbidden)
    res = _call(tmp_path)
    assert calls == [], "гейт полез в сеть за календарём"
    # устаревший календарь при fail_mode=halt_new — запрет, но БЕЗ похода в сеть
    assert res["allow"] is False and res["checks"]["news"]["ok"] is False


def test_missing_state_files_fail_closed(tmp_path):
    """Ни пульса, ни баз, ни календаря — вход запрещён, а не «проверять
    нечего»."""
    res = _call(tmp_path)
    assert res["allow"] is False and res["reasons"]


def test_stale_heartbeat_denies(tmp_path):
    """Правило контура: пульс защиты старше 90 с → модель незащищена, новых
    входов нет. Это не косметика: значит стоп-кран не считает стену."""
    _state(tmp_path, heartbeat_age_s=120)
    res = _call(tmp_path)
    assert res["allow"] is False
    assert res["checks"]["heartbeat"]["ok"] is False
    assert any("90" in r or "пульс" in r.lower() for r in res["reasons"])


def test_walls_not_checked_denies_even_if_fresh(tmp_path):
    """Свежая метка при walls_checked=false — датчик жив, а защита нет."""
    (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
        "ts": NOW.isoformat(), "walls_checked": False, "pending_undelivered": 0}),
        encoding="utf-8")
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-27", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    res = _call(tmp_path)
    assert res["allow"] is False and res["checks"]["heartbeat"]["ok"] is False


def test_pending_undelivered_denies(tmp_path):
    """С деньгами уже что-то сделано, а модели ещё не рассказали — входить
    поверх нерассказанного нельзя."""
    _state(tmp_path)
    (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
        "ts": NOW.isoformat(), "walls_checked": True, "pending_undelivered": 1}),
        encoding="utf-8")
    res = _call(tmp_path)
    assert res["allow"] is False and res["checks"]["heartbeat"]["ok"] is False


# --------------------------------------------------------------------------
# риск и требования к статусу
# --------------------------------------------------------------------------

def test_max_risk_comes_from_gate(tmp_path):
    _state(tmp_path)
    res = _call(tmp_path)
    assert res["max_risk_usd"] == pytest.approx(res["checks"]["risk"]["max_risk_usd"])


def test_require_setup_status_propagated(tmp_path):
    """Гейт может потребовать подтверждённый сетап (ступень лестницы просадки).
    Требование обязано долетать до вызывающего, а не теряться."""
    _state(tmp_path)
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-27", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    res = _call(tmp_path, market=Market(equity=9840.0))   # −1.6% за день
    assert res["require_setup_status"] == "confirmed"


def test_unconfirmed_setup_denied_when_confirmation_required(tmp_path):
    _state(tmp_path)
    res = _call(tmp_path, market=Market(equity=9840.0), setup_status="изучаю")
    assert res["allow"] is False
    assert any("подтвержд" in r for r in res["reasons"])


def test_reasons_are_journalable(tmp_path):
    res = _call(tmp_path)   # без файлов состояния — запрет с причинами
    assert res["reasons"] and all(isinstance(r, str) and len(r) > 10
                                  for r in res["reasons"])


def test_gate_uses_live_spread_window_when_the_sensor_collected_one(tmp_path):
    """Ф1, замыкание цепи: живое окно собирает ДАТЧИК, а применять его обязан
    ГЕЙТ в момент входа — иначе вся сборка не доезжает до реального решения.

    Сценарий разводит две базы далеко, чтобы тест проверял именно живую:
    барная медиана 19 (спред на закрытии свечи, тихий момент), а сессия идёт
    со структурно широким спредом 30. Против барной это ×1.58 — выше порога
    аномалии ×1.5, вход отклоняется. Против живой это ×1.0 — нормальные
    условия сессии, вход проходит. Настоящий выброс 90 отклоняется в обоих.
    """
    from trader_lib.entry_gate import _check_spread
    from trader_lib.spread_gate import LiveSpreadWindow

    sd = tmp_path
    (sd / "spread_median.json").write_text(json.dumps({
        "medians": {"XAUUSD": 19.0}, "excluded": {}, "samples": {},
        "source": {"XAUUSD": "bars"}, "computed_utc": NOW.isoformat()},
    ), encoding="utf-8")

    live = LiveSpreadWindow(minutes=60)
    for _ in range(120):
        live.observe("XAUUSD", 30, now=NOW)

    market = Market(spread=30)
    without = _check_spread(market, _cfg(tmp_path), sd, "XAUUSD", NOW)
    assert without["ok"] is False, "на барной базе ×1.58 обязан блокироваться"

    with_live = _check_spread(market, _cfg(tmp_path), sd, "XAUUSD", NOW, live=live)
    assert with_live["ok"] is True, with_live["reason"]
    assert with_live["median"] == 30.0, "база обязана быть живой"

    spike = _check_spread(Market(spread=90), _cfg(tmp_path), sd, "XAUUSD", NOW,
                          live=live)
    assert spike["ok"] is False, "настоящий выброс отклоняется и на живой базе"


# --------------------------------------------------------------------------
# КЛАСТЕРНАЯ ПРОВЕРКА (Ф2): команда не должна собирать одну ставку втроём
# --------------------------------------------------------------------------

def _clusters_file(tmp_path):
    """Карта, посчитанная из живых данных 2026-08-01 (порог 0.7, n=499)."""
    from trader_lib.clusters import save_clusters
    save_clusters(tmp_path / "clusters.json", {
        "groups": [["AUDUSD", "EURUSD", "GBPUSD", "USDCHF"],
                   ["BTCUSD", "ETHUSD"], ["USDCAD"], ["USDJPY"], ["XAUUSD"]],
        "threshold": 0.7, "insufficient": [], "computed_utc": NOW.isoformat()})


def _pos(ticket, symbol, side="buy", *, price=2400.0, sl=2395.0):
    return {"ticket": ticket, "symbol": symbol, "type": 0 if side == "buy" else 1,
            "volume": 0.1, "price_open": price, "sl": sl, "tp": 0.0,
            "price_current": price, "profit": 0.0, "magic": 0}


def test_second_position_in_the_same_cluster_is_rejected(tmp_path):
    """БОЕВОЙ СЦЕНАРИЙ СКУЧИВАНИЯ. Трейдер T1 держит лонг BTCUSD; трейдер T2
    хочет лонг ETHUSD. Символы разные, направления «независимые» — а
    корреляция +0.865, то есть это одна ставка с двойным риском.

    Так скучиваются человеческие команды: все увидели одно движение и
    набросились. Проверка обязана быть в коде, а не в инструкции директора —
    инструкции нарушаются, проверки нет.
    """
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    market = Market(positions=[_pos(1, "BTCUSD")])
    res = _check_cluster(market, _cfg(tmp_path), tmp_path, "ETHUSD", "buy")
    assert res["ok"] is False
    assert "BTCUSD" in res["reason"] and "ETHUSD" in res["reason"]


def test_opposite_side_in_the_same_cluster_is_rejected_too(tmp_path):
    """Встречные позиции внутри кластера — самый дорогой способ ничего не
    заработать: нетто-экспозиция ноль, спред платится дважды."""
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    market = Market(positions=[_pos(1, "EURUSD", "buy")])
    res = _check_cluster(market, _cfg(tmp_path), tmp_path, "USDCHF", "buy")
    assert res["ok"] is False, "EURUSD ~ USDCHF = -0.87, это один доллар"


def test_different_clusters_pass(tmp_path):
    """Настоящая диверсификация проходит: золото не коррелирует ни с чем
    (|corr| ≤ 0.11), крипта с форексом тоже (≤ 0.11)."""
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    market = Market(positions=[_pos(1, "BTCUSD")])
    res = _check_cluster(market, _cfg(tmp_path), tmp_path, "XAUUSD", "sell")
    assert res["ok"] is True, res["reason"]


def test_adding_to_the_same_symbol_is_rejected_as_same_cluster(tmp_path):
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    market = Market(positions=[_pos(1, "XAUUSD")])
    res = _check_cluster(market, _cfg(tmp_path), tmp_path, "XAUUSD", "buy")
    assert res["ok"] is False


def test_unknown_symbol_is_blocked_not_waved_through(tmp_path):
    """Инструмента нет в карте — значит про его риск ничего не известно.
    Пропустить его значило бы прочитать незнание как безопасность."""
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    market = Market(positions=[_pos(1, "BTCUSD")])
    res = _check_cluster(market, _cfg(tmp_path), tmp_path, "SILVER", "buy")
    assert res["ok"] is False
    assert "карте" in res["reason"]


def test_no_open_positions_always_passes(tmp_path):
    from trader_lib.entry_gate import _check_cluster

    _clusters_file(tmp_path)
    res = _check_cluster(Market(), _cfg(tmp_path), tmp_path, "EURUSD", "buy")
    assert res["ok"] is True


def test_missing_cluster_map_does_not_block_everything(tmp_path):
    """Карты ещё нет (первый запуск) — проверка честно сообщает, что не
    выполнялась, но не запрещает торговлю: иначе контур встал бы до первого
    пересчёта. Риск при этом держат остальные проверки гейта."""
    from trader_lib.entry_gate import _check_cluster

    res = _check_cluster(Market(positions=[_pos(1, "BTCUSD")]),
                         _cfg(tmp_path), tmp_path, "EURUSD", "buy")
    assert res["ok"] is True
    assert "карта кластеров" in res["reason"]


def test_cluster_check_is_actually_wired_into_check_entry(tmp_path):
    """СТОРОЖ ЖИВОСТИ. За неделю дважды находил код, который написан, покрыт
    тестами и НИКЕМ НЕ ВЫЗЫВАЕТСЯ: update_medians (медиана спреда застыла на
    48 часов) и net_currency_exposure (валютная раскладка, мёртвая с рождения).
    Проверка кластера обязана быть в конвейере check_entry, а не только в
    своей функции — иначе она повторит их судьбу.
    """
    from trader_lib.entry_gate import CHECKS, check_entry

    assert "cluster" in CHECKS, "проверка не включена в порядок конвейера"

    _clusters_file(tmp_path)
    _state(tmp_path)
    market = Market(positions=[_pos(1, "BTCUSD")])
    res = check_entry(market=market, cfg=_cfg(tmp_path), state=tmp_path,
                      symbol="ETHUSD", side="buy", entry=2400.0, sl=2395.0,
                      rr=2.0, setup_status="изучаю", p_win_journal=None,
                      planned=True, now=NOW)
    assert res["allow"] is False
    assert any("кластер" in r for r in res["reasons"]), res["reasons"]


# --------------------------------------------------------------------------
# МАНДАТЫ (Ф4): чужой инструмент не берётся, даже если там движение
# --------------------------------------------------------------------------

def _allocation(tmp_path, doc=None):
    (tmp_path / "allocation.json").write_text(json.dumps(doc or {
        "server_day": "2026-07-27",
        "traders": {
            "trend": {"instruments": ["XAUUSD"], "risk_share": 0.4, "active": True},
            "fade": {"instruments": ["EURUSD"], "risk_share": 0.35, "active": True},
        }}, ensure_ascii=False), encoding="utf-8")


def test_mandate_check_is_wired_into_check_entry(tmp_path):
    """СТОРОЖ ЖИВОСТИ. Третий раз за неделю: update_medians, потом
    net_currency_exposure — оба написаны, покрыты тестами и не вызывались
    ниоткуда. Проверка мандата обязана стоять в конвейере, а не только
    существовать."""
    from trader_lib.entry_gate import CHECKS

    assert "mandate" in CHECKS


def test_trader_cannot_take_a_symbol_outside_his_mandate(tmp_path):
    from trader_lib.entry_gate import _check_mandate

    _allocation(tmp_path)
    res = _check_mandate(_cfg(tmp_path), tmp_path, "EURUSD", trader="trend", now=NOW)
    assert res["ok"] is False and "мандат" in res["reason"]


def test_trader_inside_his_mandate_passes(tmp_path):
    from trader_lib.entry_gate import _check_mandate

    _allocation(tmp_path)
    res = _check_mandate(_cfg(tmp_path), tmp_path, "XAUUSD", trader="trend", now=NOW)
    assert res["ok"] is True, res["reason"]


def test_solo_mode_ignores_mandates(tmp_path):
    from trader_lib.entry_gate import _check_mandate

    _allocation(tmp_path)
    res = _check_mandate(_cfg(tmp_path), tmp_path, "GBPUSD", trader=None, now=NOW)
    assert res["ok"] is True


def test_allocation_share_caps_the_risk_the_gate_hands_out(tmp_path):
    """Доля риска обязана применяться к тому, что гейт РЕАЛЬНО выдаёт наружу,
    а не жить отдельной функцией. Директор, чьё решение не доезжает до
    max_risk_usd, распределяет бюджет только на бумаге."""
    _state(tmp_path)
    (tmp_path / "allocation.json").write_text(json.dumps({
        "server_day": "2026-07-27",
        "traders": {"trend": {"instruments": ["XAUUSD"], "risk_share": 0.01,
                              "active": True}}}, ensure_ascii=False),
        encoding="utf-8")

    from trader_lib.entry_gate import check_entry

    res = check_entry(market=Market(), cfg=_cfg(tmp_path), state=tmp_path,
                      symbol="XAUUSD", side="buy", entry=2400.0, sl=2395.0,
                      rr=2.0, setup_status="изучаю", p_win_journal=None,
                      planned=True, now=NOW, trader="trend")
    assert res["allow"] is True, res["reasons"]
    solo = check_entry(market=Market(), cfg=_cfg(tmp_path), state=tmp_path,
                       symbol="XAUUSD", side="buy", entry=2400.0, sl=2395.0,
                       rr=2.0, setup_status="изучаю", p_win_journal=None,
                       planned=True, now=NOW)
    assert res["max_risk_usd"] < solo["max_risk_usd"], (
        "доля 1% дневного бюджета обязана урезать выдаваемый риск")
