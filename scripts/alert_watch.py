"""Датчик пробуждения модели и стоп-кран (задача 3.2).

ДВА НАЗНАЧЕНИЯ, РАЗДЕЛЕНИЕ КОТОРЫХ И ЕСТЬ СМЫСЛ ЭТОГО ФАЙЛА.

1. ДАТЧИК (99% работы) — НИЧЕГО НЕ РЕШАЕТ. Раз в cfg.alerts.poll_seconds
   собирает ctx (форма — docs/alerts_schema.md), зовёт alerts.evaluate() и при
   срабатывании печатает ОДНУ строку в stdout. Эта строка асинхронно будит
   модель: датчик запускается инструментом Monitor (или Bash с
   run_in_background) внутри активной сессии Claude Code, и каждая строка
   stdout приходит модели уведомлением. Пока условия молчат — расход нулевой.
   Все пороги, уровни и типы задаёт МОДЕЛЬ в alerts.json; код только сравнивает
   числа. Ни одного собственного торгового правила у датчика нет.

2. СТОП-КРАН — ЕДИНСТВЕННОЕ МЕСТО, ГДЕ СКРИПТ ДЕЙСТВУЕТ, и ровно два правила:
     1) equity пробила стену (день −3% / всего −6% минус буфер) → закрыть ВСЕ
        позиции, испустить событие priority=critical;
     2) обнаружена НАША позиция без стоп-лосса → поставить SL по записи
        журнала; не удалось → закрыть эту позицию по рынку.
   БОЛЬШЕ НИКАКИХ ТОРГОВЫХ ДЕЙСТВИЙ. Ни перевода в безубыток, ни частичных
   фиксаций, ни трейла, ни закрытия по time-stop, ни закрытия чужих позиций.
   Всё это приходит модели алертом, и решает она. Тест
   test_no_trading_actions_beyond_two_rules ловит нарушение ПО ФАКТУ ВЫЗОВА
   исполнителя, а не по списку имён методов.

ПОЧЕМУ СТОП-КРАН ВООБЩЕ ДЕЙСТВУЕТ. Стены −3%/−6% — единственные числа, у
которых нет альтернативной трактовки, и ждать пробуждения модели нельзя: за
это время стена будет пробита. Риск позиции без стопа неограничен и не
выражается числом (см. exposure.open_risk_usd), поэтому стоп восстанавливается
немедленно. Всё остальное — суждение, и оно принадлежит модели.

ИСПОЛНЕНИЕ — ЧЕРЕЗ ИНЖЕКТИРУЕМЫЙ УЗКИЙ ПРОТОКОЛ (TradeExecutor), а не прямыми
вызовами MT5: настоящий исполнитель появится в задаче 4.1 (trader_lib/
execute.py), дублировать его здесь нельзя. В тестах подставляется мок,
регистрирующий любой вызов — именно это делает возможной проверку дисциплины.
Датчик БЕЗ исполнителя не создаётся вовсе (проверка в __init__, а не только в
CLI): «датчик без стоп-крана» был бы вторым путём мимо правила, а второй путь
всегда находится.

ПОРЯДОК ТИКА ПОДЧИНЁН БЕЗОПАСНОСТИ, А НЕ УДОБСТВУ:
  шаг 1  — счёт, базы equity, открытые позиции → расчёт стены (свой try);
  шаг 2  — ЗАКРЫТИЕ по стене, немедленно, до любых диагностик;
  шаг 3  — журнал, экспозиция, вердикт гейта, снимок счёта (свой try);
  шаг 4  — alerts.json, ctx;
  шаг 5  — рассказ о действии стоп-крана (событие с лучшим доступным снимком);
  шаг 6  — правило 2 и алерты модели;
  шаг 7  — heartbeat.
Шагу 1 нужны ТОЛЬКО equity, базы и список позиций. Раньше он был склеен с
чтением journal.jsonl, экспозицией и orphan-сверкой — и битая строка в журнале
(его пишет модель каждую сессию) отменяла закрытие по стене: данные, к стене не
относящиеся, глушили единственное правило, которое обязано работать всегда.

СОБЫТИЙНЫЙ БЮДЖЕТ — ЗАЩИТА САМОГО МЕХАНИЗМА ПРОБУЖДЕНИЯ. Monitor
останавливается автоматически при переборе событий, поэтому строка печатается
только при срабатывании и только в пределах бюджета. Датчик обязан вести
состояния для alerts.event_budget (у двух из них намеренно нет дефолтов):
  - last_event_ts           — последнее доставленное NORMAL-событие;
  - last_critical_event_ts  — последнее доставленное CRITICAL-событие;
  - recent_event_ts         — все доставленные события за последнюю минуту;
  - events_today            — счётчик normal-яруса за день.
Они восстанавливаются при старте из alert_events.jsonl, чтобы перезапуск
датчика не обнулял защиту. Ловит порчу этого ведения только property-тест на
последовательности тиков (test_event_rate_bounded_over_time) — одиночная
проверка вызова не отличает «состояние ведётся» от «не ведётся». В тесте по
сценарию на каждое состояние: сценарий, в котором поток и так ограничен другим
ярусом, порчу «своего» счётчика поймать структурно не способен, и заявление
«property-тест сторожит все состояния» было бы для него ложным.

СОБЫТИЯ СОБСТВЕННОГО ПРОИЗВОДСТВА (стоп-кран) ПРИВЯЗАНЫ К ИЗМЕНЕНИЯМ И
ДЕЙСТВИЯМ, А НЕ К СОСТОЯНИЯМ: пробитая стена держится минутами, и печатать её
каждую секунду значило бы убить Monitor ровно тогда, когда модель нужнее
всего. Поэтому событие о стене печатается на фронте (не пробита → пробита) и
при КАЖДОЙ неудачной попытке закрытия (это новая информация: деньги всё ещё в
риске). НО ФРОНТ СЧИТАЕТСЯ ИЗРАСХОДОВАННЫМ ТОЛЬКО ПОСЛЕ ФАКТИЧЕСКОЙ ДОСТАВКИ.
Раньше он расходовался независимо от неё, и если событие попадало в
15-секундный интервал critical (а аномалия спреда, data_stale, gap и пробитие
стены — события одного рыночного шока), пробуждение о закрытии ВСЕХ позиций
терялось навсегда: скрипт распорядился деньгами, модель не узнала никогда. То
же правило — для единственного сообщения о чужой позиции без стопа.

ФРОНТА НЕДОСТАТОЧНО: ОН ЖИВЁТ, ПОКА ЖИВО СОСТОЯНИЕ. Стоп-кран закрыл нашу
позицию (брокер отклонил установку стопа), сообщение попало в интервал
critical, а позиция после закрытия исчезла — фронт исчез вместе с ней, и
модель не узнавала о закрытии НИКОГДА. Поэтому сообщение о СОСТОЯВШЕМСЯ
действии деньгами (решается по содержимому action — describes_money_action, —
а не по типу события; неудачная попытка считается наравне с удачной) встаёт в
очередь _undelivered и досылается, пока не будет доставлено. Правила очереди:
  - склейка по alert_id: пока стена пробита, рассказ собирается каждый тик, и
    без склейки очередь набирала бы по записи в секунду, а при переполнении
    выбрасывалось бы САМОЕ СТАРОЕ — единственное сообщение о закрытии;
  - бюджет спрашивается ДО попытки отправки: иначе ожидание доставки писало бы
    в alert_events.jsonl по записи в секунду всё время придушения;
  - доставленный отложенный рассказ гасит фронт стены — иначе об одном закрытии
    приходило бы два сообщения (отложенное и «свежее» по фронту);
  - переполнение (MAX_UNDELIVERED) громко попадает в errors и heartbeat;
  - в heartbeat есть pending_undelivered: модель видит нерассказанное ещё до
    того, как бюджет пропустит досылку.
Придушенные алерты модели по цене в очередь НЕ идут — их условие сработает
снова само; очередь только для того, что уже сделано с деньгами.

Попытки закрытия продолжаются, пока позиции живы — они придушены только по
частоте (STOP_VALVE_RETRY_SECONDS), чтобы не долбить брокера каждую секунду.
В журнал (alert_events.jsonl) пишется КАЖДОЕ действие, даже когда строка в
stdout придушена бюджетом: stdout — будильник, журнал — след. Отказ самой
записи в журнал попадает в errors тика и в heartbeat: действие деньгами без
следа обязано быть видно, а не тихо потеряться при «зелёном» пульсе.

HEARTBEAT ПОКАЗЫВАЕТ СВЕЖЕСТЬ ЗАЩИТЫ, А НЕ ПРОЦЕССА. Правило контура (задача
3.3): watch_heartbeat.json старше 90 с → модель считает себя незащищённой.
Поэтому поле ts обновляется ТОЛЬКО в тиках, где стена реально посчитана; живой
процесс со сломанным стоп-краном обязан выглядеть мёртвым, иначе модель часами
считает себя защищённой при неработающем правиле 1. Пульс самого процесса —
отдельное поле tick_utc.

ЧЕГО ЭТОТ МОДУЛЬ НАМЕРЕННО НЕ ДЕЛАЕТ:
  - не решает, что «плохо» на рынке (это ctx + пороги модели);
  - не трогает чужие позиции (без decision-записи) вне правила стены: стоп из
    журнала для них не существует, а выдумывать уровень запрещено — тот же
    вывод, что в scripts/risk_gate_cli.py (orphan → HALT_NEW, не FORCE_FLAT);
  - не использует cfg.alerts.critical_types: приоритет алерта задаёт модель в
    alerts.json, и evaluate() читает его оттуда; переписывать приоритет модели
    датчик не вправе. Собственные события стоп-крана всегда critical — скрипт
    действовал деньгами, и модель обязана узнать об этом сейчас.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# _baselines приватна по имени, но это ЕДИНСТВЕННЫЙ в пакете читатель
# day_baseline.json/account_init.json. Копия здесь означала бы второй источник
# баз equity: риск-гейт считал бы стену от одних чисел, а стоп-кран — от
# других, и расхождение вылезло бы ровно в момент пробития стены.
from scripts.risk_gate_cli import _baselines, build_gate_inputs               # noqa: E402
from trader_lib.account import account_snapshot                               # noqa: E402
from trader_lib.alerts import (                                               # noqa: E402
    evaluate,
    event_budget,
    load_alerts,
    write_alerts_atomic,
)
from trader_lib.config import load_config, state_dir                          # noqa: E402
from trader_lib.features import compute_tf_features                           # noqa: E402
from trader_lib.journal import append_alert_event, read_records               # noqa: E402
from trader_lib.model_session import effective as effective_model             # noqa: E402
from trader_lib.risk_gate import evaluate_gate, safe_evaluate_gate            # noqa: E402
from trader_lib.spread_gate import LiveSpreadWindow                           # noqa: E402
from trader_lib.allocation import events_quota, load_allocation               # noqa: E402
from trader_lib.workspace import TRADERS_SUBDIR                               # noqa: E402
from trader_lib.session import (                                              # noqa: E402
    current_phase,
    server_day_key,
    server_day_start_utc,
    session_gate,
)

UTC = dt.timezone.utc

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H4": 14400, "D1": 86400}

# ЭТАЛОННЫЙ ТФ — первый в списке. Контракт ctx (docs/alerts_schema.md) требует
# ОДНО значение atr/atr_pctile на символ, а не словарь по ТФ: ни один тип
# алерта, кроме trend_flips, не указывает tf в своих полях, и угадывать, какой
# ATR сравнивать с порогом модели, датчик не вправе. Выбор M5 — явное решение
# задачи 3.2 (тактический ТФ внутридневных гипотез, тот же, что по умолчанию у
# scripts/perceive.py), а не случайность. trend отдаётся по ВСЕМ ТФ списка,
# потому что trend_flips получает tf параметром.
DEFAULT_TIMEFRAMES = ("M5", "H1")

# Пороги «псевдо-торговых» аномалий, которые evaluate() принимает готовыми
# булями (см. docs/alerts_schema.md, раздел «Псевдо-торговая логика вынесена из
# evaluate() в ctx»): что считать разрывом бара и когда тик считается
# устаревшим — забота этой задачи, а не модуля alerts.
GAP_ATR_MULT = 0.5      # |open[-1] − close[-2]| > 0.5 ATR → разрыв
STALE_TF_MULT = 3.0     # последний бар старше 3 длительностей ТФ → данные стухли

MAX_SL_ATTEMPTS = 2            # сколько раз пытаться поставить SL, прежде чем закрыть
MAX_UNDELIVERED = 50           # потолок очереди недоставленных сообщений о действиях
STOP_VALVE_RETRY_SECONDS = 5   # не чаще этого повторять попытку по одному тикету
RECENT_WINDOW_SECONDS = 120    # хвост меток событий в памяти (окно 60с режет event_budget)
MT5_RETCODE_DONE = 10009

WALL_EVENT_TYPE = "wall_breach"
SL_EVENT_TYPE = "position_without_sl"
# событие живости: датчик сам замечает, что модель давно не будили
SILENCE_EVENT_TYPE = "watch_silence"
SILENCE_EVENT_ID = "watch-silence"

# живая база спреда: файл и период сброса на диск (Ф1)
LIVE_SPREAD_NAME = "spread_live.json"
LIVE_SPREAD_SAVE_SECONDS = 60

def _loaded_code_mtime():
    """Отпечаток ВСЕГО кода, загруженного в этот процесс (см. _write_heartbeat).

    РЕГРЕСС 2026-08-01, найден прогоном команды. Отпечаток снимался только с
    alert_watch.py, а торговая логика живёт в trader_lib: фикс «ценовые алерты
    не срабатывают на протухшем тике» лёг в trader_lib/alerts.py, работающий
    датчик продолжал стрелять по замёрзшей цене — и brief доложил бы «код
    свежий». В тот раз изъян не сработал только потому, что оба файла менялись
    вместе; правка одной библиотеки была бы невидима полностью.

    Python не перечитывает модули на лету, поэтому единственная защита от
    «правка есть, а в живом процессе её нет» — честно показать возраст самого
    свежего из загруженных файлов.
    """
    root = Path(__file__).resolve().parents[1]
    newest = os.path.getmtime(__file__)
    for path in (root / "trader_lib").glob("*.py"):
        newest = max(newest, os.path.getmtime(path))
    return newest


CODE_MTIME = _loaded_code_mtime()


class TradeExecutor(Protocol):
    """Всё, что стоп-крану позволено делать с деньгами. Узкий намеренно: чем
    меньше поверхность, тем меньше способов протащить сюда торговое суждение.
    Реализацию подставляет вызывающий (в задаче 4.1 — адаптер над
    trader_lib/execute.py, в тестах — мок). Результат: словарь с ok/retcode,
    либо исключение при отказе (см. _result_ok)."""

    def close_position(self, ticket): ...
    def modify_sl(self, ticket, new_sl): ...


EXECUTOR_METHODS = ("close_position", "modify_sl")


class _OneShotMarket:
    """Обёртка на ОДИН тик: account_info() и positions() уходят к брокеру ровно
    по одному разу, дальше отдаётся тот же снимок.

    Без неё стена считалась по одному чтению счёта, а снимок в событии — по
    второму: при дрейфе equity модель получала строку, где
    walls.daily_loss_pct=3.0 (позиции уже закрыты), а account.equity — прежние
    10000, то есть внутренне противоречивый снимок, по которому ей предписано
    решать без дополнительного шага. Тот же приём, что и явная передача
    positions в risk_gate_cli.run: один опрос — один снимок мира на тик.
    """

    def __init__(self, market):
        self._market = market
        self._account = None
        self._positions = None

    def account_info(self):
        if self._account is None:
            self._account = self._market.account_info()
        return self._account

    def positions(self):
        if self._positions is None:
            self._positions = self._market.positions()
        return self._positions

    def __getattr__(self, name):
        return getattr(self._market, name)


# --------------------------------------------------------------------------
# мелкие чистые помощники
# --------------------------------------------------------------------------

def _parse_utc(value):
    """ISO-8601 (в т.ч. с 'Z') → tz-aware datetime UTC. Наивное время — UTC:
    сенсорный контур целиком в UTC, гадать другую таймзону не из чего."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _num(value):
    """float или None — никогда исключение и никогда подстановка правдоподобного
    значения вместо отсутствующего (bool числом не считаем: True в поле цены —
    это порча данных, а не 1.0)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _result_ok(res):
    """True (успех) / False (явный отказ) / None (результат не опознан).

    Различие важно для правила 2: ЯВНЫЙ отказ модификации стопа → позиция
    закрывается немедленно; неопознанный результат считается лишь попыткой —
    следующий тик увидит, появился ли стоп на самом деле, и закроет позицию,
    если после MAX_SL_ATTEMPTS его так и нет. Трактовать неопознанное как
    успех нельзя (риск остался бы неограниченным молча), а как отказ —
    значило бы закрывать позицию из-за незнакомой формы ответа брокера, то
    есть необратимо действовать по неизвестному.
    """
    if res is None:
        return None
    if isinstance(res, bool):
        return res
    if isinstance(res, Mapping):
        if "ok" in res:
            return bool(res["ok"])
        if "retcode" in res:
            return res["retcode"] == MT5_RETCODE_DONE
        if res.get("error"):
            return False
    return None


def read_json(path):
    """(данные, ошибка). Отсутствие файла — не ошибка (None, None): состояние
    может ещё не существовать. Битый файл → (None, текст) — видно в heartbeat,
    но цикл не падает."""
    p = Path(path)
    if not p.exists():
        return None, None
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as e:
        return None, f"{p.name}: {e!r}"


def read_event_records(path):
    """(записи, сколько строк НЕ прочитано) из alert_events.jsonl.

    Построчно и терпимо к одной оборванной строке — в отличие от
    journal.read_records, который по замыслу всё-или-ничего. При восстановлении
    событийного бюджета потеря ВСЕХ записей из-за одной битой строки означала
    бы тихо снятые ограничения (fail-open), а это ровно то, что запрещает шапка
    модуля. Число непрочитанных строк возвращается наружу, чтобы вызывающий
    трактовал их максимально ограничительно и сказал об этом громко.
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 1  # файл есть, но недоступен — считаем, что событие там было
    out, corrupt = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            corrupt += 1
    return out, corrupt


