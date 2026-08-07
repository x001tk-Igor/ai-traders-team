"""Риск-гейт: единственный носитель жёстких лимитов. Его вердикт исполняется
всегда — модель не может его переубедить, обойти или уговорить.

Чистая функция: не читает MT5/файлы/часы и НЕ импортирует соседние модули
фазы (streak/exposure/quality). Их результаты приходят готовыми числами
(loss_streak_mult, open_risk_usd, unprotected_positions, profile_mult) —
так гейт тестируется офлайн и его нельзя сломать правкой соседа. Сборку
входов делает scripts/risk_gate_cli.py.

ФОРМА ЛИМИТОВ. Основная — один параметр `limits` (dict или объект вроде
cfg.risk); ключи в нём индексируются НАПРЯМУЮ, чтобы опечатка падала громко,
а не подменялась дефолтом, ослабляющим лимит. Устаревшая форма (отдельные
kwargs daily_limit_pct/total_limit_pct/flatten_buffer_pct/K/cap_pct)
сохранена ради обратной совместимости и знает только пять старых лимитов: в
ней лимитов позиций/попыток/открытого риска и ступеней ladder нет вовсе.
Поэтому устаревшая форма ЗАПРЕЩАЕТ входы, которые эти лимиты обслуживают:
непустые open_risk_usd/positions_count/trades_today/unplanned_today вместе со
старыми kwargs → ValueError (через safe_evaluate_gate — HALT_NEW). Иначе
частично мигрировавший вызывающий («новые входы уже передаю, limits ещё нет»)
получал бы разрешение с полным риском и без единой новой проверки — до
появления этих kwargs такой вызов падал TypeError, и это поведение сохранено.
Смешивать две формы лимитов тоже нельзя — ValueError.

ЗАПРЕЩАЮЩИЙ ВЕРДИКТ ОТДАЁТ МАКСИМАЛЬНО ОГРАНИЧИТЕЛЬНЫЕ ПОЛЯ. При HALT_NEW и
FORCE_FLAT возвращаются max_risk_per_trade_usd=0.0,
require_setup_status='confirmed', planned_only=True, unplanned_allowed=False:
вызывающий, который прочитает поля и проигнорирует verdict, не должен уметь
собрать из них разрешение (fail-closed). Аварийный ответ safe_evaluate_gate
отдаёт тот же набор ключей (диагностика = None): журнал (задача 2.1) пишет
полную схему решения, и .get(key, default) на неполном ответе давал бы
разрешительный дефолт.

МНОЖИТЕЛИ — ТОЛЬКО ДЕЭСКАЛАЦИЯ. loss_streak_mult/profile_mult вне [0.0, 1.0]
→ ValueError: множитель > 1 поднял бы выданный риск выше кэпов и лимита
открытого риска, то есть обошёл бы сам смысл гейта. Это числа из кода
(streak.py/quality.py), а не ввод модели, поэтому здесь fail-loud, а не
тихий клип (safe_evaluate_gate превратит падение в HALT_NEW). По той же
причине отрицательные open_risk_usd/positions_count/trades_today/
unplanned_today/unprotected_positions → ValueError: они расширяют бюджет и
отодвигают лимиты, а приходят тоже из кода (exposure.py, счётчики журнала).

ВЫДАННЫЙ РИСК ОКРУГЛЯЕТСЯ ТОЛЬКО ВНИЗ (math.floor до копейки). round() при
остатке бюджета $0.035 выдал бы $0.04 и пробил бы бюджет — в риск-коде
округление вверх запрещено независимо от величины; так же (floor к шагу
лота) устроен size_position.py. Цена решения — возможная недодача одной
копейки, и это безопасное направление.

ВАЛИДАЦИИ, ПАДАЮЩИЕ ИСКЛЮЧЕНИЕМ, ИДУТ ПОСЛЕ ПРОВЕРКИ СТЕНЫ (шаг 1). Падение
превращается в HALT_NEW, а HALT_NEW не закрывает уже открытые позиции. Если
стена пробита, команду FORCE_FLAT терять нельзя ни из-за кривого множителя,
ни из-за K<=0, ни из-за неверной формы входов — сначала вердикт «закрыть
всё», и только потом придирки к аргументам.

МАШИННОЧИТАЕМЫЕ КОДЫ РЕШЕНИЯ РЯДОМ С РУССКОЙ ПРОЗОЙ. reasons остаётся для
человека — по нему аудитор пересчитывает выданный риск на бумаге. Но
сегментация статистики (задача 2.2: «сколько раз останавливал лимит
позиций», «какой терм связывал риск») не должна держаться на substring по
прозе: формулировки будут улучшаться. Поэтому в ответе есть blocked_by (код
из BLOCKED_CODES, None при OK/THROTTLE), binding_term (код из TERM_KEYS) и
terms — числа формулы под стабильными ключами. Русские подписи термов живут
отдельно (_term_labels) и на схему ответа не влияют.
"""

