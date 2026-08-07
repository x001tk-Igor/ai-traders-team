from scripts.close_watch import find_orphans, reconcile
from trader_lib.journal import append_decision, read_records


def _pos(ticket, symbol="XAUUSD", ptype=0, volume=0.05, price_open=2400.0, sl=2390.0, tp=0.0):
    """Позиция брокера в формате mt5_client.positions() (см. trader_lib/
    mt5_client.py: ticket/symbol/type/volume/price_open/sl/tp). sl=0.0 —
    позиция без стоп-лосса (MT5-соглашение, не None и не отсутствующий ключ)."""
    return {"ticket": ticket, "symbol": symbol, "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": tp, "price_current": price_open,
            "profit": 0.0, "magic": 1}


def test_reconcile_writes_outcome(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    # append_decision (строгий, единственный путь записи) + make_decision:
    # этот тест проверяет reconcile()/close_watch, не состав журнала
    # (задача 2.1) — фабрика убирает шум из 28 полей, не ослабляет проверку.
    append_decision(j, make_decision(trade_id="a1", risk_usd=40,
                                     entry=2634.0, sl=2631.0))
    deals = [{"position_id": "a1", "profit": 72.0, "price": 2639.4,
              "time": 1735700000, "entry": 1}]  # entry=1 → выход
    n = reconcile(j, deals_by_pos={"a1": deals})
    assert n == 1
    recs = read_records(j)
    out = [r for r in recs if r["type"] == "outcome"][0]
    assert out["trade_id"] == "a1"
    assert round(out["R"], 2) == 1.8  # 72/40


def test_reconcile_idempotent(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40))
    deals = {"a1": [{"position_id": "a1", "profit": 40.0, "price": 2639.0, "entry": 1}]}
    assert reconcile(j, deals) == 1
    assert reconcile(j, deals) == 0  # второй раз не дублирует


# ================================ find_orphans ================================


def test_orphan_position_detected(tmp_path):
    # журнал пуст (файла ещё нет) — позиция у брокера есть, decision-записи нет
    j = tmp_path / "journal.jsonl"
    orphans = find_orphans(j, [_pos(555111, symbol="XAUUSD")])
    assert len(orphans) == 1
    assert orphans[0]["ticket"] == 555111
    assert orphans[0]["symbol"] == "XAUUSD"


def test_no_orphans_when_matched(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="555111"))
    orphans = find_orphans(j, [_pos(555111, symbol="XAUUSD")])
    assert orphans == []


def test_orphans_empty_when_no_positions(tmp_path, make_decision):
    # позиций у брокера нет вообще — расхождений быть не может независимо от
    # содержимого журнала
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="555111"))
    assert find_orphans(j, []) == []


def test_decision_without_outcome_is_not_orphan(tmp_path, make_decision):
    # сделка открыта и учтена в журнале (decision есть) — outcome для открытой
    # позиции ещё не появится никогда (пока она не закрыта), и это НЕ orphan
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="777222"))
    recs = read_records(j)
    assert not any(r["type"] == "outcome" for r in recs)  # предпосылка теста
    assert find_orphans(j, [_pos(777222, symbol="XAUUSD")]) == []


def test_multiple_orphans_detected_together(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="111"))  # сопоставлена — не orphan
    positions = [
        _pos(111, symbol="XAUUSD"),
        _pos(222, symbol="EURUSD", ptype=1, volume=0.10, price_open=1.10, sl=1.11),
        _pos(333, symbol="GBPUSD", volume=0.03, price_open=1.27, sl=1.26),
    ]
    orphans = find_orphans(j, positions)
    tickets = {o["ticket"] for o in orphans}
    assert tickets == {222, 333}
    assert len(orphans) == 2  # 111 не задвоена и не потеряна


def test_orphan_description_names_the_position(tmp_path):
    # описания должно хватать, чтобы найти позицию в терминале, не открывая
    # его — проверяем реальные значения полей, а не просто наличие ключей
    j = tmp_path / "journal.jsonl"
    p = _pos(999888, symbol="GBPUSD", ptype=1, volume=0.2, price_open=1.27, sl=1.28)
    orphans = find_orphans(j, [p])
    assert len(orphans) == 1
    o = orphans[0]
    assert o["ticket"] == 999888
    assert o["symbol"] == "GBPUSD"
    assert o["volume"] == 0.2
    assert o["side"] == "sell"       # type=1
    assert o["has_sl"] is True       # sl=1.28 != 0.0


