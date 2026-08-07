"""Отправка сеансов работы модели в Telegram (задача владельца счёта, 2026-07-27).

ЗАЧЕМ. Модель работает событийно и в чат Claude Code почти не проявляется: она
просыпается, смотрит, решает и снова молчит. владелец счёта при этом хочет видеть, КАК
ОНА РАССУЖДАЕТ — ждёт отката, ждёт пробоя, почему не входит. Телеграм и есть
этот канал: не отчёт постфактум, а поток мыслей по ходу дня.

ТРИ ПРАВИЛА, ИЗ КОТОРЫХ ВЫВЕДЕНО ОСТАЛЬНОЕ.

1. ОТПРАВКА НИКОГДА НЕ БЛОКИРУЕТ ТОРГОВЛЮ. Телеграм недоступен, токен отозван,
   сеть легла — это не повод отменить вход или задержать закрытие позиции.
   Любая ошибка глотается, сообщение уходит в очередь (outbox) и досылается при
   следующей попытке.

2. ТЕКСТ МОДЕЛИ ЭКРАНИРУЕТСЯ ВСЕГДА. parse_mode=HTML ломается на первой же
   угловой скобке: «цена < 4090» в тезисе — обычное дело, а Telegram отвечает
   400 «Unsupported start tag» и сообщение не доходит. Проверено вживую
   2026-07-27. Поэтому весь текст от модели проходит через escape, а теги
   ставит только этот модуль.

3. ГРОМКОСТЬ НАСТРАИВАЕТСЯ БЕЗ ПРАВКИ КОДА. До 40 пробуждений в день — это до
   40 сообщений; если станет много, любой тип отключается флагом в
   telegram.json. Иначе канал разделит судьбу всех уведомлений, которые
   перестают читать.

ДОСТУПЫ ЖИВУТ ВНЕ РЕПОЗИТОРИЯ — в state_dir/telegram.json. В git этот файл не
попадает никогда.
"""
import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/sendMessage"
CONFIG_FILE = "telegram.json"
OUTBOX_FILE = "telegram_outbox.jsonl"
TIMEOUT_S = 15
MAX_OUTBOX = 200

# Типы сообщений. Каждый включается/выключается отдельно в telegram.json.
KINDS = ("session", "wake", "enter", "exit", "critical", "review", "director")


def escape(text):
    """HTML-экранирование текста модели. Порядок важен: & первым, иначе
    экранированные скобки испортятся повторно."""
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def load_settings(sd):
    """Настройки или None, если канал не настроен. Битый файл = не настроен:
    лучше молчать, чем падать при каждом событии."""
    p = Path(sd) / CONFIG_FILE
    if not p.exists():
        return None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not cfg.get("token") or not cfg.get("chat_id"):
        return None
    return cfg