import datetime as dt
import math
from collections.abc import Mapping

# Ключи ступени ladder: loss_pct и risk_mult обязательны, require_status —
# осознанно необязателен (ступень 3.0% в конфиге его не имеет). Именно поэтому
# набор ключей валидируется целиком: опечатка «require_statuss» иначе тихо
# сняла бы требование подтверждённого сетапа.
_RUNG_KEYS = {"loss_pct", "risk_mult", "require_status"}
_LADDER_AXES = ("daily", "total")
_STATUSES = ("any", "confirmed")

# Устаревшая форма лимитов: набор обязателен целиком (порядок — для сообщения
# о том, каких kwargs не хватает).
_LEGACY_KWARGS = ("daily_limit_pct", "total_limit_pct", "flatten_buffer_pct", "K", "cap_pct")
# Правило near-wall в устаревшей форме заморожено на исторических числах:
# в ней нет конфига, а менять поведение старых вызывающих нельзя.
_LEGACY_NEAR_WALL_PCT = 1.0
_LEGACY_NEAR_WALL_MULT = 0.5

# Стабильные коды термов формулы шага 7 (binding_term и ключи terms). Русские
# подписи — в _term_labels; переформулировка подписи не ломает аналитику.
TERM_KEYS = ("budget_div_k", "per_trade_cap", "open_risk_room", "budget_minus_open_risk")

# Стабильные коды причин запрета (blocked_by). Для сегментации журнала в 2.2.
BLOCKED_CODES = (
    "wall_daily",              # дневная стена минус буфер → FORCE_FLAT
    "wall_total",              # общая стена минус буфер → FORCE_FLAT
    "halt_rest_of_day",        # каскад серии убытков: стоп до конца дня
    "paused",                  # каскад серии убытков: пауза ещё действует
    "unprotected_positions",   # есть позиция без стоп-лосса
    "max_open_positions",      # лимит одновременных позиций
    "max_new_trades",          # лимит попыток за день
    "budget_exhausted",        # бюджет риска <= 0
    "open_risk_exhausted",     # лимит открытого риска исчерпан
    "no_risk_left",            # связывающий терм <= 0 (какой — в binding_term)
    "risk_rounds_to_zero",     # выданный риск меньше копейки
    "gate_error",              # исключение внутри гейта (safe_evaluate_gate)
    # НЕ выставляется самим evaluate_gate — код используется вызывающим на
    # уровне CLI (scripts/risk_gate_cli.py.run), когда у брокера есть открытая
    # позиция без decision-записи в журнале (scripts/close_watch.py.
    # find_orphans) и CLI отдаёт blocked_response(...) ДО обращения к гейту.
    # Здесь он присутствует, потому что BLOCKED_CODES — общий домен для
    # journal.py.validate_decision(blocked_by), а не только для кодов,
    # которые возвращает сам evaluate_gate.
    "orphan_positions",
)


