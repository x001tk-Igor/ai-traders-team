"""Атомарный вход (задача 4.2): от намерения модели до позиции с записью.

ПОРЯДОК ЖЁСТКИЙ, И КАЖДЫЙ ШАГ УМЕЕТ ОСТАНОВИТЬ ВХОД:
  1. entry_gate (задача 5.4) — сессия, спред, новости, риск-гейт, экспозиция;
  2. качество — издержки в долях R и точка безубытка против частоты из журнала;
  3. профиль модели — слабая модель торгует только по плану и подтверждённым;
  4. размер — из выданного риска, множителей статуса и профиля; 0 → входа нет;
  5. валидация записи — ДО отправки: неполный след означает «не входим»;
  6. намерение в журнал;
  7. ордер с гарантией стопа (trader_lib/execute.py);
  8. ордер не прошёл → намерение помечается несостоявшимся;
  9. успех → решение в журнал + перестановка алертов на ведение позиции.

ДВУХФАЗНАЯ ЗАПИСЬ — НЕ УКРАШЕНИЕ, А СЛЕДСТВИЕ ДВУХ ТРЕБОВАНИЙ СРАЗУ.
`trade_id` обязан быть тикетом брокера: на этом стоят обе стороны сверки —
дописывание исходов (reconcile сопоставляет с position_id из истории) и детект
чужих позиций (find_orphans — с ticket открытой позиции). Но тикет известен
только ПОСЛЕ order_send, а следа в журнале нельзя не оставить ДО отправки.
Маркеры в ордере (magic/comment) для связи запрещены — сделка должна выглядеть
ручной (решение владельца счёта по зонду 3.4). Отсюда: intent до отправки, decision с
тикетом после филла.

Что это даёт на каждом стыке сбоя:
  - упало между намерением и отправкой → намерение есть, позиции нет;
  - упало между филлом и решением → намерение есть, позиция есть, и решение
    достраивается из намерения и тикета (journal.decision_from_intent);
  - позиция без намерения и без решения → это НЕ наша сделка (ручная или
    второй агент) → HALT_NEW, не трогать.

ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ. Округления лота «для красоты» и дистанции стопа по
умолчанию: одинаково круглые лоты во всех сделках — такой же след советника,
как magic, а исправить это после накопления истории будет нельзя. Лот считает
size_position.compute_lots (floor к шагу), дистанция стопа приходит от модели
по структуре рынка.
"""
import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.alerts import load_alerts, write_alerts_atomic          # noqa: E402
from trader_lib.config import load_config, state_dir                    # noqa: E402
from trader_lib.execute import place_order                              # noqa: E402
from trader_lib.model_session import effective as effective_model
from trader_lib.workspace import resolve_trader, workspace_path       # noqa: E402
from trader_lib.journal import (                                        # noqa: E402
    append_decision,
    append_intent,
    append_outcome,
    append_skip,
    decision_from_intent,
    read_records,
    validate_intent,
)
from trader_lib.quality import (                                        # noqa: E402
    breakeven_p,
    costs_R,
    profile_risk_mult,
    status_risk_mult,
)
from trader_lib.score import compute_stats                              # noqa: E402
from trader_lib.size_position import compute_lots                       # noqa: E402

UTC = dt.timezone.utc

# оценка проскальзывания для расчёта издержек до входа: фактическое известно
# только после филла и записывается в исход сделки
SLIPPAGE_POINTS_EST = 5.0
COMMISSION_USD = 0.0        # у этого брокера комиссия в спреде

# Алерты ведения позиции, которые ставятся сразу после входа. Модель может
# добавить свои, но эти три — минимум, без которого позиция остаётся без
# наблюдения до следующего пробуждения.
# горизонт по умолчанию, если гипотеза плана его не задала
DEFAULT_HORIZON_MINUTES = 90

# Что обязана сказать модель, и сказать содержательно. journal.validate_intent
# проверяет ПРИСУТСТВИЕ ключа (дисциплина записи), а здесь проверяется, что
# значение не пустое: черновик с thesis=None прошёл бы валидацию журнала и
# оставил бы в памяти запись без тезиса — то есть бесполезную ровно там, где
# журнал единственный источник обучения.
DRAFT_REQUIRED = ("symbol", "side", "entry", "sl", "tp_plan", "rr", "tactic",
                  "setup_type", "setup_status", "regime", "thesis", "confidence",
                  "technical_trigger", "planned")