def spread_median_points(data, symbol):
    """Медиана спреда по символу из spread_median.json.

    Файл пишет задача 5.2; здесь читаются две формы — {"XAUUSD": 9.0} и
    {"XAUUSD": {"median_points": 9.0}}. Ничего другого не угадывается: чего
    нет — то None, и spread_anomaly по такому символу уйдёт в skipped с
    причиной, а не сработает на выдуманном числе.
    """
    if not isinstance(data, Mapping):
        return None
    # РЕАЛЬНАЯ форма файла (её пишет trader_lib/spread_gate.update_medians):
    # медианы лежат под ключом "medians", а не в корне. Ридер искал их в корне
    # и всегда возвращал None — spread_anomaly не мог сработать НИ ПО ОДНОМУ
    # символу с момента написания. Найдено 2026-08-01 трейдером-субагентом на
    # прогоне команды; обе стороны были покрыты тестами, каждая своей формой
    # данных, и каждая подтверждала веру своего автора. Плоские формы
    # оставлены: файл мог быть собран старой версией.
    medians = data.get("medians")
    entry = None
    if isinstance(medians, Mapping):
        entry = medians.get(symbol)
    if entry is None:
        entry = data.get(symbol)
    if isinstance(entry, Mapping):
        entry = entry.get("median_points")
    return _num(entry)


def news_windows(data, *, now):
    """[{'name', 'minutes_until'}] из news_cache.json или None, если данных нет.

    ЧИТАЕМАЯ ЗДЕСЬ ФОРМА (до появления задачи 5.1, которая станет источником
    файла): {"events": [{"name": ..., "utc": "2026-07-27T13:10:00Z"}, ...]}
    либо просто список таких элементов; время берётся из ключа 'utc' или
    'from'. Файл, который не разбирается, даёт None (=«не знаю»), а не пустой
    список (=«новостей нет»): пустой список молча разрешил бы торговлю перед
    релизом, а это ровно тот fail-open, которого в пакете быть не должно.
    """
    items = data
    if isinstance(data, Mapping):
        # ключ берём по наличию, а не через `or`: пустой список — это «календарь
        # прочитан, событий нет», и он не имеет права превращаться в «не знаю»
        # (равно как и наоборот)
        key = next((k for k in ("events", "windows") if k in data), None)
        if key is None:
            return None
        items = data[key]
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        when = _parse_utc(item.get("utc") or item.get("from"))
        if when is None:
            return None
        out.append({"name": item.get("name") or item.get("title"),
                    "minutes_until": round((when - now).total_seconds() / 60.0, 1)})
    return out


def session_phase(now, cfg):
    """Ярлык фазы дня или None вне всех окон.

    Задача 5.3 перенесла логику в trader_lib/session.py — здесь остался тонкий
    вызов, чтобы фазы считались ровно так же, как их считает сессионный гейт.
    Второй копии этой арифметики в проекте быть не должно: расхождение
    проявилось бы как «гейт считает, что уже REVIEW, а алерт смены фазы ещё
    молчит».
    """
    return current_phase(utc_now=now, cfg=cfg)["phase"]


def decisions_by_ticket(records):
    """{trade_id: последняя decision-запись}. Связь decision.trade_id ↔ тикет
    позиции — та же, на которой стоят reconcile() и find_orphans()."""
    return {r["trade_id"]: r for r in records
            if r.get("type") == "decision" and r.get("trade_id") is not None}


def orphan_tickets(positions, decisions):
    """Тикеты открытых позиций, за которыми НЕТ decision-записи.

    Тождество ровно то же, что у scripts/close_watch.find_orphans
    (decision.trade_id == str(ticket)); тест
    test_orphan_detection_agrees_with_find_orphans держит их в согласии. Здесь
    оно считается по УЖЕ прочитанным записям, а не вторым чтением
    journal.jsonl: датчик крутится раз в секунду, файл растёт, и два разбора
    одного файла за тик — это не только двойная работа, но и два РАЗНЫХ
    момента чтения (модель дописывает журнал в это же время), то есть
    «принадлежность позиции» и «исходный стоп позиции» могли определяться по
    разным снимкам журнала.
    """
    return {p["ticket"] for p in positions if str(p["ticket"]) not in decisions}