def evaluate_gate(*, equity, day_start_equity, initial_balance, limits=None,
                  open_risk_usd=0.0, unprotected_positions=0, positions_count=0,
                  trades_today=0, unplanned_today=0, loss_streak_mult=1.0,
                  paused_until=None, halt_rest_of_day=False,
                  profile_mult=1.0, now=None,
                  # --- устаревшая форма лимитов (см. шапку модуля) ---
                  daily_limit_pct=None, total_limit_pct=None,
                  flatten_buffer_pct=None, K=None, cap_pct=None) -> dict:
    """Можно ли открывать новую сделку и с каким риском.

    verdict:
      OK          — торгуй в пределах max_risk_per_trade_usd
      THROTTLE    — можно, но риск урезан (близко к стене)
      HALT_NEW    — новых входов нет, веди уже открытые
      FORCE_FLAT  — немедленно закрыть всё

    Возвращает:
      max_risk_per_trade_usd — с учётом ВСЕХ множителей и остатка открытого риска
      require_setup_status   — 'any' | 'confirmed'
      planned_only           — bool: только гипотезы из плана дня
      unplanned_allowed      — bool: остался ли лимит внеплановых входов
      reasons                — список сработавших ограничений (для журнала)
      risk_mult_applied      — произведение ВСЕХ применённых множителей
                               (ступени × серия × профиль × near-wall);
                               max_risk = связывающий терм × risk_mult_applied
      blocked_by             — код причины запрета из BLOCKED_CODES;
                               None при OK/THROTTLE
      binding_term           — код связывающего терма из TERM_KEYS (None,
                               если до шага 7 не дошли)
      terms                  — {код терма: число} по всем TERM_KEYS;
                               open_risk_room = None, если лимит не задан;
                               None целиком, если до шага 7 не дошли
      daily_risk_remaining_usd — daily_budget − open_risk_usd, то есть остаток
                               риска строго по ДНЕВНОЙ стене (так его называет
                               DECISION_REQUIRED в задаче 2.1). В формуле
                               участвует terms['budget_minus_open_risk'] —
                               то же за вычетом ОБЩЕЙ стены: min(дневного,
                               общего) бюджета − открытый риск. Числа
                               расходятся, когда общий бюджет меньше дневного
      daily_loss_pct, total_loss_pct, daily_budget, total_budget, budget,
      open_risk_usd, open_risk_room_usd, limits_source

    Порядок проверок, первый сработавший решает:
      1. стены (день/всего) минус буфер → FORCE_FLAT
      2. halt_rest_of_day / paused_until (ещё действует) /
         unprotected_positions > 0 → HALT_NEW
      3. positions_count >= max_open_positions → HALT_NEW
      4. trades_today >= max_new_trades_per_day → HALT_NEW
      5. бюджет <= 0 → HALT_NEW
      6. ступени ladder → risk_mult, require_setup_status
      7. max_risk = min(бюджет / K,                                budget_div_k
                        cap_pct/100 * equity,                      per_trade_cap
                        max_open_risk_pct/100*equity − open_risk,  open_risk_room
                        бюджет − open_risk_usd)                    budget_minus_open_risk
                    * ladder_mult * loss_streak_mult * profile_mult
         (min <= 0 → HALT_NEW: риска для новой сделки не осталось)
      8. near-wall (осталось < near_wall_pct лимита) → THROTTLE,
         риск × near_wall_mult

    ЧЕТВЁРТЫЙ ТЕРМ (бюджет − открытый риск) делает инвариант «сумма
    одновременно открытого риска не пробивает бюджет» структурным свойством
    формулы, а не следствием удачных чисел в конфиге. Терма
    max_open_risk_pct% × equity − open_risk_usd для этого НЕ достаточно: он
    совпадает с бюджетом только пока max_open_risk_pct <= daily_loss_limit_pct,
    и при max_open_risk_pct=5% против дневного лимита 3% пять позиций по
    бюджет/K пробивали бы дневную стену. `бюджет` здесь — min(дневного,
    общего), поэтому терм держит обе стены сразу.

    unplanned_today НЕ участвует в вердикте: исчерпанный лимит внеплановых
    входов — не причина остановить торговлю целиком, он лишь запрещает вход
    вне плана дня. Поэтому он отражён полями unplanned_allowed/planned_only,
    а решение по конкретному входу принимает вызывающий (entry_gate).
    Профильный planned_only (слабая модель) здесь не виден: гейт получает
    только число profile_mult, не имя профиля — его накладывает вызывающий.
    """
    # Разбор лимитов — единственная валидация ДО стены: без лимитов стену не
    # посчитать. Остальные валидации отложены за шаг 1 (см. шапку модуля).
    lim, limits_source = _resolve_limits(
        limits, daily_limit_pct=daily_limit_pct, total_limit_pct=total_limit_pct,
        flatten_buffer_pct=flatten_buffer_pct, K=K, cap_pct=cap_pct)

    daily_loss_pct = (day_start_equity - equity) / day_start_equity * 100
    total_loss_pct = (initial_balance - equity) / initial_balance * 100
    daily_budget = (lim["daily_limit_pct"] - daily_loss_pct) / 100 * day_start_equity
    total_budget = (lim["total_limit_pct"] - total_loss_pct) / 100 * initial_balance
    budget = max(0.0, min(daily_budget, total_budget))

    # Остаток лимита открытого риска. None — лимит не задан (устаревшая форма).
    max_open_risk_pct = lim["max_open_risk_pct"]
    room = None if max_open_risk_pct is None else max_open_risk_pct / 100 * equity - open_risk_usd

    max_unplanned = lim["max_unplanned"]
    unplanned_allowed = max_unplanned is None or unplanned_today < max_unplanned

    base = {
        "daily_loss_pct": round(daily_loss_pct, 3),
        "total_loss_pct": round(total_loss_pct, 3),
        "daily_budget": round(daily_budget, 2),
        "total_budget": round(total_budget, 2),
        "budget": round(budget, 2),
        "daily_risk_remaining_usd": round(daily_budget - open_risk_usd, 2),
        "open_risk_usd": round(open_risk_usd, 2),
        "open_risk_room_usd": None if room is None else round(room, 2),
        "unplanned_allowed": unplanned_allowed,
        "planned_only": not unplanned_allowed,
        "limits_source": limits_source,
        # заполняются на шаге 7; до него решение принято без термов формулы
        "binding_term": None,
        "terms": None,
    }

    reasons = []
    if limits_source == "legacy_kwargs":
        reasons.append("устаревшая форма лимитов (старые kwargs): лимиты позиций, попыток, "
                       "открытого риска и ступени ladder НЕ переданы и не действуют")
    if not unplanned_allowed:
        reasons.append(f"внеплановые входы исчерпаны: {unplanned_today} из {max_unplanned} "
                       "— допустимы только гипотезы из плана дня")

    # --- 1. стены минус буфер ---------------------------------------------
    daily_flat = lim["daily_limit_pct"] - lim["flatten_buffer_pct"]
    total_flat = lim["total_limit_pct"] - lim["flatten_buffer_pct"]
    walls = []
    if daily_loss_pct >= daily_flat:
        walls.append(f"дневной убыток {daily_loss_pct:.2f}% >= стена {lim['daily_limit_pct']}% "
                     f"− буфер {lim['flatten_buffer_pct']}% = {daily_flat:.2f}%")
    if total_loss_pct >= total_flat:
        walls.append(f"общий убыток {total_loss_pct:.2f}% >= стена {lim['total_limit_pct']}% "
                     f"− буфер {lim['flatten_buffer_pct']}% = {total_flat:.2f}%")
    if walls:
        # код — по первой сработавшей стене в порядке проверки; если пробиты
        # обе, обе перечислены в reasons
        code = "wall_daily" if daily_loss_pct >= daily_flat else "wall_total"
        return _blocked(base, "FORCE_FLAT", reasons + walls + ["закрыть все позиции немедленно"],
                        blocked_by=code)

    # Валидации, падающие исключением: строго ПОСЛЕ стены (см. шапку модуля) —
    # HALT_NEW из safe_evaluate_gate не закрывает уже открытые позиции.
    _validate_runtime(lim, limits_source=limits_source, loss_streak_mult=loss_streak_mult,
                      profile_mult=profile_mult, open_risk_usd=open_risk_usd,
                      positions_count=positions_count, trades_today=trades_today,
                      unplanned_today=unplanned_today,
                      unprotected_positions=unprotected_positions)

    # --- 2. каскад серии убытков и позиции без стопа -----------------------
    if halt_rest_of_day:
        return _blocked(base, "HALT_NEW", reasons + ["серия убытков: стоп торговли до конца дня"],
                        blocked_by="halt_rest_of_day")

    pause_end = _parse_paused_until(paused_until)
    if pause_end is not None:
        now = now or dt.datetime.now(dt.timezone.utc)
        if now < pause_end:
            return _blocked(base, "HALT_NEW",
                            reasons + [f"пауза после серии убытков до {pause_end.isoformat()}"],
                            blocked_by="paused")

    if unprotected_positions > 0:
        # риск позиции без стоп-лосса неограничен и не выражается числом —
        # учесть его в бюджете нельзя, поэтому новые входы запрещены
        return _blocked(base, "HALT_NEW",
                        reasons + [f"позиций без стоп-лосса: {unprotected_positions} — их риск "
                                   "неизвестен и не учитывается в бюджете (fail-closed)"],
                        blocked_by="unprotected_positions")

    # --- 3. лимит одновременных позиций ------------------------------------
    max_open_positions = lim["max_open_positions"]
    if max_open_positions is not None and positions_count >= max_open_positions:
        return _blocked(base, "HALT_NEW",
                        reasons + [f"открыто позиций {positions_count} из {max_open_positions}"],
                        blocked_by="max_open_positions")

    # --- 4. лимит попыток за день ------------------------------------------
    max_new_trades = lim["max_new_trades"]
    if max_new_trades is not None and trades_today >= max_new_trades:
        return _blocked(base, "HALT_NEW",
                        reasons + [f"попыток за день {trades_today} из {max_new_trades}"],
                        blocked_by="max_new_trades")

    # --- 5. бюджет ---------------------------------------------------------
    if budget <= 0:
        return _blocked(base, "HALT_NEW",
                        reasons + [f"бюджет риска исчерпан: день ${daily_budget:.2f}, "
                                   f"всего ${total_budget:.2f}"],
                        blocked_by="budget_exhausted")

    # --- 6. ступени деэскалации (бросает ValueError на битом ladder) --------
    ladder = _active_ladder(lim["ladder"], daily_loss_pct=daily_loss_pct,
                            total_loss_pct=total_loss_pct)
    reasons += ladder["reasons"]

    # --- 7. сколько риска отдать новой сделке -------------------------------
    terms = {"budget_div_k": budget / lim["K"],
             "per_trade_cap": lim["cap_pct"] / 100 * equity,
             # держит инвариант «сумма одновременного риска <= бюджета» вне
             # зависимости от того, шире ли max_open_risk_pct дневного лимита
             "budget_minus_open_risk": budget - open_risk_usd}
    if room is not None:
        terms["open_risk_room"] = room
    binding = min(terms, key=terms.get)
    labels = _term_labels(lim["cap_pct"])
    at_step_7 = {"binding_term": binding,
                 "terms": {key: None if key not in terms else round(terms[key], 2)
                           for key in TERM_KEYS}}

    mult = ladder["mult"] * loss_streak_mult * profile_mult

    # --- 8. близко к стене → THROTTLE --------------------------------------
    near = []
    if lim["daily_limit_pct"] - daily_loss_pct < lim["near_wall_pct"]:
        near.append(f"до дневной стены {lim['daily_limit_pct'] - daily_loss_pct:.2f}%")
    if lim["total_limit_pct"] - total_loss_pct < lim["near_wall_pct"]:
        near.append(f"до общей стены {lim['total_limit_pct'] - total_loss_pct:.2f}%")
    if near:
        mult *= lim["near_wall_mult"]
        reasons.append("близко к стене (" + ", ".join(near) +
                       f") → риск ×{lim['near_wall_mult']}")

    # Единственное место, где считается выданный риск: mult к этому моменту
    # собран целиком (ступени × серия × профиль × near-wall), поэтому raw и
    # risk_mult_applied не могут разойтись — их источник один.
    # ВНИЗ до копейки, никогда round: округление вверх выдаёт больше, чем
    # позволяет связывающий терм, и при остатке бюджета $0.035 выданные $0.04
    # пробивают дневной бюджет. Величина смешная, направление — нет; тем же
    # правилом (math.floor к шагу лота) живёт size_position.py.
    raw = terms[binding] * mult
    max_risk = math.floor(raw * 100) / 100
    if max_risk <= 0:
        if room is not None and room <= 0:
            code = "open_risk_exhausted"
            why = (f"открытый риск ${open_risk_usd:.2f} исчерпал лимит {max_open_risk_pct}% "
                   f"(=${max_open_risk_pct / 100 * equity:.2f}) — новых входов нет")
        elif terms[binding] <= 0:
            code = "no_risk_left"
            why = (f"риска для новой сделки не осталось: «{labels[binding]}» = "
                   f"${terms[binding]:.2f} (открытый риск ${open_risk_usd:.2f} "
                   f"при бюджете ${budget:.2f})")
        else:
            code = "risk_rounds_to_zero"
            why = (f"допустимый риск ${raw:.4f} после множителей округляется до нуля — "
                   "сделка невозможна")
        return _blocked(base, "HALT_NEW", reasons + [why], blocked_by=code, **at_step_7)

    terms_str = ", ".join(f"{labels[key]} ${value:.2f}" for key, value in terms.items())
    reasons.append(f"риск ${max_risk:.2f} = связывающий терм «{labels[binding]}» "
                   f"(${terms[binding]:.2f}) × общий множитель ×{round(mult, 6)} "
                   f"(термы: {terms_str})")

    return {**base, **at_step_7,
            "verdict": "THROTTLE" if near else "OK",
            "max_risk_per_trade_usd": max_risk,
            "require_setup_status": ladder["require_setup_status"],
            "risk_mult_applied": round(mult, 6),
            "blocked_by": None,
            "reasons": reasons}