def tp_levels(tp_plan):
    """Числовые уровни из плана целей, в порядке их записи.

    План пишет модель, и она пишет его по-разному: словарями с долей объёма
    ({"level": X, "fraction": 0.5}) или просто числами ([4051.0, 4043.0]) —
    оба варианта естественны и оба встречались в живых черновиках.

    РЕГРЕСС 2026-07-31 (сделка 2287196528, цена ~1R): читались ТОЛЬКО словари.
    Список чисел отсеивался в пустой, broker_tp возвращал None, и ордер уходил
    брокеру вовсе без цели — при том что в журнале план целей записан как
    заданный. Отказ был тихий: ни исключения, ни предупреждения. Разбор
    формата — одно место на всех, чтобы расхождение не смогло возникнуть снова.
    """
    if tp_plan is None or isinstance(tp_plan, (int, float)):
        return []
    levels = []
    for t in tp_plan:
        if isinstance(t, dict):
            value = t.get("level")
        elif isinstance(t, (int, float)):
            value = t
        else:
            continue
        if value:
            levels.append(float(value))
    return levels


def broker_tp(tp_plan, side):
    """Какой тейк-профит уходит БРОКЕРУ из плана частичных целей.

    `tp_plan` — список уровней с долями объёма: TP1 половину, TP2 остаток.
    Брокер таких планов не понимает — у ордера один tp, и он закрывает ВЕСЬ
    объём. Поэтому брокеру отдаётся САМАЯ ДАЛЬНЯЯ цель, а промежуточные берёт
    модель через exit.py.

    Ставить брокеру TP1 было бы хуже всего: позиция закрылась бы целиком там,
    где по плану выходит половина, и вторая половина плана не существовала бы
    никогда — при этом в журнале план остался бы записан как выполненный.
    Дальняя цель работает как страховка на случай, когда модель не проснулась:
    стоп держит убыток, дальний TP забирает прибыль.

    Найдено на живом входе 2026-07-27: сюда передавался сам список, и
    place_order падал на float(list). --dry-run этого не видел, потому что
    ордер в нём не отправляется вовсе.
    """
    if tp_plan is None or isinstance(tp_plan, (int, float)):
        return tp_plan
    levels = tp_levels(tp_plan)
    if not levels:
        return None
    # «дальняя» зависит от направления: для лонга это максимум, для шорта —
    # минимум. Одинаковый max() отдал бы шорту ближайшую цель, то есть закрыл
    # бы весь объём там, где по плану выходит половина
    return max(levels) if side == "buy" else min(levels)


def _stop(stage, message, **extra):
    out = {"ok": False, "stage": stage, "message": message}
    out.update(extra)
    return out


def _resolve_gate(gate_fn):
    """Гейт входа приходит извне; по умолчанию — из задачи 5.4.

    Пока его нет, вход не работает вовсе: «пока поторгуем без гейта» — это и
    есть второй путь мимо правил, а он всегда находится. Тот же приём, что у
    alert_watch.live_executor с исполнителем.
    """
    if gate_fn is not None:
        return gate_fn, None
    try:
        from trader_lib.entry_gate import check_entry
    except ImportError as e:
        return None, ("предвходовой гейт (trader_lib/entry_gate.py, задача 5.4) "
                      f"недоступен: {e}. Вход без него запрещён — это был бы "
                      "второй путь мимо сессии, спреда, новостей и риск-гейта")
    return check_entry, None


def _p_win_journal(records, setup_type, *, cfg):
    """Частота выигрыша этого сетапа ПО ЖУРНАЛУ, а не оценка модели.

    None, если выборки не хватает (learning.min_n_for_confirmed): «данных нет»
    и «данные плохие» — разные ответы, и подменять первое вторым нельзя.
    """
    if not records:
        return None
    stats = compute_stats(records, min_n_for_confirmed=cfg.learning.min_n_for_confirmed)
    seg = stats["by_setup"].get(setup_type)
    if not seg or seg.get("insufficient") or seg.get("wr") is None:
        return None
    return seg["wr"]


