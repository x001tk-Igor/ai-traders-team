"""Исполнение с гарантией стопа (задача 4.1). Всё офлайн на FakeMarket.

ГЛАВНОЕ УТВЕРЖДЕНИЕ ЭТОГО ФАЙЛА: после place_order позиция либо имеет стоп,
либо не существует. Третьего состояния — «позиция открыта, стопа нет, функция
вернула ok» — быть не может. Риск позиции без стопа неограничен и не выражается
числом, поэтому проверка идёт ПОСЛЕ филла по факту чтения позиции у брокера, а
не по retcode: брокер может принять ордер и отклонить стоп.

Второе по важности — маскировка: magic и comment не параметры функции, а
жёстко зашитые 0 и "". Тест проверяет отправленный запрос, а не сигнатуру:
параметр можно было бы добавить обратно, а вот отправить непустой comment
незаметно — нет.
"""
import datetime as dt

import pytest

from trader_lib.execute import (
    RETRY_RETCODES,
    close_partial,
    close_position,
    modify_sl,
    place_order,
)
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc

SI = {"point": 0.01, "digits": 2, "spread": 20, "trade_contract_size": 100,
      "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
      "filling_mode": 1}


class ScriptedMarket(FakeMarket):
    """Брокер со сценарием: заданная последовательность retcode на order_send,
    управляемое состояние позиций, запись всех отправленных запросов."""

    def __init__(self, *, retcodes=None, positions=None, symbol_info=None,
                 fill_price=None, bid=2400.0, ask=2400.2, sl_after_fill=None):
        super().__init__(positions=list(positions or []))
        self.sent = []
        self._retcodes = list(retcodes or [])
        self._si = dict(symbol_info or SI)
        self._fill_price = fill_price
        self._bid, self._ask = bid, ask
        # каким стоп окажется у позиции после филла: None → как просили
        self._sl_after_fill = sl_after_fill
        self._next_ticket = 5000

    def symbol_info(self, symbol):
        return dict(self._si)

    def tick(self, symbol):
        return {"bid": self._bid, "ask": self._ask}

    def positions(self):
        return [dict(p) for p in self._positions]

    def order_send(self, req):
        self.sent.append(dict(req))
        retcode = self._retcodes.pop(0) if self._retcodes else 10009
        if retcode != 10009:
            return {"retcode": retcode, "order": 0, "price": 0.0,
                    "comment": f"scripted {retcode}"}

        action = req.get("action")
        if action == "TRADE_ACTION_SLTP":
            for p in self._positions:
                if p["ticket"] == req["position"]:
                    p["sl"] = req["sl"]
            return {"retcode": 10009, "order": 0, "price": 0.0}

        if req.get("position"):                      # закрытие / частичка
            for p in list(self._positions):
                if p["ticket"] == req["position"]:
                    left = round(p["volume"] - req["volume"], 8)
                    if left <= 0:
                        self._positions.remove(p)
                    else:
                        p["volume"] = left
            return {"retcode": 10009, "order": 0, "price": self._bid}

        ticket = self._next_ticket
        self._next_ticket += 1
        price = self._fill_price if self._fill_price is not None else req["price"]
        sl = self._sl_after_fill if self._sl_after_fill is not None else req.get("sl", 0.0)
        self._positions.append({"ticket": ticket, "symbol": req["symbol"],
                                "type": 0 if req["type"] == "BUY" else 1,
                                "volume": req["volume"], "price_open": price,
                                "sl": sl, "tp": req.get("tp", 0.0),
                                "price_current": price, "profit": 0.0, "magic": 0})
        return {"retcode": 10009, "order": ticket, "price": price,
                "volume": req["volume"]}


def _pos(ticket=5000, *, ptype=0, volume=0.1, sl=2395.0, price_open=2400.0):
    return {"ticket": ticket, "symbol": "XAUUSD", "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": 0.0,
            "price_current": price_open, "profit": 0.0, "magic": 0}


# --------------------------------------------------------------------------
# маскировка
# --------------------------------------------------------------------------

def test_order_carries_empty_magic_and_comment():
    """Требование владельца счёта: сделка не должна нести следов советника в полях,
    которые мы контролируем. REASON=EXPERT ставит терминал и он неустраним
    (зонд 3.4), но magic и comment обязаны быть пустыми."""
    m = ScriptedMarket()
    place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert m.sent[0]["magic"] == 0
    assert m.sent[0]["comment"] == ""