def _resolve_limits(limits, **legacy) -> tuple:
    """Приводит обе формы лимитов к одному внутреннему виду (см. шапку модуля).

    Возвращает (lim, limits_source). В устаревшей форме лимиты, которых в ней
    нет, равны None (проверка пропускается), ladder пуст.
    """
    given_legacy = sorted(name for name, value in legacy.items() if value is not None)

    if limits is not None:
        if given_legacy:
            raise ValueError(
                "переданы обе формы лимитов одновременно: limits и устаревшие kwargs "
                f"({', '.join(given_legacy)}) — неоднозначно, оставь только limits")
        pick = _limit_picker(limits)
        lim = {
            "daily_limit_pct": pick("daily_loss_limit_pct"),
            "total_limit_pct": pick("total_loss_limit_pct"),
            "flatten_buffer_pct": pick("flatten_buffer_pct"),
            "K": pick("risk_budget_divisor_K"),
            "cap_pct": pick("per_trade_risk_cap_pct"),
            "max_open_positions": pick("max_open_positions"),
            "max_open_risk_pct": pick("max_open_risk_pct"),
            "max_new_trades": pick("max_new_trades_per_day"),
            "max_unplanned": pick("max_unplanned_trades_per_day"),
            "near_wall_pct": pick("near_wall_pct"),
            "near_wall_mult": pick("near_wall_mult"),
            "ladder": pick("ladder"),
        }
        source = "limits"
    else:
        missing = [name for name in _LEGACY_KWARGS if legacy[name] is None]
        if missing:
            raise ValueError(
                "не передан ни limits, ни полный набор устаревших kwargs "
                f"(нет: {', '.join(missing)})")
        lim = {
            "daily_limit_pct": legacy["daily_limit_pct"],
            "total_limit_pct": legacy["total_limit_pct"],
            "flatten_buffer_pct": legacy["flatten_buffer_pct"],
            "K": legacy["K"],
            "cap_pct": legacy["cap_pct"],
            "max_open_positions": None,
            "max_open_risk_pct": None,
            "max_new_trades": None,
            "max_unplanned": None,
            # правило near-wall в старой форме заморожено: конфига в ней нет,
            # а менять поведение существующих вызывающих нельзя
            "near_wall_pct": _LEGACY_NEAR_WALL_PCT,
            "near_wall_mult": _LEGACY_NEAR_WALL_MULT,
            "ladder": {},
        }
        source = "legacy_kwargs"

    return lim, source


