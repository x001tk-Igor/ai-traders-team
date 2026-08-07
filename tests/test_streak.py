import datetime as dt

from trader_lib.streak import compute_streak

UTC = dt.timezone.utc

STREAK_CFG = {
    "two_losses": {"risk_mult": 0.5, "minutes": 120},
    "three_losses": {"pause_minutes": 60},
    "four_losses": {"halt_rest_of_day": True},
}

DAY_START = dt.datetime(2026, 7, 27, 0, 0, 0, tzinfo=UTC)


def _outcome(close_ts, R, trade_id="t"):
    return {"type": "outcome", "close_ts": close_ts.isoformat(), "R": R, "trade_id": trade_id}


def _decision(ts, trade_id="t"):
    return {"type": "decision", "ts": ts.isoformat(), "trade_id": trade_id, "symbol": "XAUUSD"}


def test_two_losses_halves_risk_120min():
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t1"),
        _outcome(last_loss, -1.0, "t2"),
    ]
    now = last_loss + dt.timedelta(minutes=30)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 2
    assert v["risk_mult"] == 0.5
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False


def test_three_losses_sets_pause():
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=2), -1.0, "t1"),
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t2"),
        _outcome(last_loss, -1.0, "t3"),
    ]
    now = last_loss + dt.timedelta(minutes=10)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 3
    expected_pause = last_loss + dt.timedelta(minutes=60)
    assert v["paused_until"] == expected_pause.isoformat()
    assert v["halt_rest_of_day"] is False


def test_four_losses_halts_day():
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=3), -1.0, "t1"),
        _outcome(last_loss - dt.timedelta(hours=2), -1.0, "t2"),
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t3"),
        _outcome(last_loss, -1.0, "t4"),
    ]
    now = last_loss + dt.timedelta(minutes=5)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 4
    assert v["halt_rest_of_day"] is True
    # halt does not expire within the day, unlike the two-loss window
    later = last_loss + dt.timedelta(hours=10)
    v2 = compute_streak(records, now=later, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v2["halt_rest_of_day"] is True


def test_win_resets_streak():
    t1 = DAY_START + dt.timedelta(hours=8)
    t2 = DAY_START + dt.timedelta(hours=8, minutes=30)
    t3 = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(t1, -1.0, "t1"),
        _outcome(t2, -1.0, "t2"),
        _outcome(t3, 1.5, "t3"),  # win resets the streak to 0
    ]
    now = t3 + dt.timedelta(minutes=5)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 0
    assert v["risk_mult"] == 1.0
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False


def test_window_expires():
    # 2 losses closed 3 hours ago (window is 120 min) → mult back to 1.0
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t1"),
        _outcome(last_loss, -1.0, "t2"),
    ]
    now = last_loss + dt.timedelta(hours=3)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 2
    assert v["risk_mult"] == 1.0


