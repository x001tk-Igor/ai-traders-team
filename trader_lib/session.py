"""Серверное время, фазы дня и сессионный гейт (задача 5.3).

ЭТО ФИКС БАГА. Граница торгового дня считалась по UTC-полуночи в трёх местах
(risk_gate_cli._day_start_utc, perceive._day_baseline, alert_watch.session_
phase), а брокер живёт на UTC+3: с 21:00 до 24:00 UTC у него уже следующий
день. Значит дневной лимит −3% три часа в сутки отмерялся не от того нуля —
главное защитное число считалось от неверной точки отсчёта.

ЧТО В КАКОМ ВРЕМЕНИ (без этого разделения путаница неизбежна):

  * ГРАНИЦА ДНЯ — СЕРВЕРНАЯ. Это точка отсчёта дневного лимита и день, за
    который брокер начисляет свопы. server_day_key — единственный правильный
    ключ «за какой день эта сделка/этот baseline».
  * ОКНА И ФАЗЫ — UTC. Так они записаны в конституции (ключи *_utc) и так
    сверяются с расписанием бирж: LONDON 07:00, NY 12:15 — это UTC. Переводить
    их в серверное время значило бы привязать расписание сессий к тому, какой
    часовой пояс выбрал брокер.

ДЕНЬ НЕДЕЛИ ДЛЯ ПЯТНИЧНЫХ ПРАВИЛ БЕРЁТСЯ ПО UTC, потому что и сами правила
записаны в UTC (friday_no_new_utc=15:00, friday_flat_utc=19:00). У брокера с
большим положительным смещением серверная пятница начинается раньше, но
«не переносить позиции через выходные» — правило про закрытие РЫНКА, а рынок
закрывается в 21:00-22:00 UTC независимо от часового пояса брокера.
"""
import datetime as dt

UTC = dt.timezone.utc


def server_now(*, utc_now, offset_hours):
    """Время брокера. Смещение — из конституции (risk.server_utc_offset_hours),
    а его фактическое значение проверяет bootstrap_env (задача 0.3): расхождение
    блокирует старт, потому что от него зависит граница дня."""
    return utc_now + dt.timedelta(hours=offset_hours)


def server_day_key(*, utc_now, offset_hours, reset_hour=0):
    """Ключ торгового дня брокера: 'YYYY-MM-DD'.

    reset_hour — час СЕРВЕРНОГО времени, в который у брокера начинается новый
    день (обычно 0). Если он не полночь, точка отсчёта сдвигается вместе с ним.
    """
    s = server_now(utc_now=utc_now, offset_hours=offset_hours)
    return (s - dt.timedelta(hours=reset_hour)).date().isoformat()


