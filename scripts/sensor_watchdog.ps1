# Сторож торгового датчика.
#
# ИНЦИДЕНТ 2026-08-03. Датчик тикал, пульс шёл, а торговый конвейер стоял:
# после обрыва связи с терминалом в 02:23 UTC модуль MT5 больше не отдавал
# данные счёта, walls не считались ни разу, и весь конвейер алертов остался за
# этим гейтом. Будильник директора на 06:30 был взведён и НЕ выстрелил. Снаружи
# всё выглядело исправным.
#
# Отсюда два вывода, заложенных в этот скрипт:
#
# 1. Проверять надо не «жив ли процесс», а «идёт ли работа». Живой процесс,
#    который ничего не делает, — ровно тот случай, что стоил торгового дня.
#    Поэтому проверяется ВОЗРАСТ ПУЛЬСА и флаг walls_checked, а не наличие PID.
#
# 2. Сторож обязан жить ВНЕ агентского харнесса. Прошлый датчик был привязан к
#    Monitor-задаче: пропала задача — пропал и он, и заметить это было некому.
#    Планировщик Windows переживает и харнесс, и сессию, и перезагрузку.
#
# Регистрация (однократно):
#   schtasks /Create /TN "Trader Sensor Watchdog" /SC MINUTE /MO 5 /RL HIGHEST /F ^
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File <REPO>\scripts\sensor_watchdog.ps1"
#
# Пути ниже — под свою машину. <REPO> — каталог этого репозитория,
# <STATE_DIR> — рабочее состояние счёта (вне репозитория).

$ErrorActionPreference = "Stop"

$StateDir   = Join-Path $env:USERPROFILE ".claude\trader-state\demo-primary"
$Heartbeat  = Join-Path $StateDir "watch_heartbeat.json"
$Python     = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
$Sensor     = Join-Path $env:USERPROFILE ".claude/trader-lib/scripts/alert_watch.py"
$WatchLog   = Join-Path $StateDir "watchdog.log"

# Порог протухания. Датчик пишет пульс каждый тик (1 с). Три минуты — это
# ~180 пропущенных тиков: столько не бывает от разовой заминки, только от
# настоящей остановки работы.
$StaleSeconds = 180

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}Z  {1}" -f (Get-Date).ToUniversalTime(), $msg
    Add-Content -Path $WatchLog -Value $line -Encoding utf8
}

function Restart-Sensor([string]$why) {
    Write-Log "ПЕРЕЗАПУСК: $why"
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*alert_watch*" } |
        ForEach-Object {
            Write-Log "  снимаю старый PID $($_.ProcessId)"
            try { Stop-Process -Id $_.ProcessId -Force } catch {}
        }
    Start-Sleep -Seconds 2
    $p = Start-Process -FilePath $Python -ArgumentList $Sensor `
        -RedirectStandardOutput (Join-Path $StateDir "sensor_stream.log") `
        -RedirectStandardError  (Join-Path $StateDir "sensor_stderr.log") `
        -WindowStyle Hidden -PassThru
    Write-Log "  поднят PID $($p.Id)"
}

# --- собственно проверка ---------------------------------------------------

# ПРОВЕРКА 1: процесс вообще существует.
#
# Найдена ПРОВЕРКОЙ СТОРОЖА БОЕМ 2026-08-03, и найдена тем, что тест сначала
# «провалился». Датчик был убит, а сторож промолчал — потому что пульс, записанный
# за секунды до убийства, ещё не успел протухнуть. Логика порога была верна,
# неверен был вывод, что одного порога достаточно: исчезнувший процесс не
# обнаруживался бы ТРИ МИНУТЫ, а на открытии сессии это дорогие минуты.
#
# Два сигнала ловят два разных отказа и не заменяют друг друга:
#   нет процесса        → видно мгновенно, здесь;
#   процесс есть, но встал → виден только по возрасту пульса, ниже.
# Инцидент этого утра был ВТОРОГО рода, и проверка PID его бы не поймала.
$alive = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like "*alert_watch*" })
if ($alive.Count -eq 0) {
    Restart-Sensor "процесса датчика нет ни одного"
    exit 0
}
if ($alive.Count -gt 1) {
    # Два стоп-крана на одном счёте хуже, чем ни одного: оба увидят одну стену
    # и оба пошлют приказ на закрытие. Restart-Sensor снимает все и поднимает один.
    Restart-Sensor "датчиков запущено $($alive.Count) — должен быть ровно один"
    exit 0
}

if (-not (Test-Path $Heartbeat)) {
    Restart-Sensor "файла пульса нет вовсе"
    exit 0
}

try {
    $hb = Get-Content $Heartbeat -Raw -Encoding utf8 | ConvertFrom-Json
} catch {
    # Битый JSON — это не «наверное, всё хорошо». Незнание не равно разрешению:
    # тот же принцип, по которому гейт входа отказывает при нечитаемом состоянии.
    Restart-Sensor "пульс не разбирается как JSON: $($_.Exception.Message)"
    exit 0
}

$age = ((Get-Date).ToUniversalTime() - [datetime]::Parse($hb.ts).ToUniversalTime()).TotalSeconds

if ($age -gt $StaleSeconds) {
    Restart-Sensor ("пульс протух: {0:N0} с при пороге {1} с (PID в пульсе {2}, тик {3})" -f $age, $StaleSeconds, $hb.pid, $hb.tick)
    exit 0
}

# Живой процесс, который не может посчитать стену, — ровно случай 2026-08-03.
# Пульс при этом идёт, и проверка «жив ли PID» его не ловит.
if ($hb.walls_checked -ne $true) {
    Restart-Sensor ("пульс свежий ({0:N0} с), но стены НЕ считаются — связь с терминалом мертва" -f $age)
    exit 0
}

# Тишина сторожа — норма; писать в лог каждые 5 минут «всё хорошо» значит
# утопить в нём те строки, ради которых лог и заведён.
exit 0