def _limit_picker(limits):
    """Доступ к лимиту по имени с внятным KeyError — и для dict, и для объекта
    вроде cfg.risk. Голый KeyError от limits[key] не говорит, какой лимит и в
    каком месте отсутствует, а в этом модуле каждая ошибка обязана объяснять
    себя. Индексация прямая (без .get с дефолтом): опечатка в конфиге должна
    падать, а не подменяться значением, ослабляющим лимит.
    """
    if isinstance(limits, Mapping):
        def pick(key):
            try:
                return limits[key]
            except KeyError as e:
                raise KeyError(
                    f"в limits нет лимита {key!r} (есть: {sorted(limits)}) — "
                    "проверь блок risk в конфиге") from e
        return pick

    def pick(key):
        try:
            return getattr(limits, key)
        except AttributeError as e:
            raise KeyError(f"в limits нет лимита {key!r}: {e}") from e
    return pick


def _term_labels(cap_pct) -> dict:
    """Русские подписи термов формулы для reasons. Отдельно от TERM_KEYS:
    подпись можно переформулировать, не ломая аналитику по кодам."""
    return {"budget_div_k": "бюджет/K",
            "per_trade_cap": f"кап {cap_pct}% equity",
            "open_risk_room": "остаток открытого риска",
            "budget_minus_open_risk": "бюджет − открытый риск"}