def _near_tp_alerts(*, ticket, symbol, side, tp_plan):
    """Будильники на ПРОМЕЖУТОЧНЫЕ цели плана.

    Брокеру уходит только дальняя цель (см. broker_tp) — она страхует, когда
    модель не проснулась. Ближние уровни существуют лишь как замысел модели, и
    без будильника этот замысел не исполняется никем: событийная работа как раз
    и означает, что на экран никто не смотрит.

    РЕГРЕСС 2026-07-31 (сделка 2287196528): цена прошла TP1=4051 насквозь
    (лоу 4050.11, MFE 0.94R) и вернулась к безубытку. Половина позиции по 1R
    была в замысле, но не в будильнике. Пробел замечен вручную через полтора
    часа — когда уровень уже отработал и ушёл.
    """
    levels = tp_levels(tp_plan)
    if len(levels) < 2:
        return []
    far = max(levels) if side == "buy" else min(levels)
    near = [x for x in levels if x != far]
    # цель лонга выше входа, цель шорта ниже: сторона срабатывания зеркальна
    kind = "price_above" if side == "buy" else "price_below"
    return [
        {"id": f"pos-{ticket}-tp{i + 1}", "type": kind, "symbol": symbol,
         "level": level, "ticket": ticket, "priority": "normal", "once": True,
         "note": f"цель {i + 1} плана ({level}) — зафиксировать долю объёма и "
                 "решить по остатку: безубыток, ведение или выход"}
        for i, level in enumerate(sorted(near, reverse=(side == "buy")))
    ]


def _position_alerts(*, ticket, symbol, side, entry, sl, now, model_id,
                     tp_plan=None,
                     horizon_minutes=DEFAULT_HORIZON_MINUTES):
    """Минимальный набор наблюдения за открытой позицией.

    ТИПЫ БЕРУТСЯ ИЗ КОНТРАКТА alerts.py (ALERT_TYPE_FIELDS), а не придумываются
    здесь. Выдуманный тип датчик молча пропускает как нераспознанный — позиция
    осталась бы вообще без наблюдения, и это не было бы видно нигде: алерты в
    файле есть, а будильник не звонит. Обязательные поля типа тоже из контракта:
    position_R_reaches — (ticket, level), position_time_elapsed — (ticket,
    minutes, min_progress_R).
    """
    # РЕГРЕСС 2026-07-29: ни один из трёх не имел once=true. Диспетчер
    # (trader_lib/alerts.py) разоружает условие ТОЛЬКО если once или
    # rearm_after_minutes заданы — без этого "позиция прошла 1R" срабатывала
    # заново на каждом тике, пока R остаётся >=1.0, и тихо жгла дневной
    # бюджет событий на первой же реальной сделке. Каждое из трёх условий —
    # разовое решение (зафиксировать/подождать/среагировать на инвалидацию),
    # а не повторяющееся напоминание.
    return [
        {"id": f"pos-{ticket}-1r", "type": "position_R_reaches", "ticket": ticket,
         "symbol": symbol, "level": 1.0, "priority": "normal", "once": True,
         "note": "позиция прошла 1R — решить: частичка, безубыток, ведение"},
        {"id": f"pos-{ticket}-stall", "type": "position_time_elapsed", "ticket": ticket,
         "symbol": symbol, "minutes": horizon_minutes, "min_progress_R": 0.5,
         "priority": "normal", "once": True,
         "note": "горизонт исчерпан, а позиция не прошла и половины R — "
                 "тезис не отработал"},
        {"id": f"pos-{ticket}-invalidation", "type": "price_below" if side == "buy"
         else "price_above", "symbol": symbol, "level": sl,
         "priority": "critical", "ticket": ticket, "once": True,
         "note": "цена у уровня инвалидации тезиса"},
    ] + _near_tp_alerts(ticket=ticket, symbol=symbol, side=side, tp_plan=tp_plan)


def _update_alerts(alerts_path, *, fired_alert_id, position_alerts, now, model_id):
    """Снимает сработавший триггер и ставит алерты ведения.

    Читает существующий файл через load_alerts (отсутствие файла — не ошибка,
    модель могла ещё ничего не ставить) и пишет атомарно: датчик читает его
    раз в секунду и не должен увидеть половину.
    """
    doc = load_alerts(alerts_path, now=now) or {}
    alerts = [a for a in (doc.get("alerts") or [])
              if not (fired_alert_id and a.get("id") == fired_alert_id)]
    alerts += position_alerts
    out = {"version": 1, "written_by": model_id, "written_utc": now.isoformat(),
           "expires_utc": doc.get("expires_utc"), "alerts": alerts}
    write_alerts_atomic(alerts_path, out)
    return out


