from typing import Protocol

import numpy as np
import pandas as pd


class MarketData(Protocol):
    def copy_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...
    def symbol_info(self, symbol: str) -> dict: ...
    def account_info(self) -> dict: ...
    def positions(self) -> list: ...
    def history_deals(self, since_ts) -> list: ...
    def order_send(self, req: dict) -> dict: ...
    # tick нужен исполнению (trader_lib/execute.py): цена повтора после реквоты
    # обязана быть СВЕЖЕЙ, а не той, с которой приказ уже отклонили
    def tick(self, symbol: str) -> dict: ...


class FakeMarket:
    """Детерминированный рынок для тестов/E2E. Бары можно задать явно."""

    def __init__(self, point=0.01, digits=2, spread_points=20, bars=None,
                 account=None, positions=None, deals=None):
        self._point, self._digits, self._spread = point, digits, spread_points
        self._bars = bars
        self._account = account or {"balance": 10000, "equity": 10000}
        self._positions = positions or []
        self._deals = deals or []

    def copy_rates(self, symbol, timeframe, count):
        if self._bars is not None:
            return self._bars.tail(count).reset_index(drop=True)
        rng = np.random.default_rng(42)
        px = 2600 + np.cumsum(rng.normal(0, 0.5, count))
        return pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=count, freq="5min"),
            "open": px, "high": px + 0.3, "low": px - 0.3, "close": px,
            "tick_volume": rng.integers(50, 500, count), "spread": self._spread})

    def symbol_info(self, symbol):
        # filling_mode — МАСКА символа (FOK=1, IOC=2), не константа ордера:
        # нумерации разные, и execute.py читает именно маску
        return {"point": self._point, "digits": self._digits, "spread": self._spread,
                "trade_contract_size": 100, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "filling_mode": 1}

    def tick(self, symbol):
        """Цена вокруг последнего бара: спред кладём симметрично, чтобы
        FakeMarket годился и для проверки повторов по свежей цене."""
        px = float(self.copy_rates(symbol, "M5", 1)["close"].iloc[-1])
        half = self._spread * self._point / 2
        return {"bid": round(px - half, 8), "ask": round(px + half, 8)}

    def account_info(self):
        return self._account

    def positions(self):
        return self._positions

    def history_deals(self, since_ts):
        return self._deals

    def order_send(self, req):
        return {"retcode": 10009, "order": 123, "price": req.get("price")}