def test_orphan_without_stop_loss_flagged(tmp_path):
    # orphan без стопа должен быть виден и как orphan, и как незащищённая
    # позиция (риск неизвестен) — has_sl=False, отдельно от unprotected из
    # exposure.py, который считает то же самое на уровне гейта (см.
    # test_risk_gate_cli.py: test_unmatched_naked_position_blocks_via_orphan_
    # not_unprotected для стороны гейта этого же случая)
    j = tmp_path / "journal.jsonl"
    orphans = find_orphans(j, [_pos(444, symbol="XAUUSD", sl=0.0)])
    assert len(orphans) == 1
    assert orphans[0]["has_sl"] is False


# --------------------------------------------------------------------------
# позиция, закрытая брокером напрямую, не должна оставлять алерты ведения
# --------------------------------------------------------------------------

def test_reconcile_drops_position_alerts_when_alerts_path_given(tmp_path, make_decision):
    """РЕГРЕСС 2026-07-29: позицию закрыл брокер (SL/TP) напрямую, а не
    scripts/exit.py — тот сам снимает свои алерты только при ручном полном
    закрытии. Алерты 1R/stall/инвалидации оставались висеть на уже не
    существующей позиции. reconcile() теперь снимает их сам, если ему дали
    alerts_path."""
    import datetime as dt
    import json

    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="7001", risk_usd=40,
                                     entry=2634.0, sl=2631.0))
    alerts_path = tmp_path / "alerts.json"
    alerts_path.write_text(json.dumps({
        "version": 1, "written_by": "claude-opus-5",
        "written_utc": "2026-07-29T10:00:00+00:00", "expires_utc": None,
        "alerts": [
            {"id": "pos-7001-1r", "type": "position_R_reaches", "ticket": 7001,
             "symbol": "XAUUSD", "level": 1.0},
            {"id": "pos-7001-invalidation", "type": "price_above", "ticket": 7001,
             "symbol": "XAUUSD", "level": 2631.0},
            {"id": "unrelated-level", "type": "price_above", "symbol": "EURUSD",
             "level": 1.1},
        ]}), encoding="utf-8")

    deals = [{"position_id": "7001", "profit": 72.0, "price": 2639.4, "entry": 1}]
    n = reconcile(j, {"7001": deals}, alerts_path=alerts_path,
                 now=dt.datetime(2026, 7, 29, 11, tzinfo=dt.timezone.utc))
    assert n == 1

    doc = json.loads(alerts_path.read_text(encoding="utf-8"))
    ids = {a["id"] for a in doc["alerts"]}
    assert "pos-7001-1r" not in ids
    assert "pos-7001-invalidation" not in ids
    assert "unrelated-level" in ids, "чужие алерты трогать нельзя"


def test_reconcile_without_alerts_path_does_not_touch_alerts(tmp_path, make_decision):
    """Обратная совместимость: не передали alerts_path — поведение как было."""
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40))
    deals = {"a1": [{"position_id": "a1", "profit": 40.0, "price": 2639.0, "entry": 1}]}
    assert reconcile(j, deals) == 1  # не падает без alerts_path


# ============================ MFE/MAE: шов, а не модуль ========================
# Сам замер проверен в test_excursion.py. Здесь проверяется ровно то, что в этом
# проекте ломалось семь раз: что рабочий код его ВЫЗЫВАЕТ. Поля mfe_R/mae_R
# полгода стояли в схеме журнала и всегда писались None — тесты на схему при
# этом были зелёными, потому что проверяли наличие ключа, а не его смысл.
#
# Времена здесь ОБЯЗАНЫ быть реальными: append_decision штампует ts моментом
# записи, и замер меряет окно [ts; выход]. Первая версия этих тестов взяла
# эпоху из 2025 года и получила окно отрицательной длины — замер честно вернул
# None, и тест это поймал. Оставляю как напоминание: фикстура со временем
# «из головы» проверяет не тот мир, в котором работает код.

import datetime as dt  # noqa: E402

import pandas as pd  # noqa: E402

UTC = dt.timezone.utc


