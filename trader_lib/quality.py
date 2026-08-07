"""Качество сделки: издержки в R, требуемый винрейт, множители риска.

Чистые функции: не читают MT5/файлы/часы — все данные приходят аргументами,
всё тестируется офлайн. Числа этого модуля идут двум потребителям:
entry_gate (задача 5.4) отклоняет вход при costs_R > cfg.risk.max_costs_R и
сравнивает required_p_win с фактическим винрейтом сетапа из журнала;
size_position/enter.py умножают риск на status_mult и profile_mult. Сам
гейт (сравнение с порогом) здесь НЕ реализован — это ответственность
вызывающего.

ПРОЕКТНОЕ РЕШЕНИЕ — нераспознанные status/profile (status_risk_mult,
profile_risk_mult): в отличие от остального пакета, где вложенные ключи
конфига индексируются напрямую и опечатка в КОНФИГЕ обязана падать громко
(см. config.py), здесь status и profile приходят не из конфига, а из
МОДЕЛИ — того самого LLM, что ведёт сессию. Модель может выдумать статус
(«полу-подтверждён») или опечататься, и это не ошибка конфигурации, которую
нужно чинить руками перед стартом, а рантайм-ввод, который обязан быть
обработан безопасно:
  - падать (raise) нельзя — опечатка модели заблокировала бы торговлю целиком
    из-за одной кривой строки в её выводе;
  - тихий дефолт вроде "unknown" тоже нельзя — выдуманный/неверный статус не
    равен «статусу unknown»: мы не знаем, насколько сетап на самом деле
    проверен, а "unknown" в конфиге — это конкретное, осознанно выбранное
    значение риска, а не синоним «не смогли распознать».
Единственно безопасное направление — САМЫЙ КОНСЕРВАТИВНЫЙ (минимальный)
множитель из тех, что реально есть в конфиге: fail-closed в сторону
минимального риска, а не в сторону падения или тихого дефолта. recognized
явно сообщает вызывающему, что произошла именно эта ветка — не путать со
случаем, когда значение есть в конфиге. Ключи самого конфига при этом
по-прежнему индексируются напрямую (без .get(key, default)) — этот модуль
не должен тихо проглатывать опечатку в самом конфиге.
"""


def costs_R(*, spread_points, commission_usd, slippage_points_est,
            sl_points, lots, value_per_point) -> float:
    """Издержки сделки в долях риска (R).

    costs_usd = (spread_points + slippage_points_est) * value_per_point * lots
                + commission_usd
    risk_usd  = sl_points * value_per_point * lots
    → costs_usd / risk_usd

    sl_points, lots и value_per_point обязаны быть положительными: сделка с
    нулевой/отрицательной дистанцией стопа, нулевым/отрицательным лотом или
    нулевой/отрицательной стоимостью пункта не существует. Тихо вернуть inf
    (или отрицательное costs_R) нельзя — это скрыло бы ошибку вызывающего
    (перепутанные аргументы, забытый SL) за правдоподобным числом.
    """
    if sl_points <= 0:
        raise ValueError(f"sl_points должен быть > 0, получено {sl_points!r}")
    if lots <= 0:
        raise ValueError(f"lots должен быть > 0, получено {lots!r}")
    if value_per_point <= 0:
        raise ValueError(f"value_per_point должен быть > 0, получено {value_per_point!r}")

    costs_usd = (spread_points + slippage_points_est) * value_per_point * lots + commission_usd
    risk_usd = sl_points * value_per_point * lots
    return costs_usd / risk_usd


def breakeven_p(*, rr, costs_r) -> float:
    """Минимальная вероятность выигрыша, при которой ожидание сделки ровно
    нулевое с учётом издержек: P_be = (1 + costs_r) / (1 + rr).

    rr <= 0 → ValueError: соотношение прибыль/риск не определено (rr == 0)
    или лишено смысла (rr < 0) — тихо считать с ним нельзя.
    """
    if rr <= 0:
        raise ValueError(f"rr должен быть > 0, получено {rr!r}")
    return (1 + costs_r) / (1 + rr)


