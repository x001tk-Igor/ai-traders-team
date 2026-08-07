"""Автодетект среды (задача 0.3). Всё офлайн на подставном зонде.

Смысл этого модуля: при переносе на другой ПК/брокера/после перехода на
зимнее время профиль пересчитывается сам, а не молча врёт числами, которые
кто-то однажды вписал руками. Поэтому главные тесты здесь — не «поля
заполнились», а:

  * test_offset_mismatch_blocks_start — расхождение серверного смещения с
    конституцией блокирует старт. Смещение задаёт границу торгового дня, от
    которой считается стена −3%: ошибка на час означает, что дневной лимит
    отмеряется не от того нуля.
  * test_probe_failure_is_fail_closed — упавший зонд даёт ok=False, а не
    профиль с дырами, который выглядит рабочим.
  * test_profile_written_and_reused — тяжёлые опросы (символы, бары) не
    повторяются, но опознание терминала делается ВСЕГДА: иначе смена брокера
    осталась бы незамеченной ровно до истечения суток.
"""
import dataclasses
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from scripts.bootstrap_env import (
    EnvNotReady,
    MACRO_SYMBOLS,
    build_profile,
    check_against_config,
    detect_offset_hours,
    load_or_build,
    require_tradable,
)
from trader_lib.config import load_config

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
OFFSET_H = 3  # cfg.risk.server_utc_offset_hours у брокера трейдера


def _cfg(tmp_path, **risk_over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if risk_over:
        cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, **risk_over))
    return cfg