def _exit_epoch(minutes_held=30, offset_hours=0):
    """Серверная эпоха выхода. Вход берётся из ts решения = момент записи, то
    есть «сейчас», поэтому выход обязан быть ПОЗЖЕ него: в первой версии обе
    точки оказались одним и тем же мгновением, окно вышло нулевой длины и замер
    честно вернул None."""
    closed = dt.datetime.now(UTC) + dt.timedelta(minutes=minutes_held)
    return (closed + dt.timedelta(hours=offset_hours)).timestamp()


def _bars_around(*, offset_hours=0, high=2640.0, low=2630.0, hours_back=2, n=60):
    """Бары M5 вокруг «сейчас»: назад на hours_back и вперёд с запасом, чтобы
    окно удержания целиком попало внутрь. Время СЕРВЕРНОЕ, как отдаёт MT5."""
    start = (dt.datetime.now(UTC) - dt.timedelta(hours=hours_back)
             + dt.timedelta(hours=offset_hours)).replace(tzinfo=None)
    return pd.DataFrame({"time": pd.date_range(start, periods=n, freq="5min"),
                         "high": [high] * n, "low": [low] * n})


class _Market:
    def __init__(self, bars=None):
        self._bars = _bars_around() if bars is None else bars
        self.calls = []

    def copy_rates(self, symbol, tf, count):
        self.calls.append((symbol, tf, count))
        return self._bars


def test_reconcile_заполняет_mfe_когда_дан_рынок(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40, symbol="XAUUSD",
                                     side="buy", entry=2634.0, sl=2631.0))
    epoch = _exit_epoch()
    deals = [{"position_id": "a1", "profit": 72.0, "price": 2639.4,
              "time": epoch, "entry": 1}]
    m = _Market()
    assert reconcile(j, {"a1": deals}, market=m) == 1
    out = [r for r in read_records(j) if r["type"] == "outcome"][0]
    assert m.calls, "рынок не опрошен: замер MFE не вызван"
    # ход вверх до 2640 при риске 3.0 = +2R, вниз до 2630 = −1.333R
    assert out["mfe_R"] == 2.0
    assert out["mae_R"] == -1.333


def test_reconcile_без_рынка_честно_пишет_none(tmp_path, make_decision):
    """Обратная совместимость: старые вызовы не начинают выдумывать нули."""
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40))
    epoch = _exit_epoch()
    deals = [{"position_id": "a1", "profit": 40.0, "price": 2639.0,
              "time": epoch, "entry": 1}]
    assert reconcile(j, {"a1": deals}) == 1
    out = [r for r in read_records(j) if r["type"] == "outcome"][0]
    assert out["mfe_R"] is None and out["mae_R"] is None


def test_упавший_замер_не_мешает_записать_исход(tmp_path, make_decision):
    """Журнал закрытой сделки важнее аналитики о ней."""
    class Dead:
        def copy_rates(self, *a, **k):
            raise RuntimeError("MT5 умер")

    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40))
    epoch = _exit_epoch()
    deals = [{"position_id": "a1", "profit": 40.0, "price": 2639.0,
              "time": epoch, "entry": 1}]
    assert reconcile(j, {"a1": deals}, market=Dead()) == 1
    out = [r for r in read_records(j) if r["type"] == "outcome"][0]
    assert out["R"] == 1.0 and out["mfe_R"] is None


def test_смещение_сервера_прокинуто_в_замер(tmp_path, make_decision):
    """Ловушка на потерю offset по дороге. Бары и эпоха сделки помечены
    серверным временем UTC+3; если reconcile не передаст смещение в замер, окно
    съедет на три часа, целых баров в нём не окажется и mfe_R станет None —
    молча и правдоподобно."""
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1", risk_usd=40, symbol="XAUUSD",
                                     side="buy", entry=2634.0, sl=2631.0))
    epoch = _exit_epoch(offset_hours=3)
    deals = [{"position_id": "a1", "profit": 72.0, "price": 2639.4,
              "time": epoch, "entry": 1}]
    m = _Market(_bars_around(offset_hours=3))
    reconcile(j, {"a1": deals}, market=m, server_utc_offset_hours=3)
    out = [r for r in read_records(j) if r["type"] == "outcome"][0]
    assert out["mfe_R"] == 2.0, "смещение сервера не доехало до замера"
