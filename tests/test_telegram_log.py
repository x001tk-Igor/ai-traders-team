"""Канал в Telegram и лог событий по времени ПК (задача владельца счёта 2026-07-27).

ДВА УТВЕРЖДЕНИЯ, РАДИ КОТОРЫХ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ.

1. ОТПРАВКА НЕ ИМЕЕТ ПРАВА ЛОМАТЬ ТОРГОВЛЮ. Телеграм лежит, токен отозван,
   сеть отвалилась — вход всё равно исполняется, а сообщение ждёт в очереди.
   Обратное означало бы, что доступность мессенджера решает, войдём ли мы в
   рынок.

2. ТЕКСТ МОДЕЛИ ЭКРАНИРУЕТСЯ. parse_mode=HTML отвечает 400 на первой же
   угловой скобке, а «цена < 4090» в рассуждении — обычное дело. Проверено
   вживую: сообщение с «<» не дошло, Telegram вернул «Unsupported start tag».
   Молча потерянное рассуждение — ровно то, чего канал должен был избежать.
"""
import datetime as dt
import json

import pytest

from trader_lib import telegram as tg
from trader_lib.eventlog import CATEGORIES, log, log_path, read_log

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 8, 25, 30, tzinfo=UTC)
LOCAL = dt.datetime(2026, 7, 27, 8, 25, 30, 123000)


def _settings(**over):
    base = {"enabled": True, "token": "T", "chat_id": 1,
            "send": {k: True for k in tg.KINDS}}
    base.update(over)
    return base


class Sender:
    """Мок отправки: считает вызовы, умеет падать заданное число раз."""

    def __init__(self, fail_times=0, exc=RuntimeError("сеть недоступна")):
        self.sent = []
        self._fail = fail_times
        self._exc = exc

    def __call__(self, text):
        if self._fail > 0:
            self._fail -= 1
            raise self._exc
        self.sent.append(text)
        return {"ok": True}


# --------------------------------------------------------------------------
# экранирование
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("цена < 4090", "цена &lt; 4090"),
    ("риск > 1%", "риск &gt; 1%"),
    ("A & B", "A &amp; B"),
    ("<b>подделка</b>", "&lt;b&gt;подделка&lt;/b&gt;"),
    (None, ""),
])
def test_model_text_is_escaped(raw, expected):
    assert tg.escape(raw) == expected


def test_reasoning_with_angle_bracket_survives():
    """Живой отказ, который это и породил: «<» в тексте → 400 и сообщение не
    дошло. Теги ставит только модуль, текст модели экранируется весь."""
    msg = tg.wake(now=NOW, symbol="XAUUSD", alert_type="price_below", level=4087.0,
                  price=4086.9, regime="флет",
                  reasoning="жду закрепления: цена < 4087 держится меньше минуты")
    assert "&lt;" in msg and "цена < 4087" not in msg
    assert msg.count("<b>") == 2 and "</b>" in msg


def test_html_tags_come_only_from_module():
    msg = tg.entered(now=NOW, symbol="XAUUSD", side="buy", lots=0.05, ticket=1,
                     thesis="<script>alert(1)</script>", entry=1, sl=1, tp=1,
                     risk_usd=1, rr=2, confidence=0.5, setup_type="s",
                     setup_status="изучаю", planned=True)
    assert "<script>" not in msg and "&lt;script&gt;" in msg


# --------------------------------------------------------------------------
# отправка и очередь
# --------------------------------------------------------------------------

def test_send_delivers(tmp_path):
    s = Sender()
    res = tg.send(tmp_path, "wake", "привет", now=NOW, sender=s,
                  settings=_settings())
    assert res["sent"] is True and s.sent == ["привет"]


def test_failure_never_raises_and_queues(tmp_path):
    """Телеграм лёг — торговля продолжается, сообщение ждёт."""
    s = Sender(fail_times=1)
    res = tg.send(tmp_path, "enter", "вход", now=NOW, sender=s, settings=_settings())
    assert res["sent"] is False and res["queued"] == 1
    assert s.sent == []
    outbox = (tmp_path / tg.OUTBOX_FILE).read_text(encoding="utf-8")
    assert "вход" in outbox