def _bars(n=2000, *, spread=20, spread_filled=1.0):
    c = 2400.0 + np.arange(n) * 0.01
    sp = np.full(n, spread, dtype=float)
    if spread_filled < 1.0:
        sp[: int(n * (1 - spread_filled))] = 0.0
    return pd.DataFrame({"time": pd.date_range("2026-07-01", periods=n, freq="5min"),
                         "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                         "tick_volume": 200, "spread": sp})


class FakeProbe:
    """Подставной зонд среды. Считает обращения — на этом держится проверка
    переиспользования профиля."""

    def __init__(self, *, offset_h=OFFSET_H, trade_allowed=True, present=None,
                 suffix="", bars=None, multiprocess=True, server="Broker-Demo",
                 login=9999, name="Broker MT5 Terminal", raises=None):
        self.offset_h = offset_h
        self._trade_allowed = trade_allowed
        # None → присутствуют все whitelist-символы и все макро
        self._present = present
        self._suffix = suffix
        self._bars = bars if bars is not None else _bars()
        self._multiprocess = multiprocess
        self._server, self._login, self._name = server, login, name
        self._raises = raises or {}
        self.calls = {}

    def _hit(self, what):
        self.calls[what] = self.calls.get(what, 0) + 1
        if what in self._raises:
            raise self._raises[what]

    def terminal(self):
        self._hit("terminal")
        return {"name": self._name, "build": 5735, "connected": True,
                "trade_allowed": self._trade_allowed}

    def account(self):
        self._hit("account")
        return {"login": self._login, "server": self._server, "currency": "USD"}

    def tick_time(self, symbol):
        self._hit("tick_time")
        # МТ5 отдаёт серверное время наивным datetime — зонд обязан сам понять смещение
        return (NOW + dt.timedelta(hours=self.offset_h)).replace(tzinfo=None)

    def select(self, symbol):
        self._hit("select")
        if self._present is None:
            base = symbol[: -len(self._suffix)] if self._suffix and symbol.endswith(self._suffix) else symbol
            return symbol.endswith(self._suffix) and (base in _all_bases())
        return symbol in self._present

    def bars(self, symbol, timeframe, count):
        self._hit("bars")
        return self._bars.tail(count)

    def multiprocess_ok(self):
        self._hit("multiprocess")
        return self._multiprocess


def _all_bases():
    cfg = load_config("config/trader.config.json")
    return set(cfg.instruments.whitelist) | set(MACRO_SYMBOLS)


# --------------------------------------------------------------------------
# смещение серверного времени
# --------------------------------------------------------------------------

@pytest.mark.parametrize("server_shift_min,expected", [
    (180, 3.0), (120, 2.0), (150, 2.5), (0, 0.0), (-300, -5.0),
    # 2 минуты дрейфа часов не имеют права стать получасом
    (182, 3.0), (178, 3.0),
])
def test_offset_detected_from_tick(server_shift_min, expected):
    server_naive = (NOW + dt.timedelta(minutes=server_shift_min)).replace(tzinfo=None)
    assert detect_offset_hours(server_naive, NOW) == expected


@pytest.mark.parametrize("stale_hours", [24, 42.5, 60])
def test_stale_tick_gives_no_offset(stale_hours):
    """Метод опирается на «тик пришёл только что», и в выходные это ложно:
    последняя котировка пятничная, и расчёт даёт −42.5 часа. Такое число нельзя
    ни использовать, ни вписать в конституцию — только None.

    Найдено ПЕРВЫМ ЖИВЫМ ЗАПУСКОМ bootstrap_env (суббота): офлайн-тесты подавали
    свежий тик и потому были слепы к этому целиком.
    """
    server_naive = (NOW - dt.timedelta(hours=stale_hours)).replace(tzinfo=None)
    assert detect_offset_hours(server_naive, NOW) is None


@pytest.mark.parametrize("offset", [-12.0, 0.0, 3.0, 14.0])
def test_plausible_offsets_still_accepted(offset):
    server_naive = (NOW + dt.timedelta(hours=offset)).replace(tzinfo=None)
    assert detect_offset_hours(server_naive, NOW) == offset


def test_stale_tick_blocks_start_with_readable_reason(tmp_path):
    """Не «обнаружено −42.5» в блокировке, а объяснение, что делать."""
    p = build_profile(FakeProbe(offset_h=-42.5), _cfg(tmp_path), now=NOW)
    assert p["ok"] is False
    assert p["server_utc_offset_hours"] is None
    assert any("не определён" in b for b in p["blocking"])
    assert any("рынок закрыт" in w for w in p["warnings"])


def test_offset_taken_from_freshest_symbol(tmp_path):
    """У неликвидного символа последний тик может быть часовой давности —
    он сдвинул бы расчёт ровно на свой возраст. Берётся самый свежий."""
    cfg = _cfg(tmp_path)
    primary = cfg.instruments.whitelist[0]

    class Mixed(FakeProbe):
        def tick_time(self, symbol):
            self._hit("tick_time")
            # у первого символа тик протух на 5 часов, у остальных свежий
            lag = 5 if symbol == primary else 0
            return (NOW + dt.timedelta(hours=self.offset_h - lag)).replace(tzinfo=None)

    assert build_profile(Mixed(), cfg, now=NOW)["server_utc_offset_hours"] == 3.0


def test_offset_detected_in_profile(tmp_path):
    p = build_profile(FakeProbe(), _cfg(tmp_path), now=NOW)
    assert p["server_utc_offset_hours"] == 3.0
    assert p["ok"] is True, p["blocking"]


def test_offset_mismatch_blocks_start(tmp_path):
    """Смещение задаёт границу дня, от которой отмеряется стена −3%. Код НЕ
    правит конституцию сам (config — единственный источник лимитов), но и
    работать с расхождением не имеет права."""
    p = build_profile(FakeProbe(offset_h=2), _cfg(tmp_path), now=NOW)
    assert p["ok"] is False
    assert any("server_utc_offset_hours" in b for b in p["blocking"])
    # в сообщении должно быть КОНКРЕТНОЕ число для конфига, а не «поправьте смещение»
    assert "2" in " ".join(p["blocking"])
    with pytest.raises(EnvNotReady):
        require_tradable(p)


# --------------------------------------------------------------------------
# символы
# --------------------------------------------------------------------------

def test_absent_macro_symbols_marked_null(tmp_path):
    cfg = _cfg(tmp_path)
    present = set(cfg.instruments.whitelist) | {"USTEC", "SP500"}
    p = build_profile(FakeProbe(present=present), cfg, now=NOW)
    assert p["macro_symbols_available"]["USTEC"] is True
    assert p["macro_symbols_available"]["DXY"] is None, "отсутствующий макро-символ = null"
    assert p["ok"] is True, "макро-символы не блокируют торговлю"


def test_missing_whitelist_symbol_blocks_start(tmp_path):
    """Торговать тем, что не выбирается в терминале, нельзя — это не
    предупреждение, а стоп."""
    cfg = _cfg(tmp_path)
    present = set(cfg.instruments.whitelist) - {"XAUUSD"}
    p = build_profile(FakeProbe(present=present), cfg, now=NOW)
    assert p["ok"] is False
    assert any("XAUUSD" in b for b in p["blocking"])


def test_symbol_suffix_detected(tmp_path):
    """У другого брокера имена с суффиксом (XAUUSD.m) сломают раскладку пары
    на валюты в exposure.net_currency_exposure — профиль обязан зафиксировать
    суффикс, а не сделать вид, что символа нет."""
    cfg = _cfg(tmp_path)
    p = build_profile(FakeProbe(suffix=".m"), cfg, now=NOW)
    assert p["symbol_suffix"] == ".m"
    assert p["symbol_map"]["XAUUSD"] == "XAUUSD.m"
    assert p["ok"] is True


def test_no_suffix_on_clean_broker(tmp_path):
    p = build_profile(FakeProbe(), _cfg(tmp_path), now=NOW)
    assert p["symbol_suffix"] is None
    assert p["symbol_map"]["EURUSD"] == "EURUSD"


# --------------------------------------------------------------------------
# спред в барах и многопроцессность
# --------------------------------------------------------------------------

def test_bars_spread_reported(tmp_path):
    p = build_profile(FakeProbe(), _cfg(tmp_path), now=NOW)
    s = p["bars_have_spread"]
    assert s["filled_fraction"] == 1.0 and s["median"] == 20.0


def test_empty_bars_spread_is_warning_not_block(tmp_path):
    """Пустой spread в барах — известная особенность части брокеров: медиана
    спреда (задача 5.2) тогда считается по тикам. Это предупреждение."""
    p = build_profile(FakeProbe(bars=_bars(spread_filled=0.0)), _cfg(tmp_path), now=NOW)
    assert p["bars_have_spread"]["filled_fraction"] == 0.0
    assert p["ok"] is True
    assert any("spread" in w for w in p["warnings"])


# --------------------------------------------------------------------------
# fail-closed
# --------------------------------------------------------------------------

def test_trade_not_allowed_blocks_start(tmp_path):
    p = build_profile(FakeProbe(trade_allowed=False), _cfg(tmp_path), now=NOW)
    assert p["ok"] is False
    assert any("Algo Trading" in b for b in p["blocking"])
    with pytest.raises(EnvNotReady) as e:
        require_tradable(p)
    assert "Algo Trading" in str(e.value)


def test_probe_failure_is_fail_closed(tmp_path):
    """Терминал недоступен — профиль обязан быть НЕ ok. Профиль с дырами,
    у которого ok=True, хуже отсутствия профиля: по нему стартуют."""
    p = build_profile(FakeProbe(raises={"terminal": RuntimeError("MT5 not running")}),
                      _cfg(tmp_path), now=NOW)
    assert p["ok"] is False
    assert any("MT5" in b or "терминал" in b for b in p["blocking"])


def test_partial_probe_failure_keeps_field_null(tmp_path):
    """Отказ ОДНОГО детектора не роняет весь профиль: поле = null + причина."""
    p = build_profile(FakeProbe(raises={"bars": RuntimeError("no rates")}),
                      _cfg(tmp_path), now=NOW)
    assert p["bars_have_spread"] is None
    assert any("no rates" in w for w in p["warnings"])
    assert p["ok"] is True, "спред в барах не блокирует торговлю"


def test_multiprocess_failure_blocks_start(tmp_path):
    """Датчик — отдельный процесс к тому же терминалу (зонд Z1). Если второй
    процесс не подключается, стоп-крана в отдельном процессе не будет."""
    p = build_profile(FakeProbe(multiprocess=False), _cfg(tmp_path), now=NOW)
    assert p["ok"] is False
    assert any("процесс" in b for b in p["blocking"])


def test_require_tradable_passes_on_good_profile(tmp_path):
    require_tradable(build_profile(FakeProbe(), _cfg(tmp_path), now=NOW))


# --------------------------------------------------------------------------
# кэш профиля: переиспользование, устаревание, смена брокера
# --------------------------------------------------------------------------

def test_profile_written_and_reused(tmp_path):
    cfg, probe = _cfg(tmp_path), FakeProbe()
    p1, built1 = load_or_build(tmp_path / "env_profile.json", probe, cfg, now=NOW)
    assert built1 is True
    heavy_after_first = (probe.calls.get("select", 0), probe.calls.get("bars", 0))
    assert heavy_after_first[0] > 0

    p2, built2 = load_or_build(tmp_path / "env_profile.json", probe, cfg,
                               now=NOW + dt.timedelta(hours=1))
    assert built2 is False
    assert p2["server_utc_offset_hours"] == p1["server_utc_offset_hours"]
    assert (probe.calls.get("select", 0), probe.calls.get("bars", 0)) == heavy_after_first, \
        "тяжёлые опросы не должны повторяться при свежем профиле"
    assert probe.calls["terminal"] == 2, "опознание терминала делается КАЖДЫЙ раз"
    assert json.loads((tmp_path / "env_profile.json").read_text(encoding="utf-8"))["ok"] is True


def test_stale_profile_recomputed(tmp_path):
    """Переход на зимнее время меняет смещение сервера на час — профиль
    старше суток обязан пересчитаться, иначе граница дня уедет молча."""
    cfg, probe = _cfg(tmp_path), FakeProbe()
    load_or_build(tmp_path / "env_profile.json", probe, cfg, now=NOW)
    before = probe.calls.get("select", 0)
    _, built = load_or_build(tmp_path / "env_profile.json", probe, cfg,
                             now=NOW + dt.timedelta(hours=25))
    assert built is True
    assert probe.calls.get("select", 0) > before


def test_broker_change_recomputed(tmp_path):
    """Смена счёта/сервера — пересчёт немедленно, не по истечении суток."""
    cfg = _cfg(tmp_path)
    load_or_build(tmp_path / "env_profile.json", FakeProbe(), cfg, now=NOW)
    other = FakeProbe(server="OtherBroker-Live", login=1234)
    p, built = load_or_build(tmp_path / "env_profile.json", other, cfg,
                             now=NOW + dt.timedelta(minutes=5))
    assert built is True
    assert p["account"]["server"] == "OtherBroker-Live"


def test_trade_allowed_switched_off_seen_without_rebuild(tmp_path):
    """владелец счёта выключил Algo Trading при свежем профиле: старт обязан
    заблокироваться СЕЙЧАС, а не через сутки."""
    cfg = _cfg(tmp_path)
    load_or_build(tmp_path / "env_profile.json", FakeProbe(), cfg, now=NOW)
    off = FakeProbe(trade_allowed=False)
    p, _ = load_or_build(tmp_path / "env_profile.json", off, cfg,
                         now=NOW + dt.timedelta(minutes=5))
    assert p["ok"] is False
    assert any("Algo Trading" in b for b in p["blocking"])


def test_not_ok_profile_is_never_cached(tmp_path):
    """Профиль с ok=False обязан пересчитываться КАЖДЫЙ раз, а не жить сутки.

    Иначе исправленная причина отказа (символ появился у брокера, второй
    процесс заработал) признаётся только по истечении срока жизни профиля:
    трейдер починил среду, а контур сутки отвечает «стартовать нельзя» по
    фактам, собранным до починки. Пересчёт вердикта по кэшу здесь не спасает —
    в кэше лежат СТАРЫЕ факты.
    """
    cfg = _cfg(tmp_path)
    broken = FakeProbe(present=set(cfg.instruments.whitelist) - {"XAUUSD"})
    p1, _ = load_or_build(tmp_path / "env_profile.json", broken, cfg, now=NOW)
    assert p1["ok"] is False

    fixed = FakeProbe()
    p2, built = load_or_build(tmp_path / "env_profile.json", fixed, cfg,
                              now=NOW + dt.timedelta(minutes=5))
    assert built is True, "не-ok профиль не имеет права переиспользоваться"
    assert p2["ok"] is True and p2["symbols_missing"] == []


def test_corrupted_profile_recomputed(tmp_path):
    (tmp_path / "env_profile.json").write_text("{битый", encoding="utf-8")
    _, built = load_or_build(tmp_path / "env_profile.json", FakeProbe(), _cfg(tmp_path), now=NOW)
    assert built is True


def test_check_against_config_lists_mismatches(tmp_path):
    p = build_profile(FakeProbe(offset_h=2), _cfg(tmp_path), now=NOW)
    problems = check_against_config(p, _cfg(tmp_path))
    assert problems and any("server_utc_offset_hours" in x for x in problems)
    assert check_against_config(build_profile(FakeProbe(), _cfg(tmp_path), now=NOW),
                                _cfg(tmp_path)) == []


# --------------------------------------------------------------------------
# медиана спреда пересчитывается раз в сутки — но её обязан кто-то звать
# --------------------------------------------------------------------------

def test_main_recomputes_spread_median_via_update_medians(tmp_path, monkeypatch):
    """РЕГРЕСС 2026-07-29: update_medians существовала и была покрыта
    unit-тестами, но её никто не вызывал ни из одного скрипта — 2026-07-27
    06:24 медиана XAUUSD застыла на первом замере, и XAUUSD просидел в
    исключении гейта спреда 48+ часов подряд без единого снятия, хотя живой
    спред неоднократно подходил к порогу возврата вплотную. bootstrap_env —
    единственная точка, гарантированно вызываемая раз за сессию, поэтому
    пересчёт живёт здесь; тест ловит будущий разрыв связи, а не только
    корректность самой функции."""
    import scripts.bootstrap_env as be
    from trader_lib.mt5_client import FakeMarket

    monkeypatch.setattr(be, "state_dir", lambda cfg: str(tmp_path))

    class FailingProbe:
        def terminal(self):
            raise RuntimeError("терминал недоступен — не важно для этого теста")

    monkeypatch.setattr(be, "live_probe", lambda: FailingProbe())
    monkeypatch.setattr(be, "live_market", lambda: FakeMarket(spread_points=25))

    be.main(["--quiet"])

    doc = json.loads((tmp_path / "spread_median.json").read_text(encoding="utf-8"))
    computed = dt.datetime.fromisoformat(doc["computed_utc"])
    age_s = (dt.datetime.now(UTC) - computed).total_seconds()
    assert age_s < 30, "медиана не пересчиталась при вызове main()"
    assert "XAUUSD" in doc["medians"]


def test_main_force_refreshes_news_cache(tmp_path, monkeypatch):
    """РЕГРЕСС 2026-07-31: календарь новостей протух ПОСРЕДИ сессии и заблокировал
    живой вход.

    Кэш взят накануне 06:30 при суточном лимите; утренний брифинг прошёл в 06:12,
    когда кэшу было 23.7ч — формально свежий, сеть не дёрнулась. Через 18 минут
    кэш пересёк границу суток, и обновить его внутри дня стало нечем: гейт по
    сети не ходит принципиально, а восприятие за день больше не вызывается.
    Утренняя подготовка обязана обновлять календарь безусловно, иначе её
    результат зависит от того, на сколько минут она разминулась с границей.
    """
    import scripts.bootstrap_env as be
    from trader_lib.mt5_client import FakeMarket

    monkeypatch.setattr(be, "state_dir", lambda cfg: str(tmp_path))

    class FailingProbe:
        def terminal(self):
            raise RuntimeError("терминал недоступен — не важно для этого теста")

    monkeypatch.setattr(be, "live_probe", lambda: FailingProbe())
    monkeypatch.setattr(be, "live_market", lambda: FakeMarket(spread_points=25))

    seen = {}

    def fake_load_windows(path, *, cfg, now, loader=None, force=False):
        seen["force"] = force
        return {"windows": [], "stale": False, "source": "network", "ambiguous": []}

    monkeypatch.setattr(be, "load_windows", fake_load_windows)

    be.main(["--quiet"])

    assert seen.get("force") is True, "утренняя подготовка обязана звать календарь с force"


def test_main_rebuilds_correlation_clusters(tmp_path, monkeypatch):
    """СТОРОЖ ЖИВОСТИ (Ф2). Дважды за неделю находил код, который написан,
    покрыт тестами и никем не вызывается: update_medians (медиана спреда
    застыла на 48 часов) и net_currency_exposure (валютная раскладка, мёртвая
    с рождения). Карта кластеров без пересчёта повторит их судьбу — гейт будет
    честно сообщать «карта не построена» и пропускать всё подряд.

    bootstrap_env — единственная точка, гарантированно вызываемая раз за
    сессию, поэтому пересчёт живёт здесь.
    """
    import scripts.bootstrap_env as be
    from trader_lib.mt5_client import FakeMarket

    monkeypatch.setattr(be, "state_dir", lambda cfg: str(tmp_path))

    class FailingProbe:
        def terminal(self):
            raise RuntimeError("терминал недоступен — не важно для этого теста")

    monkeypatch.setattr(be, "live_probe", lambda: FailingProbe())
    monkeypatch.setattr(be, "live_market", lambda: FakeMarket(spread_points=25))
    monkeypatch.setattr(be, "load_windows",
                        lambda *a, **k: {"windows": [], "stale": False,
                                         "source": "network", "ambiguous": []})

    be.main(["--quiet"])

    doc = json.loads((tmp_path / "clusters.json").read_text(encoding="utf-8"))
    assert doc["computed_utc"], "карта обязана строиться при подготовке сессии"
    assert doc["threshold"] > 0
