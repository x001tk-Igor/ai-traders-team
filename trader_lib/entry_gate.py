"""Единый предвходовой гейт (задача 5.4).

СВОИХ ПРАВИЛ У ЭТОГО МОДУЛЯ НЕТ. Он вызывает восемь уже написанных проверок в
фиксированном порядке и складывает вердикты. Вся ценность — в трёх свойствах:

  1. ОДНА ТОЧКА ВХОДА. До неё каждый вызывающий решал сам, что проверить перед
     сделкой, и рано или поздно кто-нибудь проверил бы не всё. Теперь путь
     один: scripts/enter.py зовёт check_entry и больше ничего не решает.
  2. FAIL-CLOSED НА КАЖДОМ ШАГЕ. Исключение внутри проверки — это запрет, а не
     «проверка промолчала, значит можно». Отсутствие файла состояния — тоже
     запрет: незнание не равно разрешению.
  3. ПРИЧИНЫ ДЛЯ ЖУРНАЛА. Каждый отказ — фраза, которую человек поймёт через
     месяц, а не код. Они уходят в skip-запись и в разбор дня.

ПОРЯДОК ПРОВЕРОК ВЫБРАН ПО ЦЕНЕ, А НЕ ПО ВАЖНОСТИ: сначала дешёвые и
детерминированные (время, whitelist), потом читающие файлы (спред, новости),
потом требующие журнала и расчётов (риск, экспозиция, качество), последней —
свежесть защиты. Все проверки выполняются ВСЕГДА, даже после первого запрета:
модель должна увидеть полную картину за один заход, а не чинить по одной
причине за цикл. Единственное исключение — проверки, которым нечем работать
после падения предыдущей (тогда они честно пишут «не выполнена»).

ЧТО СЮДА НЕ ВХОДИТ. Размер позиции (это scripts/enter.py по выданному
max_risk_usd), запись в журнал, отправка ордера. Гейт отвечает на один вопрос:
можно ли входить и на какой риск.
"""
import datetime as dt
import json
from pathlib import Path

from trader_lib.allocation import load_allocation, mandate_state, risk_cap_usd
from trader_lib.clusters import cluster_of, load_clusters
from trader_lib.config import state_dir
from trader_lib.constitution import HASH_FILE, check_config
from trader_lib.exposure import open_risk_usd
from trader_lib.journal import CONFIRMED_SETUP_STATUS, read_records
from trader_lib.model_session import current as current_model
from trader_lib.news import load_windows, news_state
from trader_lib.quality import breakeven_p, costs_R
from trader_lib.risk_gate import safe_evaluate_gate
from trader_lib.session import server_day_key, session_gate
from trader_lib.spread_gate import LiveSpreadWindow, load_medians, spread_state

UTC = dt.timezone.utc

# Порядок важен: он же определяет порядок причин в отчёте.
CHECKS = ("constitution", "identity", "session", "instrument", "mandate",
          "spread", "news", "risk", "exposure", "cluster", "quality",
          "heartbeat")

# Пульс защиты старше этого — модель считает себя незащищённой (docs/
# alerts_schema.md, протокол вооружения датчика).
HEARTBEAT_MAX_AGE_S = 90

SLIPPAGE_POINTS_EST = 5.0
COMMISSION_USD = 0.0

# Путь к конституции по умолчанию — тот же, что читает весь контур.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "trader.config.json"


def _ok(reason="", **extra):
    return {"ok": True, "reason": reason, **extra}


def _no(reason, **extra):
    return {"ok": False, "reason": reason, **extra}


def _read_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - битый файл равнозначен отсутствию
        return None


def _check_constitution(cfg, sd, config_path=None):
    """Лимиты не менялись с момента подтверждения человеком (задача 8.2).

    Проверка стоит ПЕРВОЙ и сравнивает хэш охраняемых блоков файла на диске с
    подтверждённым. Читается именно файл, а не переданный cfg: cfg мог быть
    собран в памяти (тесты, dry-run), а защита нужна от правки того, что реально
    прочитает следующий запуск.
    """
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    raw = _read_json(path)
    if raw is None:
        return _no(f"конфиг не прочитан для проверки конституции: {path}")
    verdict = check_config(raw, Path(sd) / HASH_FILE)
    return {"ok": verdict["ok"], "reason": verdict["reason"],
            "config_hash": verdict["current"], "acked": verdict["acked"]}


