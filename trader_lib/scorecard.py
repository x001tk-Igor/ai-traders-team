"""Рендеринг markdown-отчётов (задача 6.3).

Вынесено из score.py по указанию ревью задачи 2.2: агрегация и рендеринг не
делят состояние, а score.py уже 368 строк. Третий потребитель отчётов (дневной
разбор) стал поводом сделать это сейчас, а не когда их станет пять.

ГРАНИЦА МОДУЛЯ. Здесь только превращение готовых чисел в текст для человека.
Никаких вычислений: если в отчёте появляется новая метрика, она считается в
score.py (агрегация журнала) или в scripts/review.py (дневной срез), а сюда
приходит готовой. Иначе одно и то же число начнёт считаться в двух местах и
разойдётся — ровно так уже разъезжались границы дня (см. session.py).

КОНВЕНЦИЯ ПРО None. «н/д» вместо голого None: в документе для человека None
читается как недоделка, а не как осознанное «данных нет». Реальный 0 остаётся
0 — разница «нет данных» против «данные говорят ноль» не стирается.
"""

_UNKNOWN_EMPTY_KEYS = frozenset({"unknown", "empty_label"})


def fmt_or_na(value):
    """None → «н/д». Реальный 0 остаётся 0 (см. шапку модуля)."""
    return "н/д" if value is None else value


def render_segment_lines(segment):
    if not segment:
        return ["- нет данных"]
    lines = []
    for key, a in sorted(segment.items(), key=lambda kv: -(kv[1]["sum_R"] or 0)):
        flag = ("⚠ мало данных" if a.get("insufficient")
                else ("✅ edge" if a.get("edge_significant") else "—"))
        lines.append(f"- **{key}**: n={a['n']} WR={a['wr']} avgR={a['avg_R']} {flag}")
    return lines


def _has_unknown_or_empty_bucket(stats):
    """True, если хотя бы один из сегментов (или calibration_by_model) реально
    содержит бакет "unknown" или "empty_label" — легенда печатается ТОЛЬКО
    тогда: журнал без легаси-записей и без мусорных меток не должен нести шум
    про случай, которого нет."""
    keys = set()
    for seg_name in ("by_regime", "by_session", "by_model", "planned_vs_unplanned"):
        keys |= set(stats.get(seg_name, {}))
    keys |= set(stats.get("calibration_by_model", {}))
    return bool(keys & _UNKNOWN_EMPTY_KEYS)


def render_scorecard(stats, *, progress_to_target_pct, target_pct):
    """progress_to_target_pct может быть None: run_score.py честно передаёт
    None, когда снимок счёта (perceive.py → account_snapshot) не был доступен в
    этом запуске — лучше явное «н/д», чем тихо подставленный 0.0, который
    выглядел бы как реальный прогресс «ноль» (см. задачу 2.2)."""
    o = stats["overall"]
    prog_str = ("н/д (снимок счёта не передан)" if progress_to_target_pct is None
                else f"{progress_to_target_pct:.2f}%")
    lines = [
        "# Scorecard",
        f"**Прогресс к цели:** {prog_str} из {target_pct}%",
        f"**Матожидание (avg R):** {fmt_or_na(o['avg_R'])}  ·  n={o['n']}  ·  "
        f"WR={fmt_or_na(o['wr'])}  ·  ΣR={o['sum_R']}",
        "",
        "## Сетапы",
    ]
    lines += render_segment_lines(stats["by_setup"])

    if _has_unknown_or_empty_bucket(stats):
        lines += ["", "_unknown — в записи не было этого поля (журнал старого "
                      "формата, до задачи 2.1); empty_label — поле было, но "
                      "после нормализации оказалось пустым (мусорное "
                      "значение, не легаси)._"]

    lines += ["", "## Режимы рынка"]
    lines += render_segment_lines(stats["by_regime"])

    lines += ["", "## Сессии"]
    lines += render_segment_lines(stats["by_session"])

    lines += ["", "## Модели"]
    lines += render_segment_lines(stats["by_model"])

    lines += ["", "## План vs вне плана"]
    lines += render_segment_lines(stats["planned_vs_unplanned"])

    lines += ["", "## Калибровка (заявленная уверенность → реальный WR, глобально)"]
    if not stats["calibration"]:
        lines.append("- нет данных")
    for c in stats["calibration"]:
        lines.append(f"- {c['conf_bucket']}: n={c['n']} реальный WR={c['realized_wr']}")

    lines += ["", "## Калибровка по модели"]
    if not stats["calibration_by_model"]:
        lines.append("- нет данных")
    for model_id, calib in sorted(stats["calibration_by_model"].items()):
        lines.append(f"- **{model_id}**:")
        if not calib:
            lines.append("  - нет данных")
        for c in calib:
            lines.append(f"  - {c['conf_bucket']}: n={c['n']} реальный WR={c['realized_wr']}")

    # markdown показывает ТОЛЬКО near_duplicate. small_n при малом
    # min_n_for_confirmed (порог = min_n_for_confirmed // 4) кандидатом
    # становится КАЖДАЯ метка молодого журнала — сигнал о реальном дрейфе тонет
    # в шуме «выборка ещё набирается». Информация не теряется: факт малой
    # выборки виден как «⚠ мало данных» на самих сегментах, а trader-reflect
    # читает stats.json, где _label_drift отдаёт small_n без изменений.
    near_dup = [d for d in (stats.get("label_drift") or []) if d["kind"] == "near_duplicate"]
    if near_dup:
        lines += ["", "## Возможный дрейф ярлыков (похожие метки, кандидаты для reflect)"]
        for d in near_dup:
            labels = ", ".join(d["labels"])
            lines.append(f"- [{d['field']}] {labels} — {d['note']}")

    return "\n".join(lines)