def test_window_boundary_exact_minutes_is_expired():
    # exactly `minutes` elapsed → window is considered over, not active anymore
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t1"),
        _outcome(last_loss, -1.0, "t2"),
    ]
    now = last_loss + dt.timedelta(minutes=120)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["risk_mult"] == 1.0

    just_before = last_loss + dt.timedelta(minutes=119, seconds=59)
    v2 = compute_streak(records, now=just_before, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v2["risk_mult"] == 0.5


def test_empty_journal():
    v = compute_streak([], now=DAY_START + dt.timedelta(hours=1),
                        day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 0
    assert v["risk_mult"] == 1.0
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False


def test_previous_day_records_ignored():
    prev_day_loss = DAY_START - dt.timedelta(hours=2)
    records = [
        _outcome(prev_day_loss - dt.timedelta(hours=1), -1.0, "y1"),
        _outcome(prev_day_loss, -1.0, "y2"),
        _outcome(prev_day_loss, -1.0, "y3"),
    ]
    now = DAY_START + dt.timedelta(hours=1)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 0
    assert v["risk_mult"] == 1.0
    assert v["halt_rest_of_day"] is False


def test_unsorted_input_matches_sorted_result():
    last_loss = DAY_START + dt.timedelta(hours=9)
    sorted_records = [
        _outcome(last_loss - dt.timedelta(hours=2), -1.0, "t1"),
        _outcome(last_loss - dt.timedelta(hours=1), 2.0, "t2"),  # win resets
        _outcome(last_loss, -1.0, "t3"),
    ]
    # shuffled order — the module must sort by close_ts itself
    shuffled_records = [sorted_records[2], sorted_records[0], sorted_records[1]]
    now = last_loss + dt.timedelta(minutes=5)

    v_sorted = compute_streak(sorted_records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    v_shuffled = compute_streak(shuffled_records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v_sorted == v_shuffled
    assert v_sorted["loss_streak"] == 1  # win between the two losses breaks the streak


def _losing_streak_records(n, last_loss):
    """n убытков подряд, последний закрылся ровно в last_loss, предыдущие — раньше в тот же день."""
    return [
        _outcome(last_loss - dt.timedelta(minutes=10 * (n - 1 - i)), -1.0, f"t{i}")
        for i in range(n)
    ]


def test_three_losses_keeps_reduced_risk():
    # серия 3: пауза (60 мин) ещё действует, риск-окно (120 мин) тоже — деэскалация
    # накапливается, а не переключается «либо risk_mult, либо paused_until»
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = _losing_streak_records(3, last_loss)
    now = last_loss + dt.timedelta(minutes=10)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 3
    assert v["risk_mult"] == 0.5
    assert v["paused_until"] is not None


def test_four_losses_also_reduced_risk():
    # серия 4: halt действует, но risk_mult не «отменяется» halt-ом — оба поля
    # отражают реальное состояние независимо друг от друга
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = _losing_streak_records(4, last_loss)
    now = last_loss + dt.timedelta(minutes=10)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 4
    assert v["halt_rest_of_day"] is True
    assert v["risk_mult"] == 0.5
    assert v["paused_until"] is not None  # three_losses (>=3) продолжает действовать и при 4


def test_cascade_is_monotonic():
    # Свойство: риск не может РАСТИ по мере ухудшения серии. Последний убыток
    # только что закрылся (все окна ещё активны для каждой длины серии) —
    # risk_mult(n) должен быть неубывающей... точнее НЕвозрастающей последовательностью.
    last_loss = DAY_START + dt.timedelta(hours=9)
    now = last_loss + dt.timedelta(seconds=1)
    mults = []
    for n in range(0, 6):
        records = _losing_streak_records(n, last_loss) if n > 0 else []
        v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
        assert v["loss_streak"] == n
        mults.append(v["risk_mult"])

    for i in range(1, len(mults)):
        assert mults[i] <= mults[i - 1], f"risk_mult вырос при серии {i}: {mults}"


def test_ignores_decision_records_mixed_with_outcomes():
    # journal.jsonl в бою — это decision И outcome вперемешку в одном списке.
    # decision-записи не имеют close_ts — модуль обязан фильтровать по type
    # ДО попытки прочитать close_ts, а не полагаться на память о порядке.
    last_loss = DAY_START + dt.timedelta(hours=9)
    records = [
        _decision(last_loss - dt.timedelta(hours=1), "t1"),
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t1"),
        _decision(last_loss, "t2"),
        _outcome(last_loss, -1.0, "t2"),
    ]
    now = last_loss + dt.timedelta(minutes=10)
    v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
    assert v["loss_streak"] == 2
    assert v["risk_mult"] == 0.5


def test_reason_content_by_streak_length():
    # reason — то, что читает человек/модель при разборе дня. Проверяем по
    # осмысленным подстрокам (не полным совпадением), чтобы правка формулировки
    # не ломала тест, но дрейф уровня каскада или числа/склонения — ломал.
    last_loss = DAY_START + dt.timedelta(hours=9)
    now = last_loss + dt.timedelta(minutes=10)
    expected_substrings = {
        2: ["2 убытка", "риск"],
        3: ["3 убытка", "риск", "пауза"],
        4: ["4 убытка", "риск", "пауза", "стоп"],
        5: ["5 убытков", "риск", "пауза", "стоп"],
    }
    not_expected = {
        2: ["пауза", "стоп"],
        3: ["стоп"],
    }
    for n, substrings in expected_substrings.items():
        records = _losing_streak_records(n, last_loss)
        v = compute_streak(records, now=now, day_start_utc=DAY_START, streak_cfg=STREAK_CFG)
        for s in substrings:
            assert s in v["reason"], f"серия {n}: {s!r} не найдено в {v['reason']!r}"
        for s in not_expected.get(n, []):
            assert s not in v["reason"], f"серия {n}: {s!r} не должно быть в {v['reason']!r}"
