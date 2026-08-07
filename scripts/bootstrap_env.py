"""Автодетект среды (задача 0.3): что за терминал, брокер и символы вокруг.

Пишет `state_dir/env_profile.json` и отвечает на один вопрос: можно ли вообще
стартовать торговый контур на этом ПК. Заменяет ручные зонды Z2/Z3/Z4 — при
смене брокера, терминала или переходе на зимнее время профиль пересчитается
сам, а не будет молча врать числами, вписанными однажды руками.

ДВА ПРИНЦИПА, из которых выведено остальное:

1. **Профиль не правит конституцию.** Единственный источник лимитов и
   серверного смещения — `config/trader.config.json`. Если обнаруженное
   смещение расходится с конфигом, профиль СТОПОРИТ старт и называет точное
   число для правки, но сам конфиг не переписывает: смещение задаёт границу
   торгового дня, от которой отмеряется стена −3%, и код, тихо меняющий
   основание этого расчёта, страшнее неверного числа — неверное число хотя бы
   видно.

2. **Fail-closed по отдельности.** Отказ детектора, без которого торговать
   нельзя (терминал, Algo Trading, смещение, символы whitelist, второй
   процесс), даёт `ok=False`. Отказ детектора, без которого можно (спред в
   барах, макро-символы), даёт `null` в поле и запись в `warnings`. Профиль с
   дырами и `ok=True` был бы худшим исходом: по нему стартуют.

ПРЕДОХРАНИТЕЛЯ ПО ТИПУ СЧЁТА ЗДЕСЬ НЕТ — решение владельца счёта от 2026-07-26. Система
не делит демо и реальные средства: риск-контур один и тот же (стены 3%/6%,
лимит одновременного риска, каскад по серии убытков, обязательный SL). Если
ограничение когда-нибудь понадобится, это одна проверка `trade_mode` в
`_verdict`.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.alerts import write_alerts_atomic                # noqa: E402
from trader_lib.clusters import rebuild_clusters                  # noqa: E402
from trader_lib.config import load_config, state_dir             # noqa: E402
from trader_lib.mt5_client import live_market                    # noqa: E402
from trader_lib.news import load_windows                         # noqa: E402
from trader_lib.spread_gate import update_medians                # noqa: E402

UTC = dt.timezone.utc

PROFILE_VERSION = 1
PROFILE_NAME = "env_profile.json"
MAX_AGE_HOURS = 24
SPREAD_BARS = 2000
SPREAD_TF = "M5"

# Макро-контекст: недоступные у брокера символы фиксируются как null навсегда
# (для этого профиля) и торговлю не блокируют — это фон, а не инструмент.
MACRO_SYMBOLS = ("DXY", "USDX", "US10Y", "USTEC", "SP500", "XTIUSD", "BRENT", "VIX")

# Порядок важен: пустой суффикс проверяется первым, иначе у брокера, где есть и
# XAUUSD, и XAUUSD.m, мы бы «нашли» суффикс там, где его нет.
SUFFIX_CANDIDATES = ("", ".m", "m", ".raw", ".pro", ".ecn", "-ECN", "_ECN", ".a", ".c", ".s")

# доля непустого spread в барах, ниже которой медиану спреда (задача 5.2)
# придётся считать по тикам, а не по барам
SPREAD_FILLED_MIN = 0.5


class EnvNotReady(RuntimeError):
    """Среда не готова к торговле. Поднимается require_tradable — единственная
    точка, где профиль превращается в запрет старта."""


class EnvProbe(Protocol):
    """Что нужно знать о среде. Живая реализация — live_probe(), в тестах
    подставной зонд: весь модуль обязан быть проверяем офлайн."""

    def terminal(self) -> dict: ...
    def account(self) -> dict: ...
    def tick_time(self, symbol: str): ...
    def select(self, symbol: str) -> bool: ...
    def bars(self, symbol: str, timeframe: str, count: int): ...
    def multiprocess_ok(self) -> bool: ...


# --------------------------------------------------------------------------
# отдельные детекторы (чистые там, где это возможно)
# --------------------------------------------------------------------------

# Реально встречающиеся смещения брокеров: от UTC−12 до UTC+14. Всё, что вне
# этого диапазона, означает не экзотический часовой пояс, а УСТАРЕВШИЙ ТИК —
# последняя котировка пришла не «только что», и разность «тик минус сейчас»
# измеряет возраст тика, а не смещение сервера.
MIN_OFFSET_HOURS, MAX_OFFSET_HOURS = -12.0, 14.0


def detect_offset_hours(server_naive, now_utc):
    """Смещение серверного времени брокера от UTC, округлённое до 0.5 часа.
    None — определить нельзя (см. ниже).

    Округление обязательно: время последнего тика отстаёт от «сейчас» на
    секунды-минуты, и без округления смещение получалось бы дробным и дёргалось
    между запусками. Полчаса — минимальный шаг, встречающийся у брокеров
    реально (UTC+5:30).

    ПРОВЕРКА ПРАВДОПОДОБИЯ ОБЯЗАТЕЛЬНА. Метод опирается на допущение «последний
    тик пришёл только что», и в выходные оно ложно: на закрытом рынке последняя
    котировка пятничная, и расчёт даёт что-нибудь вроде −42.5 часа. Такое число
    нельзя ни использовать, ни записать в конституцию — возвращаем None, и
    вызывающий обязан заблокировать старт с понятной причиной, а не работать с
    границей дня, уехавшей на двое суток. (Найдено первым живым запуском
    bootstrap_env в субботу.)
    """
    if server_naive is None:
        return None
    delta_h = (server_naive.replace(tzinfo=UTC) - now_utc).total_seconds() / 3600.0
    offset = round(delta_h * 2) / 2
    if not (MIN_OFFSET_HOURS <= offset <= MAX_OFFSET_HOURS):
        return None
    return offset


def _detect_symbols(probe, names) -> tuple:
    """Разрешает имена символов в те, что реально выбираются в терминале.

    Возвращает (map, suffixes, missing). Суффикс — не косметика: имя вида
    XAUUSD.m ломает раскладку пары на валюты в exposure.net_currency_exposure
    (там намеренно ValueError, а не угадывание), поэтому нормализация имён
    живёт выше по стеку, а профиль фиксирует, какой суффикс у этого брокера.
    """
    resolved, suffixes, missing = {}, [], []
    for name in names:
        for suffix in SUFFIX_CANDIDATES:
            candidate = name + suffix
            try:
                ok = probe.select(candidate)
            except Exception:  # noqa: BLE001 - отказ по одному имени не роняет обход
                ok = False
            if ok:
                resolved[name] = candidate
                suffixes.append(suffix)
                break
        else:
            missing.append(name)
    return resolved, suffixes, missing


def _detect_bars_spread(probe, symbol, count):
    """Заполнено ли поле spread в барах: доля непустых, медиана, p95."""
    df = probe.bars(symbol, SPREAD_TF, count)
    if df is None or len(df) == 0 or "spread" not in df:
        return None
    col = df["spread"].astype(float)
    filled = col[col > 0]
    return {"bars": int(len(col)),
            "filled_fraction": round(float(len(filled)) / len(col), 4),
            "median": float(filled.median()) if len(filled) else None,
            "p95": float(filled.quantile(0.95)) if len(filled) else None}


# --------------------------------------------------------------------------
# сбор фактов и вердикт
# --------------------------------------------------------------------------

def _facts(probe, cfg, *, now, spread_bars, macro):
    """Тяжёлая часть: символы, бары, второй процесс, серверное время.

    Каждый детектор в своём try: отказ одного не имеет права оставить остальные
    поля неизвестными — иначе профиль зависит от порядка проверок.
    """
    facts = {"warnings": []}
    whitelist = list(cfg.instruments.whitelist)
    primary = whitelist[0] if whitelist else "XAUUSD"

    resolved, suffixes, missing = _detect_symbols(probe, whitelist)
    facts["symbol_map"] = resolved
    facts["symbols_missing"] = missing
    uniq = sorted(set(suffixes))
    if len(uniq) == 1 and uniq[0] != "":
        facts["symbol_suffix"] = uniq[0]
    elif len(uniq) > 1:
        facts["symbol_suffix"] = None
        facts["warnings"].append(
            f"у брокера разные суффиксы символов {uniq}: нормализация имён на стороне "
            "вызывающего обязательна, единый суффикс профиль зафиксировать не может")
    else:
        facts["symbol_suffix"] = None

    macro_map, _, macro_missing = _detect_symbols(probe, list(macro))
    # None, а не False: «у этого брокера такого символа нет», а не «есть и выключен»
    facts["macro_symbols_available"] = {
        name: (True if name in macro_map else None) for name in macro}
    facts["macro_symbol_map"] = macro_map
    if macro_missing:
        facts["warnings"].append(
            f"макро-символы недоступны у брокера: {macro_missing} — соответствующий "
            "контекст остаётся null, торговлю не блокирует")

    # Смещение берём по САМОМУ СВЕЖЕМУ тику среди доступных символов, а не по
    # одному: у неликвидного инструмента последняя котировка может быть
    # часовой давности, и она сдвинет расчёт ровно на свой возраст.
    seen = []
    for name in [primary] + [s for s in whitelist if s != primary]:
        try:
            ts = probe.tick_time(facts["symbol_map"].get(name, name))
        except Exception as e:  # noqa: BLE001 - один символ не роняет обход
            facts["warnings"].append(f"время тика по {name} не прочитано: {e}")
            continue
        if ts is not None:
            seen.append(ts)
    server_naive = max(seen) if seen else None
    facts["server_time_seen"] = server_naive.isoformat() if server_naive else None
    facts["server_utc_offset_hours"] = detect_offset_hours(server_naive, now)
    if server_naive is None:
        facts["warnings"].append("серверное время не определено: ни по одному символу "
                                 "нет времени последнего тика")
    elif facts["server_utc_offset_hours"] is None:
        # это НЕ экзотический часовой пояс — это устаревший тик
        age_h = (now - server_naive.replace(tzinfo=UTC)).total_seconds() / 3600.0
        facts["warnings"].append(
            f"серверное время не определено: последний тик пришёл {age_h:.1f} ч назад "
            "(рынок закрыт?), и разность «тик минус сейчас» измеряет возраст тика, "
            "а не смещение сервера — запусти при открытом рынке")

    try:
        facts["bars_have_spread"] = _detect_bars_spread(
            probe, facts["symbol_map"].get(primary, primary), spread_bars)
    except Exception as e:  # noqa: BLE001
        facts["bars_have_spread"] = None
        facts["warnings"].append(f"спред в барах не определён: {e}")
    spread = facts["bars_have_spread"]
    if spread is not None and spread["filled_fraction"] < SPREAD_FILLED_MIN:
        facts["warnings"].append(
            f"поле spread в барах заполнено на {spread['filled_fraction']:.0%} — медиану "
            "спреда (задача 5.2) считать по тикам, а не по барам")

    try:
        facts["mt5_multiprocess_ok"] = probe.multiprocess_ok()
    except Exception as e:  # noqa: BLE001
        facts["mt5_multiprocess_ok"] = None
        facts["warnings"].append(f"второй процесс к терминалу не проверен: {e}")

    return facts


def _verdict(profile, cfg):
    """Единственное место, где факты превращаются в «стартовать нельзя».

    Держится отдельно от сбора фактов, потому что при переиспользовании
    кэшированного профиля вердикт пересчитывается заново по свежему состоянию
    терминала: Algo Trading выключили минуту назад — старт обязан
    заблокироваться сейчас, а не по истечении суток жизни профиля.
    """
    blocking = []

    if profile.get("trade_allowed") is not True:
        blocking.append("в терминале выключен Algo Trading — включи его "
                        "(Сервис → Настройки → Советники), торговля не стартует")

    detected = profile.get("server_utc_offset_hours")
    expected = cfg.risk.server_utc_offset_hours
    if detected is None:
        blocking.append("server_utc_offset_hours не определён: без серверного смещения "
                        "граница торгового дня неизвестна, а от неё считается стена −3%")
    elif float(detected) != float(expected):
        blocking.append(
            f"server_utc_offset_hours: обнаружено {detected}, в конфиге {expected}. "
            f"Профиль не правит конституцию — впиши {detected} в config/trader.config.json "
            "(risk.server_utc_offset_hours) и перезапусти")

    missing = profile.get("symbols_missing") or []
    if missing:
        blocking.append(f"символы whitelist не выбираются в терминале: {missing} — "
                        "торговать тем, чего нет у брокера, нельзя")

    if profile.get("mt5_multiprocess_ok") is not True:
        blocking.append("второй процесс Python не подключается к терминалу: датчик "
                        "пробуждения и стоп-кран живут отдельным процессом, без этого "
                        "их не запустить")

    return blocking


def build_profile(probe, cfg, *, now, spread_bars=SPREAD_BARS, macro=MACRO_SYMBOLS,
                  terminal=None, account=None):
    """Полный профиль среды. terminal/account можно передать уже опрошенными —
    load_or_build так и делает, чтобы опознание терминала не стоило двух
    round-trip к MT5 за один запуск."""
    profile = {"version": PROFILE_VERSION, "built_utc": now.isoformat(),
               "checked_utc": now.isoformat(),
               "config_expected_offset_hours": cfg.risk.server_utc_offset_hours,
               "warnings": [], "blocking": [], "ok": False}
    try:
        term = terminal if terminal is not None else probe.terminal()
    except Exception as e:  # noqa: BLE001 - без терминала остальное бессмысленно
        profile["terminal"] = None
        profile["trade_allowed"] = None
        profile["blocking"] = [f"терминал MT5 недоступен: {e}"]
        return profile
    profile["terminal"] = term
    profile["trade_allowed"] = bool(term.get("trade_allowed"))

    try:
        profile["account"] = account if account is not None else probe.account()
    except Exception as e:  # noqa: BLE001 - опознание счёта не блокирует
        profile["account"] = None
        profile["warnings"].append(f"счёт не опознан: {e}")

    profile.update(_facts(probe, cfg, now=now, spread_bars=spread_bars, macro=macro))
    profile["blocking"] = _verdict(profile, cfg)
    profile["ok"] = not profile["blocking"]
    return profile


def check_against_config(profile, cfg):
    """Расхождения профиля с конституцией — списком, без вердикта.

    Отдельно от _verdict, чтобы этим мог пользоваться отчёт и проверка
    конституции (задача 8.2): «что в конфиге не соответствует реальности».
    """
    problems = []
    detected = profile.get("server_utc_offset_hours")
    if detected is not None and float(detected) != float(cfg.risk.server_utc_offset_hours):
        problems.append(f"risk.server_utc_offset_hours={cfg.risk.server_utc_offset_hours}, "
                        f"у брокера {detected}")
    for name in profile.get("symbols_missing") or []:
        problems.append(f"instruments.whitelist: символа {name} нет у брокера")
    if profile.get("symbol_suffix"):
        problems.append(f"имена символов у брокера с суффиксом {profile['symbol_suffix']!r}: "
                        "whitelist в конфиге записан без него, нормализация обязательна")
    return problems


def require_tradable(profile):
    """Фраза «торговля не стартует» становится исключением здесь. Вызывать
    ПЕРЕД любым торговым действием (задача 4.1) и при аварийном взводе датчика.
    """
    if not profile.get("ok"):
        raise EnvNotReady("среда не готова: " + "; ".join(profile.get("blocking") or ["?"]))
    return profile


# --------------------------------------------------------------------------
# кэш профиля
# --------------------------------------------------------------------------

def _read_profile(path):
    p = Path(path)
    if not p.exists():
        return None, "профиля нет"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - битый профиль = профиля нет
        return None, f"профиль не прочитан ({e})"
    if not isinstance(data, dict) or data.get("version") != PROFILE_VERSION:
        return None, "версия профиля другая"
    return data, None


def _needs_rebuild(cached, *, terminal, account, now, max_age_hours):
    """Почему профиль пересчитывается. Возвращает причину или None.

    Смена брокера/терминала — немедленный пересчёт, не по истечении суток:
    иначе на новом ПК первые сутки работали бы чужие суффиксы символов и чужое
    серверное смещение. Профиль с ok=False тоже пересчитывается всегда — иначе
    исправленная причина (символ появился, второй процесс заработал) была бы
    признана только через сутки.
    """
    if cached is None:
        return "профиля нет"
    if not cached.get("ok"):
        return "прошлый профиль был не ok — проверяем заново"
    built = cached.get("built_utc")
    try:
        age_h = (now - dt.datetime.fromisoformat(built)).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return "built_utc не разобран"
    if age_h >= max_age_hours or age_h < 0:
        return f"профилю {age_h:.1f}ч (лимит {max_age_hours}ч)"
    old_acc = cached.get("account") or {}
    new_acc = account or {}
    if (old_acc.get("server"), old_acc.get("login")) != (new_acc.get("server"),
                                                         new_acc.get("login")):
        return (f"счёт другой: было {old_acc.get('server')}/{old_acc.get('login')}, "
                f"стало {new_acc.get('server')}/{new_acc.get('login')}")
    if (cached.get("terminal") or {}).get("name") != (terminal or {}).get("name"):
        return "терминал другой"
    return None


def load_or_build(path, probe, cfg, *, now, max_age_hours=MAX_AGE_HOURS, force=False,
                  spread_bars=SPREAD_BARS, macro=MACRO_SYMBOLS):
    """Профиль из файла или заново. Возвращает (профиль, пересчитан ли).

    Опознание терминала и счёта делается ВСЕГДА — это два дешёвых вызова, и
    только они отвечают на вопросы «тот же брокер?» и «Algo Trading всё ещё
    включён?». Тяжёлая часть (обход символов, 2000 баров, второй процесс)
    переиспользуется, пока профиль свежий и брокер тот же.
    """
    path = Path(path)
    try:
        terminal = probe.terminal()
    except Exception as e:  # noqa: BLE001 - терминала нет: строим ok=False профиль
        profile = build_profile(probe, cfg, now=now, spread_bars=spread_bars, macro=macro)
        profile.setdefault("blocking", []).append(f"опрос терминала не удался: {e}")
        profile["ok"] = False
        _write(path, profile)
        return profile, True
    try:
        account = probe.account()
    except Exception:  # noqa: BLE001 - разберётся build_profile/вердикт
        account = None

    cached, read_note = _read_profile(path)
    reason = "принудительный пересчёт" if force else _needs_rebuild(
        cached, terminal=terminal, account=account, now=now, max_age_hours=max_age_hours)
    if reason is None and read_note:
        reason = read_note

    if reason is not None:
        profile = build_profile(probe, cfg, now=now, spread_bars=spread_bars, macro=macro,
                                terminal=terminal, account=account)
        profile["rebuilt_because"] = reason
        _write(path, profile)
        return profile, True

    # свежий профиль того же брокера: факты берём из кэша, вердикт — заново по
    # свежему состоянию терминала
    profile = dict(cached)
    profile["terminal"] = terminal
    profile["account"] = account
    profile["trade_allowed"] = bool(terminal.get("trade_allowed"))
    profile["checked_utc"] = now.isoformat()
    profile["config_expected_offset_hours"] = cfg.risk.server_utc_offset_hours
    profile["blocking"] = _verdict(profile, cfg)
    profile["ok"] = not profile["blocking"]
    _write(path, profile)
    return profile, False


def _write(path, profile):
    write_alerts_atomic(path, profile)


# --------------------------------------------------------------------------
# живой зонд и CLI
# --------------------------------------------------------------------------

def live_probe():
    """Реальная реализация поверх пакета MetaTrader5 (только на ПК трейдера)."""
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    class Live:
        def terminal(self):
            t = mt5.terminal_info()
            if t is None:
                raise RuntimeError(f"terminal_info недоступен: {mt5.last_error()}")
            return {"name": t.name, "build": t.build, "connected": t.connected,
                    "trade_allowed": t.trade_allowed, "path": t.path}

        def account(self):
            a = mt5.account_info()
            if a is None:
                raise RuntimeError(f"account_info недоступен: {mt5.last_error()}")
            # trade_mode фиксируем как факт среды; предохранителя по нему НЕТ
            # (решение владельца счёта) — поле нужно отчётам, чтобы в них было видно,
            # на каком счёте получены числа
            return {"login": a.login, "server": a.server, "currency": a.currency,
                    "trade_mode": a.trade_mode, "leverage": a.leverage}

        def tick_time(self, symbol):
            mt5.symbol_select(symbol, True)
            t = mt5.symbol_info_tick(symbol)
            if t is None or not t.time:
                return None
            # время тика — серверное, в epoch-секундах; наивный datetime здесь
            # намеренный: detect_offset_hours сам сравнит его с UTC
            return dt.datetime.fromtimestamp(t.time, UTC).replace(tzinfo=None)

        def select(self, symbol):
            return bool(mt5.symbol_select(symbol, True) and mt5.symbol_info(symbol))

        def bars(self, symbol, timeframe, count):
            import pandas as pd
            tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                  "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}[timeframe]
            r = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if r is None or len(r) == 0:
                raise RuntimeError(f"нет баров {symbol} {timeframe}: {mt5.last_error()}")
            return pd.DataFrame(r)

        def multiprocess_ok(self):
            """Второй процесс Python к тому же терминалу (зонд Z1 локально).

            Проверяется дочерним процессом, а не в этом: смысл именно в том,
            что подключений два одновременно — датчик держит своё постоянно,
            пока цикл восприятия стартует рядом.
            """
            code = ("import MetaTrader5 as m, sys;"
                    "sys.exit(0 if m.initialize() and m.account_info() else 1)")
            try:
                r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                   timeout=60)
            except Exception:  # noqa: BLE001
                return None
            return r.returncode == 0

    return Live()


def main(argv=None):
    ap = argparse.ArgumentParser(description="автодетект среды → env_profile.json")
    ap.add_argument("--config", default="config/trader.config.json")
    ap.add_argument("--force", action="store_true", help="пересчитать, игнорируя кэш")
    ap.add_argument("--quiet", action="store_true", help="печатать только вердикт")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    path = Path(state_dir(cfg)) / PROFILE_NAME
    profile, rebuilt = load_or_build(path, live_probe(), cfg,
                                     now=dt.datetime.now(UTC), force=a.force)

    # Медианы спреда (задача 5.2) обязаны пересчитываться раз в сутки — но
    # update_medians больше нигде не вызывался, и 2026-07-27..29 медиана
    # застыла на первом замере на 48+ часов, пока XAUUSD сидел в исключении
    # гейта. bootstrap_env — единственная точка, гарантированно вызываемая
    # раз за сессию, поэтому пересчёт живёт здесь. Функция сама решает,
    # рано ли (кэш моложе RECOMPUTE_HOURS) — вызов дешёвый в обычный день.
    try:
        update_medians(live_market(), cfg,
                       Path(state_dir(cfg)) / "spread_median.json",
                       now=dt.datetime.now(UTC), force=a.force)
    except Exception:  # noqa: BLE001 - отсутствие рынка не должно ронять bootstrap
        pass

    # Календарь новостей — БЕЗУСЛОВНО, а не «если протух». РЕГРЕСС 2026-07-31:
    # кэш взят накануне в 06:30 при суточном лимите, утренний брифинг прошёл в
    # 06:12 — кэшу 23.7ч, формально свежий, сеть не дёрнулась. Через 18 минут
    # он пересёк границу суток и заблокировал живой вход, а обновить его внутри
    # дня стало нечем: гейт по сети не ходит принципиально, восприятие за день
    # больше не вызывается. Утренняя подготовка не должна зависеть от того, на
    # сколько минут она разминулась с границей.
    try:
        load_windows(Path(state_dir(cfg)) / "news_cache.json", cfg=cfg,
                     now=dt.datetime.now(UTC), force=True)
    except Exception:  # noqa: BLE001 - нет сети: гейт сам увидит устаревший кэш
        pass

    # Карта корреляционных кластеров (Ф2). Без неё проверка в гейте честно
    # сообщает «карта не построена» и пропускает всё подряд — то есть команда
    # снова может собрать одну ставку втроём. Замер 2026-08-01: девять
    # инструментов whitelist складываются в ПЯТЬ независимых факторов.
    # Корреляции нестационарны, поэтому пересчёт раз за сессию, а не однажды.
    try:
        rebuild_clusters(live_market(), cfg, Path(state_dir(cfg)) / "clusters.json",
                         now=dt.datetime.now(UTC))
    except Exception:  # noqa: BLE001 - нет истории: гейт увидит пустую карту
        pass

    if a.quiet:
        print(json.dumps({"ok": profile["ok"], "blocking": profile["blocking"],
                          "rebuilt": rebuilt, "path": str(path)},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(profile, ensure_ascii=False, indent=2, default=str))
    # ненулевой код возврата — чтобы обёртка (задача 9.1) могла остановить
    # запуск контура, не разбирая JSON
    return 0 if profile["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