def render_daily_report(review):
    """Дневной разбор (scripts/review.py → build_review) в markdown.

    Порядок разделов подчинён вопросу «что я сделала и чему это учит», а не
    удобству сборки: сначала факт (числа дня), потом соответствие замыслу
    (план против факта), потом качество механизма пробуждения, потом
    калибровка, и только в конце — что мешало и что будет завтра.
    """
    r = review
    t = r["trades"]
    lines = [
        f"# Разбор дня · {r['server_day']}",
        "",
        "## Числа",
        f"- Сделок закрыто: **{t['closed']}**  ·  открыто и ведётся: {t['still_open']}",
        f"- PnL: **{fmt_or_na(t['pnl_usd'])}$**  ·  в R: **{fmt_or_na(t['sum_R'])}**",
        f"- WR: {fmt_or_na(t['wr'])}  ·  средний выигрыш {fmt_or_na(t['avg_win_R'])}R  ·  "
        f"средний проигрыш {fmt_or_na(t['avg_loss_R'])}R",
        f"- Профит-фактор: {fmt_or_na(t['profit_factor'])}",
        f"- Внутридневная просадка по кривой R: {fmt_or_na(t['max_drawdown_R'])}R",
        f"- Риск, отданный в рынок: {fmt_or_na(r['risk_used_usd'])}$ "
        f"({fmt_or_na(r['risk_used_pct_of_budget'])}% дневного бюджета)",
    ]

    p = r["plan_vs_fact"]
    lines += [
        "",
        "## План против факта",
        f"- Гипотез в плане: {p['hypotheses_total']}  ·  отработало: {p['hypotheses_traded']}",
        f"- Плановых входов: **{p['planned']}**  ·  внеплановых: **{p['unplanned']}** "
        f"(лимит {p['unplanned_limit']})",
    ]
    if p["untouched"]:
        lines.append(f"- Не сработали: {', '.join(p['untouched'])}")
    if p["off_plan_setups"]:
        lines.append(f"- Входы вне плана по сетапам: {', '.join(p['off_plan_setups'])}")

    a = r["alert_efficiency"]
    lines += [
        "",
        "## Пробуждения",
        f"- Событий доставлено: **{a['delivered']}** (придушено бюджетом: {a['suppressed']})",
        f"- Из них дали решение: **{a['with_decision']}** вход, {a['with_skip']} осознанный "
        f"пропуск, {a.get('with_observation', 0)} наблюдение, **{a['ignored']} впустую**",
        f"- Полезность: {fmt_or_na(a['usefulness'])}",
    ]
    if a["noisy_alerts"]:
        lines.append("- Кандидаты в мусор (будили, решения не дали): "
                     + ", ".join(f"{x['alert_id']}×{x['count']}" for x in a["noisy_alerts"]))

    lines += ["", "## Калибровка дня (заявленная уверенность → факт)"]
    if not r["calibration"]:
        lines.append("- нет данных")
    for c in r["calibration"]:
        lines.append(f"- {c['conf_bucket']}: n={c['n']} реальный WR={c['realized_wr']}")

    lines += ["", "## Что мешало"]
    if not r["blocks"]:
        lines.append("- ничего не блокировало")
    for b in r["blocks"]:
        lines.append(f"- {b['reason']} ×{b['count']}")

    lines += ["", "## Завтра"]
    if not r["news_tomorrow"]:
        lines.append("- значимых новостей не найдено (или календарь устарел)")
    for w in r["news_tomorrow"]:
        lines.append(f"- {w['at']} · {w['title']} ({w['level']}, {', '.join(w['currencies'])})")

    return "\n".join(lines)
