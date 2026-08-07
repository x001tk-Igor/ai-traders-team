"""Проверка развёртывания: всё ли на месте и работает (для Claude на новом ПК).

ЗАЧЕМ СКРИПТ, А НЕ ЧЕКЛИСТ ПРОЗОЙ. Развёртывание проваливается тихо: файл не
докопировался, пакет другой версии, терминал не залогинен, конфиг отредактирован
по дороге. Всё это выглядит как «вроде поставили», а всплывает на первой сделке.
Поэтому каждый пункт здесь — проверка с ответом да/нет, а не пункт для галочки.

ПРОВЕРКИ ИДУТ ОТ ФУНДАМЕНТА К ВЕРХУ и останавливаются на первом провале
критической: смысла проверять MT5, если не хватает половины файлов, нет.

  1. Python и пакеты        — на чём вообще всё работает
  2. Целостность файлов     — совпадает ли пакет с тем, что собирали
  3. Раскладка путей        — лежит ли пакет там, куда смотрят скиллы и промт
  4. Конфигурация           — читается ли конституция, есть ли state_dir
  5. Тесты                  — 800+ проверок самой логики
  6. Терминал MT5           — связь, счёт, разрешена ли торговля
  7. Готовность к работе    — подтверждена ли конституция, объявлена ли модель

MANIFEST.sha256 создаётся на машине-источнике (--write-manifest) и переносится
вместе с пакетом. Файл, изменившийся по дороге, будет назван поимённо.
"""
import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.sha256"

MIN_PYTHON = (3, 10)
REQUIRED_PACKAGES = ("MetaTrader5", "pandas", "numpy", "pytest")

# Папки, которые входят в контрольную сумму. Тесты — тоже: пакет без них
# проверить нельзя, а именно проверка отличает рабочее развёртывание от
# «вроде скопировалось».
MANIFEST_DIRS = ("trader_lib", "scripts", "tests", "config", "skills", "prompts",
                 "docs")
SKIP_PARTS = ("__pycache__", ".git", ".pytest_cache")

# Куда пакет ОБЯЗАН быть положен: скиллы и промт зовут скрипты этим путём.
EXPECTED_HOME = Path.home() / ".claude" / "trader-lib"

OK, FAIL, WARN = "OK", "ПРОВАЛ", "ВНИМАНИЕ"


def _iter_files():
    for folder in MANIFEST_DIRS:
        base = ROOT / folder
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not any(s in p.parts for s in SKIP_PARTS):
                yield p


# Расширения, у которых окончания строк — свойство платформы, а не содержимого.
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".ini", ".cfg", ".yml", ".yaml",
                 ".sha256", ".gitignore", ".gitattributes"}


def _digest(path):
    """SHA-256 файла; у текстовых — по НОРМАЛИЗОВАННОМУ содержимому.

    ЗАЧЕМ НОРМАЛИЗАЦИЯ. Манифест существует, чтобы ловить файл, который не
    докопировался при переносе. Смена окончаний строк — не повреждение, а
    штатное поведение платформы: git при checkout конвертирует их по
    .gitattributes, а Python в текстовом режиме на Windows сам превращает \\n в
    \\r\\n при записи. Байтовое сравнение объявляет такой файл повреждённым.

    За 2026-08-01 это трижды приводило к тому, что свежий клон с GitHub
    сообщал «пакет повреждён» на полностью исправном пакете с 979 зелёными
    тестами. Цена не в самой ошибке, а в реакции: развёртывающий либо
    застревает, либо приучается проверку игнорировать — и тогда она перестаёт
    ловить настоящее повреждение, ради которого написана.

    Бинарные файлы сравниваются побайтово: там \\r\\n — данные, а не разметка.
    """
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def write_manifest():
    lines = [f"{_digest(p)}  {p.relative_to(ROOT).as_posix()}" for p in _iter_files()]
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _check(name, ok, detail="", critical=True):
    return {"name": name, "status": OK if ok else (FAIL if critical else WARN),
            "ok": bool(ok), "critical": critical, "detail": detail}


# --------------------------------------------------------------------------
# 1. окружение
# --------------------------------------------------------------------------

def check_python():
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return _check("Python", ok,
                  f"{v.major}.{v.minor}.{v.micro} (нужен {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)")


