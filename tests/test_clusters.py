"""Корреляционные кластеры (Ф2).

ЗАЧЕМ ЭТО СУЩЕСТВУЕТ. Девять инструментов в whitelist — это не девять ставок.
Замер часовых доходностей 2026-08-01 (n=499):

    EURUSD ~ GBPUSD  +0.80     BTCUSD ~ ETHUSD  +0.865
    EURUSD ~ USDCHF  -0.87     XAUUSD ~ всё     |0.11| максимум
    EURUSD ~ AUDUSD  +0.77
    GBPUSD ~ AUDUSD  +0.71
    AUDUSD ~ USDCHF  -0.72

Три трейдера, разошедшиеся по EURUSD, GBPUSD и AUDUSD, выглядели бы
диверсифицированными, а вели бы ОДНУ сделку в трёх экземплярах с тройным
риском — по символам это не видно. Именно так скучиваются человеческие
команды, и структура обязана делать это невозможным, а не отговаривать.

ЗНАК КОРРЕЛЯЦИИ НЕ ВАЖЕН. EURUSD и USDCHF ходят строго противоположно
(-0.87), но это один и тот же доллар: лонг одного и лонг другого — ставка на
разные стороны одного фактора, а не диверсификация. Поэтому группируем по
МОДУЛЮ.

СВЯЗНОСТЬ ТРАНЗИТИВНА. Если A связан с B, а B с C, то риск перетекает по
цепочке, даже когда A и C напрямую не коррелируют. Кластер = компонента
связности, а не только прямые пары.
"""
import datetime as dt

import numpy as np
import pytest

from trader_lib.clusters import (
    build_clusters,
    cluster_of,
    load_clusters,
    same_cluster,
    save_clusters,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _returns(**series):
    return {k: np.asarray(v, dtype=float) for k, v in series.items()}


def _correlated(base, *, k=1.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return base * k + rng.normal(0, noise, size=len(base))


def test_uncorrelated_symbols_stay_apart():
    rng = np.random.default_rng(1)
    r = _returns(XAUUSD=rng.normal(0, 1, 400), EURUSD=rng.normal(0, 1, 400))
    clusters = build_clusters(r, threshold=0.7)
    assert cluster_of("XAUUSD", clusters) != cluster_of("EURUSD", clusters)
    assert same_cluster("XAUUSD", "EURUSD", clusters) is False


def test_positively_correlated_symbols_group():
    rng = np.random.default_rng(2)
    base = rng.normal(0, 1, 400)
    r = _returns(EURUSD=base, GBPUSD=_correlated(base, noise=0.4, seed=3))
    clusters = build_clusters(r, threshold=0.7)
    assert same_cluster("EURUSD", "GBPUSD", clusters) is True


def test_negatively_correlated_symbols_group_too():
    """EURUSD ~ USDCHF = -0.87. Это один доллар, а не два инструмента:
    лонг обоих — ставка на разные стороны одного фактора."""
    rng = np.random.default_rng(4)
    base = rng.normal(0, 1, 400)
    r = _returns(EURUSD=base, USDCHF=_correlated(-base, noise=0.3, seed=5))
    clusters = build_clusters(r, threshold=0.7)
    assert same_cluster("EURUSD", "USDCHF", clusters) is True


def test_linkage_is_transitive():
    """A~B и B~C связывают A и C в один кластер: риск течёт по цепочке, даже
    если прямая корреляция A~C ниже порога."""
    rng = np.random.default_rng(6)
    a = rng.normal(0, 1, 600)
    c = rng.normal(0, 1, 600)
    b = a + c                      # b связан с обоими, a и c между собой — нет
    r = _returns(A=a, B=b, C=c)
    clusters = build_clusters(r, threshold=0.6)
    assert same_cluster("A", "C", clusters) is True


def test_threshold_is_respected():
    rng = np.random.default_rng(7)
    base = rng.normal(0, 1, 500)
    r = _returns(A=base, B=_correlated(base, noise=1.2, seed=8))
    loose = build_clusters(r, threshold=0.3)
    strict = build_clusters(r, threshold=0.95)
    assert same_cluster("A", "B", loose) is True
    assert same_cluster("A", "B", strict) is False


def test_unknown_symbol_has_no_cluster():
    rng = np.random.default_rng(9)
    clusters = build_clusters(_returns(A=rng.normal(0, 1, 100)), threshold=0.7)
    assert cluster_of("ZZZZZZ", clusters) is None


def test_unknown_symbol_is_never_silently_grouped():
    """Неизвестный символ не должен считаться «не в том же кластере» — это
    трактовалось бы как разрешение. Незнание не равно безопасности."""
    rng = np.random.default_rng(10)
    clusters = build_clusters(_returns(A=rng.normal(0, 1, 100)), threshold=0.7)
    with pytest.raises(ValueError):
        same_cluster("A", "ZZZZZZ", clusters, strict=True)


def test_too_few_observations_is_honest_not_guessed():
    """На пяти точках корреляция — шум. Честнее не строить кластер вовсе."""
    r = _returns(A=[0.1, -0.2, 0.3, 0.0, 0.1], B=[0.1, -0.2, 0.3, 0.0, 0.1])
    clusters = build_clusters(r, threshold=0.7, min_observations=50)
    assert same_cluster("A", "B", clusters) is False
    assert clusters["insufficient"] == ["A", "B"]


def test_clusters_persist_and_reload(tmp_path):
    rng = np.random.default_rng(11)
    base = rng.normal(0, 1, 400)
    r = _returns(EURUSD=base, GBPUSD=_correlated(base, noise=0.4, seed=12),
                 XAUUSD=rng.normal(0, 1, 400))
    clusters = build_clusters(r, threshold=0.7, now=NOW)
    path = tmp_path / "clusters.json"
    save_clusters(path, clusters)

    back = load_clusters(path)
    assert same_cluster("EURUSD", "GBPUSD", back) is True
    assert same_cluster("EURUSD", "XAUUSD", back) is False
    assert back["computed_utc"] == NOW.isoformat()


def test_missing_file_is_not_an_error(tmp_path):
    """Кластеров ещё нет — гейт должен получить пустую карту и решать сам,
    а не падать."""
    back = load_clusters(tmp_path / "нет.json")
    assert back["groups"] == []
    assert cluster_of("EURUSD", back) is None