def test_queue_is_flushed_in_order_on_next_send(tmp_path):
    """Порядок сообщений для человека важен: сначала долги, потом новое."""
    dead = Sender(fail_times=1)
    tg.send(tmp_path, "wake", "первое", now=NOW, sender=dead, settings=_settings())
    alive = Sender()
    res = tg.send(tmp_path, "wake", "второе", now=NOW, sender=alive,
                  settings=_settings())
    assert res["sent"] is True
    assert alive.sent == ["первое", "второе"]
    assert not (tmp_path / tg.OUTBOX_FILE).exists()


def test_outbox_keeps_newest_when_overflowing(tmp_path, monkeypatch):
    """Свежее рассуждение важнее вчерашнего: при переполнении выбрасывается
    самое старое."""
    monkeypatch.setattr(tg, "MAX_OUTBOX", 3)
    dead = Sender(fail_times=99)
    for i in range(5):
        tg.send(tmp_path, "wake", f"msg{i}", now=NOW, sender=dead, settings=_settings())
    rows = [json.loads(x) for x in
            (tmp_path / tg.OUTBOX_FILE).read_text(encoding="utf-8").splitlines() if x]
    assert [r["text"] for r in rows] == ["msg2", "msg3", "msg4"]


def test_kind_can_be_switched_off(tmp_path):
    """До 40 пробуждений в день — если станет много, тип отключается настройкой,
    а не правкой кода."""
    s = Sender()
    cfg = _settings(send={**{k: True for k in tg.KINDS}, "wake": False})
    res = tg.send(tmp_path, "wake", "шум", now=NOW, sender=s, settings=cfg)
    assert res["sent"] is False and "отключён" in res["reason"] and s.sent == []
    assert tg.send(tmp_path, "enter", "вход", now=NOW, sender=s, settings=cfg)["sent"]


def test_missing_settings_is_silent(tmp_path):
    assert tg.load_settings(tmp_path) is None
    res = tg.send(tmp_path, "wake", "x", now=NOW, sender=Sender())
    assert res["sent"] is False and "не настроен" in res["reason"]


def test_broken_settings_file_is_silent(tmp_path):
    (tmp_path / tg.CONFIG_FILE).write_text("{битый", encoding="utf-8")
    assert tg.load_settings(tmp_path) is None


def test_settings_without_token_are_ignored(tmp_path):
    (tmp_path / tg.CONFIG_FILE).write_text(json.dumps({"chat_id": 1}), encoding="utf-8")
    assert tg.load_settings(tmp_path) is None


# --------------------------------------------------------------------------
# формат сообщений
# --------------------------------------------------------------------------

def test_wake_message_leads_with_reasoning():
    """Ради этого канал и заводился: видно, о чём модель думала."""
    msg = tg.wake(now=NOW, symbol="XAUUSD", alert_type="price_below", level=4087.06,
                  price=4087.04, regime="тренд вниз",
                  reasoning="уровень достигнут, но закрепления нет — жду закрытия M15",
                  equity=10000.0, wall_left_pct=2.99, positions=0)
    assert msg.startswith("<b>🔔 Пробуждение</b>")
    assert "жду закрытия M15" in msg
    assert "Режим: тренд вниз" in msg
    assert "10 000$" in msg and "2.99%" in msg


def test_enter_message_has_thesis_and_numbers():
    msg = tg.entered(now=NOW, symbol="XAUUSD", side="buy", lots=0.05, ticket=2256692453,
                     thesis="откат к EMA20 в тренде вверх", entry=4091.35, sl=4085.18,
                     tp=4103.68, risk_usd=94.45, rr=2.0, confidence=0.5,
                     setup_type="smoke-continuation", setup_status="изучаю",
                     planned=True, hypothesis_id="H1", gate_verdict="OK", spread=21)
    assert "📈 ВХОД" in msg and "2256692453" in msg
    assert "откат к EMA20" in msg and "плановый H1" in msg
    assert "Риск 94.45$" in msg


