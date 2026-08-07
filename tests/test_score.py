from trader_lib.score import compute_stats
from trader_lib.scorecard import render_scorecard


def _journal():
    recs = []
    data = [("A", 1.5, 0.7), ("A", -1.0, 0.6), ("A", 2.0, 0.7),
            ("B", -1.0, 0.8), ("B", -1.0, 0.8)]
    for i, (su, R, conf) in enumerate(data):
        tid = f"t{i}"
        recs.append({"type": "decision", "trade_id": tid, "setup_type": su,
                     "confidence": conf, "symbol": "XAUUSD"})
        recs.append({"type": "outcome", "trade_id": tid, "R": R})
    return recs


def test_expectancy_per_setup():
    s = compute_stats(_journal(), min_n_for_confirmed=20)
    a = s["by_setup"]["A"]
    assert a["n"] == 3 and abs(a["avg_R"] - 0.833) < 0.01
    assert a["insufficient"] is True  # n<20
    b = s["by_setup"]["B"]
    assert b["avg_R"] == -1.0 and b["wr"] == 0.0


def test_calibration_bucketed():
    s = compute_stats(_journal(), min_n_for_confirmed=20)
    assert "calibration" in s and isinstance(s["calibration"], list)


def test_scorecard_renders():
    s = compute_stats(_journal(), min_n_for_confirmed=20)
    md = render_scorecard(s, progress_to_target_pct=1.2, target_pct=10)
    assert "Прогресс к цели" in md and "Матожидание" in md


# --- задача 2.2: сегментация статистики ---------------------------------
#
# Ниже decision-записи строятся через фикстуру make_decision (полная схема
#28 полей из journal.py, см. tests/conftest.py) — тесты этого блока проверяют
# арифметику сегментации, не состав журнала (задача 2.1 её уже покрыла).
# _dec/_out склеивают decision+outcome так же, как это делает журнал на диске
# (два разных "type", связанные по trade_id) — compute_stats() читает записи
# именно в этой форме, а не через append_decision/append_outcome напрямую,
# чтобы тесты не платили за диск ради чистой арифметики.

def _dec(make_decision, **overrides):
    return {"type": "decision", **make_decision(**overrides)}


def _out(trade_id, R, **overrides):
    return {"type": "outcome", "trade_id": trade_id, "R": R, **overrides}


def test_by_regime_segments(make_decision):
    """Разные ярлыки regime — разные бакеты, статистика не блендится."""
    recs = []
    for i, (regime, R) in enumerate(
            [("trend", 1.5), ("trend", 2.0), ("range", -1.0)]):
        tid = f"rg{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime=regime))
        recs.append(_out(tid, R))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["by_regime"]["trend"]["n"] == 2
    assert abs(s["by_regime"]["trend"]["avg_R"] - 1.75) < 1e-9
    assert s["by_regime"]["range"]["n"] == 1
    assert s["by_regime"]["range"]["avg_R"] == -1.0
    assert s["overall"]["n"] == 3  # оба бакета вместе, не потеряли записи


def test_by_session_segments(make_decision):
    """session_phase (BRIEF/LONDON/LULL/NY/WINDDOWN/REVIEW) — отдельные бакеты."""
    recs = []
    for i, (phase, R) in enumerate(
            [("LONDON", 1.0), ("LONDON", -0.5), ("NY", 2.0)]):
        tid = f"ss{i}"
        recs.append(_dec(make_decision, trade_id=tid, session_phase=phase))
        recs.append(_out(tid, R))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["by_session"]["LONDON"]["n"] == 2
    assert abs(s["by_session"]["LONDON"]["avg_R"] - 0.25) < 1e-9
    assert s["by_session"]["NY"]["n"] == 1
    assert s["by_session"]["NY"]["avg_R"] == 2.0


