"""Сквозной прогон команды офлайн (Ф6).

ЗАЧЕМ ОТДЕЛЬНЫМ ФАЙЛОМ. Ф1–Ф5 проверены поштучно, и каждая часть работает. Но
команда — это не сумма частей: мандат, кластерный потолок, квота событий и
пространства имён встречаются впервые именно здесь. За неделю 27–31.07 три
дефекта из одиннадцати были ровно такими — код написан, покрыт тестами и не
подключён к пути, где он должен работать (update_medians,
net_currency_exposure, и почти — проверки cluster/mandate).

ЧТО ПРОВЕРЯЕТСЯ: полный день трёх трейдеров на FakeMarket, без сети и без
терминала. Директор раздал мандаты, трейдеры взвели свои условия, датчик
разнёс события по авторам, гейт пропустил своё и отклонил чужое.
"""
import dataclasses
import datetime as dt
import io
import json

from trader_lib.alerts import write_alerts_atomic
from trader_lib.clusters import save_clusters
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket
from trader_lib.workspace import trader_dir, workspace_path

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 3, 10, 0, tzinfo=UTC)      # понедельник, окно открыто

CLUSTERS = {
    "groups": [["AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF"],
               ["USDJPY"], ["XAUUSD"]],
    "threshold": 0.65, "insufficient": [], "computed_utc": NOW.isoformat()}

ALLOCATION = {
    "server_day": "2026-08-03",
    "written_by": "claude-opus-5",
    "traders": {
        "trend": {"instruments": ["XAUUSD"], "risk_share": 0.34,
                  "active": True, "events_quota": 12},
        "fade": {"instruments": ["EURUSD"], "risk_share": 0.33,
                 "active": True, "events_quota": 12},
        "range": {"instruments": ["USDJPY"], "risk_share": 0.33,
                  "active": True, "events_quota": 12},
    }}


def _cfg(tmp_path):
    cfg = load_config("config/trader.config.json")
    return dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})


def _team(tmp_path):
    """Мир, каким его оставил директор на открытии дня."""
    cfg = _cfg(tmp_path)
    save_clusters(tmp_path / "clusters.json", CLUSTERS)
    (tmp_path / "allocation.json").write_text(
        json.dumps(ALLOCATION, ensure_ascii=False), encoding="utf-8")
    for name in ALLOCATION["traders"]:
        trader_dir(cfg, name, create=True)
    return cfg


def _arm(cfg, trader, *items):
    write_alerts_atomic(workspace_path(cfg, "alerts.json", trader=trader, create=True),
                        {"version": 1, "written_by": "test",
                         "written_utc": NOW.isoformat(), "expires_utc": None,
                         "alerts": list(items)})


def _pos(ticket, symbol, side="buy"):
    return {"ticket": ticket, "symbol": symbol, "type": 0 if side == "buy" else 1,
            "volume": 0.1, "price_open": 2400.0, "sl": 2395.0, "tp": 0.0,
            "price_current": 2400.0, "profit": 0.0, "magic": 0}


# --------------------------------------------------------------------------
# состояние: личное отдельно, общее вместе
# --------------------------------------------------------------------------

def test_traders_keep_separate_journals_and_share_the_world(tmp_path):
    """Журналы врозь — иначе три механизма смешаются в один бакет статистики и
    разделить их будет нечем. Карта кластеров вместе — иначе каждый увидит
    только свои позиции, и кластерный потолок отключится именно тогда, когда
    он нужен."""
    cfg = _team(tmp_path)
    j = {t: workspace_path(cfg, "journal.jsonl", trader=t) for t in ALLOCATION["traders"]}
    assert len(set(j.values())) == 3, "журналы обязаны быть разными"
    shared = {workspace_path(cfg, "clusters.json", trader=t)
              for t in ALLOCATION["traders"]}
    assert shared == {tmp_path / "clusters.json"}, "карта мира обязана быть одна"