def enter(market, cfg, draft, *, journal_path, alerts_path=None, now=None,
          gate_fn=None, dry_run=False, trader=None):
    """Один вход по намерению модели. draft — то, что может сказать только
    модель (символ, сторона, уровни, тезис, уверенность, статус сетапа);
    механическое (риск, издержки, частота из журнала, вердикт гейта, фаза
    сессии) считается здесь.

    → {ok, stage, ticket, lots, risk_usd, intent_id, message, ...}
    """
    now = now or dt.datetime.now(UTC)
    journal_path = Path(journal_path)
    alerts_path = Path(alerts_path) if alerts_path else journal_path.parent / "alerts.json"
    # Подпись записи — модель, объявившая себя В ЭТОМ СЕАНСЕ, а не строка из
    # конституции: её человек правит руками и забывает при переносе на другой
    # ПК, а по model_id считаются калибровка и by_model (задача 2026-07-27).
    model_id, profile = effective_model(journal_path.parent, cfg)

    empty = [f for f in DRAFT_REQUIRED if draft.get(f) is None]
    if empty:
        return _stop("journal_validation",
                     "черновик неполон — вход отменён до отправки ордера",
                     problems=[f"{f}: пусто" for f in empty])

    symbol, side = draft["symbol"], draft["side"]
    entry, sl, rr = draft["entry"], draft["sl"], draft["rr"]

    # --- 0. порог R:R, объявленный САМОЙ моделью ---
    # Число выбирает модель (это тактика, код её не назначает), но объявив его,
    # она обязана ему подчиниться — иначе порог остаётся благим намерением.
    #
    # РЕГРЕСС 2026-07-31 (сделка 2288394009, −0.222R): между расчётом входа и
    # отправкой ордера цена ушла, риск вырос 6.88 → 8.93, R:R упал 1.62 → 1.00.
    # Скрипт пересчитал rr на живой цене и отправил ордер безусловно — при том
    # что часом ранее модель отказалась от входа при R:R 0.97 как от не
    # проходящего тот же порог 1.5. Экономика сделки успевает испортиться за
    # секунды, и проверять её надо в момент отправки, а не в момент замысла.
    min_rr = draft.get("min_rr")
    if min_rr is not None and rr is not None and rr < min_rr:
        reason = (f"R:R {rr} ниже объявленного порога {min_rr} — экономика сделки "
                  "испортилась между замыслом и отправкой")
        _skip(journal_path, draft, reason=reason, cfg=cfg, dry_run=dry_run)
        return _stop("min_rr", reason, rr=rr, min_rr=min_rr)

    # --- 1. предвходовой гейт ---
    gate, unavailable = _resolve_gate(gate_fn)
    if unavailable:
        return _stop("gate_unavailable", unavailable)

    records = read_records(journal_path)
    p_win = _p_win_journal(records, draft["setup_type"], cfg=cfg)

    # ЧЕЙ это вход — знает только enter.py. Без передачи автора гейт видит
    # None, трактует как одиночный режим и пропускает вход по ЛЮБОМУ
    # инструменту: мандаты директора, кластерный потолок и квоты остались бы
    # существующими, но ни на что не влияющими.
    gate_res = gate(market=market, cfg=cfg, symbol=symbol, side=side, entry=entry,
                    sl=sl, rr=rr, setup_status=draft["setup_status"],
                    p_win_journal=p_win, planned=draft["planned"], state=None,
                    trader=trader)
    if not gate_res.get("allow"):
        reasons = "; ".join(gate_res.get("reasons") or ["гейт запретил вход"])
        _skip(journal_path, draft, reason=f"гейт: {reasons}", cfg=cfg, dry_run=dry_run)
        return _stop("gate", reasons, gate=gate_res)

    si = market.symbol_info(symbol)
    point = si["point"]
    sl_points = abs(entry - sl) / point
    if sl_points <= 0:
        return _stop("size", "дистанция стопа нулевая")

    # --- 3. профиль модели (до расчёта размера: он меняет и риск, и допуски) ---
    prof = profile_risk_mult(profile, cfg.model)
    rules = cfg.model.profile_rules.get(profile, {})
    if rules.get("planned_only") and not draft["planned"]:
        _skip(journal_path, draft, reason=f"профиль {profile}: внеплановые входы запрещены",
              cfg=cfg, dry_run=dry_run)
        return _stop("profile", f"профиль {profile} торгует только по плану дня")
    if rules.get("require_status") == "confirmed" and draft["setup_status"] != "подтверждён":
        _skip(journal_path, draft,
              reason=f"профиль {profile}: только подтверждённые сетапы",
              cfg=cfg, dry_run=dry_run)
        return _stop("profile", f"профиль {profile} требует подтверждённый сетап, "
                                f"получен {draft['setup_status']!r}")

    stat = status_risk_mult(draft["setup_status"], cfg.risk.status_risk_mult)
    # gate_res.max_risk_usd уже учитывает лимиты гейта и профиль модели, если
    # 5.4 передала его в risk_gate; статус сетапа применяется здесь и только
    # здесь — иначе множитель применился бы дважды
    risk_usd = float(gate_res["max_risk_usd"]) * stat["mult"] * prof["mult"]

    # --- 4. размер ---
    lots = compute_lots(risk_usd=risk_usd, entry=entry, sl=sl, symbol_info=si)
    if lots <= 0:
        return _stop("size", f"бюджет риска {risk_usd:.2f}$ не покрывает минимальный лот",
                     risk_usd=risk_usd, lots=0.0)

    # --- 2. качество: издержки и точка безубытка ---
    value_per_point = si["trade_contract_size"] * point
    spread_points = gate_res.get("spread_at_entry")
    if spread_points is None:
        spread_points = si.get("spread", 0.0)
    c_r = costs_R(spread_points=spread_points, commission_usd=COMMISSION_USD,
                  slippage_points_est=SLIPPAGE_POINTS_EST, sl_points=sl_points,
                  lots=lots, value_per_point=value_per_point)
    if c_r > cfg.risk.max_costs_R:
        _skip(journal_path, draft,
              reason=f"издержки costs_R={c_r:.3f} выше предела {cfg.risk.max_costs_R}",
              cfg=cfg, dry_run=dry_run)
        return _stop("quality", f"costs_R={c_r:.3f} выше предела {cfg.risk.max_costs_R}",
                     costs_R=c_r)
    be_p = breakeven_p(rr=rr, costs_r=c_r)
    if p_win is not None and p_win < be_p:
        _skip(journal_path, draft,
              reason=f"p_win по журналу {p_win} ниже точки безубытка {be_p:.3f}",
              cfg=cfg, dry_run=dry_run)
        return _stop("quality",
                     f"p_win по журналу {p_win} ниже точки безубытка {be_p:.3f}",
                     p_win_journal=p_win, breakeven_p=be_p)

    # --- 5. запись собирается и проверяется ДО отправки ---
    intent_id = f"int-{uuid.uuid4().hex[:12]}"
    intent = {
        "intent_id": intent_id,
        "symbol": symbol, "side": side, "regime": draft["regime"],
        "tactic": draft["tactic"], "setup_type": draft["setup_type"],
        "setup_status": draft["setup_status"], "thesis": draft["thesis"],
        "confidence": draft["confidence"],
        "technical_trigger": draft["technical_trigger"],
        "entry": entry, "sl": sl, "tp_plan": draft["tp_plan"],
        "risk_usd": round(risk_usd, 2), "rr": rr,
        "costs_R": round(c_r, 4), "breakeven_p": round(be_p, 4),
        "p_win_journal": p_win,
        "news_check": gate_res.get("news_check"),
        "spread_at_entry": spread_points,
        "correlation_check": gate_res.get("correlation_check"),
        "daily_risk_remaining_usd": gate_res.get("daily_risk_remaining_usd"),
        "planned": draft["planned"], "plan_hypothesis_id": draft.get("plan_hypothesis_id"),
        "gate_verdict": gate_res.get("verdict"),
        "session_phase": gate_res.get("session_phase"),
        "model_id": model_id, "model_profile": profile,
        "lots": lots,
    }
    for optional in ("daily_bias", "macro_note", "unplanned_reason", "alert_id",
                     "smoke"):
        if draft.get(optional) is not None:
            intent[optional] = draft[optional]

    problems = validate_intent(intent)
    if problems:
        return _stop("journal_validation",
                     "запись неполна — вход отменён до отправки ордера",
                     problems=problems)

    if dry_run:
        return {"ok": True, "dry_run": True, "stage": "dry_run", "lots": lots,
                "risk_usd": round(risk_usd, 2), "costs_R": round(c_r, 4),
                "breakeven_p": round(be_p, 4), "p_win_journal": p_win,
                "intent": intent, "ticket": None, "message": "расчёт без отправки"}

    # --- 6. намерение на диск ---
    append_intent(journal_path, intent)

    # --- 7. ордер ---
    res = place_order(market, symbol=symbol, side=side, lots=lots, entry=entry, sl=sl,
                      tp=broker_tp(draft.get("tp_plan"), side))
    if not res.get("ok"):
        # --- 8. намерение не состоялось: помечаем, иначе оно вечно выглядит
        # незакрытой сделкой при сверке
        append_outcome(journal_path, {"trade_id": intent_id, "exit": None, "profit": 0.0,
                                      "R": 0, "exit_reason": "aborted",
                                      "note": res.get("message"),
                                      "error": res.get("error")})
        return _stop("order", res.get("message") or "ордер не прошёл",
                     intent_id=intent_id, order=res)

    # --- 9. решение с тикетом брокера + алерты ведения ---
    ticket = res["ticket"]
    decision = decision_from_intent(intent, ticket=ticket)
    decision["fill_price"] = res.get("fill_price")
    decision["slippage_points"] = res.get("slippage_points")
    append_decision(journal_path, decision)

    # --- 10. рассказать человеку: лог по времени ПК + телеграм ---
    # Отдельным шагом ПОСЛЕ записи решения и в своём try: отказ мессенджера не
    # имеет права отменить или задержать уже исполненную сделку. Без этого
    # вызова канал существует, но молчит — модуль есть, а событий нет.
    try:
        from scripts.report import entered as _report_entered

        _report_entered(cfg, trader=trader, result={"ticket": ticket, "lots": lots,
                                     "risk_usd": round(risk_usd, 2),
                                     "fill_price": res.get("fill_price")},
                        draft=draft, gate_verdict=gate_res.get("verdict"),
                        spread=spread_points, news=gate_res.get("news_check"),
                        now=now)
    except Exception as e:  # noqa: BLE001
        print(f"[enter] отчёт о входе не отправлен: {e!r}", file=sys.stderr)

    _update_alerts(alerts_path, fired_alert_id=draft.get("alert_id"),
                   position_alerts=_position_alerts(
                       ticket=ticket, symbol=symbol, side=side, entry=entry, sl=sl,
                       now=now, model_id=model_id, tp_plan=draft.get("tp_plan"),
                       horizon_minutes=draft.get("horizon_minutes")
                       or DEFAULT_HORIZON_MINUTES),
                   now=now, model_id=model_id)

    return {"ok": True, "stage": "done", "ticket": ticket, "lots": lots,
            "risk_usd": round(risk_usd, 2), "intent_id": intent_id,
            "fill_price": res.get("fill_price"),
            "slippage_points": res.get("slippage_points"),
            "sl_verified": res.get("sl_verified"), "costs_R": round(c_r, 4),
            "breakeven_p": round(be_p, 4), "p_win_journal": p_win,
            "message": "вход выполнен"}