def test_by_model_segments(make_decision):
    """Требование задачи: статистика двух моделей не блендится. model-a
    стабильно плюсовая, model-b стабильно минусовая — если бы score.py
    считал их вместе (как раньше, безby_model), обе картины потерялись бы
    в одном общем среднем около нуля."""
    recs = []
    for i in range(4):
        tid = f"ma{i}"
        recs.append(_dec(make_decision, trade_id=tid, model_id="model-a"))
        recs.append(_out(tid, 1.0))
    for i in range(4):
        tid = f"mb{i}"
        recs.append(_dec(make_decision, trade_id=tid, model_id="model-b"))
        recs.append(_out(tid, -1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    a = s["by_model"]["model-a"]
    b = s["by_model"]["model-b"]
    assert a["n"] == 4 and a["avg_R"] == 1.0
    assert b["n"] == 4 and b["avg_R"] == -1.0
    assert a["avg_R"] != b["avg_R"]  # не смешаны
    # overall блендит оба (как раньше) — ровно то, чего by_model избегает
    assert s["overall"]["avg_R"] == 0.0


def test_planned_vs_unplanned_split(make_decision):
    recs = []
    for i in range(2):
        tid = f"pl{i}"
        recs.append(_dec(make_decision, trade_id=tid, planned=True,
                         plan_hypothesis_id="h-1"))
        recs.append(_out(tid, 1.0))
    tid = "unpl0"
    recs.append(_dec(make_decision, trade_id=tid, planned=False,
                     plan_hypothesis_id=None))
    recs.append(_out(tid, -2.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["planned_vs_unplanned"]["planned"]["n"] == 2
    assert s["planned_vs_unplanned"]["planned"]["avg_R"] == 1.0
    assert s["planned_vs_unplanned"]["unplanned"]["n"] == 1
    assert s["planned_vs_unplanned"]["unplanned"]["avg_R"] == -2.0


def test_legacy_records_without_new_fields_default_unknown():
    """Запись старого формата (до задачи 2.1) — без regime/session_phase/
    model_id/planned — не должна выпадать из выборки и не должна ронять
    compute_stats. Она обязана попасть в бакет "unknown" каждого нового
    сегмента И остаться в overall/calibration (не потеряться молча)."""
    recs = [
        {"type": "decision", "trade_id": "legacy1", "setup_type": "s1",
         "confidence": 0.6, "symbol": "XAUUSD"},
        {"type": "outcome", "trade_id": "legacy1", "R": 1.2},
    ]
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["overall"]["n"] == 1  # не выпала из выборки
    assert s["by_regime"]["unknown"]["n"] == 1
    assert s["by_session"]["unknown"]["n"] == 1
    assert s["by_model"]["unknown"]["n"] == 1
    assert s["planned_vs_unplanned"]["unknown"]["n"] == 1
    assert s["calibration_by_model"]["unknown"][0]["n"] == 1


def test_empty_after_normalization_label_gets_own_bucket_not_unknown(make_decision):
    """Ревью: "unknown" обязан означать ИМЕННО легаси-запись (поля нет
    вообще), а не "поле есть, но там мусор". regime="___" нормализуется в
    пустую строку (сепараторы схлопнулись без остатка) — это не то же самое,
    что отсутствие поля, и не должно тихо слиться с настоящими легаси-
    записями в одном бакете "unknown". Обе ситуации должны сосуществовать
    раздельно в одном и том же журнале."""
    recs = [
        # поле есть, но нормализуется в пустоту — "empty_label"
        _dec(make_decision, trade_id="junk1", regime="___"),
        _out("junk1", 1.0),
        # поля нет вообще (легаси-формат) — "unknown"
        {"type": "decision", "trade_id": "legacy1", "setup_type": "s1",
         "confidence": 0.6, "symbol": "XAUUSD"},
        {"type": "outcome", "trade_id": "legacy1", "R": -1.0},
    ]
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["by_regime"]["empty_label"]["n"] == 1
    assert s["by_regime"]["empty_label"]["avg_R"] == 1.0
    assert s["by_regime"]["unknown"]["n"] == 1
    assert s["by_regime"]["unknown"]["avg_R"] == -1.0
    assert "empty_label" != "unknown"  # разные бакеты, не слиты
    assert s["overall"]["n"] == 2  # обе записи учтены, ни одна не потеряна


def test_exact_key_empty_after_strip_gets_own_bucket_not_unknown(make_decision):
    """Тот же баг мог повториться в _exact_key (session_phase/model_id) —
    контрольный тест на сестринскую функцию, а не только на
    _normalize_label."""
    recs = [
        _dec(make_decision, trade_id="ws1", session_phase="   "),
        _out("ws1", 1.0),
        {"type": "decision", "trade_id": "legacy2", "setup_type": "s1",
         "confidence": 0.6, "symbol": "XAUUSD"},
        {"type": "outcome", "trade_id": "legacy2", "R": -1.0},
    ]
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["by_session"]["empty_label"]["n"] == 1
    assert s["by_session"]["unknown"]["n"] == 1


def test_label_drift_near_duplicate_note_does_not_claim_same_meaning(make_decision):
    """Ревью-адверсариал: "mr-fade" (mean-reversion fade) и "mrf-ade" —
    РАЗНЫЕ по смыслу ярлыки, случайно совпавшие отпечатком после снятия
    разделителей целиком. near_duplicate обязан их найти (отпечаток
    совпал), но note НЕ имеет права утверждать "вероятно один и тот же
    ярлык" — это читает модель и решает, схлопывать ли статистику; ложная
    уверенность повышает риск необратимо смешать два разных сетапа."""
    recs = []
    for i, setup in enumerate(["mr-fade"] * 3 + ["mrf-ade"] * 3):
        tid = f"adv{i}"
        recs.append(_dec(make_decision, trade_id=tid, setup_type=setup))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    # разные бакеты by_setup — сама сегментация ничего не мержит (by_setup
    # ещё и raw, но проверка дрейфа идёт по setup_type независимо от него)
    candidates = [d for d in s["label_drift"]
                 if d["field"] == "setup_type" and d["kind"] == "near_duplicate"]
    assert len(candidates) == 1
    note = candidates[0]["note"].lower()
    assert set(candidates[0]["labels"]) == {"mr fade", "mrf ade"}
    # формулировка нейтральна: сообщает о совпадении, не делает вывод
    assert "требует проверки" in note or "может быть" in note
    assert "вероятно один и тот же" not in note
    assert "это один и тот же ярлык" not in note


def test_calibration_computed_per_model(make_decision):
    """Ядро требования: калибровка считается ВНУТРИ модели, а не глобально.
    Обе модели заявляют одну и ту же уверенность (confidence=0.75), но
    model-a реально попадает в 4 из 5 (WR=0.8, хорошо откалибрована), а
    model-b — в 1 из 5 (WR=0.2, сильно переоценивает себя). Блендинг обеих
    моделей в один бакет глобальной калибровки даёт WR=0.5 — ложную картину,
    не описывающую НИ ОДНУ из моделей."""
    recs = []
    for i in range(5):
        tid = f"ca{i}"
        R = 1.0 if i < 4 else -1.0
        recs.append(_dec(make_decision, trade_id=tid, model_id="model-a",
                         confidence=0.75))
        recs.append(_out(tid, R))
    for i in range(5):
        tid = f"cb{i}"
        R = 1.0 if i < 1 else -1.0
        recs.append(_dec(make_decision, trade_id=tid, model_id="model-b",
                         confidence=0.75))
        recs.append(_out(tid, R))
    s = compute_stats(recs, min_n_for_confirmed=20)
    wr_a = s["calibration_by_model"]["model-a"][0]["realized_wr"]
    wr_b = s["calibration_by_model"]["model-b"][0]["realized_wr"]
    assert wr_a == 0.8
    assert wr_b == 0.2
    assert wr_a != wr_b
    # глобальная калибровка (оставлена ради обратной совместимости recall.py)
    # ДЕЙСТВИТЕЛЬНО блендит обе модели в 0.5 — вот ровно та ложная картина,
    # которую per-model калибровка обязана обходить
    assert s["calibration"][0]["realized_wr"] == 0.5


def test_scorecard_renders_regime_block(make_decision):
    recs = [_dec(make_decision, trade_id="t1", regime="trend"), _out("t1", 1.5)]
    s = compute_stats(recs, min_n_for_confirmed=20)
    md = render_scorecard(s, progress_to_target_pct=1.2, target_pct=10)
    assert "Режим" in md
    assert "trend" in md


def test_label_normalisation_collapses_case_whitespace_separators(make_decision):
    """Тривиальные различия форматирования одного и того же ярлыка модели
    (регистр/пробелы по краям/разделители -,_,пробел) схлопываются в ОДИН
    бакет — иначе значимость (n>=20) никогда не набирается на сетапе,
    который модель просто записала по-разному в разные дни (см. задачу)."""
    recs = []
    variants = ["Trend Up", "trend-up", "trend_up", "  trend   up  "]
    for i, regime in enumerate(variants):
        tid = f"nl{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime=regime))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert len(s["by_regime"]) == 1  # НЕ 4 бакета
    ((bucket, a),) = s["by_regime"].items()
    assert bucket == "trend up"
    assert a["n"] == 4


def test_label_normalisation_does_not_guess_synonyms(make_decision):
    """Контрольный тест дисциплины: нормализация НЕ склеивает разные по
    смыслу ярлыки, даже похожие ("trend_up" и "uptrend" остаются разными
    бакетами) — угадывание синонимов запрещено задачей (требует смысла, не
    строковых правил, и ошибочное слияние тихо смешает два разных сетапа)."""
    recs = []
    for i, regime in enumerate(["trend_up", "uptrend"]):
        tid = f"sy{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime=regime))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert len(s["by_regime"]) == 2
    assert set(s["by_regime"]) == {"trend up", "uptrend"}


def test_label_drift_reports_near_duplicate_candidate(make_decision):
    """"trend up" (с пробелом) и "trendup" (без разделителя вообще) —
    нормализация схлопывает регистр/разделители, но не удаляет разделитель
    целиком, поэтому это два РАЗНЫХ бакета by_regime. label_drift обязан
    сообщить о них как о кандидате на дрейф (одинаковый "отпечаток" без
    пробелов), не сливая бакеты сам — решение схлопнуть остаётся за
    trader-reflect."""
    recs = []
    for i, regime in enumerate(["trend up"] * 3 + ["trendup"] * 2):
        tid = f"dr{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime=regime))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert len(s["by_regime"]) == 2  # НЕ схлопнуто автоматически
    candidates = [d for d in s["label_drift"]
                 if d["field"] == "regime" and d["kind"] == "near_duplicate"]
    assert len(candidates) == 1
    assert set(candidates[0]["labels"]) == {"trend up", "trendup"}


def test_label_drift_reports_small_n_candidate(make_decision):
    recs = []
    for i in range(2):  # 2 << порог значимости
        tid = f"sn{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime="rare_regime"))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    candidates = [d for d in s["label_drift"]
                 if d["field"] == "regime" and d["kind"] == "small_n"]
    assert len(candidates) == 1
    assert candidates[0]["labels"] == ["rare regime"]


def test_by_setup_stays_raw_not_normalized(make_decision):
    """Дисциплина границы задачи: by_setup — СУЩЕСТВУЮЩИЙ сегмент (не новый),
    от него зависят scripts/recall.py и tests/test_e2e_dryrun.py по точным
    строкам setup_type. Нормализация (задача 2.2) применена только к НОВЫМ
    сегментам (by_regime и т.п.), где нет обратной совместимости, которую
    можно сломать. Если бы by_setup тоже начал нормализовать (лишний
    рефакторинг вне запроса задачи), "EMA_Pullback" и "ema_pullback" схлопнулись
    бы в один бакет и сломали бы вызывающих, которые ищут точное имя."""
    recs = []
    for i, (su, R) in enumerate([("EMA_Pullback", 1.0), ("ema_pullback", -1.0)]):
        tid = f"raw{i}"
        recs.append(_dec(make_decision, trade_id=tid, setup_type=su))
        recs.append(_out(tid, R))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert len(s["by_setup"]) == 2  # НЕ схлопнуто, в отличие от by_regime
    assert set(s["by_setup"]) == {"EMA_Pullback", "ema_pullback"}


def test_label_drift_small_n_threshold_tracks_min_n_for_confirmed(make_decision):
    """Порог "подозрительно мало n" — max(2, min_n_for_confirmed // 4), а не
    зашитая константа 5, которая случайно совпадает с дефолтным конфигом
    (min_n_for_confirmed=20). Один и тот же n=6 обязан вести себя по-разному
    в зависимости от порога значимости: не дрейф при пороге 8 (6 не меньше
    8//4=2), дрейф при пороге 40 (6 меньше 40//4=10)."""
    recs = []
    for i in range(6):
        tid = f"th{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime="mid_regime"))
        recs.append(_out(tid, 1.0))

    lo = compute_stats(recs, min_n_for_confirmed=8)
    hi = compute_stats(recs, min_n_for_confirmed=40)

    def _small_n_labels(stats):
        return {tuple(d["labels"]) for d in stats["label_drift"]
               if d["field"] == "regime" and d["kind"] == "small_n"}

    assert ("mid regime",) not in _small_n_labels(lo)
    assert ("mid regime",) in _small_n_labels(hi)


def test_label_drift_fires_for_every_free_text_field(make_decision):
    """Каждое из трёх свободнотекстовых полей модели (setup_type/regime/
    tactic — не только regime, который используют остальные тесты этого
    блока) обязано сканироваться на дрейф независимо: урок ревью этой фазы —
    дефект живёт в поле, которое есть в контракте, но не проверено ни одним
    тестом."""
    recs = []
    for i, (setup, regime, tactic) in enumerate([
            ("break out", "trend up", "fade dip"),
            ("breakout", "trendup", "fadedip")]):
        tid = f"fld{i}"
        recs.append(_dec(make_decision, trade_id=tid, setup_type=setup,
                         regime=regime, tactic=tactic))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    fields_with_near_dup = {d["field"] for d in s["label_drift"]
                            if d["kind"] == "near_duplicate"}
    assert fields_with_near_dup == {"setup_type", "regime", "tactic"}


def test_empty_journal_no_crash_empty_segments():
    s = compute_stats([], min_n_for_confirmed=20)
    assert s["overall"]["n"] == 0
    for key in ("by_setup", "by_symbol", "by_regime", "by_session", "by_model",
                "planned_vs_unplanned", "calibration_by_model"):
        assert s[key] == {}
    assert s["calibration"] == []
    assert s["label_drift"] == []


def test_decision_without_outcome_excluded_from_stats(make_decision):
    """Открытая (ещё не закрытая) сделка: decision есть, outcome ещё нет —
    _join() её не пары не находит, и она не должна появляться ни в одном
    R-based сегменте (не только overall, но и во всех новых by_*)."""
    recs = [_dec(make_decision, trade_id="open1", regime="trend")]
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["overall"]["n"] == 0
    assert s["by_regime"] == {}
    assert s["by_session"] == {}
    assert s["by_model"] == {}
    assert s["planned_vs_unplanned"] == {}


# --- ревью #2: markdown-отчёт читает человек после плохого дня + модель на
# границе сессии — три правки читаемости (легенда unknown/empty_label,
# small_n не топит near_duplicate в markdown, None не печатается голым). ---

def test_scorecard_shows_legend_when_unknown_or_empty_label_bucket_present(make_decision):
    """Легенда обязана появиться, когда в отчёте реально есть бакет
    "unknown" или "empty_label" — иначе человек не может отличить "поля не
    было" от "поле было, но пусто", не открывая score.py (ревью)."""
    recs = [
        # unknown: поля regime нет вообще (легаси-запись)
        {"type": "decision", "trade_id": "legacy1", "setup_type": "s1",
         "confidence": 0.6, "symbol": "XAUUSD"},
        {"type": "outcome", "trade_id": "legacy1", "R": 1.0},
    ]
    s = compute_stats(recs, min_n_for_confirmed=20)
    md = render_scorecard(s, progress_to_target_pct=1.0, target_pct=10)
    assert "unknown" in md and "empty_label" in md  # обе расшифрованы рядом
    assert "поля не было" in md or "старого формата" in md
    assert "после нормализации" in md or "пустым" in md


def test_scorecard_omits_legend_when_no_unknown_or_empty_label_bucket(make_decision):
    """Обратная сторона: журнал без легаси-записей и без мусорных меток не
    должен нести пояснение про случай, которого в этом отчёте нет."""
    recs = [_dec(make_decision, trade_id="t1", regime="trend"), _out("t1", 1.0)]
    s = compute_stats(recs, min_n_for_confirmed=20)
    md = render_scorecard(s, progress_to_target_pct=1.0, target_pct=10)
    assert "empty_label" not in md
    assert "поля не было" not in md


def test_scorecard_drift_section_shows_near_duplicate_not_small_n(make_decision):
    """В markdown должен остаться только near_duplicate — small_n при
    min_n_for_confirmed=20 (порог 5) кандидатом делает почти любую метку
    молодого журнала и топит ценный сигнал в шуме. stats.json при этом не
    трогаем (см. отдельный тест ниже) — это программный вход reflect."""
    s = compute_stats([], min_n_for_confirmed=20)
    s["label_drift"] = [
        {"field": "regime", "kind": "near_duplicate", "labels": ["mr fade", "mrf ade"],
         "n_by_label": {"mr fade": 3, "mrf ade": 3},
         "note": "разные бакеты, но совпадают без разделителей — требует "
                 "проверки смысла, автоматически НЕ схлопнуто"},
        {"field": "regime", "kind": "small_n", "labels": ["rare regime"],
         "n_by_label": {"rare regime": 2},
         "note": "n=2 < 5 — возможный осколок другого ярлыка, не доказательство"},
    ]
    md = render_scorecard(s, progress_to_target_pct=1.0, target_pct=10)
    assert "mr fade" in md and "mrf ade" in md  # near_duplicate показан
    assert "rare regime" not in md              # small_n НЕ показан
    assert "small_n" not in md


def test_scorecard_drift_section_absent_when_only_small_n_candidates(make_decision):
    """Если весь label_drift состоит из small_n (типичная картина молодого
    журнала), секция дрейфа в markdown не печатается вовсе — печатать
    заголовок с пустым по сути содержимым хуже, чем не печатать его."""
    s = compute_stats([], min_n_for_confirmed=20)
    s["label_drift"] = [
        {"field": "regime", "kind": "small_n", "labels": ["rare regime"],
         "n_by_label": {"rare regime": 2}, "note": "n=2 < 5"},
    ]
    md = render_scorecard(s, progress_to_target_pct=1.0, target_pct=10)
    assert "Возможный дрейф ярлыков" not in md


def test_compute_stats_label_drift_still_returns_small_n_unchanged(make_decision):
    """stats.json (compute_stats) НЕ фильтруется — только markdown. Это
    программный вход trader-reflect, урезать его нельзя (ревью явно просил
    "в stats.json оставь как есть")."""
    recs = []
    for i in range(2):
        tid = f"kn{i}"
        recs.append(_dec(make_decision, trade_id=tid, regime="rare_kept"))
        recs.append(_out(tid, 1.0))
    s = compute_stats(recs, min_n_for_confirmed=20)
    kinds = {d["kind"] for d in s["label_drift"] if d["field"] == "regime"}
    assert "small_n" in kinds


def test_scorecard_empty_overall_shows_na_not_bare_none():
    """Пустой журнал: avg_R/wr в overall — None (см. _agg). Раньше это
    печаталось буквально как "None" в markdown — читается как недоделка, а
    не как осознанное "данных нет". Приведено к той же конвенции, что уже
    применена к progress_to_target_pct."""
    s = compute_stats([], min_n_for_confirmed=20)
    md = render_scorecard(s, progress_to_target_pct=None, target_pct=10)
    assert "None" not in md
    assert "н/д" in md


def test_scorecard_real_zero_avg_r_still_renders_as_zero(make_decision):
    """Контроль: реальный ноль (не отсутствие данных) остаётся 0, а не
    подменяется на "н/д" — иначе стёрлась бы разница "нет данных" vs "данные
    говорят ноль", которую весь этот блок правок обязан сохранить."""
    recs = []
    for i, R in enumerate([1.0, -1.0]):
        tid = f"z{i}"
        recs.append(_dec(make_decision, trade_id=tid))
        recs.append(_out(tid, R))
    s = compute_stats(recs, min_n_for_confirmed=20)
    assert s["overall"]["avg_R"] == 0.0
    md = render_scorecard(s, progress_to_target_pct=1.0, target_pct=10)
    assert "avg R):** 0.0" in md


# --------------------------------------------------------------------------
# смоук-прогоны не попадают в статистику
# --------------------------------------------------------------------------

def test_smoke_runs_are_excluded_from_stats():
    """РЕГРЕСС 2026-07-27: три технических прогона стека дали «WR 0.0 · n=4»
    вместо честного «данных нет», и калибровка модели считалась по мусору.
    Проверка живого пути — не торговое решение и статистикой не является."""
    records = []
    for i in range(3):
        records += [
            {"type": "decision", "trade_id": f"s{i}", "smoke": True,
             "setup_type": "smoke", "confidence": 0.55, "model_id": "m1",
             "symbol": "XAUUSD", "regime": "тест", "session_phase": "NY",
             "planned": True},
            {"type": "outcome", "trade_id": f"s{i}", "R": -0.02},
        ]
    records += [
        {"type": "decision", "trade_id": "real", "setup_type": "range_fade",
         "confidence": 0.6, "model_id": "m1", "symbol": "XAUUSD",
         "regime": "флет", "session_phase": "NY", "planned": True},
        {"type": "outcome", "trade_id": "real", "R": 1.5},
    ]

    stats = compute_stats(records, min_n_for_confirmed=20)
    assert stats["overall"]["n"] == 1
    assert stats["overall"]["sum_R"] == 1.5
    assert "smoke" not in stats["by_setup"]


def test_stats_are_empty_when_only_smoke_runs_exist():
    """День, в котором были только проверки стека, обязан выглядеть как день
    без сделок, а не как день с нулевым винрейтом."""
    records = [
        {"type": "decision", "trade_id": "s1", "smoke": True, "setup_type": "smoke",
         "confidence": 0.55, "model_id": "m1", "symbol": "XAUUSD",
         "regime": "тест", "session_phase": "NY", "planned": True},
        {"type": "outcome", "trade_id": "s1", "R": -0.02},
    ]
    stats = compute_stats(records, min_n_for_confirmed=20)
    assert stats["overall"]["n"] == 0
    assert stats["overall"]["wr"] is None
    assert stats["calibration_by_model"] == {} or not stats["calibration_by_model"].get("m1")