def position_side(p):
    return {0: "buy", 1: "sell"}.get(p.get("type"), p.get("type"))


def position_r_multiple(p, decision):
    """Прогресс позиции в R или None (=«не знаю», алерт по такому полю не
    сработает).

    Знаменатель — ИСХОДНЫЙ риск из decision-записи (|price_open − sl журнала|),
    а не текущий стоп позиции: после переноса стопа в безубыток текущий
    знаменатель обнулился бы, и R «взорвался» бы ровно в тот момент, когда
    модель ждёт от него осмысленного числа. Запасной вариант — текущий стоп
    позиции (когда записи в журнале нет вовсе).
    """
    entry = _num(p.get("price_open"))
    price = _num(p.get("price_current"))
    ptype = p.get("type")
    sl0 = _num((decision or {}).get("sl")) or _num(p.get("sl"))
    if entry is None or price is None or not sl0 or ptype not in (0, 1):
        return None
    risk = abs(entry - sl0)
    if risk <= 0:
        return None
    direction = 1.0 if ptype == 0 else -1.0
    r = round(direction * (price - entry) / risk, 3)
    return 0.0 if r == 0 else r  # без «−0.0» в снимке, который читает модель


def position_beyond_sl(p):
    """Цена уже за стопом, а позиция ещё жива (ctx.positions[t].beyond_sl).
    Стопа нет → None: «за стопом» неопределимо, а не False."""
    sl = _num(p.get("sl")) or None
    price = _num(p.get("price_current"))
    if sl is None or price is None or p.get("type") not in (0, 1):
        return None
    return price < sl if p["type"] == 0 else price > sl


def position_opened_utc(p, decision, *, server_offset_hours):
    """Момент открытия позиции. Приоритет — ts decision-записи: его писал этот
    же пакет и он заведомо в UTC. Запасной вариант — поле time позиции MT5
    (epoch в СЕРВЕРНОМ времени, отсюда вычет смещения)."""
    ts = _parse_utc((decision or {}).get("ts"))
    if ts is not None:
        return ts
    raw = _num(p.get("time"))
    if raw is None:
        return None
    return dt.datetime.fromtimestamp(raw, UTC) - dt.timedelta(hours=server_offset_hours)


def _bar_time_utc(value, *, server_offset_hours):
    """Время бара MT5 (наивное, в часовом поясе сервера) → UTC."""
    stamp = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if getattr(stamp, "tzinfo", None) is not None:
        return stamp.astimezone(UTC)
    return stamp.replace(tzinfo=UTC) - dt.timedelta(hours=server_offset_hours)


def wall_state(numbers, limits):
    """Правило 1 стоп-крана: пробита ли стена по equity.

    numbers — {'equity', 'day_start_equity', 'initial_balance'} и НИЧЕГО
    больше: правилу, которое обязано работать всегда, нельзя зависеть от
    журнала, экспозиции и вердикта гейта.

    Считается НЕ повторной арифметикой, а вызовом самого risk_gate.evaluate_gate
    — единственного носителя лимитов в пакете: FORCE_FLAT он возвращает только
    на шаге 1 (стены), причём ДО любых валидаций входов, которые могли бы
    превратиться в HALT_NEW и замаскировать пробитую стену (см. шапку
    risk_gate.py).

    ЗДЕСЬ НАМЕРЕННО evaluate_gate, А НЕ safe_evaluate_gate. Обёртка превращает
    любое исключение в HALT_NEW, то есть в «стена не пробита» — стоп-кран
    оказался бы тихо выключен, а heartbeat при этом рапортовал бы, что стена
    проверена. Исключение обязано долететь до тика: тогда walls_checked=False,
    пульс защиты не обновляется и модель узнаёт, что незащищена (тест
    test_wall_error_makes_watch_visibly_blind).
    """
    v = evaluate_gate(equity=numbers["equity"], day_start_equity=numbers["day_start_equity"],
                      initial_balance=numbers["initial_balance"], limits=limits)
    breached = v["verdict"] == "FORCE_FLAT"
    return {"breached": breached, "blocked_by": v["blocked_by"],
            "daily_loss_pct": v["daily_loss_pct"], "total_loss_pct": v["total_loss_pct"],
            "reasons": v["reasons"] if breached else []}


def describes_money_action(event):
    """Состоялось ли в этом событии действие деньгами (или попытка).

    Решение по СОДЕРЖИМОМУ action, а не по типу события: новое правило
    стоп-крана (которого сейчас нет) не должно требовать правки этого места,
    чтобы его сообщения тоже не терялись. Неудачная попытка считается наравне
    с удачной — «стена пробита, закрыть не смог» модель обязана узнать тем
    более. done="none" (чужая позиция, к которой мы не прикасались) — не
    действие: там работает фронт _sl_reported по живому состоянию.
    """
    a = event.get("action") or {}
    if a.get("rule") == WALL_EVENT_TYPE:
        return bool(a.get("closed") or a.get("failed") or a.get("unverified"))
    return a.get("done") not in (None, "none")


def plan_sl_action(p, decision):
    """Что стоп-кран сделает с НАШЕЙ позицией без стопа: поставить SL из
    журнала или закрыть по рынку.

    Чистая функция — тестируется без цикла и без исполнителя. Закрытие
    выбирается в двух случаях: (а) пригодного уровня в журнале нет — выдумывать
    стоп запрещено; (б) записанный стоп уже пробит ценой, то есть сделка должна
    была быть закрыта им же (и брокер такой стоп всё равно отверг бы как
    «неверную сторону»).
    """
    sl = _num((decision or {}).get("sl"))
    price = _num(p.get("price_current"))
    ptype = p.get("type")
    if decision is None:
        return {"action": "close", "sl": None,
                "reason": "нет decision-записи — исходный стоп неизвестен"}
    if not sl:
        return {"action": "close", "sl": None,
                "reason": "в decision-записи нет пригодного sl"}
    if price is None or ptype not in (0, 1):
        return {"action": "close", "sl": sl,
                "reason": "неизвестны текущая цена или тип позиции — сторону стопа не проверить"}
    breached = price <= sl if ptype == 0 else price >= sl
    if breached:
        return {"action": "close", "sl": sl,
                "reason": f"стоп из журнала {sl} уже пробит ценой {price}"}
    return {"action": "modify", "sl": sl, "reason": None}


def build_symbol_ctx(market, cfg, symbol, *, now, timeframes, median_points):
    """Блок ctx['symbols'][symbol] (форма — docs/alerts_schema.md).

    Недостаток данных → None + reason, никогда угадывание: правило унаследовано
    от features.py и означает «не знаю» — алерт по такому полю не срабатывает и
    попадает в skipped.
    """
    out = {"price": None, "atr": None, "atr_pctile": None, "trend": {},
           # перцентиль ПО КАЖДОМУ ТФ — чтобы алерт мог назвать свой явно.
           # Одиночный atr_pctile выше остаётся эталонным (DEFAULT_TIMEFRAMES[0])
           # ради обратной совместимости уже взведённых алертов.
           "atr_pctile_by_tf": {},
           "spread_points": None, "spread_median_points": median_points,
           "bar_gap": None, "tick_stale": None, "last_bar_utc": None, "reason": None}
    P = cfg.perception
    si = market.symbol_info(symbol)
    out["spread_points"] = _num(si.get("spread"))
    ref = timeframes[0]
    for tf in timeframes:
        bars = market.copy_rates(symbol, tf, P.atr_pctile_lookback + 50)
        closed = bars.iloc[:-1] if P.use_closed_bars_only else bars
        feats = compute_tf_features(
            closed, point=si["point"], atr_period=P.atr_period,
            momentum_bars=P.momentum_bars, range_bars=P.range_bars,
            ema_fast=P.ema_fast, ema_slow=P.ema_slow,
            atr_pctile_lookback=P.atr_pctile_lookback)
        out["trend"][tf] = feats["trend"]
        out["atr_pctile_by_tf"][tf] = feats["atr_pctile"]
        if tf != ref or len(bars) == 0:
            continue
        out["price"] = _num(bars["close"].iloc[-1])
        out["atr"] = feats["atr_price"]
        out["atr_pctile"] = feats["atr_pctile"]
        out["reason"] = feats["reason"]
        if feats["atr_price"] and len(bars) >= 2:
            gap = abs(_num(bars["open"].iloc[-1]) - _num(bars["close"].iloc[-2]))
            out["bar_gap"] = gap > GAP_ATR_MULT * feats["atr_price"]
        last_bar = _bar_time_utc(bars["time"].iloc[-1],
                                 server_offset_hours=cfg.risk.server_utc_offset_hours)
        out["last_bar_utc"] = last_bar.isoformat()
        out["tick_stale"] = (now - last_bar).total_seconds() > STALE_TF_MULT * TF_SECONDS[tf]
    return out


# --------------------------------------------------------------------------
# датчик
# --------------------------------------------------------------------------