def _check_identity(cfg, sd):
    """Кто торгует — подтверждено текущим сеансом? (задача владельца счёта 2026-07-27)

    Неверный model_id не ломает сделку, он ломает ПАМЯТЬ: калибровка и by_model
    считаются по этому полю, а разделить перемешанные записи двух моделей
    задним числом нечем. Поэтому идентичность проверяется наравне с
    конституцией: цена ошибки не в одной сделке, а во всей накопленной
    статистике.

    Стоит недорого: одна команда в начале сеанса.
    """
    st = current_model(Path(sd), cfg)
    return {"ok": st["ok"], "reason": st["reason"],
            "model_id": st["model_id"], "profile": st["profile"],
            "source": st["source"]}


def _check_session(cfg, now):
    st = session_gate(utc_now=now, cfg=cfg)
    if not st["allow_new"]:
        return _no("; ".join(st["reasons"]), phase=st["phase"],
                   flat_required=st["flat_required"])
    return _ok(f"фаза {st['phase']}", phase=st["phase"], flat_required=False)


def _check_instrument(cfg, symbol):
    if symbol not in cfg.instruments.whitelist:
        return _no(f"{symbol} нет в instruments.whitelist конституции")
    return _ok(f"{symbol} в whitelist")


def _check_spread(market, cfg, sd, symbol, now, live=None):
    """Живая база спреда (Ф1) поднимается с диска, если её не передали явно.

    Собирает её датчик — он читает спред каждую секунду и накапливает часовое
    окно в spread_live.json. Гейт обязан ею ПОЛЬЗОВАТЬСЯ, иначе сбор ни на что
    не влияет: базой сравнения останется барная медиана, которая меряет спред
    на закрытии свечи (тихий момент), тогда как решения принимаются в активные.
    За 2026-07-27..31 это стоило 9 отклонённых входов и двух прибыльных сделок.
    """
    path = Path(sd) / "spread_median.json"
    doc = load_medians(path)
    if live is None:
        live = LiveSpreadWindow.load(Path(sd) / "spread_live.json")
    st = spread_state(market, cfg, doc, symbol=symbol, now=now, path=path, live=live)
    return ({"ok": st["allowed"], "reason": st["reason"],
             "spread_points": st["spread_points"], "median": st["median"],
             "median_unknown": st["median_unknown"]})


def _check_news(cfg, sd, symbol, now):
    doc = load_windows(Path(sd) / "news_cache.json", cfg=cfg, now=now,
                       loader=_no_network)
    st = news_state(doc, now=now, symbol=symbol)
    return {"ok": not st["blocked"], "reason": st["reason"],
            "next_event_in_min": st["next_event_in_min"]}


def _no_network():
    """Гейт НЕ ходит в сеть: обновление календаря — задача цикла восприятия
    (там это делается раз в сессию), а вход не имеет права ждать сетевого
    таймаута. Отсутствие свежего кэша здесь честно означает stale, и это
    трактуется по cfg.news.fail_mode."""
    raise RuntimeError("гейт не обновляет календарь по сети; кэш обновляет цикл "
                       "восприятия")


def _check_risk(market, cfg, sd, symbol, side, entry, sl, setup_status, planned, now):
    """Риск-гейт конституции: стены, лестница просадки, серия убытков, лимиты
    дня. Собирается тем же кодом, что и CLI, — второй сборки входов быть не
    должно."""
    from scripts.risk_gate_cli import build_gate_inputs

    records = read_records(Path(sd) / "journal.jsonl")
    inputs = build_gate_inputs(market, cfg, records, now=now)
    verdict = safe_evaluate_gate(**inputs)
    ok = verdict["verdict"] == "OK"
    reason = ("; ".join(verdict["reasons"]) if not ok
              else f"риск-гейт: {verdict['verdict']}")
    return {"ok": ok, "reason": reason, "verdict": verdict["verdict"],
            "max_risk_usd": verdict["max_risk_per_trade_usd"],
            "require_setup_status": verdict["require_setup_status"],
            "planned_only": verdict.get("planned_only", False),
            "daily_risk_remaining_usd": verdict.get("daily_risk_remaining_usd"),
            "blocked_by": verdict.get("blocked_by"),
            "binding_term": verdict.get("binding_term")}


