"""Статистика журнала: единственное место, где сырые decision+outcome
превращаются в факты о самой системе (детерминированно, 0 токенов) — модель
делает выводы уже из этих чисел (recall перед решением, reflect на границе
сессии). Сохраняй существующее поведение и имена: на них уже опираются
scripts/recall.py, scripts/run_score.py и tests/test_e2e_dryrun.py.

СЕГМЕНТЫ (задача 2.2). Кроме исходных overall/by_setup/by_symbol/calibration:
by_regime/by_session/by_model/planned_vs_unplanned — каждый использует ту же
арифметику, что и by_setup (_segment_stats: n/wr/avg_R/sum_R + insufficient +
edge_significant), просто с другим ключом бакета.

ОБРАТНАЯ СОВМЕСТИМОСТЬ — жёсткое требование. Журнал на ПК трейдера содержит
записи, написанные до этой схемы (задача 2.1 расширила DECISION_REQUIRED, но
read_records ничего не валидирует на чтение — см. journal.py). Отсутствующее
поле НЕ роняет compute_stats и НЕ выбрасывает запись из выборки — оно уходит
в бакет "unknown" (_exact_key/_normalize_label), который участвует в overall/
calibration наравне со всеми. Уронить запись здесь значило бы тихо занизить
n и сместить каждую метрику, которая на этой выборке считается.

"unknown" ≠ "empty_label". Поля НЕТ вообще (ключ отсутствует/None) → "unknown"
— честная легаси-совместимость. Поле ЕСТЬ, но нормализуется в пустоту
(например regime="___") → "empty_label" — модель что-то записала, но это не
ярлык. Смешивать их значило бы стереть разницу между «данных нет» и «данные
есть, но битые» под одним именем (см. ревью задачи 2.2, детали — в
docstring'ах _normalize_label/_exact_key).

КАЛИБРОВКА — ТОЛЬКО ПО МОДЕЛИ (обязательное требование задачи). Разные модели
по определению по-разному откалиброваны: "заявленные 0.7" от одной модели —
не то же самое число, что "заявленные 0.7" от другой. Смешать их в один
глобальный бакет — получить число, которое не описывает ни одну из них (см.
test_calibration_computed_per_model: блендинг двух моделей с реальным WR
0.8 и 0.2 в одном бакете confidence даёт ложные 0.5). `calibration_by_model`
— обязательный результат этой задачи; `calibration` (глобальная) оставлена
ради recall.py, который уже читает этот ключ как плоский список — ломать его
здесь не нужно, per-model разбивка живёт рядом под новым ключом.

LABEL DRIFT — НЕ РЕКОНСИЛЯЦИЯ. regime/setup_type/tactic — свободный текст в
таксономии модели: она может написать "trend_up" в понедельник и "uptrend" в
пятницу. _normalize_label схлопывает ТОЛЬКО тривиальные различия форматирования
(регистр, пробелы по краям, разделители - _ пробел → один пробел) — то, что
доказуемо один и тот же ярлык. Она НАМЕРЕННО не трогает "trend_up" vs
"uptrend": это вопрос смысла, а не строки, и ошибочное автоматическое слияние
тихо смешает два разных сетапа без возможности потом различить, что откуда.
_label_drift — это только НАВОДКА (кандидаты) для trader-reflect, который уже
отвечает за «схлопни синонимы» осознанно; она не мержит бакеты сама. Две
дешёвые проверки, обе на уже нормализованных ярлыках:
  1. near_duplicate — разные нормализованные ярлыки одного поля совпадают,
     если убрать из них ВСЕ разделители целиком (не просто схлопнуть, как
     _normalize_label). ЭТО НЕ ДОКАЗАТЕЛЬСТВО СИНОНИМА: "mr-fade" (mean-
     reversion fade) и гипотетический "mrf-ade" дают тот же отпечаток
     "mrfade", хотя это разные сетапы, случайно совпавшие после снятия
     разделителей. Поэтому текст кандидата ("note") НЕ утверждает "это один
     и тот же ярлык" — только сообщает, что совпало, и явно требует
     проверки смысла человеком/моделью перед тем, как reflect что-то
     схлопнёт. Уверенный текст здесь опасен: reflect читает note и решает,
     сливать ли статистику — ложная уверенность повышает шанс необратимо
     смешать два разных сетапа.
  2. small_n — нормализованный ярлык с n меньше четверти min_n_for_confirmed:
     возможно, ещё не собравшийся осколок более крупного ярлыка. Не
     доказательство дрейфа, просто повод посмотреть.
compute_stats/stats.json отдают ОБА вида без изменений — это программный вход
для trader-reflect, урезать его нельзя. render_scorecard (человекочитаемый
markdown; живёт в trader_lib/scorecard.py) показывает ТОЛЬКО near_duplicate: при малом min_n_for_confirmed
(порог small_n = min_n_for_confirmed // 4) в молодом журнале кандидатом
становится каждая метка сразу — реальный сигнал дрейфа тонет в шуме "выборка
ещё набирается". Эта информация не теряется для человека: то же самое малое n
уже видно как "⚠ мало данных" на каждом сегменте выше (insufficient).
Обе проверки игнорируют бакеты "unknown"/"empty_label" (см. выше) — это не
ярлыки модели, а отсутствие/мусор, дрейфу не подвержены по определению.
Это НЕ fuzzy-matching движок (никакого расстояния Левенштейна/похожести
строк) и не реконсиляция (это отдельная задача 2.3) — обе проверки чисто
структурные и держатся на set-операциях над уже нормализованными строками.

SKIP-ЗАПИСИ В R-СТАТИСТИКЕ НЕ УЧАСТВУЮТ (осознанное решение, не пропуск).
skip — это не исход сделки (сделки не было), а _join() строит rows только из
пар decision+outcome по trade_id; skip-записи (type="skip") никогда не имеют
outcome, поэтому structurally не попадают ни в overall, ни в один из below
сегментов — совершенно не нужно фильтровать их отдельно. Смешивать "решил не
входить" с R было бы категориальной ошибкой (см. задачу). Отдельная метрика
дисциплины "сколько раз назвал сетап и не вошёл" — полезная, но другая по
типу статистика (частота решений, не R), и в required-тестах задачи 2.2 её
нет: строить её здесь значило бы отвечать на вопрос, который не задавали, и
множить поверхность без нужды.
"""
import re
from collections import defaultdict