def _http_send(token, chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode("utf-8")
    with urllib.request.urlopen(API.format(token=token), data=data,
                                timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def _queue(sd, text, kind, *, now, reason):
    """Недоставленное — в очередь. При переполнении выбрасывается САМОЕ СТАРОЕ:
    свежее рассуждение важнее вчерашнего."""
    p = Path(sd) / OUTBOX_FILE
    try:
        rows = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] \
            if p.exists() else []
        rows.append(json.dumps({"queued_utc": now.isoformat(), "kind": kind,
                                "text": text, "reason": reason},
                               ensure_ascii=False))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(rows[-MAX_OUTBOX:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _read_outbox(sd):
    p = Path(sd) / OUTBOX_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _write_outbox(sd, rows):
    p = Path(sd) / OUTBOX_FILE
    try:
        if rows:
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                         + "\n", encoding="utf-8")
        elif p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


def send(sd, kind, text, *, now=None, sender=None, settings=None):
    """Отправляет сообщение и досылает всё, что застряло раньше.

    Никогда не бросает. Возвращает {'sent': bool, 'reason': str, 'queued': int}.
    sender инжектируется в тестах — сеть в них не нужна.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cfg = settings if settings is not None else load_settings(sd)
    if not cfg or not cfg.get("enabled", True):
        return {"sent": False, "reason": "канал не настроен или выключен", "queued": 0}
    if kind in KINDS and not (cfg.get("send") or {}).get(kind, True):
        return {"sent": False, "reason": f"тип {kind} отключён настройками", "queued": 0}

    post = sender or (lambda t: _http_send(cfg["token"], cfg["chat_id"], t))

    # сначала долги: порядок сообщений для человека имеет значение
    pending = _read_outbox(sd)
    left = []
    for i, row in enumerate(pending):
        try:
            post(row["text"])
        except Exception:  # noqa: BLE001 - не дошло: остальные тоже не пойдут
            left = pending[i:]
            break
    if left:
        _write_outbox(sd, left)
        _queue(sd, text, kind, now=now, reason="очередь не разобрана")
        return {"sent": False, "reason": "телеграм недоступен", "queued": len(left) + 1}
    _write_outbox(sd, [])

    try:
        post(text)
    except Exception as e:  # noqa: BLE001 - отправка не имеет права ломать торговлю
        _queue(sd, text, kind, now=now, reason=repr(e))
        return {"sent": False, "reason": f"не доставлено: {e}", "queued": 1}
    return {"sent": True, "reason": "доставлено", "queued": 0}


# --------------------------------------------------------------------------
# формат сообщений
# --------------------------------------------------------------------------

def _money(value):
    return "—" if value is None else f"{value:,.0f}$".replace(",", " ")


def _pct(value, digits=2):
    return "—" if value is None else f"{value:.{digits}f}%"


def _hhmm(now):
    return f"{now:%H:%M:%S}"


def session_open(*, now, equity, day_pnl_pct, wall_left_pct, hypotheses,
                 armed_alerts, night=None, news_count=None):
    lines = [f"<b>▶️ Сессия открыта</b> · {_hhmm(now)}",
             f"Счёт {_money(equity)} · день {_pct(day_pnl_pct)} · "
             f"до стены {_pct(wall_left_pct)}", ""]
    if hypotheses:
        lines.append(f"План: {len(hypotheses)} гипотез — "
                     + escape(", ".join(hypotheses)))
    lines.append(f"Датчик вооружён: {armed_alerts} условий")
    if night:
        lines.append(escape(night))
    if news_count is not None:
        lines.append(f"Новостей за сутки: {news_count}")
    return "\n".join(lines)


def wake(*, now, symbol, alert_type, level, price, regime, reasoning,
         equity=None, wall_left_pct=None, positions=None):
    """Пробуждение без сделки — то, ради чего канал и заводился: видно, о чём
    модель думала, а не только что сделала."""
    lines = [f"<b>🔔 Пробуждение</b> · {_hhmm(now)} · {escape(symbol or '—')}",
             f"Алерт: {escape(alert_type)}"
             + (f" {level}" if level is not None else "")
             + (f" (цена {price})" if price is not None else ""), ""]
    if regime:
        lines.append(f"Режим: {escape(regime)}")
    lines.append(f"<b>Рассуждение:</b> {escape(reasoning)}")
    tail = []
    if equity is not None:
        tail.append(f"Счёт {_money(equity)}")
    if wall_left_pct is not None:
        tail.append(f"до стены {_pct(wall_left_pct)}")
    if positions is not None:
        tail.append(f"позиций {positions}")
    if tail:
        lines += ["", " · ".join(tail)]
    return "\n".join(lines)


def entered(*, now, symbol, side, lots, ticket, thesis, entry, sl, tp, risk_usd,
            rr, confidence, setup_type, setup_status, planned, hypothesis_id=None,
            gate_verdict=None, spread=None, news=None):
    plan = (f"плановый {hypothesis_id}" if planned and hypothesis_id
            else ("плановый" if planned else "ВНЕПЛАНОВЫЙ"))
    lines = [
        f"<b>📈 ВХОД</b> · {escape(symbol)} {escape(side)} {lots} · тикет {ticket}",
        _hhmm(now), "",
        f"<b>Тезис:</b> {escape(thesis)}",
        f"Вход {entry} · стоп {sl}" + (f" · цель {tp}" if tp else ""),
        f"Риск {risk_usd}$ · R:R {rr} · уверенность {confidence}",
        f"Сетап: {escape(setup_type)} ({escape(setup_status)}) · {plan}",
    ]
    extra = [x for x in (f"гейт {gate_verdict}" if gate_verdict else None,
                         f"спред {spread}" if spread is not None else None,
                         escape(news) if news else None) if x]
    if extra:
        lines.append(" · ".join(extra))
    return "\n".join(lines)


def exited(*, now, symbol, ticket, r_multiple, profit, reason, exit_price=None,
           entry_price=None, day_trades=None, day_r=None, wall_left_pct=None):
    head = f"<b>📉 ВЫХОД</b> · {escape(symbol)}"
    if r_multiple is not None:
        head += f" · R {r_multiple:+.2f}"
    if profit is not None:
        head += f" · {profit:+.2f}$"
    lines = [head, f"{_hhmm(now)} · тикет {ticket}", "",
             f"<b>Причина:</b> {escape(reason)}"]
    if exit_price is not None:
        lines.append(f"Выход {exit_price}"
                     + (f" (вход {entry_price})" if entry_price is not None else ""))
    day = [x for x in (f"сделок {day_trades}" if day_trades is not None else None,
                       f"R {day_r:+.2f}" if day_r is not None else None,
                       f"до стены {_pct(wall_left_pct)}" if wall_left_pct is not None
                       else None) if x]
    if day:
        lines.append("День: " + " · ".join(day))
    return "\n".join(lines)


def director(*, now, title, body, facts=None, decision=None):
    """Рассуждение ДИРЕКТОРА — то, что раньше жило только в чате.

    Заведено по прямой просьбе владельца счёта 2026-08-03: он видел решения
    трейдеров (kind `wake`), но не видел, почему директор маршрутизировал
    событие так, а не иначе, и на каких числах.

    Разделение title/facts/decision не косметика: в канал должно попадать то,
    что можно проверить (числа) отдельно от того, что было выбором (решение).
    Иначе через неделю нельзя будет отличить «данные были такие» от «я так
    рассудил», а именно это различение и делает канал полезным для надзора.
    """
    lines = [f"<b>🧭 {escape(title)}</b> · {_hhmm(now)}", ""]
    if body:
        lines += [escape(body), ""]
    for f in (facts or []):
        lines.append(f"· {escape(f)}")
    if decision:
        lines += ["", f"<b>Решение:</b> {escape(decision)}"]
    return "\n".join(lines).strip()


def critical(*, now, title, details, action=None):
    lines = [f"<b>🚨 {escape(title)}</b>", _hhmm(now), ""]
    lines += [escape(d) for d in details]
    if action:
        lines += ["", f"<b>{escape(action)}</b>"]
    return "\n".join(lines)


def session_close(*, now, duration, trades, sum_r, pnl_usd, wr, wakes, useful,
                  noisy=None, progress_pct=None, plan_traded=None, plan_total=None):
    lines = [f"<b>⏹ Сессия закрыта</b> · {_hhmm(now)} · длилась {escape(duration)}",
             f"Сделок {trades} · R {sum_r:+.2f} · P&amp;L {pnl_usd:+.2f}$"
             + (f" · WR {wr:.0%}" if wr is not None else ""), ""]
    usefulness = f" (полезность {useful / wakes:.2f})" if wakes else ""
    lines.append(f"Пробуждений {wakes}, с решением {useful}{usefulness}")
    if noisy:
        lines.append("Впустую: " + escape(", ".join(noisy)))
    if progress_pct is not None:
        lines.append(f"Прогресс к цели: {_pct(progress_pct)}")
    if plan_total:
        lines.append(f"План: {plan_total} гипотез, отработали {plan_traded}")
    return "\n".join(lines)
