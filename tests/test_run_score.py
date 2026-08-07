"""Регресс на баг задачи 2.2 (scripts/run_score.py:19-21): прогресс к цели
считался в переменную prog и тут же выбрасывался — render_scorecard всегда
получал захардкоженный progress_to_target_pct=0.0. Решение: run_score.py не
имеет доступа к живому счёту (только конфиг+журнал), поэтому реальное
значение (perceive.py → account_snapshot → progress_to_target_pct) прокидывается
СНАРУЖИ через --progress-pct/run(progress_to_target_pct=...); по умолчанию —
честное None → scorecard.md показывает «н/д», а не выдуманный ноль."""
import scripts.run_score as run_score
from trader_lib.config import load_config


def test_progress_pct_wired_through_when_given(tmp_path, monkeypatch):
    """Журнал в этом тесте пуст (n=0), поэтому avg_R/wr в overall — тоже None
    и честно рендерятся как "н/д" (отдельная, независимая правка ревью —
    см. test_scorecard_empty_overall_shows_na_not_bare_none в test_score.py).
    Здесь проверяем узко: именно строка ПРОГРЕССА не говорит "н/д", когда
    значение реально передано — не весь документ целиком, иначе тест ловил
    бы чужой (законный) "н/д" из overall и ничего не говорил бы о баге
    прогресса."""
    monkeypatch.setattr(run_score, "state_dir", lambda cfg: str(tmp_path))
    cfg = load_config("config/trader.config.json")
    run_score.run(cfg, render_scorecard_flag=True, progress_to_target_pct=3.7)
    md = (tmp_path / "scorecard.md").read_text(encoding="utf-8")
    progress_line = next(line for line in md.splitlines() if "Прогресс к цели" in line)
    assert "3.70%" in progress_line
    assert "н/д" not in progress_line


def test_progress_pct_honestly_unknown_when_not_given(tmp_path, monkeypatch):
    """Без --progress-pct (например, reflect вызван не сразу после perceive)
    scorecard обязан честно показать «нет данных», а не 0.0 — захардкоженный
    ноль неотличим от реального нулевого прогресса, а это ложь о состоянии
    счёта (см. шапку файла)."""
    monkeypatch.setattr(run_score, "state_dir", lambda cfg: str(tmp_path))
    cfg = load_config("config/trader.config.json")
    run_score.run(cfg, render_scorecard_flag=True)
    md = (tmp_path / "scorecard.md").read_text(encoding="utf-8")
    progress_line = next(line for line in md.splitlines() if "Прогресс к цели" in line)
    assert "н/д" in progress_line
    assert "0.00%" not in progress_line


def test_run_without_render_scorecard_flag_skips_scorecard_file(tmp_path, monkeypatch):
    """run() без --render-scorecard просто освежает stats.json (дёшево, каждый
    цикл) — не должен падать из-за отсутствующего progress_to_target_pct и не
    должен писать scorecard.md вовсе (поведение до задачи 2.2 не менялось)."""
    monkeypatch.setattr(run_score, "state_dir", lambda cfg: str(tmp_path))
    cfg = load_config("config/trader.config.json")
    stats = run_score.run(cfg, render_scorecard_flag=False)
    assert stats["overall"]["n"] == 0
    assert not (tmp_path / "scorecard.md").exists()
    assert (tmp_path / "stats.json").exists()