def _check_exposure(market, cfg, symbol, side):
    """Число одновременных позиций и совпадение направления по валюте.

    Корреляцию в строгом смысле здесь не считаем (для этого нужна история
    доходностей пар) — проверяется то, что видно точно: сколько позиций уже
    открыто и не набираем ли мы третью позицию в ту же сторону по одной
    валюте. Формулировка в отчёте честная, без притязаний на корреляцию.
    """
    positions = market.positions()
    if len(positions) >= cfg.risk.max_open_positions:
        return _no(f"уже открыто {len(positions)} позиций при лимите "
                   f"{cfg.risk.max_open_positions}", positions=len(positions),
                   correlation_check="не проверялась: лимит позиций")
    same = [p for p in positions
            if p.get("symbol") == symbol and (p.get("type") == 0) == (side == "buy")]
    note = (f"по {symbol} уже есть {len(same)} позиция(й) в ту же сторону"
            if same else "пересечений по символу и направлению нет")
    return _ok(note, positions=len(positions), correlation_check=note)


def _check_mandate(cfg, sd, symbol, trader, now):
    """Торгует ли этот трейдер сегодня этот инструмент (Ф4).

    Прямой ответ на болезнь человеческих команд: все увидели одно движение и
    набросились на один инструмент, забыв про остальные. Мандат раздаёт
    директор до открытия, а исполняет ЭТА проверка — указание директора можно
    нарушить, проверку в гейте нельзя.

    Одиночный режим и отсутствие аллокации проходят: там мандатов не
    существует, и риск держат остальные проверки. Зато УСТАРЕВШИЙ мандат
    отвергается — вчерашние инструменты назначались под вчерашнюю структуру.
    """
    alloc = load_allocation(Path(sd) / "allocation.json")
    day = server_day_key(utc_now=now,
                         offset_hours=cfg.risk.server_utc_offset_hours,
                         reset_hour=cfg.risk.server_day_reset_hour)
    st = mandate_state(alloc, trader=trader, symbol=symbol, now=now,
                       server_day=day)
    return {"ok": st["allowed"], "reason": st["reason"],
            "instruments": st["instruments"], "trader": trader}


def _check_cluster(market, cfg, sd, symbol, side):
    """Не собирает ли команда одну ставку под видом нескольких (Ф2).

    ЗАЧЕМ ОТДЕЛЬНО ОТ _check_exposure. Та проверка сравнивает символы и
    направления буквально и по построению не видит, что EURUSD и USDCHF — это
    один доллар (corr −0.87), а BTCUSD и ETHUSD — одна крипта (+0.865). Замер
    2026-08-01 показал: девять инструментов whitelist складываются в ПЯТЬ
    независимых факторов. Три трейдера, разошедшиеся по EURUSD, GBPUSD и
    AUDUSD, выглядели бы диверсифицированными, а держали бы одну сделку в трёх
    экземплярах с тройным риском.

    ПРАВИЛО: одна открытая позиция на кластер, командой целиком. Направление не
    смягчает запрет — встречные позиции внутри кластера дают нулевую нетто
    экспозицию при двойном спреде, то есть самый дорогой способ ничего не
    заработать.

    Неизвестный символ БЛОКИРУЕТСЯ: отсутствие в карте означает, что про его
    корреляции ничего не известно, а не что он независим.

    Отсутствие карты целиком (первый запуск, пересчёт не делался) торговлю НЕ
    останавливает — иначе контур встанет до первого пересчёта; риск в этот
    период держат остальные проверки гейта.
    """
    clusters = load_clusters(Path(sd) / "clusters.json")
    positions = market.positions()
    if not positions:
        return _ok("открытых позиций нет", cluster=cluster_of(symbol, clusters))
    if not clusters.get("groups"):
        return _ok("карта кластеров ещё не построена — проверка не выполнялась",
                   cluster=None)

    mine = cluster_of(symbol, clusters)
    if mine is None:
        return _no(f"{symbol} нет в карте кластеров: про его корреляции ничего "
                   "не известно, а незнание не равно независимости",
                   cluster=None)

    clash = [p for p in positions if cluster_of(p.get("symbol"), clusters) == mine]
    if clash:
        held = ", ".join(sorted({p["symbol"] for p in clash}))
        return _no(f"{symbol} в одном кластере риска с уже открытой позицией "
                   f"({held}) — это одна ставка, а не две", cluster=mine,
                   clashes_with=held)
    return _ok(f"{symbol} в свободном кластере риска", cluster=mine)