def _validate_runtime(lim, *, limits_source, loss_streak_mult, profile_mult, open_risk_usd,
                      positions_count, trades_today, unplanned_today, unprotected_positions):
    """Валидации входов И конфига, которые обязаны выполняться ПОСЛЕ стены.

    Бросает ValueError: множители вне [0.0, 1.0] (включая near_wall_mult из
    конфига), отрицательные счётчики и суммы риска, K <= 0, устаревшая форма
    лимитов вместе с входами, которых она не обслуживает. Падение через
    safe_evaluate_gate становится HALT_NEW — вердиктом, который НЕ закрывает
    открытые позиции. Поэтому порядок: сначала FORCE_FLAT (шаг 1), потом
    придирки к аргументам. Структуру ladder проверяет _active_ladder (шаг 6),
    он тоже бросает ValueError.
    """
    _check_mult("loss_streak_mult", loss_streak_mult)
    _check_mult("profile_mult", profile_mult)
    _check_mult("near_wall_mult", lim["near_wall_mult"])

    # Суммы риска и счётчики приходят из кода (exposure.py, счётчики по журналу),
    # а не от модели — та же категория, что множители вне [0.0, 1.0], и та же
    # реакция: fail-loud. Отрицательные значения не «безобидный ноль»: они
    # РАСШИРЯЮТ бюджет (остаток открытого риска, бюджет − открытый риск) и
    # отодвигают лимиты позиций/попыток.
    negative = [f"{name}={value!r}" for name, value in (
        ("open_risk_usd", open_risk_usd), ("positions_count", positions_count),
        ("trades_today", trades_today), ("unplanned_today", unplanned_today),
        ("unprotected_positions", unprotected_positions)) if value < 0]
    if negative:
        raise ValueError(
            f"отрицательные входы гейта: {', '.join(negative)} — суммы риска и "
            "счётчики не бывают отрицательными, а такие значения ослабляют лимиты")

    if lim["K"] <= 0:
        raise ValueError(f"делитель бюджета K должен быть > 0, получено {lim['K']!r}")

    if limits_source == "legacy_kwargs":
        # Устаревшая форма не содержит лимитов, которые обслуживают эти входы:
        # молча проигнорировать их значило бы выдать разрешение с полным риском
        # вообще без новых проверок (см. шапку модуля).
        v2_inputs = {"open_risk_usd": open_risk_usd, "positions_count": positions_count,
                     "trades_today": trades_today, "unplanned_today": unplanned_today}
        passed = [f"{name}={value!r}" for name, value in v2_inputs.items() if value]
        if passed:
            raise ValueError(
                "устаревшая форма лимитов (старые kwargs) не знает лимитов открытого "
                f"риска, позиций, попыток и внеплановых, но эти входы переданы: "
                f"{', '.join(passed)} — они были бы молча проигнорированы; передай "
                "limits (cfg.risk)")


