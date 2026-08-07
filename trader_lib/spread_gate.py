"""Спред-гейт и авто-исключение инструмента (задача 5.2).

Спред — единственная издержка, которая меняется на порядок за секунды и делает
нормальную сделку заведомо убыточной. Сравнивать текущий спред не с чем в
момент, когда он уже разъехался, поэтому «нормально» считается заранее: медиана
по истории, отдельно для каждого символа и брокера, и хранится на диске
(spread_median.json).

ГИСТЕРЕЗИС — НЕ УКРАШЕНИЕ. Инструмент исключается при превышении порога
(cfg.instruments.spread_anomaly_mult × медиана) и возвращается в торговлю
только когда спред пришёл К МЕДИАНЕ, а не когда опустился чуть ниже порога.
Без этого на границе он мигал бы вход-выход каждую секунду, и решение модели
зависело бы от того, в какую секунду она посмотрела. Исключение живёт в файле:
перезапуск процесса не возвращает инструмент в торговлю.

ПЕРЕСЧЁТ РАЗ В СУТКИ. Гейт и датчик зовут это часто, а 1500 баров по семи
символам — не та цена, которую стоит платить на каждом тике.

ЕСЛИ У БРОКЕРА ПУСТОЕ ПОЛЕ spread В БАРАХ (это обнаруживает bootstrap_env,
задача 0.3), медиана набирается ежедневными замерами текущего спреда. Пока
замеров меньше трёх, медианы нет — и это честное «неизвестно», а не подстановка
правдоподобного числа.

НЕИЗВЕСТНАЯ МЕДИАНА НЕ БЛОКИРУЕТ. В первые дни её просто нет, и запрет всего
до её накопления запретил бы ровно ту торговлю, которая её накапливает.
Реальная защита от дорогого входа в этот период — costs_R в scripts/enter.py:
он считает издержки по ФАКТИЧЕСКОМУ спреду и отклоняет сделку, где они съедают
риск. Здесь же выставляется флаг median_unknown, и он попадает в журнал.
"""
import datetime as dt
import json
from pathlib import Path

import numpy as np

UTC = dt.timezone.utc

MEDIAN_TF = "M5"
BARS_PER_DAY = 288          # M5-баров в сутках
MIN_SAMPLES = 3             # меньше — медианы нет, а не «примерно столько»
RECOMPUTE_HOURS = 24
EMPTY = {"medians": {}, "excluded": {}, "samples": {}, "source": {}}