def check_packages():
    missing, versions = [], []
    for name in REQUIRED_PACKAGES:
        try:
            mod = importlib.import_module(name)
            versions.append(f"{name} {getattr(mod, '__version__', '?')}")
        except ImportError:
            missing.append(name)
    detail = ("не установлены: " + ", ".join(missing) if missing
              else " · ".join(versions))
    return _check("Пакеты", not missing, detail)


def check_bitness():
    """Разрядность Python и терминала должна совпадать, иначе initialize()
    возвращает False без внятной причины."""
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    return _check("Разрядность Python", bits == 64,
                  f"{bits}-бит (терминал MT5 обычно 64-бит)", critical=False)


# --------------------------------------------------------------------------
# 2-3. файлы и раскладка
# --------------------------------------------------------------------------

def check_manifest():
    if not MANIFEST.exists():
        return _check("Целостность файлов", False,
                      "нет MANIFEST.sha256 — проверить нечем; собери его на "
                      "машине-источнике: python scripts/verify_install.py --write-manifest")
    expected = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            expected[rel] = digest

    actual = {p.relative_to(ROOT).as_posix(): _digest(p) for p in _iter_files()}
    missing = sorted(set(expected) - set(actual))
    changed = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
    extra = sorted(set(actual) - set(expected))

    problems = []
    if missing:
        problems.append(f"НЕ ДОКОПИРОВАНЫ ({len(missing)}): " + ", ".join(missing[:5]))
    if changed:
        problems.append(f"ИЗМЕНЕНЫ ({len(changed)}): " + ", ".join(changed[:5]))
    detail = ("; ".join(problems) if problems
              else f"{len(expected)} файлов совпадают"
                   + (f", лишних {len(extra)}" if extra else ""))
    return _check("Целостность файлов", not (missing or changed), detail)


def check_location():
    here = ROOT.resolve()
    ok = here == EXPECTED_HOME.resolve()
    return _check("Расположение пакета", ok,
                  f"{here}" + ("" if ok else
                               f" — скиллы и промт зовут скрипты из {EXPECTED_HOME}; "
                               "по другому пути модель их не найдёт"))


def check_skills():
    home = Path.home() / ".claude" / "skills"
    # список берётся из комплекта, а не зашивается: добавленный скилл иначе
    # молча не попадёт на новый ПК и там не активируется
    need = sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md"))
    missing = [n for n in need if not (home / n / "SKILL.md").exists()]
    return _check("Скиллы в ~/.claude/skills", not missing,
                  f"{home}" + ("" if not missing else f" — нет: {', '.join(missing)}"))


# --------------------------------------------------------------------------
# 4. конфигурация
# --------------------------------------------------------------------------

def check_config():
    try:
        sys.path.insert(0, str(ROOT))
        from trader_lib.config import load_config, state_dir

        cfg = load_config(str(ROOT / "config" / "trader.config.json"))
        sd = Path(state_dir(cfg))
        sd.mkdir(parents=True, exist_ok=True)
        return _check("Конституция и state_dir", True,
                      f"стены {cfg.risk.daily_loss_limit_pct}%/{cfg.risk.total_loss_limit_pct}%, "
                      f"риск на сделку {cfg.risk.per_trade_risk_cap_pct}%, "
                      f"входов в день {cfg.risk.max_new_trades_per_day} · {sd}")
    except Exception as e:  # noqa: BLE001
        return _check("Конституция и state_dir", False, repr(e))


def check_journal():
    try:
        sys.path.insert(0, str(ROOT))
        from trader_lib.config import load_config, state_dir
        from trader_lib.journal import read_records

        cfg = load_config(str(ROOT / "config" / "trader.config.json"))
        p = Path(state_dir(cfg)) / "journal.jsonl"
        if not p.exists():
            return _check("Журнал сделок", True, "пуст — новая история", critical=False)
        recs = read_records(p)
        types = {}
        for r in recs:
            types[r.get("type")] = types.get(r.get("type"), 0) + 1
        return _check("Журнал сделок", True, f"{len(recs)} записей: {types}",
                      critical=False)
    except Exception as e:  # noqa: BLE001
        return _check("Журнал сделок", False, f"НЕ ЧИТАЕТСЯ: {e!r}")


# --------------------------------------------------------------------------
# 5. тесты
# --------------------------------------------------------------------------