class AlertWatch:
    """Цикл опроса: стена → закрытие → диагностика → ctx → evaluate → строка.

    Вся вычислительная часть вынесена в функции модуля и в методы, принимающие
    now параметром: tick() вызывается тестами напрямую, цикл run() ничего, кроме
    сна и часов, не добавляет.
    """

    def __init__(self, market, cfg, *, executor, state_dir_path=None, out=None, log=None,
                 timeframes=DEFAULT_TIMEFRAMES):
        # Без исполнителя датчик не создаётся: иначе стоп-кран падал бы
        # AttributeError в момент пробитой стены, то есть «датчик без
        # стоп-крана» существовал бы как рабочий режим — второй путь мимо
        # правила. Проверка здесь, а не только в CLI, потому что конструктор —
        # это и есть точка, где такой режим возникал.
        if executor is None:
            raise ValueError(
                "executor обязателен: датчик без стоп-крана не запускается (шапка модуля). "
                "В задаче 4.1 сюда подставляется адаптер над trader_lib/execute.py, "
                "в тестах — мок.")
        missing = [m for m in EXECUTOR_METHODS if not callable(getattr(executor, m, None))]
        if missing:
            raise ValueError(f"executor не реализует протокол TradeExecutor: нет {missing}")

        self.market = market
        self.cfg = cfg
        self.executor = executor
        self.sd = Path(state_dir_path or state_dir(cfg))
        self.alerts_path = self.sd / "alerts.json"
        self.journal_path = self.sd / "journal.jsonl"
        self.events_path = self.sd / "alert_events.jsonl"
        self.heartbeat_path = self.sd / "watch_heartbeat.json"
        self.out = out if out is not None else sys.stdout
        self.log = log if log is not None else sys.stderr
        self.timeframes = tuple(timeframes)

        self._alerts_by_trader = {}
        # сколько событий за серверные сутки доставлено каждому трейдеру (Ф5)
        self._events_by_trader = {}
        self._alerts_sig = None
        self._alerts_error = None
        # состояния событийного бюджета (см. шапку модуля). Ведутся только по
        # ФАКТИЧЕСКИ доставленным событиям и восстанавливаются из журнала.
        self._last_event_ts = None
        self._last_critical_event_ts = None
        self._recent_event_ts = []
        self._events_today = 0
        self._events_day = None
        self._restored = False
        self._tick_no = 0
        # момент старта: от него отсчитывается тишина, пока не было ни одного
        # доставленного события
        self._started_at = None
        self._tick_errors = []
        # свежесть ЗАЩИТЫ: момент последнего тика, в котором стена реально
        # посчитана (см. шапку про heartbeat)
        self._walls_ok_ts = None
        # ЖИВАЯ база спреда (Ф1). Датчик читает спред на каждом тике ради
        # spread_anomaly и до сих пор выбрасывал значение сразу после проверки.
        # Барная медиана меряет спред на ЗАКРЫТИИ свечи — в самый спокойный
        # момент, — а решения принимаются в активные: за 2026-07-27..31 это
        # дало 9 отклонённых входов, шесть из них при ×1.05, включая обе
        # упущенные прибыльные сделки недели. Собирать базу должен тот, кто и
        # так смотрит каждую секунду; модель просыпается 10-20 раз в сутки и
        # для этого непригодна.
        self.live_spread = LiveSpreadWindow.load(
            Path(self.sd) / LIVE_SPREAD_NAME)
        self._live_spread_saved_at = None
        # сколько позиций датчик видел у брокера в последний раз, когда
        # терминал ответил. Это СОБСТВЕННОЕ прямое наблюдение, а не запись в
        # журнале (журналу правило тишины намеренно не доверяет). Нужно на
        # выходных: терминал не отвечает двое суток, positions приходит None,
        # и без этой памяти правило будит каждые три часа впустую.
        self._last_seen_positions = None
        # состояние стоп-крана между тиками
        self._wall_reported = False
        self._sl_attempts = {}
        self._sl_reported = set()
        self._last_attempt = {}
        # сообщения о СОСТОЯВШИХСЯ действиях деньгами, которые придушил бюджет:
        # досылаются, пока не будут доставлены, независимо от того, существует
        # ли ещё позиция (см. шапку модуля)
        self._undelivered = []

    # --- служебное -------------------------------------------------------

    def _fail(self, message, exc=None):
        """Единственный путь для «что-то не получилось»: и в stderr, и в список
        ошибок тика — а значит, и в heartbeat, и в ответе tick().

        Раньше часть отказов (запись события в журнал, вызов исполнителя,
        данные по символу) уходила только в stderr: скрипт распоряжался
        деньгами, след терялся, а пульс рапортовал «всё хорошо».
        """
        self._tick_errors.append(message)
        print(f"[alert_watch] {message}", file=self.log)
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=self.log)
        try:
            self.log.flush()
        except Exception:  # noqa: BLE001 - поток лога не имеет права ронять цикл
            pass

    # --- событийный бюджет ------------------------------------------------

    def _restore_budget_state(self, now):
        """Состояния бюджета из alert_events.jsonl — перезапуск датчика не
        обнуляет защиту механизма пробуждения.

        Запись без priority (чужой/старый формат) трактуется МАКСИМАЛЬНО
        ОГРАНИЧИТЕЛЬНО: считается и за normal, и за critical. Непрочитанная
        строка — тоже: считаем, что событие было, и что оно было ТОЛЬКО ЧТО.
        Отсутствующее состояние не имеет права читаться как «ограничивать
        нечего» — ровно на этом уже один раз молча отключалась защита (см.
        docs/alerts_schema.md, раздел про дефолты event_budget).
        """
        self._events_day = self._server_day(now)
        records, corrupt = [], 0
        for path in self._event_sources():
            recs, bad = read_event_records(path)
            records.extend(recs)
            corrupt += bad
        # день событийного бюджета — серверный (задача 5.3), как и торговый
        day_start = server_day_start_utc(
            utc_now=now, offset_hours=self.cfg.risk.server_utc_offset_hours,
            reset_hour=self.cfg.risk.server_day_reset_hour)
        for rec in records:
            if rec.get("type") != "alert_event" or rec.get("delivered") is False:
                continue
            ts = _parse_utc(rec.get("fired_utc") or rec.get("ts"))
            if ts is None:
                continue
            priority = rec.get("priority")
            if priority != "critical":
                if ts >= day_start:
                    self._events_today += 1
                self._last_event_ts = max(self._last_event_ts or ts, ts)
            if priority != "normal":
                self._last_critical_event_ts = max(self._last_critical_event_ts or ts, ts)
            self._recent_event_ts.append(ts)
        if corrupt:
            self._fail(f"журнал событий: {corrupt} строк не прочитано — бюджет восстановлен "
                       "по самой ограничительной трактовке (событие было, и было только что)")
            self._last_event_ts = now
            self._last_critical_event_ts = now
            self._events_today += corrupt
            self._recent_event_ts.extend([now] * corrupt)
        self._prune_recent(now)

    def _event_sources(self):
        """Все журналы событий: корневой + по одному на трейдера.

        РЕГРЕСС 2026-08-03, первый живой день команды. Читался только корневой,
        а события трейдеров пишутся в traders/<имя>/alert_events.jsonl. Отсюда
        два отказа сразу:

        1. ПРАВИЛО ТИШИНЫ кричало «волки». В 11:41 оно доложило «модель молчит
           305 минут» — при том что за день было шесть доставленных событий и
           три трейдера непрерывно работали. Отсчёт шёл от 06:36, когда стрелял
           последний КОРНЕВОЙ будильник. В командном режиме такое повторялось бы
           каждые три часа, тратя critical-ярус на ложную тревогу; а поскольку
           этот ярус существует ровно для того, чтобы отличить «кончился бюджет»
           от «механизм сломался», ложные срабатывания разрушают именно то
           различение, ради которого правило и написано.

        2. СОБЫТИЙНЫЙ БЮДЖЕТ недосчитывал события трейдеров, то есть защита от
           перерасхода пробуждений была тем слабее, чем активнее команда.

        Тот же класс, что корневой alerts.json (регресс 01.08), декоративный
        --trader в пяти скриптах и report.py, писавший в общий журнал: код не
        обновили под командный режим, а тесты остались зелёными, потому что
        проверяли одиночный.
        """
        paths = [self.events_path]
        traders_dir = self.sd / "traders"
        if traders_dir.is_dir():
            paths.extend(sorted(traders_dir.glob("*/alert_events.jsonl")))
        return paths

    def _server_day(self, now):
        return server_day_key(utc_now=now,
                              offset_hours=self.cfg.risk.server_utc_offset_hours,
                              reset_hour=self.cfg.risk.server_day_reset_hour)

    def _prune_recent(self, now):
        self._recent_event_ts = [t for t in self._recent_event_ts
                                 if (now - t).total_seconds() <= RECENT_WINDOW_SECONDS]

    def _budget(self, now):
        # день событийного бюджета — СЕРВЕРНЫЙ (задача 5.3): дневной лимит
        # событий обязан обнуляться там же, где обнуляется торговый день, иначе
        # в 21:00–24:00 UTC (при смещении +3) счётчик жил бы по вчерашнему дню
        day = self._server_day(now)
        if self._events_day != day:
            self._events_day = day
            self._events_today = 0
            self._events_by_trader = {}
        self._prune_recent(now)
        return event_budget(self._events_today, self.cfg.alerts, now=now,
                            last_event_ts=self._last_event_ts,
                            last_critical_event_ts=self._last_critical_event_ts,
                            recent_event_ts=self._recent_event_ts)

    def _can_deliver(self, priority, now):
        """Пропустит ли бюджет событие этого приоритета прямо сейчас.
        Возвращает (можно, причина отказа). Единственный расчёт этого решения:
        им пользуются и _emit, и досылка недоставленного — иначе досылка имела
        бы собственную трактовку бюджета и обходила бы потолок в минуту."""
        b = self._budget(now)
        tier_ok = b["critical_allowed"] if priority == "critical" else b["allowed"]
        tier_reason = b["critical_reason"] if priority == "critical" else b["reason"]
        if not tier_ok:
            return False, tier_reason
        if b["hard_cap_remaining"] <= 0:
            return False, b["hard_cap_reason"]
        return True, None

    def _record_delivery(self, priority, now):
        """Обновление состояний ПОСЛЕ фактической доставки события."""
        if priority == "critical":
            self._last_critical_event_ts = now
        else:
            self._last_event_ts = now
            self._events_today += 1
        self._recent_event_ts.append(now)

    def _last_event_utc(self):
        stamps = [t for t in (self._last_event_ts, self._last_critical_event_ts) if t]
        return max(stamps) if stamps else None

    # --- alerts.json ------------------------------------------------------

    def _alert_sources(self):
        """[(трейдер|None, путь)] — чьи условия слушать в этом тике.

        ОДИН ДАТЧИК НА КОМАНДУ, А НЕ ПО ОДНОМУ НА ТРЕЙДЕРА. Три процесса
        означали бы три стоп-крана с разными представлениями о позициях: при
        пробое стены каждый независимо бросился бы закрывать одно и то же.
        Пульс защиты тоже обязан быть единым — модель должна видеть одно
        состояние защищённости, а не три.

        Каталог traders/ отсутствует — одиночный режим, файл в корне, как
        было до появления команды.
        """
        # КОРЕНЬ ЧИТАЕТСЯ ВСЕГДА, а команда его ДОПОЛНЯЕТ. РЕГРЕСС 2026-08-01,
        # найден подготовкой к понедельнику: источники переключались на
        # командные, как только появлялся первый traders/<имя>/alerts.json, и
        # корневой файл переставал читаться МОЛЧА. Будильник «начало недели»,
        # взведённый в корне до появления команды, не сработал бы вовсе — а
        # узнать об этом было неоткуда: датчик бодро рапортовал о тринадцати
        # источниках, среди которых его просто не было.
        #
        # В корне живут условия, не принадлежащие никакому трейдеру:
        # пробуждение директора, начало сессии, всё общекомандное.
        sources = []
        team_root = Path(self.sd) / TRADERS_SUBDIR
        if team_root.exists():
            for d in sorted(team_root.iterdir()):
                if d.is_dir() and (d / "alerts.json").exists():
                    sources.append((d.name, d / "alerts.json"))
        if self.alerts_path.exists() or not sources:
            sources.append((None, self.alerts_path))
        return sources

    def _alerts_signature(self):
        parts = []
        for trader, path in self._alert_sources():
            try:
                st = os.stat(path)
            except OSError:
                parts.append((trader, None))
                continue
            parts.append((trader, st.st_mtime_ns, st.st_size))
        return tuple(parts)

    def _reload_alerts(self, now):
        """Перечитывает alerts.json, когда файл изменился (модель переписала
        условия в конце своего цикла) — перезапуск датчика для этого не нужен.
        Между тиками состояние живёт в памяти, на диск уходит только при
        изменении (см. _persist_alerts)."""
        sig = self._alerts_signature()
        if sig == self._alerts_sig and self._alerts_by_trader:
            return
        self._alerts_sig = sig
        self._alerts_by_trader = {
            trader: load_alerts(path, now=now)
            for trader, path in self._alert_sources()}
        self._alerts_error = None

    @property
    def _alerts(self):
        """Сводный набор для кода, которому авторство не нужно: символы для
        контекста, счётчики в heartbeat, диагностика тишины."""
        docs = list(self._alerts_by_trader.values())
        if not docs:
            return None
        if len(docs) == 1:
            return docs[0]
        merged = {"version": 1, "alerts": [], "skipped": []}
        for doc in docs:
            merged["alerts"] += doc.get("alerts") or []
            merged["skipped"] += doc.get("skipped") or []
        return merged

    def _persist_alerts(self, before, after, path=None):
        """Состояние разоружения/памяти алертов на диск — иначе оно теряется
        при перезапуске датчика (docs/alerts_schema.md).

        Пишем ТОЛЬКО при изменении _state и только если файл на диске не
        изменился с момента нашей загрузки: иначе тик, начавшийся до того, как
        модель переписала alerts.json, затёр бы её свежие условия своим старым
        снимком. Пустой набор (файла нет либо он истёк) не пишем никогда — это
        затёрло бы файл модели.
        """
        if not after.get("alerts"):
            return
        if [a.get("_state") for a in before.get("alerts", [])] == \
                [a.get("_state") for a in after["alerts"]]:
            return
        if self._alerts_signature() != self._alerts_sig:
            return
        payload = {k: v for k, v in after.items() if k != "skipped"}
        write_alerts_atomic(path or self.alerts_path, payload)
        self._alerts_sig = self._alerts_signature()

    # --- опрос мира -------------------------------------------------------

    def _poll_walls(self, market, now):
        """ШАГ 1: ровно то, без чего нельзя проверить стену и закрыть позиции —
        счёт, базы equity и список открытых позиций. Ни журнала, ни экспозиции,
        ни гейта: см. шапку модуля про склеенный опрос."""
        acc = market.account_info()
        equity = acc["equity"]
        day_start_equity, initial_balance = _baselines(str(self.sd), equity, now=now,
                                                       cfg=self.cfg)
        numbers = {"equity": equity, "day_start_equity": day_start_equity,
                   "initial_balance": initial_balance}
        return {"account_info": acc, "positions": market.positions(),
                "numbers": numbers, "wall": wall_state(numbers, self.cfg.risk)}

    def _poll_rest(self, market, walls, now):
        """ШАГ 3: диагностика, без которой стоп-кран работает, но модель хуже
        видит мир — журнал, экспозиция, вердикт гейта, снимок счёта."""
        positions = walls["positions"]
        records = read_records(self.journal_path)
        decisions = decisions_by_ticket(records)
        inputs = build_gate_inputs(market, self.cfg, records, now=now, positions=positions)
        return {
            "records": records,
            "decisions": decisions,
            "orphan_tickets": orphan_tickets(positions, decisions),
            "inputs": inputs,
            "account": account_snapshot(
                walls["account_info"], day_start_equity=walls["numbers"]["day_start_equity"],
                initial_balance=walls["numbers"]["initial_balance"],
                profit_target_pct=self.cfg.goal["profit_target_pct"],
                daily_limit_pct=self.cfg.risk.daily_loss_limit_pct,
                total_limit_pct=self.cfg.risk.total_loss_limit_pct, positions=positions),
            "gate": safe_evaluate_gate(**inputs),
        }

    def _save_live_spread(self, now):
        """Сброс живого окна на диск не чаще раза в LIVE_SPREAD_SAVE_SECONDS."""
        last = self._live_spread_saved_at
        if last is not None and (now - last).total_seconds() < LIVE_SPREAD_SAVE_SECONDS:
            return False
        self.live_spread.save(Path(self.sd) / LIVE_SPREAD_NAME)
        self._live_spread_saved_at = now
        return True

    def _build_ctx(self, market, walls, rest, now):
        """ctx для evaluate(). Работает и без rest (журнал не прочитан): тогда
        нет decision-записей и вердикта гейта, и алерты, которым они нужны,
        уйдут в skipped с причиной — но алерты по цене, спреду, разрыву и
        свежести данных продолжают будить модель."""
        positions = walls["positions"]
        decisions = (rest or {}).get("decisions", {})
        alert_symbols = {a.get("symbol") for a in (self._alerts or {}).get("alerts", [])
                         if a.get("symbol")}
        symbols = sorted(alert_symbols | {p["symbol"] for p in positions})

        medians, median_error = read_json(self.sd / "spread_median.json")
        news_raw, news_error = read_json(self.sd / "news_cache.json")
        for err in (median_error, news_error):
            if err:
                self._fail(f"состояние не прочитано: {err}")

        sym_ctx = {}
        for s in symbols:
            try:
                sym_ctx[s] = build_symbol_ctx(
                    market, self.cfg, s, now=now, timeframes=self.timeframes,
                    median_points=spread_median_points(medians, s))
                # живая база спреда копится ровно из того замера, который
                # датчик и так только что сделал — лишних обращений к рынку нет
                self.live_spread.observe(
                    s, sym_ctx[s].get("spread_points"), now=now)
            except Exception as e:  # noqa: BLE001 - один плохой символ не слепит остальные
                self._fail(f"символ {s}: данные не собраны: {e!r}", e)
                sym_ctx[s] = {"price": None, "atr": None, "atr_pctile": None, "trend": {},
                              "spread_points": None, "spread_median_points": None,
                              "bar_gap": None, "tick_stale": None, "last_bar_utc": None,
                              "reason": repr(e)}

        pos_ctx = {}
        for p in positions:
            decision = decisions.get(str(p["ticket"]))
            pos_ctx[p["ticket"]] = {
                "r_multiple": position_r_multiple(p, decision),
                "opened_utc": position_opened_utc(
                    p, decision, server_offset_hours=self.cfg.risk.server_utc_offset_hours),
                "beyond_sl": position_beyond_sl(p),
            }

        return {"symbols": sym_ctx, "positions": pos_ctx,
                "gate_verdict": ((rest or {}).get("gate") or {}).get("verdict"),
                "session_phase": session_phase(now, self.cfg),
                "news_windows": news_windows(news_raw, now=now),
                "last_event_utc": self._last_event_utc()}

    # --- снимок и событие --------------------------------------------------

    def _snapshot(self, walls, rest, ctx, now, *, symbol=None):
        """Готовый снимок в событии — чтобы модель, разбуженная строкой, могла
        решать сразу, не тратя отдельный шаг на «посмотреть, что там». Объём
        ограничен намеренно: счёт, стены, вердикт гейта, фаза, открытые позиции
        и блок ТОЛЬКО того символа, по которому сработал алерт.

        Всё в снимке посчитано по ОДНОМУ опросу счёта и позиций за тик
        (_OneShotMarket): equity в account и equity, по которой посчитана
        стена, — одно и то же число. То, что в этот тик собрать не удалось
        (журнал, гейт, ctx), отсутствует явно (None), а не подменяется прошлыми
        значениями.
        """
        # walls=None — терминал недоступен в этот тик. Снимок всё равно нужен:
        # событие живости (_rule_silence) обязано уйти именно тогда, когда стену
        # посчитать не удалось, — молчание при мёртвом терминале опаснее всего.
        # Отсутствующее показывается как None, а не подменяется прошлым.
        walls = walls or {}
        numbers = walls.get("numbers") or {}
        wall = walls.get("wall") or {}
        buf = self.cfg.risk.flatten_buffer_pct
        rest = rest or {}
        account = {k: v for k, v in (rest.get("account") or {}).items()
                   if k != "open_positions"}
        account.setdefault("equity", numbers.get("equity"))
        account["day_start_equity"] = numbers.get("day_start_equity")
        account["initial_balance"] = numbers.get("initial_balance")
        gate = rest.get("gate")
        inputs = rest.get("inputs") or {}
        decisions = rest.get("decisions", {})
        orphans = rest.get("orphan_tickets")
        pos_ctx = (ctx or {}).get("positions", {})

        positions = []
        for p in walls.get("positions") or []:
            extra = pos_ctx.get(p["ticket"], {})
            opened = extra.get("opened_utc")
            positions.append({
                "ticket": p["ticket"], "symbol": p.get("symbol"), "side": position_side(p),
                "volume": p.get("volume"), "price_open": p.get("price_open"),
                "sl": p.get("sl"), "tp": p.get("tp"),
                "price_current": p.get("price_current"), "profit": p.get("profit"),
                "r_multiple": extra.get("r_multiple", position_r_multiple(
                    p, decisions.get(str(p["ticket"])))),
                "beyond_sl": extra.get("beyond_sl", position_beyond_sl(p)),
                "opened_utc": opened.isoformat() if opened else None,
                # есть ли за позицией decision-запись (обратное — orphan);
                # None = журнал в этот тик не прочитан, принадлежность неизвестна
                "in_journal": None if orphans is None else p["ticket"] not in orphans,
            })

        symbols = (ctx or {}).get("symbols", {})
        if symbol and symbol in symbols:
            symbols = {symbol: symbols[symbol]}

        news = (ctx or {}).get("news_windows")
        upcoming = [w for w in news if (w.get("minutes_until") or -1) >= 0] if news else []

        return {
            "ts": now.isoformat(),
            "account": account,
            # при недоступном терминале стена не посчитана: показываем None, а
            # не «не пробита» — разница между «проверено» и «неизвестно» и есть
            # то, ради чего событие живости вообще уходит
            "walls": {"breached": wall.get("breached"),
                      "blocked_by": wall.get("blocked_by"),
                      "daily_loss_pct": wall.get("daily_loss_pct"),
                      "total_loss_pct": wall.get("total_loss_pct"),
                      "daily_flat_pct": self.cfg.risk.daily_loss_limit_pct - buf,
                      "total_flat_pct": self.cfg.risk.total_loss_limit_pct - buf},
            "gate": None if gate is None else {
                "verdict": gate["verdict"], "blocked_by": gate["blocked_by"],
                "max_risk_per_trade_usd": gate["max_risk_per_trade_usd"],
                "require_setup_status": gate["require_setup_status"],
                "planned_only": gate["planned_only"], "reasons": gate["reasons"]},
            "open_risk_usd": inputs.get("open_risk_usd"),
            "unprotected_positions": inputs.get("unprotected_positions"),
            "orphan_tickets": None if orphans is None else sorted(orphans),
            "session_phase": (ctx or {}).get("session_phase"),
            "symbols": symbols,
            "positions": positions,
            "news_next": min(upcoming, key=lambda w: w["minutes_until"]) if upcoming else None,
            "watch": {"tick": self._tick_no, "events_today": self._events_today,
                      "poll_seconds": self.cfg.alerts.poll_seconds,
                      "errors": list(self._tick_errors)},
        }

    def _emit(self, event, *, now, budgeted, deliver=True):
        """Единственная точка, где событие уходит модели и в журнал. Возвращает
        ФАКТ доставки — вызывающий обязан им пользоваться: на нём держится
        правило «фронт израсходован только после доставки».

        budgeted=False — бюджет уже применён внутри evaluate() (алерты модели).
        budgeted=True  — собственное событие стоп-крана, бюджет проверяем здесь.
        deliver=False  — печатать не нужно (повтор действия без новой
                         информации), но запись в журнал делается всё равно.
        """
        priority = event["priority"]
        allowed, reason = deliver, None
        if deliver and budgeted:
            allowed, reason = self._can_deliver(priority, now)
        elif not deliver:
            reason = "повтор действия стоп-крана без новой информации"

        if allowed:
            print(json.dumps(event, ensure_ascii=False, default=str), file=self.out, flush=True)
            self._record_delivery(priority, now)
        elif deliver and budgeted and describes_money_action(event) \
                and not event.get("delayed_report"):
            # Состоявшееся действие деньгами не имеет права быть потерянным.
            # Фронты (_wall_reported/_sl_reported) удерживают повтор рассказа,
            # пока живо СОСТОЯНИЕ; здесь удерживается сам ФАКТ действия —
            # позиция после закрытия исчезает, и фронт вместе с ней (см. шапку
            # модуля, раздел про очередь недоставленного).
            self._enqueue_undelivered(event, now)

        event["delivered"] = allowed
        event["suppressed_reason"] = reason
        self._journal_event(event)
        return allowed

    def _enqueue_undelivered(self, event, now):
        """Придушенное сообщение о состоявшемся действии — в очередь досылки.

        Дедупликация по alert_id обязательна: пока стена пробита, рассказ
        собирается каждый тик, и без склейки очередь набирала бы по записи в
        секунду, а при переполнении выбрасывалось бы САМОЕ СТАРОЕ — то есть
        первое, единственное сообщение о фактическом закрытии. Считаем повторы
        счётчиком: модель увидит, сколько тиков она была не в курсе.
        """
        for item in self._undelivered:
            if item["event"]["alert_id"] == event["alert_id"]:
                item["repeats"] += 1
                item["last_seen_utc"] = now.isoformat()
                return
        self._undelivered.append({"event": event, "enqueued_utc": now.isoformat(),
                                  "last_seen_utc": now.isoformat(),
                                  "attempts": 0, "repeats": 0})
        if len(self._undelivered) > MAX_UNDELIVERED:
            dropped = self._undelivered.pop(0)
            # тихо потерять сообщение о действии деньгами нельзя даже при
            # переполнении: остаётся хотя бы громкая ошибка в пульсе
            self._fail(f"очередь недоставленных сообщений переполнена "
                       f"({MAX_UNDELIVERED}): выброшено {dropped['event']['alert_id']} "
                       f"от {dropped['enqueued_utc']} — модель об этом действии не узнает")

    def _flush_undelivered(self, now):
        """Досылка того, что бюджет придушил ранее. Идёт в начале тика,
        старейшее первым, и останавливается на первом отказе бюджета: событие
        помечено delayed_report, чтобы модель не приняла отложенный рассказ за
        второе действие."""
        sent = []
        while self._undelivered:
            item = self._undelivered[0]
            ok, _ = self._can_deliver(item["event"]["priority"], now)
            if not ok:
                break
            event = dict(item["event"])
            event["delayed_report"] = True
            event["original_fired_utc"] = item["event"]["fired_utc"]
            event["reported_utc"] = now.isoformat()
            event["delayed_by_seconds"] = round(
                (now - _parse_utc(item["event"]["fired_utc"])).total_seconds(), 1)
            event["suppressed_repeats"] = item["repeats"]
            event["note"] = ("ОТЛОЖЕННЫЙ РАССКАЗ, действие уже состоялось: "
                             + (item["event"].get("note") or ""))
            item["attempts"] += 1
            if self._emit(event, now=now, budgeted=True):
                self._undelivered.pop(0)
                sent.append(event)
                if event["alert_type"] == WALL_EVENT_TYPE:
                    # Модель узнала о закрытии по стене — фронт живого рассказа
                    # израсходован. Без этого о том же закрытии приходило бы
                    # второе сообщение: отложенное здесь и «свежее» по фронту.
                    # Гасится только фронт стены: сообщения по правилу 2 в
                    # очередь попадают лишь когда мы ДЕЙСТВОВАЛИ, а фронт
                    # _sl_reported существует для чужих позиций, которых мы не
                    # трогаем, — путать их нельзя.
                    self._wall_reported = True
            else:
                break
        return sent

    def _model_id(self):
        """Кому адресованы события: модель, объявившаяся в этом сеансе.

        Читается КАЖДЫЙ РАЗ, а не кэшируется на старте: датчик живёт часами и
        переживает смену сеанса — если модель объявилась заново (другая модель
        на другом ПК или после перезапуска Claude Code), события должны
        подписываться новым именем, а не тем, что было при запуске процесса.
        Отказ чтения не имеет права уронить датчик: подпись важна, но стена
        важнее."""
        try:
            model_id, _profile = effective_model(self.sd, self.cfg)
            return model_id
        except Exception:  # noqa: BLE001
            return self.cfg.model.id

    def _journal_event(self, event):
        rec = {"alert_id": event["alert_id"], "alert_type": event["alert_type"],
               "model_id": self._model_id(), "priority": event["priority"],
               "fired_utc": event["fired_utc"], "symbol": event.get("symbol"),
               "ticket": event.get("ticket"), "note": event.get("note"),
               "detail": event.get("detail"), "action": event.get("action"),
               "delivered": event["delivered"],
               "suppressed_reason": event.get("suppressed_reason"),
               "trader": event.get("trader"),
               "snapshot": event.get("snapshot")}
        # СОБЫТИЕ ЛОЖИТСЯ В ЖУРНАЛ СВОЕГО ТРЕЙДЕРА (Ф3). РЕГРЕСС 2026-08-01,
        # найден первой обкаткой команды: всё писалось в общий файл, и
        # review.py --trader <имя> показал бы НОЛЬ пробуждений, выглядя
        # исправным. Тот же дефект, что чинился 27.07 («метрика пробуждений
        # всегда показывала ноль»), в командной форме: тогда события искали не
        # в том файле, теперь клали не в тот.
        path = self.events_path
        trader = event.get("trader")
        if trader:
            path = Path(self.sd) / TRADERS_SUBDIR / trader / "alert_events.jsonl"
        try:
            append_alert_event(path, rec)
        except Exception as e:  # noqa: BLE001 - отказ журнала не роняет датчик, но виден
            # действие деньгами без следа обязано быть видно модели, а не тонуть
            # в stderr при «зелёном» пульсе
            self._fail(f"запись события {event['alert_id']} в журнал не удалась: {e!r}", e)

    # --- стоп-кран --------------------------------------------------------

    def _may_attempt(self, rule, ticket, now):
        """Придушивает ПОВТОРНЫЕ попытки по одному тикету (первая — всегда
        сразу): при отказе брокера повтор раз в секунду ничего не чинит, но
        заваливает и терминал, и журнал."""
        last = self._last_attempt.get((rule, ticket))
        if last is not None and (now - last).total_seconds() < STOP_VALVE_RETRY_SECONDS:
            return False
        self._last_attempt[(rule, ticket)] = now
        return True

    def _call_executor(self, method, *args, rule, now, actions):
        entry = {"rule": rule, "method": method, "args": list(args), "utc": now.isoformat()}
        try:
            res = getattr(self.executor, method)(*args)
            entry["ok"] = _result_ok(res)
            entry["result"] = _jsonable(res)
        except Exception as e:  # noqa: BLE001 - отказ исполнителя фиксируем, цикл живёт
            entry["ok"] = False
            entry["error"] = repr(e)
            self._fail(f"{method}{tuple(args)} не удался: {e!r}", e)
        actions.append(entry)
        return entry

    def _stop_valve_event(self, atype, *, now, walls, rest, ctx, ticket=None, action=None,
                          detail=None, note=None):
        """Событие собственного производства. Всегда critical: скрипт действовал
        деньгами (или обязан был, но не смог), и модель должна узнать сейчас, а
        не в конце дневного лимита normal-событий."""
        alert_id = f"stop-valve-{atype}" if ticket is None else f"stop-valve-{atype}-{ticket}"
        return {"event": "stop_valve", "alert_id": alert_id, "alert_type": atype,
                "priority": "critical", "fired_utc": now.isoformat(),
                "symbol": None, "ticket": ticket, "note": note, "detail": detail or {},
                "action": action,
                "snapshot": self._snapshot(walls, rest, ctx, now)}

    def _rule_wall_act(self, walls, now, actions):
        """ПРАВИЛО 1, ДЕЙСТВИЕ. Идёт сразу после расчёта стены, до журнала,
        гейта, ctx и алертов: закрытие не имеет права ждать диагностик.
        Возвращает описание действия или None, если стена не пробита."""
        if not walls["wall"]["breached"]:
            self._wall_reported = False
            return None

        closed, failed, unverified = [], [], []
        for p in walls["positions"]:
            ticket = p["ticket"]
            if not self._may_attempt("wall", ticket, now):
                continue
            r = self._call_executor("close_position", ticket, rule=WALL_EVENT_TYPE,
                                    now=now, actions=actions)
            {True: closed, False: failed, None: unverified}[r["ok"]].append(ticket)

        return {"rule": WALL_EVENT_TYPE, "closed": closed, "failed": failed,
                "unverified": unverified, "positions_seen": len(walls["positions"])}

    def _rule_wall_report(self, action, walls, rest, ctx, now):
        """ПРАВИЛО 1, РАССКАЗ. Отдельно от действия, чтобы событие несло лучший
        доступный снимок, но само закрытие ничего не ждало.

        Печатаем, пока модель не разбужена (фронт), и при каждой неудачной
        попытке закрытия. `_wall_reported` выставляется ТОЛЬКО по факту
        доставки: иначе событие, попавшее в интервал critical, терялось бы
        навсегда — позиции закрыты, модель не узнала (см. шапку модуля).
        """
        wall = walls["wall"]
        deliver = bool(not self._wall_reported or action["failed"])
        if not (deliver or action["closed"] or action["unverified"]):
            return []  # уже рассказано, действий в этот тик не было
        event = self._stop_valve_event(
            WALL_EVENT_TYPE, now=now, walls=walls, rest=rest, ctx=ctx, action=action,
            detail={"daily_loss_pct": wall["daily_loss_pct"],
                    "total_loss_pct": wall["total_loss_pct"],
                    "blocked_by": wall["blocked_by"], "reasons": wall["reasons"]},
            note="стоп-кран: стена по equity пробита, все позиции закрываются")
        if self._emit(event, now=now, budgeted=True, deliver=deliver):
            self._wall_reported = True
        return [event]

    def _rule_unprotected(self, walls, rest, ctx, now, actions):
        """ПРАВИЛО 2: наша позиция без стоп-лосса.

        Требует прочитанного журнала: без него неизвестно ни чья позиция, ни
        каким был исходный стоп, а придумывать уровень и закрывать чужое
        запрещено. Отказ чтения журнала уже попал в errors и в heartbeat.
        """
        if rest is None:
            return []
        events = []
        orphans = rest["orphan_tickets"]
        for p in walls["positions"]:
            ticket = p["ticket"]
            if _num(p.get("sl")):
                self._sl_attempts.pop(ticket, None)
                self._sl_reported.discard(ticket)
                continue

            if ticket in orphans:
                # Чужая позиция: стопа в журнале нет, выдумывать уровень нельзя,
                # закрывать чужое — не наше дело (та же развилка, что orphan →
                # HALT_NEW в risk_gate_cli). Модель будим один раз, а не каждый
                # тик — иначе одна такая позиция сожгла бы весь бюджет событий;
                # но «один раз» считается по ФАКТУ ДОСТАВКИ, иначе единственное
                # сообщение о позиции с неограниченным риском теряется в
                # интервале critical.
                if ticket in self._sl_reported:
                    continue
                event = self._stop_valve_event(
                    SL_EVENT_TYPE, now=now, walls=walls, rest=rest, ctx=ctx, ticket=ticket,
                    action={"rule": SL_EVENT_TYPE, "done": "none",
                            "reason": "позиция без decision-записи в журнале: исходный стоп "
                                      "неизвестен, датчик её не трогает"},
                    detail={"symbol": p.get("symbol"), "volume": p.get("volume"),
                            "side": position_side(p), "price_current": p.get("price_current")})
                if self._emit(event, now=now, budgeted=True):
                    self._sl_reported.add(ticket)
                events.append(event)
                continue

            if not self._may_attempt("sl", ticket, now):
                continue

            plan = plan_sl_action(p, rest["decisions"].get(str(ticket)))
            attempts = self._sl_attempts.get(ticket, 0)
            if plan["action"] == "modify" and attempts < MAX_SL_ATTEMPTS:
                self._sl_attempts[ticket] = attempts + 1
                r = self._call_executor("modify_sl", ticket, plan["sl"], rule=SL_EVENT_TYPE,
                                        now=now, actions=actions)
                if r["ok"] is False:
                    c = self._call_executor("close_position", ticket, rule=SL_EVENT_TYPE,
                                            now=now, actions=actions)
                    action = {"rule": SL_EVENT_TYPE, "done": "closed_after_failed_sl",
                              "sl": plan["sl"], "modify": r, "close": c,
                              "reason": "установка стопа отклонена"}
                else:
                    action = {"rule": SL_EVENT_TYPE, "done": "sl_restored", "sl": plan["sl"],
                              "modify": r, "attempt": attempts + 1,
                              "reason": "стоп восстановлен по записи журнала"}
            else:
                reason = plan["reason"] or (
                    f"стоп не появился после {attempts} попыток установки")
                c = self._call_executor("close_position", ticket, rule=SL_EVENT_TYPE,
                                        now=now, actions=actions)
                action = {"rule": SL_EVENT_TYPE, "done": "closed", "sl": plan["sl"],
                          "close": c, "reason": reason}

            event = self._stop_valve_event(
                SL_EVENT_TYPE, now=now, walls=walls, rest=rest, ctx=ctx, ticket=ticket,
                action=action,
                detail={"symbol": p.get("symbol"), "volume": p.get("volume"),
                        "side": position_side(p), "price_current": p.get("price_current")},
                note="стоп-кран: позиция без стоп-лосса")
            self._emit(event, now=now, budgeted=True)
            events.append(event)
        return events

    def _rule_silence(self, walls, rest, ctx, now):
        """ПРОВЕРКА ЖИВОСТИ: молчание дольше cfg.alerts.max_silence_minutes —
        само по себе событие.

        ЗАЧЕМ ЭТО ЖИВЁТ В ДАТЧИКЕ, А НЕ В alerts.json. Будильник нельзя
        поручать тому, кто спит. Модель должна была бы сама заранее вооружить
        себе алерт на тишину — но если она этого не сделала (а именно так и
        выглядит забывчивость), заметить это некому: все её условия сработали
        и разоружились, цена больше не пришла, и сессия молча заканчивается.
        Это и есть «модель уснула навсегда, пока человек не написал в чат».
        У датчика свои часы и отдельный процесс, он не зависит от аккуратности
        модели — поэтому проверка здесь.

        ПРИОРИТЕТ CRITICAL — не потому что рыночная тревога, а потому что
        дневной лимит normal-яруса к этому моменту может быть уже исчерпан
        (сорок событий), и именно тогда сообщить особенно важно: иначе
        «кончился бюджет» и «механизм сломался» неразличимы. Отдельного обхода
        бюджета не вводится — используется существующий ярус.

        Часы отсчитываются от последнего ДОСТАВЛЕННОГО события, а если их не
        было вовсе — от старта датчика.
        """
        limit_min = getattr(self.cfg.alerts, "max_silence_minutes", 0) or 0
        if limit_min <= 0:
            return []
        last = self._last_event_utc() or self._started_at
        if last is None:
            return []
        silent_s = (now - last).total_seconds()
        if silent_s < limit_min * 60:
            return []

        # ВНЕ ТОРГОВОГО ОКНА И БЕЗ ПОЗИЦИЙ БУДИТЬ НЕКОГО И НЕЗАЧЕМ.
        # Правило страхует от «модель уснула, пока рынок шёл»; ночью рынка для
        # неё нет, и три-четыре пробуждения за ночь просто жгут дневной бюджет
        # событий и внимание. 2026-07-27 22:30 — первое такое: тишина 180 мин,
        # три взведённых условия, торговать нельзя, делать нечего.
        #
        # ОТКРЫТАЯ ПОЗИЦИЯ ОТМЕНЯЕТ ЭТО ПОСЛАБЛЕНИЕ ЦЕЛИКОМ: позиция без
        # присмотра ночью опаснее, чем днём, и молчать о ней нельзя ни в каком
        # часу. Проверка идёт по факту у брокера, а не по журналу.
        # walls=None означает, что терминал не ответил и про позиции ничего не
        # известно. Тогда послабление НЕ применяется: молчать о возможной
        # незакрытой позиции, потому что «наверное, ночь», — ровно та ошибка,
        # от которой это правило и защищает.
        positions = walls.get("positions") if walls else None
        if positions is None:
            # Терминал не ответил. Падать обратно на ПОСЛЕДНЕЕ СОБСТВЕННОЕ
            # наблюдение датчика — но только на него, не на журнал: позицию
            # нельзя открыть мимо того же терминала, поэтому увиденный ноль
            # остаётся действительным, пока связи нет.
            #
            # РЕГРЕСС 2026-08-01 (суббота 07:05): за выходные правило дало бы
            # ~16 критических пробуждений подряд — терминал молчит двое суток,
            # состояние позиций «неизвестно», послабление не применяется.
            #
            # Защитное свойство не теряется: наблюдения не было вовсе (None) —
            # будим, как и раньше; последнее наблюдение показывало позицию —
            # тоже будим.
            positions = ([] if self._last_seen_positions == 0
                         else self._last_seen_positions)
        if positions is not None and not positions:
            if not session_gate(utc_now=now, cfg=self.cfg).get("allow_new"):
                return []

        alerts_doc = self._alerts or {}
        armed = [a for a in (alerts_doc.get("alerts") or [])
                 if (a.get("_state") or {}).get("armed", True)]
        event = {
            "event": "watch_silence", "alert_id": SILENCE_EVENT_ID,
            "alert_type": SILENCE_EVENT_TYPE, "priority": "critical",
            "fired_utc": now.isoformat(), "symbol": None, "ticket": None,
            "action": None,
            "detail": {"silent_minutes": round(silent_s / 60, 1),
                       "limit_minutes": limit_min,
                       "armed_alerts": len(armed),
                       "armed_ids": [a.get("id") for a in armed][:10],
                       "alerts_error": self._alerts_error},
            "note": (f"тишина {silent_s / 60:.0f} мин (порог {limit_min}): событий не "
                     f"было, вооружено условий — {len(armed)}. Если условий ноль или "
                     "они больше не сработают, цикл остановился: перепиши alerts.json"),
            "snapshot": self._snapshot(walls, rest, ctx, now)}
        self._emit(event, now=now, budgeted=True)
        return [event]

    def _prune_position_state(self, walls):
        """Состояние по тикетам, которых больше нет (позиция закрыта), не
        копится в памяти долгоживущего процесса."""
        live = {p["ticket"] for p in walls["positions"]}
        self._sl_attempts = {t: v for t, v in self._sl_attempts.items() if t in live}
        self._sl_reported &= live
        self._last_attempt = {k: v for k, v in self._last_attempt.items() if k[1] in live}

    # --- алерты модели -----------------------------------------------------

    def _alerts_pass(self, walls, rest, ctx, now):
        """Условия каждого трейдера — своим проходом, в своё состояние.

        Обход по источникам, а не по сводному набору: у каждого трейдера свой
        файл, и записывать разоружение надо в ЕГО файл. Событие несёт поле
        trader — без него непонятно, кого будить и в чей журнал ляжет решение.

        Бюджет событий общий на команду (он про подписку, а не про трейдера) и
        расходуется в порядке обхода. Делится он явно на уровне Ф5.
        """
        budget = self._budget(now)
        alloc = load_allocation(Path(self.sd) / "allocation.json")
        events = []
        for trader, path in self._alert_sources():
            doc = self._alerts_by_trader.get(trader) or {"version": 1, "alerts": []}
            fired, updated = evaluate(doc, ctx, now=now, budget=budget)
            quota = events_quota(alloc, trader,
                                 total=self.cfg.alerts.max_events_per_day)
            spent = self._events_by_trader.get(trader, 0)
            for f in fired:
                # Личная квота поверх общего бюджета: дневной лимит — свойство
                # подписки, один на команду, и без деления первый же
                # разговорчивый трейдер выест его целиком.
                #
                # КРИТИЧЕСКОЕ ПРОХОДИТ ВСЕГДА. Квота исчерпана, а цена подошла
                # к стопу — молчать нельзя: инвалидация и стоп-кран это
                # безопасность, а не разговорчивость.
                if f["priority"] != "critical" and spent >= quota:
                    continue
                spent += 1
                self._events_by_trader[trader] = spent
                event = {"event": "alert", "alert_id": f["id"], "alert_type": f["type"],
                         "priority": f["priority"], "fired_utc": f["fired_utc"],
                         "symbol": f["symbol"], "ticket": f["ticket"], "note": f["note"],
                         "detail": f["detail"], "action": None, "trader": trader,
                         "snapshot": self._snapshot(walls, rest, ctx, now,
                                                    symbol=f["symbol"])}
                # бюджет уже применён внутри evaluate(): повторная проверка здесь
                # выкинула бы часть того, что evaluate сочла доставленной, и
                # состояния бюджета разъехались бы с реальностью
                self._emit(event, now=now, budgeted=False)
                events.append(event)
            self._alerts_by_trader[trader] = updated
            self._persist_alerts(doc, updated, path=path)
        return events

    # --- тик и цикл --------------------------------------------------------

    def tick(self, now=None):
        """Один цикл датчика. Не бросает: любая беда становится записью в
        errors и в heartbeat, а цикл продолжается — датчик, упавший на
        транзиентной ошибке MT5, оставляет модель без стоп-крана.

        Порядок шагов — см. шапку модуля: сначала стена и закрытие, потом всё
        остальное.
        """
        now = now or dt.datetime.now(UTC)
        self._tick_errors = []
        if self._started_at is None:
            self._started_at = now
        if not self._restored:
            self._restore_budget_state(now)
            self._restored = True
        self._tick_no += 1
        events, actions = [], []
        walls = rest = ctx = None
        wall_action = None
        market = _OneShotMarket(self.market)

        # --- шаг 0: досылка сообщений о действиях, которые бюджет придушил
        # ранее. Раньше опроса рынка: это долг перед моделью за уже
        # состоявшиеся действия, и он не зависит от того, доступен ли терминал
        # в этот тик.
        try:
            events += self._flush_undelivered(now)
        except Exception as e:  # noqa: BLE001
            self._fail(f"досылка недоставленных сообщений: {e!r}", e)

        # --- шаг 1: стена (свой try; зависит только от equity/баз/позиций) ---
        try:
            walls = self._poll_walls(market, now)
            self._walls_ok_ts = now
            seen = walls.get("positions") if walls else None
            if seen is not None:
                self._last_seen_positions = len(seen)
        except Exception as e:  # noqa: BLE001 - обрыв MT5 не имеет права ронять цикл
            self._fail(f"стена не посчитана (счёт/базы/позиции недоступны): {e!r}", e)

        if walls is not None:
            # --- шаг 2: ЗАКРЫТИЕ по стене, до любых диагностик ---
            try:
                wall_action = self._rule_wall_act(walls, now, actions)
            except Exception as e:  # noqa: BLE001
                self._fail(f"стоп-кран (закрытие по стене): {e!r}", e)

            # --- шаг 3: журнал, экспозиция, гейт, снимок счёта ---
            try:
                rest = self._poll_rest(market, walls, now)
            except Exception as e:  # noqa: BLE001
                self._fail(f"журнал/гейт/экспозиция не собраны: {e!r}", e)

            # --- шаг 4: alerts.json и ctx ---
            try:
                self._reload_alerts(now)
            except Exception as e:  # noqa: BLE001 - битый alerts.json не роняет датчик
                self._alerts_error = repr(e)
                self._fail(f"alerts.json не прочитан: {e!r}", e)
            try:
                ctx = self._build_ctx(market, walls, rest, now)
            except Exception as e:  # noqa: BLE001
                self._fail(f"ctx не собран: {e!r}", e)

            # живая база спреда на диск — не каждый тик (это была бы запись раз
            # в секунду), а раз в минуту. Потеря последней минуты при аварии
            # безобидна, а вот потеря всего окна заставила бы гейт целый час
            # работать на барной медиане — ровно той, что блокировала входы.
            try:
                self._save_live_spread(now)
            except Exception as e:  # noqa: BLE001 - запись базы не роняет цикл
                self._fail(f"живая база спреда не сохранена: {e!r}", e)

            # --- шаг 5: рассказ о действии стоп-крана ---
            if wall_action is not None:
                try:
                    events += self._rule_wall_report(wall_action, walls, rest, ctx, now)
                except Exception as e:  # noqa: BLE001
                    self._fail(f"стоп-кран (сообщение о стене): {e!r}", e)

            # --- шаг 6: правило 2 и алерты модели ---
            # правило стены имеет приоритет: если она пробита, все позиции и так
            # закрываются, и восстанавливать стоп у позиции, которую в этот же
            # тик закрывают, значило бы слать брокеру два противоречивых приказа
            if wall_action is None:
                try:
                    events += self._rule_unprotected(walls, rest, ctx, now, actions)
                except Exception as e:  # noqa: BLE001
                    self._fail(f"стоп-кран (позиция без стопа): {e!r}", e)

            if ctx is not None:
                try:
                    events += self._alerts_pass(walls, rest, ctx, now)
                except Exception as e:  # noqa: BLE001
                    self._fail(f"вычисление алертов: {e!r}", e)

            self._prune_position_state(walls)

        # --- шаг 6б: проверка живости самого механизма пробуждения ---
        # Идёт ВНЕ блока walls: даже когда терминал недоступен и стена не
        # считается, молчание обязано быть замечено — именно тогда оно опаснее
        # всего.
        try:
            events += self._rule_silence(walls, rest, ctx, now)
        except Exception as e:  # noqa: BLE001
            self._fail(f"проверка тишины: {e!r}", e)

        # --- шаг 7: heartbeat ---
        heartbeat = self._write_heartbeat(now, walls=walls, rest=rest, ctx=ctx)
        return {"now": now, "tick": self._tick_no, "events": events,
                "delivered": [e for e in events if e.get("delivered")],
                # что НЕ сработало из-за нехватки данных/бюджета — диагностика
                # тика, а не событие: в stdout ничего из этого не уходит
                "skipped": (self._alerts or {}).get("skipped", []),
                "actions": actions, "errors": list(self._tick_errors),
                "ctx": ctx, "heartbeat": heartbeat}

    def _write_heartbeat(self, now, *, walls, rest, ctx):
        """watch_heartbeat.json — свежесть ЗАЩИТЫ, а не процесса.

        Правило контура (задача 3.3): файл старше 90 секунд → модель считает
        себя незащищённой и новых входов не делает. Поэтому ts обновляется
        ТОЛЬКО в тиках, где стена реально посчитана: живой процесс со сломанным
        стоп-краном обязан выглядеть мёртвым, иначе модель часами считает себя
        защищённой при неработающем правиле 1. Пульс самого процесса — отдельное
        поле tick_utc, и файл пишется каждый тик, чтобы было видно и то, что
        датчик жив, и то, что защита не подтверждена.
        """
        alerts_doc = self._alerts or {}
        alerts_list = alerts_doc.get("alerts", [])
        inputs = (rest or {}).get("inputs") or {}
        wall = (walls or {}).get("wall") or {}
        hb = {
            # ts = момент последней ПОДТВЕРЖДЁННОЙ проверки стены; None = ни
            # одного такого тика ещё не было (модель обязана считать это
            # устаревшим пульсом)
            "ts": self._walls_ok_ts.isoformat() if self._walls_ok_ts else None,
            "tick_utc": now.isoformat(),
            "walls_checked": walls is not None,
            "pid": os.getpid(),
            "tick": self._tick_no,
            "poll_seconds": self.cfg.alerts.poll_seconds,
            "model_id": self._model_id(),
            # КОД, КОТОРЫЙ ФАКТИЧЕСКИ КРУТИТСЯ В ЭТОМ ПРОЦЕССЕ. Python читает
            # модуль один раз при старте: правка файла на диске в живой процесс
            # не попадает никогда. 2026-07-27 из-за этого правило живости
            # (закоммичено 05:08) отсутствовало в датчике, запущенном в 04:41 —
            # шесть часов у контура не было единственной защиты от «модель
            # уснула навсегда», и снаружи это выглядело как исправная работа.
            # Пульс обязан говорить не только «я жив», но и «я той версии».
            "code_mtime": CODE_MTIME,
            # порог тишины ИЗ ЗАГРУЖЕННОГО КОДА, а не из файла конфига: если
            # правила в процессе нет, здесь будет null, и это видно сразу
            "silence_rule_minutes": (
                getattr(self.cfg.alerts, "max_silence_minutes", 0) or None),
            "alerts_path": str(self.alerts_path),
            "alerts_count": len(alerts_list),
            "alerts_armed": sum(1 for a in alerts_list
                                if (a.get("_state") or {}).get("armed", True)),
            "alerts_error": self._alerts_error,
            "events_today": self._events_today,
            "events_last_minute": sum(1 for t in self._recent_event_ts
                                      if (now - t).total_seconds() <= 60),
            # сколько сообщений о состоявшихся действиях деньгами ждут доставки:
            # модель, читая пульс, обязана видеть, что её ждёт нерассказанное,
            # ещё до того, как бюджет пропустит досылку
            "pending_undelivered": len(self._undelivered),
            "pending_undelivered_ids": [i["event"]["alert_id"] for i in self._undelivered],
            "last_event_utc": self._last_event_ts.isoformat() if self._last_event_ts else None,
            "last_critical_event_utc": (self._last_critical_event_ts.isoformat()
                                        if self._last_critical_event_ts else None),
            "wall_breached": wall.get("breached"),
            "equity": (walls or {}).get("numbers", {}).get("equity"),
            "daily_loss_pct": wall.get("daily_loss_pct"),
            "total_loss_pct": wall.get("total_loss_pct"),
            "gate_verdict": ((rest or {}).get("gate") or {}).get("verdict"),
            "positions": len(walls["positions"]) if walls else None,
            "unprotected": inputs.get("unprotected_positions"),
            "orphans": None if rest is None else len(rest["orphan_tickets"]),
            "session_phase": (ctx or {}).get("session_phase"),
            "errors": list(self._tick_errors),
        }
        try:
            # тот же атомарный писатель JSON, что и для alerts.json: модель
            # читает heartbeat в произвольный момент и не должна увидеть половину
            write_alerts_atomic(self.heartbeat_path, hb)
        except Exception as e:  # noqa: BLE001
            self._fail(f"heartbeat не записан: {e!r}", e)
        return hb

    def run(self, *, max_ticks=None, sleep_fn=time.sleep, now_fn=None):
        """Цикл опроса. Ничего, кроме часов и сна, к tick() не добавляет —
        вся логика тестируется без цикла."""
        now_fn = now_fn or (lambda: dt.datetime.now(UTC))
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            ticks += 1
            try:
                self.tick(now_fn())
            except Exception as e:  # noqa: BLE001 - последний рубеж; tick и так не бросает
                self._fail(f"тик упал: {e!r}", e)
            sleep_fn(self.cfg.alerts.poll_seconds)
        return ticks