import numpy as np

_SEP_RE = re.compile(r"[\s_\-]+")

# Свободный текст модели (её собственная таксономия) — единственные поля, где
# нормализация формата и label_drift вообще имеют смысл. session_phase — из
# закрытого словаря конфига (BRIEF/LONDON/LULL/NY/WINDDOWN/REVIEW), model_id —
# идентификатор модели: оба точные значения, не проза, дрейфу форматирования
# не подвержены той же природы, что и regime/setup_type/tactic.
_DRIFT_FIELDS = ("setup_type", "regime", "tactic")


def _join(records):
    """Решения, у которых есть исход. Смоук-прогоны стека исключаются.

    Смоук — техническая проверка живого пути (открыть-закрыть сразу), она
    всегда даёт маленький минус на спреде. В статистике такие записи не просто
    бесполезны, а вредны: 2026-07-27 три прогона дали `WR 0.0 · n=4` вместо
    честного «данных нет», и калибровка модели считалась по мусору. Признак
    ставится в момент входа и подделать его задним числом нельзя — та же
    защита, что в trader_lib/streak.py.
    """
    dec = {r["trade_id"]: r for r in records
           if r["type"] == "decision" and r.get("smoke") is not True}
    out = {r["trade_id"]: r for r in records if r["type"] == "outcome"}
    rows = []
    for tid, d in dec.items():
        if tid in out:
            rows.append({**d, "R": out[tid]["R"]})
    return rows