# --------------------------------------------------------------------------
# гейт: свой инструмент можно, чужой нельзя, занятый кластер нельзя
# --------------------------------------------------------------------------

def _baselines(tmp_path):
    (tmp_path / "account_init.json").write_text(
        json.dumps({"initial_balance": 10000.0}), encoding="utf-8")
    (tmp_path / "day_baseline.json").write_text(
        json.dumps({"server_day": "2026-08-03", "equity": 10000.0}), encoding="utf-8")


def _world(cfg, tmp_path):
    """Состояние мира на момент NOW: подтверждённая конституция, свежий пульс
    датчика, свежий календарь, медианы спреда, базы счёта."""
    from trader_lib.constitution import HASH_FILE, write_ack
    from tests.test_entry_gate import _state

    _state(tmp_path)
    raw = json.loads(open("config/trader.config.json", encoding="utf-8").read())
    write_ack(tmp_path / HASH_FILE, config_dict=raw, now=NOW, by="test")
    (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
        "ts": NOW.isoformat(), "walls_checked": True}), encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps({
        "fetched_utc": NOW.isoformat(), "events": []}), encoding="utf-8")
    _baselines(tmp_path)
    return cfg


def _gate(cfg, tmp_path, *, symbol, trader, positions=(), side="buy"):
    from trader_lib.entry_gate import check_entry
    from tests.test_entry_gate import Market

    _world(cfg, tmp_path)
    return check_entry(market=Market(positions=list(positions)), cfg=cfg,
                       state=tmp_path, symbol=symbol, side=side, entry=2400.0,
                       sl=2395.0, rr=2.0, setup_status="изучаю",
                       p_win_journal=None, planned=True, now=NOW, trader=trader)


def test_each_trader_may_enter_his_own_instrument(tmp_path):
    cfg = _team(tmp_path)
    res = _gate(cfg, tmp_path, symbol="XAUUSD", trader="trend")
    assert res["allow"] is True, res["reasons"]


def test_trader_cannot_wander_onto_another_instrument(tmp_path):
    """Тот самый сценарий человеческих команд: увидел движение на чужой паре и
    ушёл туда. Мандат это запрещает в коде."""
    cfg = _team(tmp_path)
    res = _gate(cfg, tmp_path, symbol="EURUSD", trader="trend")
    assert res["allow"] is False
    assert any("мандат" in r for r in res["reasons"]), res["reasons"]


def test_second_trader_is_blocked_from_an_occupied_cluster(tmp_path):
    """fade держит EURUSD; range хочет USDJPY — разные кластеры, проходит.
    Но если бы range взял USDCAD (тот же кластер, что EURUSD), это была бы одна
    ставка вдвоём."""
    cfg = _team(tmp_path)
    ok = _gate(cfg, tmp_path, symbol="USDJPY", trader="range",
               positions=[_pos(1, "EURUSD")])
    assert ok["allow"] is True, ok["reasons"]

    alloc = json.loads((tmp_path / "allocation.json").read_text(encoding="utf-8"))
    alloc["traders"]["range"]["instruments"] = ["USDCAD"]
    (tmp_path / "allocation.json").write_text(json.dumps(alloc, ensure_ascii=False),
                                              encoding="utf-8")
    clash = _gate(cfg, tmp_path, symbol="USDCAD", trader="range",
                  positions=[_pos(1, "EURUSD")])
    assert clash["allow"] is False
    assert any("кластер" in r for r in clash["reasons"]), clash["reasons"]


def test_benched_trader_cannot_trade_at_all(tmp_path):
    cfg = _team(tmp_path)
    alloc = json.loads((tmp_path / "allocation.json").read_text(encoding="utf-8"))
    alloc["traders"]["trend"]["active"] = False
    (tmp_path / "allocation.json").write_text(json.dumps(alloc, ensure_ascii=False),
                                              encoding="utf-8")
    res = _gate(cfg, tmp_path, symbol="XAUUSD", trader="trend")
    assert res["allow"] is False
    assert any("не активен" in r for r in res["reasons"]), res["reasons"]


