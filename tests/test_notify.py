"""Уведомления владельцу счёта (задача 8.1). Всё офлайн.

Уведомление — единственный канал, по которому человек узнаёт о том, что система
сама исправить не может. Отсюда два требования, и они тянут в разные стороны:

  * НЕ ПРОПУСТИТЬ. Молчание при пробитой стене или пропавшем датчике хуже, чем
    лишнее сообщение.
  * НЕ ЗАСПАМИТЬ. Двадцать одинаковых строк про один и тот же orphan за час — это
    и есть способ, которым перестают читать уведомления, а вместе с ними и важные.

Поэтому дедупликация по (тип + ключ) в окне времени, а не по тексту: текст
меняется от снимка к снимку, событие остаётся тем же.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.notify import TRIGGERS, notify, read_notifications, scan
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _state(tmp_path, *, heartbeat_age_s=5, journal=(), equity=10000.0,
           positions=(), excluded=None, stats=None):
    if heartbeat_age_s is not None:
        (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
            "ts": (NOW - dt.timedelta(seconds=heartbeat_age_s)).isoformat(),
            "walls_checked": True, "pending_undelivered": 0}), encoding="utf-8")
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-27", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    (tmp_path / "account_init.json").write_text(json.dumps(
        {"initial_balance": 10000.0}), encoding="utf-8")
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal),
        encoding="utf-8")
    (tmp_path / "spread_median.json").write_text(json.dumps({
        "computed_utc": NOW.isoformat(), "medians": {"XAUUSD": 20.0},
        "excluded": excluded or {}, "samples": {}, "source": {}}), encoding="utf-8")
    if stats is not None:
        (tmp_path / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
    return FakeMarket(account={"balance": 10000.0, "equity": equity},
                      positions=list(positions))


def _pos(ticket=999, sl=2395.0):
    return {"ticket": ticket, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
            "price_open": 2400.0, "sl": sl, "tp": 0.0, "price_current": 2400.0,
            "profit": 0.0, "magic": 0}


# --------------------------------------------------------------------------
# запись и дедупликация
# --------------------------------------------------------------------------

def test_notification_written_with_all_fields(tmp_path):
    rec = notify(tmp_path / "notifications.jsonl", trigger="force_flat",
                 message="стена дня пробита", key="2026-07-27", now=NOW)
    assert rec is not None
    saved = read_notifications(tmp_path / "notifications.jsonl")
    assert len(saved) == 1
    for field in ("ts", "trigger", "message", "key", "severity"):
        assert field in saved[0], field


def test_unknown_trigger_rejected(tmp_path):
    """Список триггеров закрыт: опечатка в имени означала бы, что уведомление
    не найдёт ни дедупликация, ни отчёт."""
    with pytest.raises(ValueError):
        notify(tmp_path / "n.jsonl", trigger="что-то новое", message="?", key="k",
               now=NOW)


def test_dedup_within_window(tmp_path):
    """Один и тот же orphan за час — одно сообщение, а не двадцать."""
    p = tmp_path / "notifications.jsonl"
    first = notify(p, trigger="orphan_position", message="позиция 999", key="999",
                   now=NOW)
    again = notify(p, trigger="orphan_position", message="позиция 999 всё ещё тут",
                   key="999", now=NOW + dt.timedelta(minutes=30))
    assert first is not None and again is None
    assert len(read_notifications(p)) == 1


def test_dedup_expires(tmp_path):
    p = tmp_path / "notifications.jsonl"
    notify(p, trigger="orphan_position", message="a", key="999", now=NOW)
    later = notify(p, trigger="orphan_position", message="b", key="999",
                   now=NOW + dt.timedelta(hours=5))
    assert later is not None and len(read_notifications(p)) == 2


def test_dedup_is_per_key(tmp_path):
    """Разные позиции — разные события, даже если тип один."""
    p = tmp_path / "notifications.jsonl"
    assert notify(p, trigger="orphan_position", message="a", key="111", now=NOW)
    assert notify(p, trigger="orphan_position", message="b", key="222", now=NOW)
    assert len(read_notifications(p)) == 2


def test_broken_log_does_not_lose_new_notification(tmp_path):
    """Битая строка в журнале уведомлений не должна глотать новое сообщение:
    иначе один сбой записи отключает канал насовсем."""
    p = tmp_path / "notifications.jsonl"
    p.write_text('{"trigger": "orphan_position"\n', encoding="utf-8")
    assert notify(p, trigger="orphan_position", message="a", key="1", now=NOW)


# --------------------------------------------------------------------------
# сканирование состояния
# --------------------------------------------------------------------------

def test_heartbeat_loss_notified(tmp_path):
    """Датчик молчит — модель незащищена и об этом обязан узнать человек:
    сама она в этот момент, возможно, тоже спит."""
    market = _state(tmp_path, heartbeat_age_s=None)
    fired = scan(market, _cfg(tmp_path), now=NOW)
    assert "watchdog_lost" in {f["trigger"] for f in fired}


def test_fresh_watchdog_is_quiet(tmp_path):
    market = _state(tmp_path, heartbeat_age_s=5)
    assert "watchdog_lost" not in {f["trigger"] for f in scan(market, _cfg(tmp_path),
                                                              now=NOW)}


def test_force_flat_notified(tmp_path):
    """Пробитая стена — событие для человека, а не только для стоп-крана."""
    market = _state(tmp_path, equity=9300.0)
    triggers = {f["trigger"] for f in scan(market, _cfg(tmp_path), now=NOW)}
    assert "force_flat" in triggers


def test_ladder_step_notified(tmp_path):
    """Ступень лестницы просадки: риск урезан вдвое — владелец счёта должен знать, что
    система перешла в осторожный режим."""
    market = _state(tmp_path, equity=9820.0)   # −1.8% за день
    fired = scan(market, _cfg(tmp_path), now=NOW)
    assert "ladder_step" in {f["trigger"] for f in fired}


def test_orphan_position_notified(tmp_path):
    market = _state(tmp_path, positions=[_pos(ticket=999, sl=0.0)])
    fired = [f for f in scan(market, _cfg(tmp_path), now=NOW)
             if f["trigger"] == "orphan_position"]
    assert fired and "999" in fired[0]["message"]


def test_spread_exclusion_notified(tmp_path):
    market = _state(tmp_path, excluded={"XAUUSD": {
        "since": NOW.isoformat(), "ratio": 2.1, "median": 20.0}})
    fired = {f["trigger"] for f in scan(market, _cfg(tmp_path), now=NOW)}
    assert "spread_anomaly" in fired


def test_execution_errors_notified_only_when_repeated(tmp_path):
    """Одна неудача исполнения — рынок. Три подряд — что-то не так с системой
    или счётом, и это уже вопрос к человеку."""
    def event(i, ok):
        return {"type": "alert_event", "ts": (NOW - dt.timedelta(minutes=i)).isoformat(),
                "alert_id": f"sv-{i}", "alert_type": "position_without_sl",
                "model_id": "claude-opus-5", "delivered": True,
                "action": {"rule": "position_without_sl", "done": "closed",
                           "close": {"ok": ok}}}

    one = _state(tmp_path, journal=[event(1, False)])
    assert "execution_errors" not in {f["trigger"] for f in scan(one, _cfg(tmp_path),
                                                                 now=NOW)}
    many = _state(tmp_path, journal=[event(1, False), event(2, False), event(3, False)])
    assert "execution_errors" in {f["trigger"] for f in scan(many, _cfg(tmp_path),
                                                             now=NOW)}


def test_tactic_degradation_notified(tmp_path):
    """Подтверждённая тактика ушла в минус на достаточной выборке — повод
    посмотреть человеку, а не тихо продолжать."""
    stats = {"by_setup": {"ema_pullback": {"n": 25, "wr": 0.32, "avg_R": -0.4,
                                           "sum_R": -10.0, "insufficient": False,
                                           "edge_significant": False}}}
    market = _state(tmp_path, stats=stats)
    fired = [f for f in scan(market, _cfg(tmp_path), now=NOW)
             if f["trigger"] == "tactic_degraded"]
    assert fired and "ema_pullback" in fired[0]["message"]


def test_small_sample_is_not_degradation(tmp_path):
    stats = {"by_setup": {"new_idea": {"n": 4, "wr": 0.25, "avg_R": -0.6,
                                       "sum_R": -2.4, "insufficient": True,
                                       "edge_significant": None}}}
    market = _state(tmp_path, stats=stats)
    assert "tactic_degraded" not in {f["trigger"] for f in scan(market, _cfg(tmp_path),
                                                                now=NOW)}


def test_each_trigger_emits_once(tmp_path):
    """Повторный скан того же состояния не плодит сообщений."""
    market = _state(tmp_path, heartbeat_age_s=None, equity=9300.0,
                    positions=[_pos(ticket=999, sl=0.0)])
    cfg = _cfg(tmp_path)
    first = scan(market, cfg, now=NOW)
    again = scan(market, cfg, now=NOW + dt.timedelta(minutes=5))
    assert first and again == []
    saved = read_notifications(tmp_path / "notifications.jsonl")
    assert len(saved) == len(first)


def test_scan_survives_missing_state(tmp_path):
    """Пустой state_dir — не повод падать: скан обязан отработать и сказать,
    чего не хватает."""
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    fired = scan(market, _cfg(tmp_path), now=NOW)
    assert "watchdog_lost" in {f["trigger"] for f in fired}


def test_triggers_are_documented():
    """Список закрыт и совпадает с планом задачи 8.1."""
    assert set(TRIGGERS) == {
        "ladder_step", "force_flat", "spread_anomaly", "data_stale",
        "execution_errors", "orphan_position", "watchdog_lost",
        "tactic_degraded", "constitution_change_needed"}


# ======================= командный режим: куда пишет report =====================
# БАГ, найденный трейдером `range` 2026-08-03 на первом живом дне команды.
# report.py брал ОБЩИЙ state_dir и не знал про traders/<имя>/. Наблюдение о
# пробуждении уходило в общий журнал, review.py --trader его не видел, и
# разбудившее условие к вечеру выглядело НЕОТВЕЧЕННЫМ — то есть портилась ровно
# та метрика, ради которой наблюдения и пишутся, причём в сторону «алерт
# бесполезен»: она подталкивала снять работающее условие.
#
# Тот же класс, что декоративный флаг --trader в пяти скриптах: код не обновили
# под командный режим, а тесты не поймали, потому что проверяли одиночный.

def test_наблюдение_трейдера_пишется_в_ЕГО_журнал(tmp_path):
    from scripts.report import observed
    from trader_lib.workspace import trader_state_dir

    cfg = _cfg(tmp_path)
    trader_state_dir(cfg, "range", create=True)
    observed(cfg, trader="range", symbol="USDJPY", alert_id="range-spread-watch",
             alert_type="spread_anomaly", level=1.5, price=156.1,
             regime="тренд", reasoning="выброс спреда был одиночным тиком")

    личный = tmp_path / "traders" / "range" / "journal.jsonl"
    assert личный.exists(), "наблюдение не попало в журнал трейдера"
    rec = json.loads(личный.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["type"] == "observation"
    assert rec["alert_id"] == "range-spread-watch"
    общий = tmp_path / "journal.jsonl"
    assert not общий.exists(), "наблюдение продублировалось в общий журнал"


def test_без_трейдера_поведение_прежнее(tmp_path):
    """Одиночный режим не должен сломаться от появления командного."""
    from scripts.report import observed

    cfg = _cfg(tmp_path)
    observed(cfg, symbol="XAUUSD", alert_id="a1", alert_type="price_above",
             level=4100.0, price=4101.0, regime="тренд", reasoning="пробой")
    rec = json.loads((tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["type"] == "observation"


def test_телеграм_остаётся_общим_у_всей_команды(tmp_path):
    """Канал у команды один, и настройки лежат в общих файлах. Личная очередь
    означала бы, что часть сообщений уходит в никуда при смене трейдера."""
    from scripts.report import observed
    from trader_lib.workspace import trader_state_dir

    cfg = _cfg(tmp_path)
    trader_state_dir(cfg, "fade", create=True)
    (tmp_path / "telegram.json").write_text(
        json.dumps({"enabled": True, "token": "t", "chat_id": "c",
                    "kinds": {"wake": True}}), encoding="utf-8")

    sent = []
    observed(cfg, trader="fade", symbol="EURUSD", alert_id="f0", alert_type="spread_anomaly",
             level=2.5, price=1.1525, regime="дрейф", reasoning="книга плотная",
             sender=lambda *a, **k: sent.append(a) or {"ok": True})
    outbox = list(tmp_path.glob("telegram_outbox.jsonl")) + list(
        (tmp_path / "traders" / "fade").glob("telegram_outbox.jsonl"))
    личных = [p for p in outbox if "traders" in str(p)]
    assert not личных, "очередь телеграма не должна быть личной"


# ==================== канал директора (просьба 2026-08-03) ====================
# Владелец счёта видел решения трейдеров (`wake`) и сделки (`enter`/`exit`), но
# не видел, на каких числах директор маршрутизировал событие и почему.

def test_рассуждение_директора_уходит_в_канал(tmp_path):
    from scripts.report import director

    cfg = _cfg(tmp_path)
    (tmp_path / "telegram.json").write_text(
        json.dumps({"enabled": True, "token": "t", "chat_id": "c"}), encoding="utf-8")
    отправлено = []
    res = director(cfg, title="Событие range-vol-decay",
                   body="волатильность угасает",
                   facts=["atr_pctile 0.11 при уровне 0.85", "спред вернулся за 90 с"],
                   decision="маршрутизирую трейдеру, вход не рекомендую",
                   sender=lambda t: отправлено.append(t) or {"ok": True})
    assert res["sent"], res["reason"]
    text = отправлено[0]
    assert "range-vol-decay" in text
    assert "0.11" in text and "90" in text, "факты обязаны быть в сообщении"
    assert "Решение" in text and "не рекомендую" in text


def test_факты_и_решение_различимы_в_тексте(tmp_path):
    """Ошибку в данных и ошибку в рассуждении лечат по-разному — надзор
    возможен только если по каналу видно, где что."""
    from trader_lib.telegram import director as fmt

    text = fmt(now=NOW, title="t", body=None, facts=["ADX 14.8"],
               decision="входа нет")
    факт_строка = [ln for ln in text.splitlines() if "ADX" in ln][0]
    решение_строка = [ln for ln in text.splitlines() if "входа нет" in ln][0]
    assert факт_строка.startswith("·"), "факт помечен маркером"
    assert "Решение" in решение_строка, "решение помечено словом"


def test_директор_пишет_в_общий_корень_а_не_в_папку_трейдера(tmp_path):
    """У директора нет личной папки: его рассуждение адресовано человеку, а не
    статистике сетапов, и в журнал сделок попадать не должно."""
    from scripts.report import director

    cfg = _cfg(tmp_path)
    director(cfg, title="проверка", body="тело", sender=lambda t: {"ok": True})
    assert not (tmp_path / "journal.jsonl").exists(), \
        "рассуждение директора не имеет права попадать в журнал сделок"
    assert (tmp_path / "logs").is_dir(), "но в лог событий — обязано"
