# Проверка окружения и компиляция (Windows 10, PowerShell).
# Проверяет: политику выполнения скриптов, python 3.14, venv, пакеты, право записи;
# недостающее ставит или печатает готовую команду для решения проблемы.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$fail = $false

function Hint($msg) { Write-Host $msg -ForegroundColor Yellow }

# ---------- 1. Политика выполнения скриптов ----------
$pol = Get-ExecutionPolicy -Scope CurrentUser
if ($pol -eq "Restricted") {
    Write-Host "[X] Выполнение скриптов запрещено политикой (CurrentUser = Restricted)." -ForegroundColor Red
    Hint "Разрешить системной командой (в PowerShell от имени администратора):"
    Hint "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser"
    Hint "Либо запустить этот скрипт в обход политики:"
    Hint "  powershell -ExecutionPolicy Bypass -File scripts\check_compile.ps1"
    $fail = $true
} else {
    Write-Host "[OK] Политика выполнения: $pol (CurrentUser)"
}

# ---------- 2. Python 3.14 ----------
$py = $null
foreach ($cand in @("python3.14", "python", "py")) {
    try {
        $ver = & $cand -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -eq "3.14") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    try {
        $ver = & py -3.14 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -eq "3.14") { $py = "py -3.14" }
    } catch {}
}

if (-not $py) {
    Write-Host "[X] Python 3.14 не найден." -ForegroundColor Red
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Hint "Установить:"
        Hint "  winget install -e --id Python.Python.3.14"
    } else {
        Hint "Установите с https://www.python.org/downloads/ (отметьте 'Add python.exe to PATH')"
    }
    $fail = $true
} else {
    Write-Host "[OK] Python 3.14: $py"
}

# ---------- 3. venv ----------
if ($py) {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "[..] venv отсутствует — создаю .venv"
        if ($py -eq "py -3.14") { & py -3.14 -m venv .venv } else { & $py -m venv .venv }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[X] Не удалось создать venv." -ForegroundColor Red
            $fail = $true
        }
    }
}

if (Test-Path ".venv\Scripts\python.exe") {
    $py = ".venv\Scripts\python.exe"
    Write-Host "[OK] venv: $py"

    # ---------- 4. Пакеты ----------
    & $py -c "import PyQt5, serial" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[..] PyQt5/pyserial отсутствуют — устанавливаю"
        & $py -m pip install --upgrade pip
        & $py -m pip install PyQt5 pyserial
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[X] pip install не удался. Вручную:" -ForegroundColor Red
            Hint "  $py -m pip install PyQt5 pyserial"
            $fail = $true
        }
    }
    & $py -c "import PyQt5, serial" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "[OK] Пакеты: PyQt5, pyserial" }
}

# ---------- 5. Право записи ----------
if (-not (Test-Path -Write .)) {
    Write-Host "[X] Нет права записи в каталог проекта ($PWD)." -ForegroundColor Red
    Hint "Запустите PowerShell от имени администратора или выдайте права на папку (Свойства > Безопасность)."
    $fail = $true
}

# ---------- 6. Компиляция ----------
if ($py) {
    Write-Host "[..] python -m compileall src main.py"
    & $py -m compileall -q src main.py
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] Компиляция успешна"
    } else {
        Write-Host "[X] Ошибки компиляции — см. сообщения выше" -ForegroundColor Red
        $fail = $true
    }
}

Write-Host ""
if (-not $fail) {
    Write-Host "Готово: всё на месте, компиляция прошла." -ForegroundColor Green
    exit 0
} else {
    Write-Host "Есть проблемы — выполните напечатанные выше команды и перезапустите скрипт." -ForegroundColor Yellow
    exit 1
}