def _check_quality(market, cfg, symbol, entry, sl, rr, p_win_journal, risk_usd):
    """Издержки в долях R и достижимость точки безубытка.

    Лот здесь ещё неизвестен (его считает enter.py по выданному риску), но
    costs_R от лота не зависит: и издержки, и риск линейны по объёму. Поэтому
    считаем на условном лоте — результат тот же.
    """
    si = market.symbol_info(symbol)
    point = si["point"]
    sl_points = abs(entry - sl) / point
    if sl_points <= 0:
        return _no("дистанция стопа нулевая")
    c_r = costs_R(spread_points=si.get("spread", 0.0), commission_usd=COMMISSION_USD,
                  slippage_points_est=SLIPPAGE_POINTS_EST, sl_points=sl_points,
                  lots=1.0, value_per_point=si["trade_contract_size"] * point)
    if c_r > cfg.risk.max_costs_R:
        return _no(f"издержки {c_r:.3f}R выше предела {cfg.risk.max_costs_R}R",
                   costs_R=c_r)
    be = breakeven_p(rr=rr, costs_r=c_r)
    if be >= 1.0:
        return _no(f"точка безубытка {be:.2f} недостижима при rr={rr} и издержках "
                   f"{c_r:.3f}R", costs_R=c_r, breakeven_p=be)
    if p_win_journal is not None and p_win_journal < be:
        return _no(f"частота по журналу {p_win_journal} ниже точки безубытка {be:.3f}",
                   costs_R=c_r, breakeven_p=be)
    return _ok(f"издержки {c_r:.3f}R, безубыток {be:.2f}", costs_R=c_r, breakeven_p=be)


def _check_heartbeat(sd, now):
    """Свежесть ЗАЩИТЫ, а не процесса (см. docs/alerts_schema.md).

    Три причины запрета: пульса нет вовсе, пульс старше 90 с, и отдельно —
    стоп-крану есть что рассказать о своих действиях, а модель ещё не в курсе
    (pending_undelivered): входить поверх нерассказанного нельзя.
    """
    hb = _read_json(Path(sd) / "watch_heartbeat.json")
    if hb is None:
        return _no("датчик пробуждения не запущен (нет watch_heartbeat.json): "
                   "без стоп-крана входить нельзя")
    if not hb.get("walls_checked") or not hb.get("ts"):
        return _no("датчик жив, но стена по equity в последнем тике не посчитана — "
                   "защита не подтверждена")
    try:
        age = (now - dt.datetime.fromisoformat(hb["ts"])).total_seconds()
    except (ValueError, TypeError):
        return _no("метка времени пульса нечитаема")
    if age > HEARTBEAT_MAX_AGE_S:
        return _no(f"пульс защиты старше {HEARTBEAT_MAX_AGE_S} с ({age:.0f} с): "
                   "стоп-кран не подтверждает, что стена считается", age_s=age)
    if hb.get("pending_undelivered"):
        return _no(f"стоп-кран действовал деньгами, но модели ещё не рассказал "
                   f"({hb['pending_undelivered']} сообщ.): входить поверх "
                   "нерассказанного нельзя")
    return _ok(f"защита свежая ({age:.0f} с назад)", age_s=age)