def _check_mult(name, value):
    """Множители — только деэскалация; обоснование fail-loud см. в шапке модуля."""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}={value!r} вне [0.0, 1.0]: множитель риска может только "
                         "снижать риск, но не повышать его выше лимитов")


def _parse_paused_until(value):
    """None / ISO-8601-строка (как её отдаёт streak.py) / datetime → datetime|None.

    Наивное время (без таймзоны) и мусорная строка — ValueError: угадывать
    таймзону паузы нельзя, а safe_evaluate_gate превратит падение в HALT_NEW.
    """
    if value is None:
        return None
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"paused_until={value!r} без таймзоны — нельзя сравнить с now (UTC)")
    return parsed


def _active_ladder(ladder, *, daily_loss_pct, total_loss_pct) -> dict:
    """Сработавшие ступени деэскалации по двум осям убытка.

    Возвращает {'mult': float, 'require_setup_status': str, 'reasons': [...]}.

    Множитель нескольких сработавших ступеней — МИНИМАЛЬНЫЙ, а не произведение:
    risk_mult ступени в конфиге задаёт уровень риска на этой глубине просадки,
    а не добавочный штраф; перемножение ступеней 3.0% и 4.5% (обе ×0.5) дало бы
    ×0.25, чего автор конфига не просил. Минимум = самая строгая из сработавших.
    require_setup_status — 'confirmed', если этого требует ЛЮБАЯ сработавшая
    ступень (строжайшее из требований).

    Бросает ValueError на битом ladder: незнакомая ось, незнакомый ключ
    ступени, отсутствующий loss_pct/risk_mult, risk_mult вне [0.0, 1.0],
    неизвестное значение require_status. Проверка ЭАГЕРНАЯ — по всем ступеням
    обеих осей, а не только по сработавшим (см. _validate_ladder).
    """
    if not ladder:
        return {"mult": 1.0, "require_setup_status": "any", "reasons": []}

    _validate_ladder(ladder)

    mults = []
    require = "any"
    reasons = []
    losses = {"daily": daily_loss_pct, "total": total_loss_pct}

    for axis in _LADDER_AXES:
        loss_pct = losses[axis]
        for rung in ladder[axis]:  # обе оси обязательны: пустой список = ось отключена
            if loss_pct < rung["loss_pct"]:
                continue
            mult = rung["risk_mult"]
            mults.append(mult)
            note = ""
            if rung.get("require_status") == "confirmed":  # ключ осознанно необязателен
                require = "confirmed"
                note = ", требуется статус confirmed"
            reasons.append(f"ступень {axis}: убыток {loss_pct:.2f}% >= {rung['loss_pct']}% "
                           f"→ риск ×{mult}{note}")

    return {"mult": min(mults) if mults else 1.0,
            "require_setup_status": require, "reasons": reasons}


