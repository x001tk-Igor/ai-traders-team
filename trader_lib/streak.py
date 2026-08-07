import datetime as dt


def _loss_word(n: int) -> str:
    """Родительный падеж «убыток» по числу n: 1→убыток, 2-4→убытка, иначе убытков
    (11-14 всегда убытков, даже когда n%10 в 2-4)."""
    n_mod_100 = n % 100
    n_mod_10 = n % 10
    if 11 <= n_mod_100 <= 14:
        return "убытков"
    if n_mod_10 == 1:
        return "убыток"
    if 2 <= n_mod_10 <= 4:
        return "убытка"
    return "убытков"


def compute_streak(records, *, now, day_start_utc, streak_cfg) -> dict:
    """Каскад-стоп по серии убытков внутри торгового дня.

    records      — список записей журнала (список dict), как их отдаёт
                   trader_lib.journal.read_records
    now          — timezone-aware datetime (UTC)
    day_start_utc— timezone-aware datetime: начало текущего торгового дня в UTC
                   (границу считает вызывающий; этот модуль её не вычисляет)
    streak_cfg   — cfg.risk.streak

    Возвращает:
      {'loss_streak': int,          # длина текущей серии убытков подряд
       'risk_mult': float,          # множитель риска (1.0 если правило не действует)
       'paused_until': str|None,    # ISO-8601 UTC, если действует пауза
       'halt_rest_of_day': bool,
       'reason': str}               # короткое человекочитаемое объяснение
    """
    # Технические прогоны стека (смоук) — не торговые решения. Они открывают и
    # тут же закрывают позицию, поэтому всегда дают маленький минус (спред), и
    # три такие проверки подряд выглядят как серия убытков. 2026-07-27 это
    # остановило торговлю на весь день: три смоука по −0.02R плюс одна реальная
    # сделка дали серию из четырёх. Правило сработало по своей букве — но
    # защищать счёт от собственных проверок работоспособности оно не должно.
    #
    # Признак ставится в момент входа (draft['smoke']) и в журнал попадает
    # вместе с решением: пометить убыток смоуком ЗАДНИМ ЧИСЛОМ невозможно, и
    # это здесь главное — иначе правило обходилось бы одним словом в записи.
    smoke_ids = {r.get("trade_id") for r in records
                 if r.get("type") == "decision" and r.get("smoke") is True}
    outcomes_with_ts = (
        (dt.datetime.fromisoformat(r["close_ts"]), r) for r in records
        if r["type"] == "outcome" and r.get("trade_id") not in smoke_ids
    )
    today_outcomes = sorted(
        (pair for pair in outcomes_with_ts if pair[0] >= day_start_utc),
        key=lambda pair: pair[0],
    )

    loss_streak = 0
    last_loss_close_ts = None
    for close_ts, r in today_outcomes:
        if r["R"] > 0:
            loss_streak = 0
            last_loss_close_ts = None
        else:
            loss_streak += 1
            last_loss_close_ts = close_ts

    if loss_streak == 0:
        return _result(0, 1.0, None, False, "нет активной серии убытков")

    if loss_streak == 1:
        return _result(1, 1.0, None, False,
                        f"серия 1 {_loss_word(1)} — правила серии ещё не применяются")

    # Каскад деэскалации монотонен: пороги «>= N», а не «== N». Каждое следующее
    # ограничение НАКЛАДЫВАЕТСЯ поверх предыдущих, а не заменяет их — иначе после
    # истечения паузы (3 убытка) риск вернулся бы к 1.0 полным, хотя серия хуже,
    # чем при 2 убытках. Единственный, кто по смыслу «перекрывает всё» —
    # halt_rest_of_day: он блокирует новые сделки целиком, независимо от
    # risk_mult, но само поле risk_mult всё равно возвращается посчитанным
    # честно (вызывающий/risk_gate решает, что делать с halt).
    active = []

    risk_mult = 1.0
    if loss_streak >= 2:
        rule = streak_cfg["two_losses"]
        window_end = last_loss_close_ts + dt.timedelta(minutes=rule["minutes"])
        if now < window_end:
            risk_mult = rule["risk_mult"]
            active.append(f"риск ×{risk_mult} до {window_end.isoformat()}")

    paused_until = None
    if loss_streak >= 3:
        rule = streak_cfg["three_losses"]
        pause_end = last_loss_close_ts + dt.timedelta(minutes=rule["pause_minutes"])
        if now < pause_end:
            paused_until = pause_end.isoformat()
            active.append(f"пауза до {paused_until}")

    halt_rest_of_day = False
    if loss_streak >= 4:
        halt_rest_of_day = bool(streak_cfg["four_losses"]["halt_rest_of_day"])
        if halt_rest_of_day:
            active.append("стоп торговли до конца дня")

    streak_label = f"серия {loss_streak} {_loss_word(loss_streak)} подряд"
    if active:
        reason = f"{streak_label} — " + "; ".join(active)
    else:
        reason = f"{streak_label} — окна ограничений истекли"

    return _result(loss_streak, risk_mult, paused_until, halt_rest_of_day, reason)


def _result(loss_streak, risk_mult, paused_until, halt_rest_of_day, reason):
    return {
        "loss_streak": loss_streak,
        "risk_mult": risk_mult,
        "paused_until": paused_until,
        "halt_rest_of_day": halt_rest_of_day,
        "reason": reason,
    }
