"""Корреляционные кластеры инструментов (Ф2).

ЗАЧЕМ. Девять инструментов whitelist — это не девять независимых ставок.
Замер часовых доходностей 2026-08-01 (n=499) на демо-счёте:

    EURUSD ~ GBPUSD  +0.80      BTCUSD ~ ETHUSD  +0.865
    EURUSD ~ USDCHF  -0.87      XAUUSD ~ всё      не выше |0.11|
    EURUSD ~ AUDUSD  +0.77
    GBPUSD ~ AUDUSD  +0.71
    AUDUSD ~ USDCHF  -0.72

Три трейдера, разошедшиеся по EURUSD, GBPUSD и AUDUSD, выглядели бы
диверсифицированными, а вели бы ОДНУ сделку в трёх экземплярах с тройным
риском. По символам это не видно — видно только по корреляции. Команда без
такой проверки опаснее одиночки: тот хотя бы знает, что у него одна позиция.

ГРУППИРУЕМ ПО МОДУЛЮ КОРРЕЛЯЦИИ. EURUSD и USDCHF ходят строго противоположно,
но это один и тот же доллар: лонг обоих — ставка на две стороны одного
фактора, а не диверсификация. Знак говорит, как складывать экспозицию (этим
занимается exposure.net_currency_exposure), а не считать ли инструменты
разными.

СВЯЗНОСТЬ ТРАНЗИТИВНА. Если A связан с B, а B с C, риск течёт по цепочке, даже
когда прямая корреляция A~C ниже порога. Кластер — компонента связности графа,
а не только прямые пары.

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ. Корреляции нестационарны: +0.87 сегодня может
стать +0.3 через месяц, а XAUUSD, независимый в спокойное время, схлопнется с
остальными при бегстве в качество. Поэтому карта пересчитывается регулярно и
хранится с отметкой времени — но между пересчётами дыра остаётся, и от
общерыночного шока защищает только счётная стена, а не эта группировка.
"""
import datetime as dt
import json
from pathlib import Path

import numpy as np

UTC = dt.timezone.utc

DEFAULT_THRESHOLD = 0.7
MIN_OBSERVATIONS = 100      # меньше — корреляция шум, кластер не строится

EMPTY = {"groups": [], "threshold": DEFAULT_THRESHOLD, "insufficient": [],
         "computed_utc": None}


def build_clusters(returns_by_symbol, *, threshold=DEFAULT_THRESHOLD,
                   min_observations=MIN_OBSERVATIONS, now=None):
    """Компоненты связности графа «|corr| ≥ threshold».

    returns_by_symbol — {символ: последовательность доходностей}. Длины могут
    различаться: сравниваются последние min(len) точек каждой пары, иначе
    короткий ряд молча испортил бы все корреляции.

    Символы короче min_observations в группировке не участвуют и попадают в
    `insufficient`: посчитать корреляцию по пяти точкам можно, доверять ей
    нельзя, а «не знаю» здесь безопаснее выдуманного числа.
    """
    now = now or dt.datetime.now(UTC)
    usable, insufficient = {}, []
    for symbol, series in (returns_by_symbol or {}).items():
        values = np.asarray(series, dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < min_observations:
            insufficient.append(symbol)
            continue
        usable[symbol] = values

    symbols = sorted(usable)
    parent = {s: s for s in symbols}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            n = min(len(usable[a]), len(usable[b]))
            xa, xb = usable[a][-n:], usable[b][-n:]
            if xa.std() == 0 or xb.std() == 0:
                continue        # константный ряд: корреляция не определена
            corr = float(np.corrcoef(xa, xb)[0, 1])
            if np.isfinite(corr) and abs(corr) >= threshold:
                union(a, b)

    grouped = {}
    for s in symbols:
        grouped.setdefault(find(s), []).append(s)

    return {"groups": sorted((sorted(g) for g in grouped.values())),
            "threshold": threshold,
            "insufficient": sorted(insufficient),
            "computed_utc": now.isoformat()}


def rebuild_clusters(market, cfg, path, *, now=None, tf="H1", bars=500,
                     threshold=DEFAULT_THRESHOLD):
    """Пересчитать карту по свежей истории whitelist и записать на диск.

    Доходности берутся логарифмические по закрытиям: сравнивать нужно
    ОТНОСИТЕЛЬНЫЕ изменения, иначе BTCUSD ценой 63000 подавит EURUSD ценой 1.08
    в любой корреляции просто масштабом.

    Символ без истории молча пропускается — один недоступный инструмент не
    должен лишать карты все остальные.
    """
    returns = {}
    for symbol in cfg.instruments.whitelist:
        try:
            closes = np.asarray(market.copy_rates(symbol, tf, bars)["close"], float)
        except Exception:  # noqa: BLE001 - нет истории по символу: не повод падать
            continue
        closes = closes[np.isfinite(closes) & (closes > 0)]
        if len(closes) < 2:
            continue
        returns[symbol] = np.diff(np.log(closes))

    clusters = build_clusters(returns, threshold=threshold, now=now)
    save_clusters(path, clusters)
    return clusters


def cluster_of(symbol, clusters):
    """Индекс кластера или None, если символ в карте отсутствует."""
    for i, group in enumerate((clusters or {}).get("groups") or []):
        if symbol in group:
            return i
    return None


def same_cluster(a, b, clusters, *, strict=False):
    """Один ли это фактор риска.

    strict=True поднимает ValueError для символа, которого нет в карте.
    Незнание не равно безопасности: молчаливое «не в одном кластере» для
    неизвестного инструмента прочиталось бы гейтом как разрешение открыть
    вторую позицию на тот же риск.
    """
    ca, cb = cluster_of(a, clusters), cluster_of(b, clusters)
    if strict and (ca is None or cb is None):
        missing = [s for s, c in ((a, ca), (b, cb)) if c is None]
        raise ValueError(
            f"нет в карте кластеров: {', '.join(missing)} — карта устарела или "
            "инструмент новый; трактовать это как «риски независимы» нельзя")
    if ca is None or cb is None:
        return False
    return ca == cb


def save_clusters(path, clusters):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8")


def load_clusters(path):
    """Отсутствующий или битый файл — пустая карта, а не исключение: гейт
    должен уметь работать до первого пересчёта."""
    p = Path(path)
    if not p.exists():
        return dict(EMPTY)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - битый файл равнозначен отсутствию
        return dict(EMPTY)
    for key, default in EMPTY.items():
        doc.setdefault(key, default)
    return doc