def _validate_ladder(ladder):
    """Структура ВСЕХ ступеней обеих осей — до сравнения с порогами.

    Ленивая валидация (только сработавших ступеней) означала бы, что битый
    конфиг живёт неделями и падает в HALT_NEW ровно в тот момент, когда убыток
    дошёл до этой глубины — худшее время для сюрприза от конфигурации.
    Поэтому здесь проверяется каждая ступень, независимо от текущего убытка.
    """
    unknown_axes = set(ladder) - set(_LADDER_AXES)
    if unknown_axes:
        raise ValueError(f"незнакомые оси ladder: {sorted(unknown_axes)}; "
                         f"допустимы только {list(_LADDER_AXES)}")

    for axis in _LADDER_AXES:
        if axis not in ladder:
            raise ValueError(f"в ladder нет оси {axis!r} (есть: {sorted(ladder)}); обе оси "
                             "обязательны, отключённая ось — это пустой список")
        for index, rung in enumerate(ladder[axis]):
            where = f"ladder.{axis}[{index}]"
            unknown = set(rung) - _RUNG_KEYS
            if unknown:
                raise ValueError(f"незнакомые ключи ступени {where}: {sorted(unknown)}; "
                                 f"допустимы только {sorted(_RUNG_KEYS)}")
            for required in ("loss_pct", "risk_mult"):
                if required not in rung:
                    raise ValueError(f"в ступени {where} нет обязательного ключа {required!r}")
            # risk_mult — тоже множитель риска, и тоже только деэскалация:
            # без этой проверки опечатка в конституции (risk_mult 5.0) поднимала
            # бы выданный риск выше кэпа per_trade_risk_cap_pct
            _check_mult(f"{where}.risk_mult", rung["risk_mult"])
            status = rung.get("require_status")
            if status is not None and status not in _STATUSES:
                raise ValueError(f"{where}: require_status={status!r} не из {list(_STATUSES)}")


# Решающие поля запрещающего ответа — одно определение на _blocked и на
# аварийный ответ safe_evaluate_gate, чтобы схемы не разъехались.
_BLOCKED_DECISION = {
    "max_risk_per_trade_usd": 0.0,
    "require_setup_status": "confirmed",
    "planned_only": True,
    "unplanned_allowed": False,
    "risk_mult_applied": 0.0,
}

# Диагностика, которую при исключении посчитать нечем: ключи присутствуют со
# значением None, чтобы журнал получал полную схему, а .get(key, default) у
# вызывающего не подставил разрешительный дефолт.
_UNKNOWN_DIAGNOSTICS = {key: None for key in
                        ("daily_loss_pct", "total_loss_pct", "daily_budget", "total_budget",
                         "budget", "daily_risk_remaining_usd", "open_risk_usd",
                         "open_risk_room_usd", "limits_source", "binding_term", "terms")}


def _blocked(base, verdict, reasons, *, blocked_by, **extra) -> dict:
    """Запрещающий вердикт: максимально ограничительные поля (см. шапку модуля)."""
    return {**base, "verdict": verdict, **_BLOCKED_DECISION,
            "blocked_by": blocked_by, "reasons": reasons, **extra}


def blocked_response(*, error=None, reason=None, blocked_by="gate_error") -> dict:
    """Готовый запрещающий ответ полной схемы — для вызывающих, которым нужно
    отказать ДО обращения к гейту (нет связи с брокером, устаревший heartbeat,
    несовпадение хэша конфига). Собирать такой словарь руками нельзя: неполный
    ответ ломает журнал (задача 2.1), а вызывающий через
    .get('require_setup_status', 'any') получает разрешительное значение.

    error — текст исключения/причины для машины (ключ error появляется только
    если он передан), reason — человеческая формулировка для журнала.
    """
    text = reason or (f"ошибка гейта: {error} — новых входов нет (fail-closed)"
                      if error else "новых входов нет (fail-closed)")
    out = {**_UNKNOWN_DIAGNOSTICS,
           "verdict": "HALT_NEW",
           **_BLOCKED_DECISION,
           "blocked_by": blocked_by,
           "reasons": [text]}
    if error is not None:
        out["error"] = str(error)
    return out


def safe_evaluate_gate(**kw):
    """Обёртка fail-closed: любая ошибка → HALT_NEW (никогда не fail-open в торговлю).

    Форма ответа — та же, что у запрещающего вердикта (см. шапку модуля), плюс
    ключ error с текстом исключения.
    """
    try:
        return evaluate_gate(**kw)
    except Exception as e:  # noqa: BLE001 - safety-critical fail-closed
        return blocked_response(error=e)