# --------------------------------------------------------------------------
# датчик: один процесс, события с авторством
# --------------------------------------------------------------------------

def _watch(tmp_path, cfg, market, out):
    from scripts.alert_watch import AlertWatch
    from tests.test_alert_watch import RecordingExecutor
    return AlertWatch(market, cfg, executor=RecordingExecutor(), out=out,
                      log=io.StringIO())


def test_one_sensor_serves_the_whole_team(tmp_path):
    """ОДИН датчик, а не три. Три процесса означали бы три стоп-крана с
    разными представлениями о позициях: при пробое стены каждый независимо
    бросился бы закрывать одно и то же."""
    cfg = _team(tmp_path)
    _baselines(tmp_path)
    cfg = dataclasses.replace(cfg, session=dataclasses.replace(
        cfg.session, trade_window_utc=["00:00", "23:59"],
        no_new_after_utc="23:59", friday_no_new_utc="23:59",
        friday_flat_utc="23:59"))
    _arm(cfg, "trend", {"id": "gold-up", "type": "price_above", "symbol": "XAUUSD",
                        "level": 2000.0, "once": True})
    _arm(cfg, "fade", {"id": "eur-dn", "type": "price_below", "symbol": "EURUSD",
                       "level": 9999.0, "once": True})
    _arm(cfg, "range", {"id": "jpy-up", "type": "price_above", "symbol": "USDJPY",
                        "level": 2000.0, "once": True})

    # бары обязаны быть свежими на момент тика: защита от протухшего тика
    # (2026-08-01) справедливо гасит ценовые условия на замёрзшей цене
    from tests.test_alert_watch import _bars
    bars = FakeMarket(bars=_bars(last_bar_utc=NOW),
                      account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, bars, out)
    w.tick(NOW)

    events = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    by_trader = {e.get("trader") for e in events if e.get("event") == "alert"}
    assert by_trader, "хотя бы один трейдер обязан быть услышан"
    assert None not in by_trader, "каждое событие обязано нести авторство"


# --------------------------------------------------------------------------
# СЛОЙ СКРИПТОВ: команда живёт не только в библиотеке
# --------------------------------------------------------------------------

# Скрипты, пишущие ЛИЧНОЕ состояние трейдера. Каждый обязан уметь принять
# --trader, иначе три трейдера пишут в один файл и затирают друг друга.
# report.py — библиотека без CLI (его зовут из кода), поэтому флага у него нет
PERSONAL_SCRIPTS = ("session_start", "enter", "exit", "close_watch",
                    "review", "run_score", "recall")


def test_every_personal_script_accepts_a_trader():
    """СТРУКТУРНЫЙ СТОРОЖ, УСИЛЕННЫЙ ПОСЛЕ ЛОЖНОГО ЗЕЛЁНОГО.

    Первая версия проверяла только, что флаг --trader ОБЪЯВЛЕН. Она дала зелёный
    на пяти скриптах, где флаг разбирался, переменная trader вычислялась — и
    больше нигде не использовалась: пути состояния по-прежнему считались от
    общего state_dir. То есть команда получила бы три трейдера, пишущих в один
    журнал и одно объявление модели.

    Это тот же класс дефекта, что update_medians и net_currency_exposure, только
    теперь в моей собственной проверке: тест мерил объявление вместо применения.
    Поэтому здесь проверяется ОБА факта — флаг есть И имя трейдера доходит до
    путей состояния.
    """
    import ast
    from pathlib import Path

    no_flag, no_use = [], []
    for name in PERSONAL_SCRIPTS:
        src = Path(f"scripts/{name}.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        declared = any(
            isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "add_argument"
            and any(isinstance(x, ast.Constant) and x.value == "--trader" for x in n.args)
            for n in ast.walk(tree))
        if not declared:
            no_flag.append(name)
            continue

        # имя обязано дойти до путей: либо через личный корень состояния,
        # либо переданное дальше именованным аргументом trader=
        used = any(
            (isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None))
             in ("trader_state_dir", "workspace_path", "trader_dir"))
            or (isinstance(n, ast.keyword) and n.arg == "trader")
            for n in ast.walk(tree))
        if not used:
            no_use.append(name)

    assert not no_flag, f"скрипты не принимают --trader: {', '.join(no_flag)}"
    assert not no_use, (
        "скрипты принимают --trader, но не применяют его к путям состояния: "
        f"{', '.join(no_use)}")