def test_unplanned_entry_is_marked_loudly():
    msg = tg.entered(now=NOW, symbol="XAUUSD", side="sell", lots=0.05, ticket=1,
                     thesis="т", entry=1, sl=2, tp=None, risk_usd=1, rr=2,
                     confidence=0.4, setup_type="s", setup_status="изучаю",
                     planned=False)
    assert "ВНЕПЛАНОВЫЙ" in msg


def test_exit_message_shows_result_first():
    msg = tg.exited(now=NOW, symbol="XAUUSD", ticket=1, r_multiple=-0.019,
                    profit=-1.04, reason="тезис не отработал", exit_price=4091.23,
                    entry_price=4091.35, day_trades=2, day_r=0.35, wall_left_pct=2.9)
    assert msg.startswith("<b>📉 ВЫХОД</b>")
    assert "R -0.02" in msg and "-1.04$" in msg
    assert "тезис не отработал" in msg


def test_critical_message_names_the_action():
    msg = tg.critical(now=NOW, title="СТОП-КРАН · стена дня пробита",
                      details=["Закрыто позиций: 2", "Просадка −3.02% при лимите −3.00%"],
                      action="Торговля остановлена до завтра")
    assert msg.startswith("<b>🚨") and "Торговля остановлена" in msg


def test_session_close_reports_wake_usefulness():
    msg = tg.session_close(now=NOW, duration="11ч 38м", trades=4, sum_r=1.8,
                           pnl_usd=142.0, wr=0.75, wakes=11, useful=8,
                           noisy=["eur-range-top ×2"], progress_pct=1.4,
                           plan_traded=2, plan_total=3)
    assert "⏹ Сессия закрыта" in msg
    assert "полезность 0.73" in msg and "eur-range-top" in msg
    assert "P&amp;L" in msg, "амперсанд обязан быть экранирован"


# --------------------------------------------------------------------------
# лог событий
# --------------------------------------------------------------------------

def test_log_writes_local_time_and_header(tmp_path):
    line = log(tmp_path, "WAKE", "xau-down price_below 4087.06", now=LOCAL,
               price=4087.04)
    assert line.startswith("08:25:30.123 | WAKE")
    assert "price=4087.04" in line
    head = log_path(tmp_path, now=LOCAL).read_text(encoding="utf-8").splitlines()[0]
    assert "время машины" in head, "смещение машины обязано быть в шапке"


def test_log_file_is_per_day(tmp_path):
    log(tmp_path, "SESSION", "открыта", now=LOCAL)
    log(tmp_path, "SESSION", "закрыта", now=LOCAL + dt.timedelta(days=1))
    assert log_path(tmp_path, now=LOCAL).name == "2026-07-27.log"
    assert len(read_log(tmp_path, now=LOCAL)) == 1


def test_unknown_category_becomes_error_not_silent(tmp_path):
    """Свободные ярлыки расползаются, и grep по логу перестаёт работать."""
    line = log(tmp_path, "ЧТОТОНОВОЕ", "странность", now=LOCAL)
    assert "| ERROR" in line
    assert "ЧТОТОНОВОЕ" not in line


def test_log_failure_never_raises(tmp_path, monkeypatch):
    """Диск занят — это не повод отменить закрытие позиции."""
    def boom(*a, **k):
        raise OSError("диск недоступен")

    monkeypatch.setattr("builtins.open", boom)
    assert log(tmp_path, "EXIT", "закрытие", now=LOCAL) is None


def test_categories_are_closed_list():
    assert set(CATEGORIES) == {"SESSION", "WAKE", "THINK", "GATE", "ENTER",
                               "EXIT", "VALVE", "NOTIFY", "ERROR"}