def check_tests(run=True):
    if not run:
        return _check("Тесты", True, "пропущены по флагу --no-tests", critical=False)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [x for x in r.stdout.splitlines() if x.strip()]
    summary = tail[-1] if tail else "нет вывода"
    return _check("Тесты", r.returncode == 0, summary)


# --------------------------------------------------------------------------
# 6-7. терминал и готовность
# --------------------------------------------------------------------------

def check_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return [_check("MT5: пакет", False, "не установлен")]
    out = []
    if not mt5.initialize():
        out.append(_check("MT5: связь", False,
                          f"initialize() = False, {mt5.last_error()} — терминал не "
                          "запущен, не залогинен или разрядность не совпадает"))
        return out
    acc, term = mt5.account_info(), mt5.terminal_info()
    out.append(_check("MT5: связь", True,
                      f"{term.name}, счёт {acc.login} @ {acc.server}, "
                      f"equity {acc.equity} {acc.currency}"))
    out.append(_check("MT5: Algo Trading", bool(term.trade_allowed),
                      "включён" if term.trade_allowed else
                      "ВЫКЛЮЧЕН — Сервис → Настройки → Советники"))
    mt5.shutdown()
    return out


def check_readiness():
    """Готовность к торговле: конституция подтверждена, модель объявлена.
    Обе — действия, которые ДОЛЖЕН сделать человек и модель, а не установщик."""
    sys.path.insert(0, str(ROOT))
    from trader_lib.config import load_config, state_dir
    from trader_lib.constitution import HASH_FILE, check_config as check_const
    from trader_lib.model_session import current as current_model

    cfg = load_config(str(ROOT / "config" / "trader.config.json"))
    sd = Path(state_dir(cfg))
    raw = json.loads((ROOT / "config" / "trader.config.json").read_text(encoding="utf-8"))

    const = check_const(raw, sd / HASH_FILE)
    ident = current_model(sd, cfg)
    return [
        _check("Конституция подтверждена", const["ok"], const["reason"], critical=False),
        _check("Модель объявлена", ident["ok"], ident["reason"], critical=False),
    ]


# --------------------------------------------------------------------------

# Проверки, без которых дальнейшая диагностика бессмысленна. Расположение и
# скиллы сюда НЕ входят: они ломают работу модели, но не мешают прогнать тесты и
# опросить терминал — а знать их результат полезно ещё до раскладки по путям.
GATING = ("Python", "Пакеты", "Целостность файлов", "Конституция и state_dir")


def run_all(*, with_tests=True, with_mt5=True):
    results = [check_python(), check_packages(), check_bitness(),
               check_manifest(), check_location(), check_skills(),
               check_config(), check_journal()]
    if all(r["ok"] for r in results if r["name"] in GATING):
        results.append(check_tests(run=with_tests))
        if with_mt5:
            results += check_mt5()
        results += check_readiness()
    else:
        results.append(_check("Дальнейшие проверки", False,
                              "пропущены: сначала почини Python/пакеты/файлы/конфиг"))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="проверка развёртывания ИИ-трейдера")
    ap.add_argument("--write-manifest", action="store_true",
                    help="собрать MANIFEST.sha256 (делается на машине-источнике)")
    ap.add_argument("--no-tests", action="store_true", help="пропустить прогон тестов")
    ap.add_argument("--no-mt5", action="store_true", help="не трогать терминал")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.write_manifest:
        n = write_manifest()
        print(f"MANIFEST.sha256 собран: {n} файлов → {MANIFEST}")
        return 0

    results = run_all(with_tests=not a.no_tests, with_mt5=not a.no_mt5)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        for r in results:
            print(f"{r['status']:<9} {r['name']:<{width}}  {r['detail']}")
        bad = [r for r in results if not r["ok"] and r["critical"]]
        warn = [r for r in results if not r["ok"] and not r["critical"]]
        print()
        if bad:
            print(f"РАЗВЁРТЫВАНИЕ НЕ ГОТОВО: {len(bad)} критических провалов.")
            print("Чинить сверху вниз — нижние проверки зависят от верхних.")
        elif warn:
            print("Установка исправна. Осталось действие человека/модели:")
            for r in warn:
                print(f"  · {r['name']}: {r['detail']}")
        else:
            print("ВСЁ ГОТОВО: пакет цел, зависимости на месте, тесты зелёные, "
                  "терминал на связи, конституция подтверждена, модель объявлена.")
    return 1 if any(not r["ok"] and r["critical"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