def live_executor(market):
    """Адаптер над trader_lib/execute.py (задача 4.1) к узкому протоколу
    TradeExecutor. Пока execute.py нет — датчик НЕ запускается: «датчик без
    стоп-крана» стал бы вторым путём мимо правила, а второй путь всегда
    находится. Лучше явный отказ на старте, чем тихо незащищённая сессия."""
    try:
        from trader_lib import execute
    except ImportError as e:
        raise SystemExit(
            "стоп-кран требует исполнителя из trader_lib/execute.py (задача 4.1): "
            f"{e}. Запускать датчик без него нельзя — он остался бы без "
            "единственных двух действий, ради которых имеет право действовать.")

    class _Adapter:
        def close_position(self, ticket):
            return execute.close_position(market, ticket=ticket)

        def modify_sl(self, ticket, new_sl):
            return execute.modify_sl(market, ticket=ticket, new_sl=new_sl)

    return _Adapter()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="датчик пробуждения модели и стоп-кран")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    ap.add_argument("--max-ticks", type=int, default=None,
                    help="сколько тиков сделать (по умолчанию — до остановки)")
    a = ap.parse_args()

    cfg = load_config(a.config)
    from trader_lib.mt5_client import live_market

    market = live_market()
    AlertWatch(market, cfg, executor=live_executor(market)).run(max_ticks=a.max_ticks)