def test_magic_and_comment_are_not_parameters():
    """Их нельзя передать «для гибкости»: единственное допустимое значение
    одно, а параметр создал бы путь его изменить."""
    m = ScriptedMarket()
    with pytest.raises(TypeError):
        place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2,
                    sl=2395.0, magic=777)
    with pytest.raises(TypeError):
        place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2,
                    sl=2395.0, comment="bot")


# --------------------------------------------------------------------------
# предпроверки: до отправки
# --------------------------------------------------------------------------

@pytest.mark.parametrize("side,entry,sl", [
    ("buy", 2400.0, 2405.0),    # стоп ВЫШЕ входа у покупки
    ("sell", 2400.0, 2395.0),   # стоп НИЖЕ входа у продажи
])
def test_sl_wrong_side_rejected_before_send(side, entry, sl):
    m = ScriptedMarket()
    r = place_order(m, symbol="XAUUSD", side=side, lots=0.1, entry=entry, sl=sl)
    assert r["ok"] is False and r["error"] == "sl_wrong_side"
    assert m.sent == [], "ордер не должен уходить брокеру вовсе"


@pytest.mark.parametrize("lots,sl,error", [
    (0.0, 2395.0, "lots_not_positive"),
    (-0.1, 2395.0, "lots_not_positive"),
    (0.1, 2400.2, "sl_equals_entry"),
    (0.1, 0.0, "sl_required"),
    (0.1, None, "sl_required"),
])
def test_bad_inputs_rejected_before_send(lots, sl, error):
    m = ScriptedMarket()
    r = place_order(m, symbol="XAUUSD", side="buy", lots=lots, entry=2400.2, sl=sl)
    assert r["ok"] is False and r["error"] == error
    assert m.sent == []


# --------------------------------------------------------------------------
# повторы и режимы заполнения
# --------------------------------------------------------------------------

def test_retry_on_requote():
    """Реквота — не отказ, а «цена ушла»: повторяем со свежей ценой."""
    m = ScriptedMarket(retcodes=[10004, 10004, 10009], bid=2400.0, ask=2400.5)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is True and r["attempts"] == 3
    assert m.sent[-1]["price"] == 2400.5, "повтор идёт по СВЕЖЕЙ цене, не по старой"


def test_retry_gives_up_after_limit():
    m = ScriptedMarket(retcodes=[10004] * 10)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is False and r["error"] == "order_send_failed"
    assert len(m.sent) == 3, "три попытки, а не бесконечность"


def test_algo_trading_disabled_is_not_retried():
    """10027 — «автоторговля выключена в терминале». Повтор не чинит ничего,
    а прячет понятную причину за тремя одинаковыми отказами."""
    assert 10027 not in RETRY_RETCODES
    m = ScriptedMarket(retcodes=[10027])
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is False and len(m.sent) == 1
    assert "Algo Trading" in r["message"]


def test_filling_mode_taken_from_symbol_mask():
    """Маска символа и константы ордера живут в РАЗНЫХ нумерациях
    (SYMBOL_FILLING_FOK=1/IOC=2 против ORDER_FILLING_FOK=0/IOC=1/RETURN=2).
    Слепой перебор заканчивается неподдерживаемым режимом, чья ошибка затирает
    настоящую причину отказа."""
    m = ScriptedMarket(symbol_info={**SI, "filling_mode": 2})   # только IOC
    place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert m.sent[0]["type_filling"] == "IOC"


def test_no_supported_filling_mode_is_explicit():
    m = ScriptedMarket(symbol_info={**SI, "filling_mode": 0})
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is False and r["error"] == "no_filling_mode"
    assert m.sent == []


# --------------------------------------------------------------------------
# ГАРАНТИЯ СТОПА после филла
# --------------------------------------------------------------------------

def test_sl_verified_after_fill():
    m = ScriptedMarket()
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is True and r["sl_verified"] is True
    assert [p for p in m.positions() if p["ticket"] == r["ticket"]][0]["sl"] == 2395.0


