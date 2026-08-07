"""Отчёт о сеансе работы модели: журнал + лог + телеграм одним вызовом.

ЗАЧЕМ ОДНА ТОЧКА. Модель работает событийно и в чате Claude Code почти не
проявляется. Каждое её действие должно оставить три следа сразу: машинный
(журнал — для статистики), человеческий локальный (лог по времени ПК) и
внешний (телеграм — чтобы владелец счёта видел ход мысли, не открывая терминал).
Если бы каждый вызывающий делал это сам, рано или поздно один из следов
где-нибудь потерялся бы — и заметили бы это через неделю по пустому отчёту.

ПОРЯДОК ВАЖЕН: сначала журнал (это контракт, на нём стоит статистика и сверка
с брокером), потом лог, потом телеграм. Отправка сообщения не имеет права
задержать запись решения, а её отказ — отменить сделку.

ЗАПИСЬ РАССУЖДЕНИЯ — ГЛАВНОЕ ЗДЕСЬ. Требование владельца счёта: видеть, как модель
рассуждает (ждёт отката, ждёт пробоя, почему не входит). Поэтому у наблюдения
текст модели идёт в тело сообщения целиком, а не сворачивается в ярлык.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib import telegram as tg                                # noqa: E402
from trader_lib.config import state_dir                              # noqa: E402
from trader_lib.workspace import trader_state_dir                    # noqa: E402
from trader_lib.eventlog import log                                  # noqa: E402
from trader_lib.journal import append_observation                    # noqa: E402
from trader_lib.model_session import effective as effective_model    # noqa: E402

UTC = dt.timezone.utc


def _sd(cfg, trader=None):
    """Личная папка трейдера: журнал, лог, объявленная модель.

    БАГ, найденный трейдером `range` 2026-08-03 на первом живом дне. Здесь
    стояло `state_dir(cfg)` — ОБЩИЙ корень, без учёта командного режима.
    Наблюдение о пробуждении уходило бы в общий `journal.jsonl`, а
    `review.py --trader range` его бы не увидел: разбудившее условие к вечеру
    выглядело бы НЕОТВЕЧЕННЫМ и попало в мусорные будильники, будучи отвеченным.

    То есть портилась ровно та метрика, ради которой наблюдения и пишутся, —
    и портилась в сторону «алерт бесполезен», то есть подталкивала снять
    работающее условие.

    Тот же класс, что декоративный флаг `--trader` в пяти скриптах: код не
    обновили под командный режим, а тесты этого не поймали, потому что
    проверяли работу в одиночном.
    """
    return Path(trader_state_dir(cfg, trader) if trader else state_dir(cfg))


def _shared(cfg):
    """Общий корень — только для телеграма: канал у команды один, и его
    настройки (`telegram.json`) лежат в общих файлах (см. workspace.SHARED_FILES).
    Класть очередь отправки в личную папку значило бы, что часть сообщений
    команды уходит в никуда при смене трейдера."""
    return Path(state_dir(cfg))


def session_opened(cfg, *, trader=None, equity, day_pnl_pct, wall_left_pct, hypotheses,
                   armed_alerts, night=None, news_count=None, now=None,
                   sender=None):
    """Открытие сеанса: что на счету, с каким планом и сколько будильников."""
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    log(sd, "SESSION", "сеанс открыт", equity=equity,
        hypotheses=len(hypotheses or []), armed=armed_alerts)
    return tg.send(_shared(cfg), "session", tg.session_open(
        now=now, equity=equity, day_pnl_pct=day_pnl_pct,
        wall_left_pct=wall_left_pct, hypotheses=hypotheses or [],
        armed_alerts=armed_alerts, night=night, news_count=news_count),
        now=now, sender=sender)


def observed(cfg, *, trader=None, symbol, alert_id, alert_type, level, price, regime,
             reasoning, equity=None, wall_left_pct=None, positions=None,
             confidence=None, now=None, sender=None):
    """Пробуждение без сделки: рассуждение модели в журнал, лог и телеграм.

    Журнал пишется ПЕРВЫМ и его отказ поднимается наружу: наблюдение без записи
    не участвует в метрике пробуждений, и алерт будет ложно помечен мусорным.
    """
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    model_id, profile = effective_model(sd, cfg)
    rec = {"reasoning": reasoning, "regime": regime, "model_id": model_id,
           "model_profile": profile, "symbol": symbol, "alert_id": alert_id}
    if confidence is not None:
        rec["confidence"] = confidence
    append_observation(sd / "journal.jsonl", rec)

    log(sd, "THINK", f"{symbol}: {reasoning}", alert=alert_id, regime=regime)
    return tg.send(_shared(cfg), "wake", tg.wake(
        now=now, symbol=symbol, alert_type=alert_type, level=level, price=price,
        regime=regime, reasoning=reasoning, equity=equity,
        wall_left_pct=wall_left_pct, positions=positions), now=now, sender=sender)


def entered(cfg, *, trader=None, result, draft, gate_verdict=None, spread=None, news=None,
            now=None, sender=None):
    """Вход исполнен — сообщаем тезис и все числа сделки."""
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    log(sd, "ENTER", f"{draft['symbol']} {draft['side']} {result['lots']}",
        ticket=result["ticket"], risk=result["risk_usd"],
        entry=result.get("fill_price"), sl=draft["sl"])
    return tg.send(_shared(cfg), "enter", tg.entered(
        now=now, symbol=draft["symbol"], side=draft["side"], lots=result["lots"],
        ticket=result["ticket"], thesis=draft["thesis"],
        entry=result.get("fill_price") or draft["entry"], sl=draft["sl"],
        tp=draft.get("tp_plan"), risk_usd=result["risk_usd"], rr=draft["rr"],
        confidence=draft["confidence"], setup_type=draft["setup_type"],
        setup_status=draft["setup_status"], planned=draft["planned"],
        hypothesis_id=draft.get("plan_hypothesis_id"), gate_verdict=gate_verdict,
        spread=spread, news=news), now=now, sender=sender)


def exited(cfg, *, trader=None, result, symbol, reason, entry_price=None, day_trades=None,
           day_r=None, wall_left_pct=None, now=None, sender=None):
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    log(sd, "EXIT", f"{symbol}: {reason}", ticket=result.get("ticket"),
        R=result.get("R"), exit=result.get("exit"))
    return tg.send(_shared(cfg), "exit", tg.exited(
        now=now, symbol=symbol, ticket=result.get("ticket"),
        r_multiple=result.get("R"), profit=result.get("profit"), reason=reason,
        exit_price=result.get("exit"), entry_price=entry_price,
        day_trades=day_trades, day_r=day_r, wall_left_pct=wall_left_pct),
        now=now, sender=sender)


def director(cfg, *, title, body=None, facts=None, decision=None, now=None,
             sender=None):
    """Рассуждение директора в канал и в лог событий.

    Всегда пишет в ОБЩИЙ корень: директор не трейдер, личной папки у него нет,
    и его рассуждение адресовано человеку, а не статистике сетапов. В журнал
    сделок оно намеренно НЕ попадает — там живут только решения по деньгам, и
    разбавлять их комментариями значило бы испортить метрики пробуждений.
    """
    now = now or dt.datetime.now(UTC)
    sd = _shared(cfg)
    log(sd, "DIRECTOR", title, decision=decision or "")
    return tg.send(sd, "director", tg.director(
        now=now, title=title, body=body, facts=facts, decision=decision),
        now=now, sender=sender)


def critical(cfg, *, trader=None, title, details, action=None, now=None, sender=None):
    """Стоп-кран, чужая позиция, потеря датчика — единственное, что должно
    будить телефон."""
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    log(sd, "VALVE", title, **{"деталей": len(details)})
    return tg.send(_shared(cfg), "critical", tg.critical(
        now=now, title=title, details=details, action=action), now=now,
        sender=sender)


def session_closed(cfg, *, trader=None, review, duration, progress_pct=None, now=None,
                   sender=None):
    """Закрытие сеанса: итог дня из готового разбора (scripts/review.py)."""
    now = now or dt.datetime.now(UTC)
    sd = _sd(cfg, trader)
    t = review["trades"]
    a = review["alert_efficiency"]
    useful = a["with_decision"] + a["with_skip"] + a.get("with_observation", 0)
    noisy = [f"{x['alert_id']} ×{x['count']}" for x in a["noisy_alerts"][:3]]
    log(sd, "SESSION", "сеанс закрыт", trades=t["closed"], R=t["sum_R"],
        wakes=a["delivered"])
    return tg.send(_shared(cfg), "review", tg.session_close(
        now=now, duration=duration, trades=t["closed"], sum_r=t["sum_R"] or 0.0,
        pnl_usd=t["pnl_usd"] or 0.0, wr=t["wr"], wakes=a["delivered"],
        useful=useful, noisy=noisy, progress_pct=progress_pct,
        plan_traded=review["plan_vs_fact"]["hypotheses_traded"],
        plan_total=review["plan_vs_fact"]["hypotheses_total"]), now=now,
        sender=sender)