def _agg(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": None, "avg_R": None, "sum_R": 0.0}
    Rs = np.array([r["R"] for r in rows], float)
    wins = int((Rs > 0).sum())
    return {"n": n, "wr": round(wins / n, 3), "avg_R": round(float(Rs.mean()), 3),
            "sum_R": round(float(Rs.sum()), 3)}


def _bootstrap_gt0(Rs, iters=2000, seed=0):
    if len(Rs) < 5:
        return None
    rng = np.random.default_rng(seed)
    n = len(Rs)
    means = [rng.choice(Rs, n, replace=True).mean() for _ in range(iters)]
    lo = np.percentile(means, 2.5)
    return bool(lo > 0)  # 95% CI на avg R не пересекает 0 снизу


def _normalize_label(value):
    """Бакет свободного текста модели (regime/setup_type/tactic): схлопывает
    ТОЛЬКО тривиальные различия форматирования — регистр, пробелы по краям,
    внутренние разделители (пробел/_/-, в любом числе подряд) → один пробел.
    НЕ гадает синонимы ("trend_up" и "uptrend" остаются разными строками) —
    см. шапку модуля.

    ДВА РАЗНЫХ "ПУСТО" — разные бакеты, не один. "unknown" — поля НЕТ вообще
    (ключ отсутствует или явный None): легаси-запись до задачи 2.1, честная
    обратная совместимость. "empty_label" — поле ЕСТЬ, но после нормализации
    в нём пусто (например "___" или "   "): модель что-то записала, но это
    не ярлык, а мусор. Смешивать их в одном бакете значило бы стереть разницу
    между «данных нет» и «данные есть, но битые» — ровно то, из-за чего
    статистика потом врёт незаметно (см. ревью задачи 2.2)."""
    if value is None:
        return "unknown"
    s = _SEP_RE.sub(" ", str(value).strip()).strip().lower()
    return s if s else "empty_label"


def _exact_key(value):
    """Бакет по точному значению controlled-поля (session_phase — закрытый
    словарь конфига, model_id — идентификатор модели): НЕ свободный текст,
    регистр/разделители трогать не нужно — в отличие от _normalize_label.
    Только убирает пробелы по краям и защищает от нестрокового мусора.

    Та же двойка бакетов, что и в _normalize_label (см. её docstring):
    "unknown" — поля нет вообще, "empty_label" — поле есть, но пустое после
    strip (например "   ")."""
    if value is None:
        return "unknown"
    s = str(value).strip()
    return s if s else "empty_label"


def _planned_key(r):
    """planned — bool в схеме decision (journal.DECISION_REQUIRED), но
    старые записи его не имеют, а мусорное значение не bool — не наша забота
    угадывать: и то и другое уходит в "unknown", а не роняет запись."""
    p = r.get("planned")
    if p is True:
        return "planned"
    if p is False:
        return "unplanned"
    return "unknown"


def _segment_stats(rows, key_fn, *, min_n_for_confirmed):
    """Общая арифметика для ЛЮБОЙ сегментации rows по key_fn: n/wr/avg_R/sum_R
    (_agg) + insufficient (n < min_n_for_confirmed) + edge_significant
    (bootstrap-CI не пересекает 0). Раньше это было продублировано только для
    by_setup; by_regime/by_session/by_model/planned_vs_unplanned используют
    ровно ту же функцию — один источник арифметики, не четыре копии."""
    grp = defaultdict(list)
    for r in rows:
        grp[key_fn(r)].append(r)
    out = {}
    for key, rs in grp.items():
        a = _agg(rs)
        Rs = np.array([x["R"] for x in rs], float)
        a["insufficient"] = a["n"] < min_n_for_confirmed
        a["edge_significant"] = _bootstrap_gt0(Rs)
        out[key] = a
    return out


def _calibration(rows):
    """Бакеты по заявленной уверенности (confidence) → реальный WR. Общая
    функция и для глобальной калибровки, и для калибровки внутри одной
    модели (compute_stats зовёт её дважды: на всех rows и на срезе по
    model_id) — иначе бакетирование продублировалось бы и рисковало
    разойтись между глобальной и per-model версией."""
    cb = defaultdict(list)
    for r in rows:
        c = r.get("confidence")
        if c is None:
            continue
        bucket = int(round(min(max(c, 0.0), 0.99) * 10))
        cb[bucket].append(1 if r["R"] > 0 else 0)
    calib = []
    for bucket in sorted(cb):
        vals = cb[bucket]
        calib.append({"conf_bucket": f"{bucket / 10:.1f}-{bucket / 10 + 0.1:.1f}",
                      "n": len(vals), "realized_wr": round(sum(vals) / len(vals), 3)})
    return calib


def _label_drift(rows, min_n_for_confirmed):
    """Кандидаты на дрейф ярлыков (см. шапку модуля) по каждому свободнотекстовому
    полю в _DRIFT_FIELDS. Возвращает список записей
    {"field", "kind", "labels", "n_by_label", "note"} — kind ∈
    {"near_duplicate", "small_n"}. Только наводка для reflect, ничего не мержит."""
    small_n_threshold = max(2, min_n_for_confirmed // 4)
    drift = []
    for field in _DRIFT_FIELDS:
        raw_by_norm = defaultdict(set)
        rows_by_norm = defaultdict(list)
        for r in rows:
            raw = r.get(field)
            norm = _normalize_label(raw)
            if norm in ("unknown", "empty_label"):
                # отсутствие поля/мусор без смысла — не дрейф ярлыка, это
                # отдельный случай (см. _normalize_label)
                continue
            raw_by_norm[norm].add(str(raw).strip())
            rows_by_norm[norm].append(r)

        # 1. near_duplicate: разные нормализованные ярлыки с одинаковым
        # "отпечатком" без разделителей вообще ("trend up" / "trendup").
        # ВАЖНО: отпечаток честный и дешёвый, но не различает смысл — "mr-fade"
        # (mean-reversion fade) и гипотетический "mrf-ade" дадут тот же
        # отпечаток "mrfade", хотя это разные сетапы, случайно совпавшие
        # после снятия разделителей. Поэтому note НЕ утверждает "это один и
        # тот же ярлык" — только сообщает, что совпало, и явно требует
        # проверки смысла человеком/моделью, а не автосхлопывания.
        fp_groups = defaultdict(list)
        for norm in raw_by_norm:
            fp_groups[norm.replace(" ", "")].append(norm)
        for norms in fp_groups.values():
            if len(norms) > 1:
                norms = sorted(norms)
                drift.append({
                    "field": field, "kind": "near_duplicate", "labels": norms,
                    "n_by_label": {n: len(rows_by_norm[n]) for n in norms},
                    "note": "разные бакеты, но совпадают, если убрать все "
                            "разделители — может быть один и тот же ярлык, а "
                            "может быть совпадение (напр. «mr-fade» vs "
                            "«mrf-ade»); требует проверки смысла, "
                            "автоматически НЕ схлопнуто",
                })

        # 2. small_n: возможный ещё не собравшийся осколок дрейфа
        for norm, rs in rows_by_norm.items():
            if len(rs) < small_n_threshold:
                drift.append({
                    "field": field, "kind": "small_n", "labels": [norm],
                    "n_by_label": {norm: len(rs)},
                    "note": f"n={len(rs)} < {small_n_threshold} — возможный "
                            "осколок другого ярлыка, не доказательство",
                })
    return drift


def compute_stats(records, *, min_n_for_confirmed):
    rows = _join(records)
    overall = _agg(rows)

    by_setup = _segment_stats(rows, lambda r: r.get("setup_type", "?"),
                              min_n_for_confirmed=min_n_for_confirmed)
    by_regime = _segment_stats(rows, lambda r: _normalize_label(r.get("regime")),
                               min_n_for_confirmed=min_n_for_confirmed)
    by_session = _segment_stats(rows, lambda r: _exact_key(r.get("session_phase")),
                                min_n_for_confirmed=min_n_for_confirmed)
    by_model = _segment_stats(rows, lambda r: _exact_key(r.get("model_id")),
                              min_n_for_confirmed=min_n_for_confirmed)
    planned_vs_unplanned = _segment_stats(rows, _planned_key,
                                          min_n_for_confirmed=min_n_for_confirmed)

    seg = defaultdict(list)
    for r in rows:
        seg[r.get("symbol", "?")].append(r)
    by_symbol = {k: _agg(v) for k, v in seg.items()}

    calib = _calibration(rows)  # глобальная — оставлена ради recall.py

    model_rows = defaultdict(list)
    for r in rows:
        model_rows[_exact_key(r.get("model_id"))].append(r)
    calibration_by_model = {model_id: _calibration(rs)
                            for model_id, rs in model_rows.items()}

    label_drift = _label_drift(rows, min_n_for_confirmed)

    return {"overall": overall, "by_setup": by_setup, "by_symbol": by_symbol,
            "by_regime": by_regime, "by_session": by_session, "by_model": by_model,
            "planned_vs_unplanned": planned_vs_unplanned,
            "calibration": calib, "calibration_by_model": calibration_by_model,
            "label_drift": label_drift}


# --------------------------------------------------------------------------
# Рендеринг markdown ПЕРЕЕХАЛ в trader_lib/scorecard.py (задача 6.3).
#
# Указание ревью задачи 2.2: render_scorecard и его помощники не делят
# состояние с агрегацией, а этот модуль и без них большой. Поводом сделать
# перенос стал третий потребитель отчётов (дневной разбор) — иначе третья
# функция рендеринга легла бы сюда же.
#
# Здесь остаётся ТОЛЬКО агрегация: числа считаются в одном месте, текст
# собирается в другом. Потребители (scripts/run_score.py и отчёты) берут
# render_scorecard из trader_lib/scorecard.py.
# --------------------------------------------------------------------------