def test_every_script_actually_imports_and_parses_arguments():
    """ДЕФЕКТ, ПРОПУЩЕННЫЙ СТРУКТУРНОЙ ПРОВЕРКОЙ 2026-08-01.

    session_start.py использовал resolve_trader без импорта: автоматическая
    правка добавляла импорт только тем файлам, где ещё не было trader_state_dir,
    а туда он был вписан руками раньше. Скрипт падал NameError на первой же
    реальной команде — при этом ВСЕ тесты были зелёными, включая сторож,
    требующий и флаг, и его применение.

    Причина слепоты: сторож разбирает AST, то есть проверяет ТЕКСТ. Импорт
    отсутствует — текст всё равно выглядит правильно. Ловится это только
    запуском: `--help` доходит до argparse через все импорты модуля.
    """
    import subprocess
    import sys
    from pathlib import Path

    broken = []
    for name in (*PERSONAL_SCRIPTS, "director", "brief", "perceive",
                 "risk_gate_cli", "bootstrap_env", "alert_watch"):
        path = Path("scripts") / f"{name}.py"
        if not path.exists():
            continue
        res = subprocess.run([sys.executable, str(path), "--help"],
                             capture_output=True, text=True, timeout=60)
        # argparse на --help выходит с кодом 0; падение импорта даёт 1 и трейс
        if res.returncode != 0 or "Traceback" in res.stderr:
            broken.append(f"{name}: {res.stderr.strip().splitlines()[-1][:80]}")

    assert not broken, "скрипты не запускаются:\n  " + "\n  ".join(broken)


def test_manifest_matches_the_tree_when_it_is_committed():
    """СТОРОЖ ПРОТИВ УСТАРЕВШЕГО МАНИФЕСТА.

    MANIFEST.sha256 — первая проверка развёртывания: она ловит файл, который не
    докопировался при переносе. Устаревший манифест ВРЁТ на исправном пакете, и
    развёртывающий либо застревает, либо приучается её игнорировать. Второе
    опаснее её отсутствия.

    За 2026-08-01 манифест уходил в публикацию устаревшим ТРИЖДЫ — значит дело
    не в забывчивости, а в том, что забывчивость ничем не ловилась.

    Проверка привязана к состоянию git, а не к содержимому: пока в дереве есть
    несохранённые правки, идёт разработка и расхождение нормально. Как только
    дерево чисто — это состояние релиза, и манифест обязан ему соответствовать.
    Иначе тест ругался бы на каждое редактирование и его научились бы
    пропускать.
    """
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    sys.path.insert(0, "scripts")
    from scripts.verify_install import _digest

    dirty = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        pytest.skip("дерево грязное — идёт разработка, расхождение ожидаемо")

    manifest = Path("MANIFEST.sha256")
    if not manifest.exists():
        pytest.skip("манифеста нет")

    stale = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, _, rel = line.partition("  ")
        path = Path(rel)
        if not path.exists():
            stale.append(f"{rel}: файла нет")
            continue
        # тем же счётом, что и сама проверка развёртывания: иначе сторож и
        # проверка разойдутся, и один из них будет врать
        if _digest(path) != expected:
            stale.append(rel)

    assert not stale, (
        "MANIFEST.sha256 устарел при чистом дереве — пересобери:\n"
        "  python scripts/verify_install.py --write-manifest\n"
        f"расходятся: {', '.join(stale[:8])}")