def required_p_win(*, rr, costs_r, margin=0.05) -> float:
    """breakeven_p + margin — запас винрейта сверх точки нулевого ожидания.

    Значение > 1.0 НЕ клипуется: это честный признак, что сделка недостижима
    при таких rr/издержках (требуемый винрейт выше 100% в принципе), а не
    повод подменить его правдоподобным числом. Что делать с таким
    результатом (отклонить сетап, снизить издержки, поднять rr) — решает
    вызывающий (entry_gate, задача 5.4), не эта функция.

    margin НЕ валидируется (в отличие от rr/costs_r): это доверенный
    внутренний параметр вызывающего кода (константа запаса, не ввод модели
    и не рыночные данные), поэтому здесь нет источника «опечатки модели»,
    от которой нужно защищаться. Отрицательный margin — ошибка вызывающего,
    а не рантайм-ввод, который этот модуль обязан перехватывать.
    """
    return breakeven_p(rr=rr, costs_r=costs_r) + margin


def status_risk_mult(status, cfg_status_mult) -> dict:
    """Множитель риска по статусу сетапа (мотивация нераспознанных значений
    — см. шапку модуля). cfg_status_mult = cfg.risk.status_risk_mult, напр.
    {"unknown": 0.2, "изучаю": 0.2, "карантин": 0.1, "подтверждён": 1.0}.

    Возвращает {'mult': float, 'recognized': bool, 'reason': str}.
    status есть среди ключей конфига → его множитель, recognized=True.
    status отсутствует (опечатка/выдумка модели) → минимум значений
    словаря, recognized=False.
    """
    if status in cfg_status_mult:
        mult = cfg_status_mult[status]
        return {
            "mult": mult,
            "recognized": True,
            "reason": f"статус {status!r} распознан в конфиге, множитель {mult}",
        }

    # fail-closed на минимум из конфига — НЕ raise и НЕ тихий дефолт
    # "unknown": статус пришёл от модели, а не из конфига; обоснование — в
    # шапке модуля (ПРОЕКТНОЕ РЕШЕНИЕ).
    fallback = min(cfg_status_mult.values())
    known_str = ", ".join(sorted(cfg_status_mult))
    return {
        "mult": fallback,
        "recognized": False,
        "reason": (
            f"статус {status!r} не распознан (не найден среди "
            f"{known_str}) — применён минимальный известный "
            f"множитель {fallback} (fail-closed, см. шапку quality.py)"
        ),
    }


def profile_risk_mult(profile, cfg_model) -> dict:
    """Множитель риска по профилю модели (та же мотивация нераспознанных
    значений, что и у status_risk_mult — см. шапку модуля).
    cfg_model = cfg.model.

    "strong" — базовый профиль, множитель 1.0 (в profile_rules его нет, там
    только исключения вроде "weak"). "weak" → cfg_model.profile_rules
    ["weak"]["risk_mult"] (индексация напрямую — это ключ конфига). Любой
    другой профиль (опечатка/выдумка модели, значение вне {"strong","weak"})
    → минимум из известных множителей, recognized=False.

    Возвращает {'mult': float, 'recognized': bool, 'reason': str}.
    """
    known = {
        "strong": 1.0,
        "weak": cfg_model.profile_rules["weak"]["risk_mult"],
    }

    if profile in known:
        mult = known[profile]
        return {
            "mult": mult,
            "recognized": True,
            "reason": f"профиль {profile!r} распознан, множитель {mult}",
        }

    # fail-closed на минимум из известных — НЕ raise и НЕ тихий дефолт
    # "unknown": профиль пришёл от модели, а не из конфига; обоснование — в
    # шапке модуля (ПРОЕКТНОЕ РЕШЕНИЕ).
    fallback = min(known.values())
    known_str = ", ".join(sorted(known))
    return {
        "mult": fallback,
        "recognized": False,
        "reason": (
            f"профиль {profile!r} не распознан (не найден среди "
            f"{known_str}) — применён минимальный известный множитель "
            f"{fallback} (fail-closed, см. шапку quality.py)"
        ),
    }