def test_sl_missing_after_fill_triggers_reset():
    """Брокер принял ордер, но стоп не поставил (частая картина при быстром
    рынке). Читаем позицию и ставим стоп отдельным приказом."""
    m = ScriptedMarket(sl_after_fill=0.0)
    m._sl_after_fill = 0.0
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    sltp = [s for s in m.sent if s.get("action") == "TRADE_ACTION_SLTP"]
    assert sltp, "обязана быть повторная установка стопа"
    assert r["ok"] is True and r["sl_verified"] is True
    assert r["sl_reset_attempts"] == 1


def test_sl_still_missing_triggers_close():
    """Стоп не встал и со второй попытки → позицию закрываем. Оставить её
    открытой нельзя: риск неограничен и не выражается числом."""
    class Stubborn(ScriptedMarket):
        def order_send(self, req):
            if req.get("action") == "TRADE_ACTION_SLTP":
                self.sent.append(dict(req))
                return {"retcode": 10016, "comment": "invalid stops"}
            return super().order_send(req)

    m = Stubborn(sl_after_fill=0.0)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is False and r["error"] == "sl_not_set_position_closed"
    assert r["sl_verified"] is False
    assert m.positions() == [], "позиция без стопа не имеет права остаться открытой"


def test_close_after_failed_sl_is_reported_when_it_also_fails():
    """Худший случай: стоп не встал И закрыть не удалось. Это не «ok=False» с
    общей формулировкой, а отдельная тревога: позиция живёт без стопа."""
    class Hopeless(ScriptedMarket):
        def order_send(self, req):
            if req.get("action") == "TRADE_ACTION_SLTP" or req.get("position"):
                self.sent.append(dict(req))
                return {"retcode": 10018, "comment": "market closed"}
            return super().order_send(req)

    m = Hopeless(sl_after_fill=0.0)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["ok"] is False and r["error"] == "unprotected_position_left_open"
    assert r["ticket"] is not None, "тикет обязан вернуться: им будет заниматься стоп-кран"
    assert len(m.positions()) == 1


# --------------------------------------------------------------------------
# проскальзывание
# --------------------------------------------------------------------------

def test_slippage_recorded():
    """Знак: положительное проскальзывание = исполнились ХУЖЕ намеченного."""
    m = ScriptedMarket(fill_price=2400.5)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["fill_price"] == 2400.5
    assert r["slippage_points"] == pytest.approx(30.0)


def test_slippage_sign_for_sell():
    m = ScriptedMarket(fill_price=2399.8)
    r = place_order(m, symbol="XAUUSD", side="sell", lots=0.1, entry=2400.0, sl=2405.0)
    assert r["slippage_points"] == pytest.approx(20.0), "продали дешевле — тоже хуже"


def test_favourable_slippage_is_negative():
    m = ScriptedMarket(fill_price=2400.0)
    r = place_order(m, symbol="XAUUSD", side="buy", lots=0.1, entry=2400.2, sl=2395.0)
    assert r["slippage_points"] == pytest.approx(-20.0)


# --------------------------------------------------------------------------
# перенос стопа
# --------------------------------------------------------------------------

def test_modify_sl_tightening_ok():
    m = ScriptedMarket(positions=[_pos(sl=2395.0)])
    r = modify_sl(m, ticket=5000, new_sl=2398.0)
    assert r["ok"] is True
    assert m.positions()[0]["sl"] == 2398.0


def test_modify_sl_widening_forbidden():
    """Расширение стопа = рост риска. Это не техническая ошибка, а нарушение
    конституции: риск, выданный гейтом на сделку, уже израсходован."""
    m = ScriptedMarket(positions=[_pos(sl=2395.0)])
    r = modify_sl(m, ticket=5000, new_sl=2390.0)
    assert r["ok"] is False and r["error"] == "sl_widening_forbidden"
    assert m.sent == [] and m.positions()[0]["sl"] == 2395.0


def test_modify_sl_widening_forbidden_for_sell():
    m = ScriptedMarket(positions=[_pos(ptype=1, sl=2405.0)])
    assert modify_sl(m, ticket=5000, new_sl=2410.0)["error"] == "sl_widening_forbidden"
    assert modify_sl(m, ticket=5000, new_sl=2402.0)["ok"] is True