class LiveSpreadWindow:
    """Скользящее окно ЖИВЫХ замеров спреда, по одному на символ.

    ЗАЧЕМ ОНО ВООБЩЕ. Медиана из баров меряет спред на ЗАКРЫТИИ свечи — то есть
    в самый спокойный момент минуты, — а решения принимаются в активные. За
    неделю 2026-07-27..31 расхождение стоило дорого: 9 входов отклонены гейтом
    по спреду, шесть из них при ×1.05 (20 пунктов против барной медианы 19 —
    разница в ОДИН пункт), и обе упущенные прибыльные сделки (MFE 3.99 и 4.89
    ATR) заблокированы именно так. Требование «вернись к ratio ≤ 1.0»
    оказывалось недостижимым не из-за рынка, а из-за того, чем меряли.

    ПОЧЕМУ ЧАС, А НЕ СУТКИ. В суточную выборку попадают в основном тихие часы —
    их просто больше, — и медиана оседает на ночном штиле. Часовое окно
    сравнивает спред с тем, каким он был последние шестьдесят минут, то есть с
    текущими условиями сессии. Вопрос гейта тогда звучит правильно: «это выброс
    относительно сейчас?», а не «это шире, чем бывает ночью?».

    ПОЧЕМУ АДАПТИВНАЯ БАЗА БЕЗОПАСНА. Если спред широк устойчиво целый час,
    медиана подтянется и относительный порог перестанет его ловить. Это
    допустимо ровно потому, что абсолютная защита живёт отдельно: costs_R в
    scripts/enter.py отклоняет сделку по ФАКТИЧЕСКОЙ стоимости независимо от
    любой медианы. Относительный порог ловит СОБЫТИЯ, абсолютный — ЭКОНОМИКУ.

    Хранение — поминутные гистограммы в кольце: спред в пунктах есть небольшое
    целое, поэтому счётчики точны и компактны, а медиана считается по
    объединению корзин без потери точности.
    """

    def __init__(self, minutes=60):
        self.minutes = minutes
        # {символ: {метка_минуты: {пункты: счётчик}}}
        self._buckets = {}

    @staticmethod
    def _stamp(now):
        return int(now.timestamp() // 60)

    def _live(self, symbol, *, upto):
        oldest = upto - self.minutes + 1
        per_symbol = self._buckets.get(symbol) or {}
        stale = [k for k in per_symbol if k < oldest]
        for k in stale:
            del per_symbol[k]
        return per_symbol

    def observe(self, symbol, points, *, now):
        """Один замер. Ноль/None/отрицательное — закрытый рынок или обрыв
        терминала: такие значения базу не формируют."""
        try:
            value = int(points)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        stamp = self._stamp(now)
        per_symbol = self._buckets.setdefault(symbol, {})
        bucket = per_symbol.setdefault(stamp, {})
        bucket[value] = bucket.get(value, 0) + 1
        self._live(symbol, upto=stamp)

    def _counts(self, symbol, *, now):
        """Слитые счётчики окна, отмеренного от НАСТОЯЩЕГО «сейчас».

        БАГ, найденный трейдером `fade` 2026-08-03 на первом живом дне команды.
        Здесь стояло `upto=max(per_symbol)` — окно мерилось от самой свежей
        корзины ЭТОГО ЖЕ СИМВОЛА. Пока символ наблюдается, разницы нет; стоит
        ему замолчать — его последние замеры остаются «живыми» бессрочно, потому
        что относительно самих себя они всегда свежие.

        Что это давало на практике. Датчик опрашивает только символы, упомянутые
        в чьих-то алертах или позициях. EURUSD сегодня не был упомянут ни у кого
        до 06:50 — и в окне лежали корзины от 01.08 16:55 с медианой 19 при
        реальной сегодняшней 13. Гейт сравнивал бы сегодняшний вход с ПЯТНИЧНОЙ
        базой, причём молча и правдоподобно: `samples()` рапортовал бы полную
        выборку, а `median()` — уверенное число.

        Направление ошибки не фиксировано, и это хуже, чем систематический сдвиг:
        протухшая ВЫСОКАЯ база делает сегодняшний спред обманчиво узким (гейт
        пропускает то, что должен резать), протухшая НИЗКАЯ — обманчиво широким
        (гейт режет здоровые входы, ровно как на неделе 27–31.07).

        Теперь `now` обязателен: время нельзя «не знать» при разговоре о
        свежести. Пустой ответ на протухшем окне — правильный ответ, он честно
        роняет `samples()` ниже MIN_LIVE_SAMPLES и возвращает решение барной
        медиане, у которой хотя бы есть дата.
        """
        per_symbol = self._buckets.get(symbol) or {}
        if not per_symbol:
            return {}
        live = self._live(symbol, upto=self._stamp(now))
        merged = {}
        for bucket in live.values():
            for points, count in bucket.items():
                merged[points] = merged.get(points, 0) + count
        return merged

    def samples(self, symbol, *, now):
        return sum(self._counts(symbol, now=now).values())

    def median(self, symbol, *, now):
        """Точная медиана по окну. None — свежих замеров нет."""
        counts = self._counts(symbol, now=now)
        total = sum(counts.values())
        if not total:
            return None
        target = total / 2.0
        seen = 0
        ordered = sorted(counts.items())
        for points, count in ordered:
            seen += count
            if seen > target:
                return float(points)
            if seen == target:
                # чётное число замеров: середина между соседними значениями
                remaining = [p for p, _ in ordered if p > points]
                return float(points + remaining[0]) / 2 if remaining else float(points)
        return float(ordered[-1][0])

    def to_dict(self):
        return {"minutes": self.minutes,
                "buckets": {sym: {str(k): {str(p): c for p, c in b.items()}
                                  for k, b in per.items()}
                            for sym, per in self._buckets.items()}}

    def save(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path, *, minutes=60):
        """Битый или отсутствующий файл — не ошибка: окно наберётся заново."""
        obj = cls(minutes=minutes)
        p = Path(path)
        if not p.exists():
            return obj
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            obj.minutes = int(doc.get("minutes") or minutes)
            for sym, per in (doc.get("buckets") or {}).items():
                obj._buckets[sym] = {int(k): {int(p_): int(c) for p_, c in b.items()}
                                     for k, b in per.items()}
        except Exception:  # noqa: BLE001 - потеря окна не должна ронять датчик
            return cls(minutes=minutes)
        return obj


def load_medians(path):
    """Документ медиан с диска. Отсутствующий или битый файл — не ошибка:
    медианы пересчитываются, а исключения теряются (и это лучше, чем упасть)."""
    p = Path(path)
    if not p.exists():
        return dict(EMPTY, medians={}, excluded={}, samples={}, source={})
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - битый файл равнозначен отсутствию
        return dict(EMPTY, medians={}, excluded={}, samples={}, source={})
    for key, default in EMPTY.items():
        doc.setdefault(key, type(default)())
    return doc


def _write(path, doc):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _bars_median(market, symbol, *, days):
    """Медиана непустых значений spread в барах. None — поля нет или оно
    пустое у этого брокера."""
    try:
        bars = market.copy_rates(symbol, MEDIAN_TF, days * BARS_PER_DAY)
    except Exception:  # noqa: BLE001 - нет истории по символу: не повод падать
        return None
    if bars is None or len(bars) == 0 or "spread" not in bars:
        return None
    values = np.asarray(bars["spread"], dtype=float)
    values = values[values > 0]
    if values.size == 0:
        return None
    return float(np.median(values))


def update_medians(market, cfg, path, *, now, force=False):
    """Медианы спреда по whitelist. Пересчитывает не чаще раза в сутки.

    Возвращает документ (он же пишется на диск): medians / samples / source /
    excluded. Исключения не трогаются — ими управляет spread_state.
    """
    doc = load_medians(path)
    last = doc.get("computed_utc")
    if last and not force:
        try:
            age_h = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600.0
            if 0 <= age_h < RECOMPUTE_HOURS:
                return doc
        except ValueError:
            pass

    days = cfg.instruments.spread_median_days
    for symbol in cfg.instruments.whitelist:
        median = _bars_median(market, symbol, days=days)
        if median is not None:
            doc["medians"][symbol] = median
            doc["source"][symbol] = "bars"
            continue
        # поля spread в барах нет — набираем замерами по одному в сутки
        try:
            current = float(market.symbol_info(symbol).get("spread") or 0.0)
        except Exception:  # noqa: BLE001
            current = 0.0
        if current > 0:
            doc["samples"].setdefault(symbol, []).append(current)
            doc["samples"][symbol] = doc["samples"][symbol][-days:]
        samples = doc["samples"].get(symbol, [])
        doc["medians"][symbol] = (float(np.median(samples))
                                  if len(samples) >= MIN_SAMPLES else None)
        doc["source"][symbol] = "samples"

    doc["computed_utc"] = now.isoformat()
    _write(path, doc)
    return doc


MIN_LIVE_SAMPLES = 60      # меньше — база не набрана, работает барная
RETURN_RATIO = 1.1         # порог снятия исключения, см. spread_state


def spread_state(market, cfg, doc, *, symbol, now, path=None, live=None):
    """Можно ли входить по этому символу с точки зрения спреда.

    doc мутируется на месте (исключения ставятся и снимаются), и если передан
    path — документ сразу пишется на диск: исключение обязано пережить
    перезапуск процесса.

    → {allowed, reason, spread_points, median, ratio, excluded, median_unknown}
    """
    def out(allowed, reason, **extra):
        res = {"allowed": allowed, "reason": reason, "symbol": symbol,
               "excluded": symbol in doc.get("excluded", {}),
               "median_unknown": False}
        res.update(extra)
        if path is not None:
            _write(path, doc)
        return res

    if symbol not in cfg.instruments.whitelist:
        return out(False, f"{symbol} нет в instruments.whitelist конституции",
                   spread_points=None, median=None, ratio=None)

    try:
        spread_points = float(market.symbol_info(symbol).get("spread") or 0.0)
    except Exception as e:  # noqa: BLE001 - нет данных по символу = не входим
        return out(False, f"спред по {symbol} не прочитан: {e}",
                   spread_points=None, median=None, ratio=None)

    # База сравнения — ЖИВАЯ медиана за последний час, если она набрана.
    # Барная меряет спред на закрытии свечи, то есть в самый спокойный момент
    # минуты, тогда как решения принимаются в активные. За 2026-07-27..31 это
    # дало 9 отклонённых входов, шесть из них при ×1.05 (20 против барной 19 —
    # разница в ОДИН пункт), включая обе упущенные прибыльные сделки недели.
    # Пока живых замеров мало, честнее продолжать барной, чем выдумывать базу
    # по трём тикам.
    median = doc.get("medians", {}).get(symbol)
    if live is not None and live.samples(symbol, now=now) >= MIN_LIVE_SAMPLES:
        live_median = live.median(symbol, now=now)
        if live_median:
            median = live_median
    if not median:
        return out(True, "медиана спреда ещё не набрана — вход разрешён, издержки "
                         "проверит costs_R по фактическому спреду",
                   spread_points=spread_points, median=None, ratio=None,
                   median_unknown=True)

    ratio = spread_points / median
    mult = cfg.instruments.spread_anomaly_mult

    if symbol in doc.get("excluded", {}):
        # Возврат к медиане (гистерезис), а не «чуть ниже порога аномалии» —
        # иначе на границе инструмент мигал бы вход-выход каждую секунду.
        # Но требовать попадания в медиану РОВНО (ratio ≤ 1.0) при дискретности
        # в один пункт значит промахиваться систематически: 2026-07-28..31 шесть
        # независимых попыток подряд показали ровно 20 против медианы 19 и все
        # шесть были отклонены. Запас 1.1 оставляет гистерезис рабочим — порог
        # аномалии ×1.5 по-прежнему далеко, — но делает возврат достижимым.
        if ratio <= RETURN_RATIO:
            doc["excluded"].pop(symbol, None)
            return out(True, f"спред нормализовался ({spread_points:.0f} ≈ медиана "
                             f"{median:.0f}) — инструмент возвращён",
                       spread_points=spread_points, median=median, ratio=ratio,
                       excluded=False)
        return out(False, f"{symbol} исключён до нормализации спреда: сейчас "
                          f"{spread_points:.0f} против медианы {median:.0f} "
                          f"(×{ratio:.2f}), нужен возврат к медиане",
                   spread_points=spread_points, median=median, ratio=ratio,
                   excluded=True)

    if ratio > mult:
        doc.setdefault("excluded", {})[symbol] = {
            "since": now.isoformat(), "ratio": round(ratio, 3), "median": median,
            "spread_points": spread_points}
        return out(False, f"спред {spread_points:.0f} против медианы {median:.0f} "
                          f"(×{ratio:.2f}) выше порога ×{mult}",
                   spread_points=spread_points, median=median, ratio=ratio,
                   excluded=True)

    return out(True, f"спред {spread_points:.0f} в норме (×{ratio:.2f} от медианы)",
               spread_points=spread_points, median=median, ratio=ratio)