def live_market():
    """Реальная реализация поверх пакета MetaTrader5 (только на ПК трейдера)."""
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    _TF = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
           "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
           "D1": mt5.TIMEFRAME_D1}

    def _retry(fn, what):
        """Вызов с ОДНОЙ попыткой переподключения к терминалу.

        ИНЦИДЕНТ 2026-08-03. В 02:23 UTC связь с терминалом оборвалась на
        мгновение. Пакет MetaTrader5 держит соединение на уровне модуля и
        инициализируется здесь ОДИН раз при старте — после обрыва все вызовы
        стали возвращать None НАВСЕГДА. Датчик не падал и исправно тикал, но
        `walls` не считались ни разу, а весь конвейер алертов стоит за этим
        гейтом (fail-closed: нет данных счёта — не стреляем). Итог: будильник
        06:30 был взведён и НЕ выстрелил, а стоп-кран был слеп 4 ч 10 мин.
        Торгового времени не потеряли только потому, что человек разбудил модель
        руками через три минуты; открытых позиций в эти часы тоже не было. Оба
        обстоятельства — везение, а не свойство контура. Снаружи всё выглядело
        исправным: пульс шёл, процесс жил, ошибок в heartbeat никто не читал.

        Одна попытка, не бесконечная: если терминал закрыт совсем, отказ обязан
        быть отказом, а не молчаливым циклом переподключений. Цикл датчика
        поймает RuntimeError и переживёт тик; следующий тик попробует снова.
        """
        out = fn()
        if out is not None:
            return out
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001 - shutdown на мёртвом соединении не важен
            pass
        if not mt5.initialize():
            raise RuntimeError(f"{what}: связь с терминалом потеряна, "
                               f"переподключение не удалось ({mt5.last_error()})")
        out = fn()
        if out is None:
            raise RuntimeError(f"{what}: терминал переподключён, но данных нет "
                               f"({mt5.last_error()})")
        return out

    class Live:
        def copy_rates(self, symbol, timeframe, count):
            r = _retry(lambda: mt5.copy_rates_from_pos(symbol, _TF[timeframe], 0, count),
                       f"бары {symbol} {timeframe}")
            if len(r) == 0:
                raise RuntimeError(f"no rates {symbol} {timeframe}")
            df = pd.DataFrame(r)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df

        def symbol_info(self, symbol):
            s = _retry(lambda: mt5.symbol_info(symbol), f"символ {symbol}")
            return {"point": s.point, "digits": s.digits, "spread": s.spread,
                    "trade_contract_size": s.trade_contract_size,
                    "volume_min": s.volume_min, "volume_max": s.volume_max,
                    "volume_step": s.volume_step,
                    # маска поддерживаемых режимов заполнения (FOK=1, IOC=2)
                    "filling_mode": s.filling_mode}

        def tick(self, symbol):
            t = _retry(lambda: mt5.symbol_info_tick(symbol), f"тик {symbol}")
            return {"bid": t.bid, "ask": t.ask, "time": t.time}

        def account_info(self):
            # ИНЦИДЕНТ 2026-08-03 02:23 UTC. mt5.account_info() отдаёт None при
            # обрыве связи с терминалом (переподключение брокера), а здесь оно
            # разыменовывалось сразу — AttributeError вместо честного отказа.
            # Датчик при этом НЕ умер: он тикал, а работал вхолостую. Обрыв связи
            # обязан выглядеть как RuntimeError с текстом, а не как «у None нет
            # атрибута balance»: цикл ловит отказ и живёт дальше, читающий видит
            # причину. Тот же контракт, что у tick() и symbol_info() выше.
            a = _retry(mt5.account_info, "счёт")
            return {"balance": a.balance, "equity": a.equity}

        def positions(self):
            # positions_get() тоже отдаёт None при обрыве, и `or []` превратил бы
            # это в «позиций нет» — то есть в ТИШИНУ вместо отказа. Для стоп-крана
            # разница решающая: «нечего защищать» и «не видно, что защищать» —
            # противоположные состояния, и второе обязано быть отказом.
            p = _retry(mt5.positions_get, "список позиций")
            return [x._asdict() for x in p]

        def history_deals(self, since_ts):
            import datetime as dt
            # РЕГРЕСС 2026-07-29: MT5 маркирует сделки СЕРВЕРНЫМ временем
            # (эпоха отдаётся так, будто server_now() и есть UTC), а until
            # здесь считался истинным UTC "сейчас" — сделка, закрытая только
            # что, получает серверный таймстамп на server_utc_offset_hours
            # позже, чем это "сейчас", и close_watch.py находил 0 сделок для
            # только что закрытой стопом позиции. Настоящее исправление
            # (сдвиг until на offset_hours) потребовало бы протащить cfg в
            # live_market(), у которого сейчас нет параметров и много вызовов
            # без него. Дешевле и надёжнее: запрашивать с запасом до
            # правдоподобного максимума смещения брокера (см. диапазон
            # -12..+14ч в bootstrap_env) — узкое until молчаливо теряет
            # только что закрытые сделки, широкое до будущего безвредно:
            # сделок в будущем не бывает, лишние часы просто вернут пусто.
            until = dt.datetime.now() + dt.timedelta(hours=14)
            d = mt5.history_deals_get(since_ts, until)
            return [x._asdict() for x in (d or [])]

        def order_send(self, req):
            """Перевод символического приказа в константы MetaTrader5.

            trader_lib/execute.py намеренно не знает про пакет MetaTrader5:
            он собирает приказ словами ("TRADE_ACTION_DEAL", "BUY", "FOK"), и
            граница «символы → числа» проходит здесь. Иначе исполнение нельзя
            было бы проверить офлайн, а на живом терминале строки молча
            уехали бы в mt5.order_send и он отверг бы их как неверный запрос.

            Неизвестное значение — исключение, а не тихая подстановка
            умолчания: приказ деньгами не имеет права уйти «примерно таким».
            """
            actions = {"TRADE_ACTION_DEAL": mt5.TRADE_ACTION_DEAL,
                       "TRADE_ACTION_SLTP": mt5.TRADE_ACTION_SLTP,
                       "TRADE_ACTION_PENDING": mt5.TRADE_ACTION_PENDING,
                       "TRADE_ACTION_REMOVE": mt5.TRADE_ACTION_REMOVE}
            types = {"BUY": mt5.ORDER_TYPE_BUY, "SELL": mt5.ORDER_TYPE_SELL}
            fillings = {"FOK": mt5.ORDER_FILLING_FOK, "IOC": mt5.ORDER_FILLING_IOC,
                        "RETURN": mt5.ORDER_FILLING_RETURN}
            times = {"GTC": mt5.ORDER_TIME_GTC, "DAY": mt5.ORDER_TIME_DAY}

            out = dict(req)
            for key, table in (("action", actions), ("type", types),
                               ("type_filling", fillings), ("type_time", times)):
                value = out.get(key)
                if isinstance(value, str):
                    if value not in table:
                        raise ValueError(f"неизвестное значение {key}={value!r} "
                                         f"(допустимо: {sorted(table)})")
                    out[key] = table[value]
            res = mt5.order_send(out)
            if res is None:
                return {"retcode": None, "comment": f"order_send вернул None: "
                                                    f"{mt5.last_error()}"}
            return res._asdict()

    return Live()