def test_modify_sl_on_position_without_stop_is_allowed():
    """Позиция без стопа: любой стоп — уменьшение риска, а не расширение.
    Именно этим путём стоп-кран восстанавливает пропавший стоп."""
    m = ScriptedMarket(positions=[_pos(sl=0.0)])
    assert modify_sl(m, ticket=5000, new_sl=2380.0)["ok"] is True


def test_modify_sl_wrong_side_rejected():
    m = ScriptedMarket(positions=[_pos(sl=2395.0)])
    r = modify_sl(m, ticket=5000, new_sl=2405.0)   # выше цены открытия у покупки
    assert r["ok"] is False and r["error"] == "sl_wrong_side"


def test_modify_sl_unknown_ticket():
    m = ScriptedMarket(positions=[_pos()])
    r = modify_sl(m, ticket=999, new_sl=2398.0)
    assert r["ok"] is False and r["error"] == "position_not_found"


# --------------------------------------------------------------------------
# частичное закрытие
# --------------------------------------------------------------------------

def test_close_partial_rounds_to_lot_step():
    m = ScriptedMarket(positions=[_pos(volume=0.13)])
    r = close_partial(m, ticket=5000, fraction=0.5)
    assert r["ok"] is True and r["closed_lots"] == 0.06   # floor к шагу 0.01
    assert m.positions()[0]["volume"] == pytest.approx(0.07)


def test_partial_never_closes_more_than_asked():
    """Округление ВНИЗ, а не к ближайшему: закрыть больше, чем просила модель,
    — это изменить её решение. 0.13 × 0.6 = 0.078 → 0.07, а не 0.08.
    (Случай 0.065 такое не различает: round(6.5) в Python даёт 6, то есть
    совпадает с floor — на нём мутация «round вместо floor» выживает.)
    """
    m = ScriptedMarket(positions=[_pos(volume=0.13)])
    r = close_partial(m, ticket=5000, fraction=0.6)
    assert r["ok"] is True and r["closed_lots"] == pytest.approx(0.07)
    assert m.positions()[0]["volume"] == pytest.approx(0.06)


def test_partial_below_min_lot_reports_impossible():
    """0.01 пополам — обе половины меньше минимального лота. Не «закруглим до
    минимума» (это изменило бы решение модели), а честный отказ: модель решит,
    закрывать целиком или вести целиком."""
    m = ScriptedMarket(positions=[_pos(volume=0.01)])
    r = close_partial(m, ticket=5000, fraction=0.5)
    assert r["ok"] is False and r["error"] == "partial_not_possible"
    assert m.sent == [] and m.positions()[0]["volume"] == 0.01


def test_partial_leaving_less_than_min_lot_reports_impossible():
    """Закрыть можно, а остаток окажется 0.005 — брокер такую позицию не
    оставит; отказываемся так же честно."""
    m = ScriptedMarket(positions=[_pos(volume=0.015)])
    r = close_partial(m, ticket=5000, fraction=0.9)
    assert r["ok"] is False and r["error"] == "partial_not_possible"


@pytest.mark.parametrize("fraction", [0.0, 1.5, -0.2])
def test_partial_fraction_out_of_range(fraction):
    m = ScriptedMarket(positions=[_pos(volume=1.0)])
    assert close_partial(m, ticket=5000, fraction=fraction)["error"] == "bad_fraction"


# --------------------------------------------------------------------------
# закрытие
# --------------------------------------------------------------------------

def test_close_position_sends_opposite_side():
    m = ScriptedMarket(positions=[_pos(ptype=0, volume=0.2)])
    r = close_position(m, ticket=5000)
    assert r["ok"] is True
    assert m.sent[0]["type"] == "SELL" and m.sent[0]["volume"] == 0.2
    assert m.sent[0]["magic"] == 0 and m.sent[0]["comment"] == ""
    assert m.positions() == []


def test_close_position_already_gone_is_ok():
    """Позиция закрылась по стопу секунду назад — это не ошибка исполнения."""
    m = ScriptedMarket(positions=[])
    r = close_position(m, ticket=5000)
    assert r["ok"] is True and r["note"] == "position_already_closed"


def test_close_position_broker_refusal_is_visible():
    m = ScriptedMarket(positions=[_pos()], retcodes=[10018])
    r = close_position(m, ticket=5000)
    assert r["ok"] is False and r["retcode"] == 10018
    assert len(m.positions()) == 1