def _skip(journal_path, draft, *, reason, cfg, dry_run):
    """Осознанный отказ от названного сетапа — тоже память: без skip-записей
    журнал знает только про входы, и статистика сетапа перекошена в их пользу.
    В dry-run не пишем: это расчёт, а не решение."""
    if dry_run:
        return None
    model_id, profile = effective_model(Path(journal_path).parent, cfg)
    rec = {"setup_type": draft["setup_type"], "reason": reason,
           "confidence": draft["confidence"], "regime": draft["regime"],
           "model_id": model_id, "model_profile": profile,
           "symbol": draft.get("symbol"), "side": draft.get("side")}
    # связь с разбудившим алертом: без неё дневной разбор (scripts/review.py)
    # считает такое пробуждение ПУСТЫМ, хотя модель ответила на него осознанным
    # отказом — и «мусорными» помечались бы полезные условия
    if draft.get("alert_id"):
        rec["alert_id"] = draft["alert_id"]
    return append_skip(journal_path, rec)


def main(argv=None):
    ap = argparse.ArgumentParser(description="атомарный вход по намерению модели")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    ap.add_argument("--draft", required=True,
                    help="путь к JSON с намерением модели или сам JSON строкой")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trader", default=None,
                    help="кто входит: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    raw = Path(a.draft)
    draft = json.loads(raw.read_text(encoding="utf-8") if raw.exists() else a.draft)

    from trader_lib.mt5_client import live_market
    trader = resolve_trader(a.trader)
    res = enter(live_market(), cfg, draft,
                journal_path=workspace_path(cfg, "journal.jsonl", trader=trader,
                                            create=True),
                alerts_path=workspace_path(cfg, "alerts.json", trader=trader,
                                           create=True),
                dry_run=a.dry_run, trader=trader)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