def server_day_start_utc(*, utc_now, offset_hours, reset_hour=0):
    """Момент начала текущего серверного дня, выраженный в UTC.

    Нужен там, где сравниваются метки времени (события за день, сделки за
    день), а не ключи дня. Живёт здесь, чтобы арифметика смещения не
    расползлась копиями по вызывающим — ровно так и появился баг, который эта
    задача чинит.
    """
    s = server_now(utc_now=utc_now, offset_hours=offset_hours)
    start_server = (s - dt.timedelta(hours=reset_hour)).replace(
        hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(hours=reset_hour)
    return start_server - dt.timedelta(hours=offset_hours)


def _hm(value):
    """'HH:MM' → минуты от полуночи. Битое значение → None (вызывающий решает,
    что делать; молча подставлять 0 нельзя — это полночь, реальное время)."""
    try:
        h, m = (int(x) for x in str(value).split(":"))
    except (ValueError, AttributeError, TypeError):
        return None
    return h * 60 + m


def _minutes(utc_now):
    return utc_now.hour * 60 + utc_now.minute


def _inside(minutes, start, end):
    """Полуинтервал [start, end). Окно через полночь (start > end) считается
    как объединение хвоста суток и начала следующих."""
    if start is None or end is None:
        return False
    return start <= minutes < end if start <= end else (minutes >= start or minutes < end)


def current_phase(*, utc_now, cfg):
    """Фаза дня по cfg.session.phases (UTC). Вне всех окон — phase=None.

    Границы полуинтервальные: начало включено, конец исключён — иначе на стыке
    двух фаз ответ зависел бы от порядка ключей в конфиге.
    """
    minutes = _minutes(utc_now)
    for label, window in (cfg.session.phases or {}).items():
        try:
            start, end = _hm(window[0]), _hm(window[1])
        except (IndexError, TypeError):
            continue
        if _inside(minutes, start, end):
            left = (end - minutes) if end >= minutes else (24 * 60 - minutes + end)
            return {"phase": label, "from": window[0], "to": window[1],
                    "minutes_left": left}
    return {"phase": None, "from": None, "to": None, "minutes_left": None}


# Гэп открытия недели, начиная с которого первый час — только наблюдение.
MONDAY_GAP_ATR = 0.5
MONDAY_OBSERVE_MINUTES = 60


def monday_gap_state(*, utc_now, gap_atr, cfg):
    """Понедельничный протокол гэпа (задача 8.3).

    За выходные цена уезжает без возможности выйти по стопу, а в первый час
    недели ликвидность тонкая: разрывы двигают цену сильнее, чем участники.
    Поэтому при гэпе больше половины ATR первый час торгового окна — только
    наблюдение.

    Неизвестный гэп (нет ATR или баров) НЕ запрещает день: о недостатке данных
    скажет брифинг, а запрет по незнанию превратился бы в запрет по умолчанию
    каждый понедельник, когда история не подтянулась.
    """
    if utc_now.weekday() != 0:
        return {"observe_only": False, "until": None, "reason": "не понедельник"}
    if gap_atr is None:
        return {"observe_only": False, "until": None,
                "reason": "гэп не измерен (нет ATR или баров) — запрет по незнанию "
                          "не вводится, смотри брифинг"}

    open_min = _hm(cfg.session.trade_window_utc[0]) or 0
    start = utc_now.replace(hour=open_min // 60, minute=open_min % 60,
                            second=0, microsecond=0)
    until = start + dt.timedelta(minutes=MONDAY_OBSERVE_MINUTES)
    if abs(gap_atr) <= MONDAY_GAP_ATR or utc_now >= until:
        return {"observe_only": False, "until": until,
                "reason": (f"гэп {gap_atr:+.2f} ATR в пределах нормы"
                           if abs(gap_atr) <= MONDAY_GAP_ATR
                           else "час наблюдения после гэпа истёк")}
    return {"observe_only": True, "until": until,
            "reason": f"гэп открытия недели {gap_atr:+.2f} ATR — первый час "
                      f"(до {until.strftime('%H:%M')} UTC) только наблюдение: "
                      "цена ушла без возможности выйти по стопу, ликвидность тонкая"}


def session_gate(*, utc_now, cfg):
    """Разрешено ли открывать новое и не пора ли закрываться.

    → {allow_new, flat_required, reasons, phase, server_day}

    Причины — строки для журнала: их читает человек через месяц, поэтому это
    фразы, а не коды.
    """
    s = cfg.session
    minutes = _minutes(utc_now)
    weekday = utc_now.weekday()          # 0=пн … 4=пт, 5=сб, 6=вс
    reasons, allow, flat = [], True, False

    if weekday >= 5:
        allow = False
        reasons.append("выходные: рынок форекс закрыт")

    win_start, win_end = _hm(s.trade_window_utc[0]), _hm(s.trade_window_utc[1])
    if not _inside(minutes, win_start, win_end):
        allow = False
        reasons.append(f"вне торгового окна {s.trade_window_utc[0]}–"
                       f"{s.trade_window_utc[1]} UTC")

    no_new = _hm(s.no_new_after_utc)
    if no_new is not None and minutes >= no_new:
        allow = False
        reasons.append(f"после no_new_after {s.no_new_after_utc} UTC новых входов нет: "
                       "сделке нужно время отработать, а не попасть в закрытие")

    swap_start, swap_end = _hm(s.swap_block_utc[0]), _hm(s.swap_block_utc[1])
    if _inside(minutes, swap_start, swap_end):
        allow = False
        reasons.append(f"окно начисления свопов {s.swap_block_utc[0]}–"
                       f"{s.swap_block_utc[1]} UTC: спреды разъезжаются, "
                       "исполнение непредсказуемо")

    if weekday == 4:
        fri_no_new = _hm(s.friday_no_new_utc)
        if fri_no_new is not None and minutes >= fri_no_new:
            allow = False
            reasons.append(f"пятница после {s.friday_no_new_utc} UTC: новых входов нет")
        fri_flat = _hm(s.friday_flat_utc)
        if fri_flat is not None and minutes >= fri_flat:
            allow, flat = False, True
            reasons.append(f"пятница после {s.friday_flat_utc} UTC: позиции не "
                           "переносятся через выходные, гэп открытия не защищён стопом")

    phase = current_phase(utc_now=utc_now, cfg=cfg)
    return {"allow_new": allow, "flat_required": flat, "reasons": reasons,
            "phase": phase["phase"], "phase_window": (phase["from"], phase["to"]),
            "server_day": server_day_key(utc_now=utc_now,
                                         offset_hours=cfg.risk.server_utc_offset_hours,
                                         reset_hour=cfg.risk.server_day_reset_hour)}