def check_entry(*, market, cfg, symbol, side, entry, sl, rr, setup_status,
                p_win_journal=None, planned=True, now=None, state=None,
                config_path=None, trader=None):
    """Единственная дверь во вход. Возвращает:

    {allow, max_risk_usd, reasons, require_setup_status, verdict, checks,
     session_phase, news_check, spread_at_entry, correlation_check,
     daily_risk_remaining_usd}

    Любая внутренняя ошибка — запрет с причиной, а не исключение наружу:
    вызывающий (scripts/enter.py) обязан получить вердикт всегда.
    """
    now = now or dt.datetime.now(UTC)
    sd = state or state_dir(cfg)
    checks = {}

    def run(name, fn):
        try:
            checks[name] = fn()
        except Exception as e:  # noqa: BLE001 - ошибка проверки = запрет
            checks[name] = _no(f"проверка {name} упала: {e}")

    run("constitution", lambda: _check_constitution(cfg, sd, config_path))
    run("identity", lambda: _check_identity(cfg, sd))
    run("session", lambda: _check_session(cfg, now))
    run("instrument", lambda: _check_instrument(cfg, symbol))
    run("mandate", lambda: _check_mandate(cfg, sd, symbol, trader, now))
    run("spread", lambda: _check_spread(market, cfg, sd, symbol, now))
    run("news", lambda: _check_news(cfg, sd, symbol, now))
    run("risk", lambda: _check_risk(market, cfg, sd, symbol, side, entry, sl,
                                    setup_status, planned, now))
    run("exposure", lambda: _check_exposure(market, cfg, symbol, side))
    run("cluster", lambda: _check_cluster(market, cfg, sd, symbol, side))
    run("quality", lambda: _check_quality(market, cfg, symbol, entry, sl, rr,
                                          p_win_journal,
                                          checks.get("risk", {}).get("max_risk_usd")))
    run("heartbeat", lambda: _check_heartbeat(sd, now))

    risk = checks.get("risk", {})
    require_status = risk.get("require_setup_status", "any")
    reasons = [f"{name}: {checks[name]['reason']}"
               for name in CHECKS if not checks[name]["ok"]]

    # требование к статусу сетапа — отдельная причина: гейт мог разрешить вход,
    # но только по подтверждённому сетапу (ступень лестницы просадки)
    if (require_status == "confirmed" and setup_status != CONFIRMED_SETUP_STATUS
            and risk.get("ok")):
        reasons.append(f"риск-гейт требует подтверждённый сетап, получен "
                       f"{setup_status!r}")

    # внеплановый вход при planned_only — тоже отказ гейта, а не профиля модели
    if risk.get("planned_only") and not planned:
        reasons.append("риск-гейт разрешает только запланированные входы")

    allow = not reasons

    # Доля дневного бюджета, выделенная трейдеру директором (Ф4), урезает то,
    # что гейт выдаёт наружу. Только вниз: аллокация не может поднять
    # конституционный максимум — оркестратор, способный это сделать, был бы
    # способом обойти защиту, ради которой построен.
    max_risk = risk.get("max_risk_usd", 0.0) if allow else 0.0
    if allow and trader is not None:
        max_risk = risk_cap_usd(
            load_allocation(Path(sd) / "allocation.json"), trader=trader,
            constitution_max=max_risk,
            daily_budget=risk.get("daily_risk_remaining_usd") or 0.0,
            spent_today=0.0)

    return {
        "allow": allow,
        "max_risk_usd": max_risk,
        "reasons": reasons,
        "require_setup_status": require_status,
        "verdict": risk.get("verdict", "HALT_NEW"),
        "checks": checks,
        "session_phase": checks.get("session", {}).get("phase"),
        "news_check": checks.get("news", {}).get("reason"),
        "spread_at_entry": checks.get("spread", {}).get("spread_points"),
        "correlation_check": checks.get("exposure", {}).get("correlation_check"),
        "daily_risk_remaining_usd": risk.get("daily_risk_remaining_usd"),
        "blocked_by": risk.get("blocked_by"),
        "binding_term": risk.get("binding_term"),
    }
