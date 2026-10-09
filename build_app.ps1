<#
.SYNOPSIS
    Сборка портативной сборки Fatigue Test App: папка с exe и всеми
    зависимостями (как современное приложение), рядом configs/, logs/,
    Saved data/.

.DESCRIPTION
    Скрипт работает ТОЛЬКО с локальным venv репозитория (.venv):
      1. Проверяет наличие .venv\Scripts\python.exe; если нет —
         создаёт .venv любым найденным системным Python 3.
      2. Проверяет статус venv и обязательные пакеты (PyQt5, pyserial),
         недостающие ставит в venv (системный python не трогает).
      3. Ставит pyinstaller и собирает onedir-сборку (окно без консоли).
      4. Раскладывает рядом с exe пользовательские файлы:
         configs/ (calibration.ini, app_settings.ini), logs/, Saved data/.

    Результат: dist\FatigueTestApp\FatigueTestApp.exe — можно копировать
    папку целиком на любую машину с Windows (x64), установка не нужна.

.PARAMETER Clean
    Удалить сборочные артефакты (build/, dist/) перед сборкой.

.PARAMETER CleanVenv
    Удалить и пересоздать .venv с нуля.

.EXAMPLE
    .\build_app.ps1                 # обычная сборка
    .\build_app.ps1 -Clean          # пересборка exe с нуля
    .\build_app.ps1 -CleanVenv      # пересоздать venv и собрать
#>
param(
    [switch]$Clean,
    [switch]$CleanVenv
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$AppName = "FatigueTestApp"
$VenvDir = Join-Path $PSScriptRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# Пакеты, без которых приложение не работает.
$RequiredModules = @("PyQt5", "pyserial")

Write-Host "=== Fatigue Test App: сборка портативной сборки ===" -ForegroundColor Cyan
Write-Host "Репозиторий: $PSScriptRoot"

# --- 0. Чистка ---
if ($Clean) {
    foreach ($dir in @("build", "dist")) {
        if (Test-Path $dir) {
            Write-Host "Удаляю $dir ..."
            Remove-Item -Recurse -Force $dir
        }
    }
}

if ($CleanVenv -and (Test-Path $VenvDir)) {
    Write-Host "Удаляю .venv (пересоздание) ..."
    Remove-Item -Recurse -Force $VenvDir
}

# --- 1. Локальный .venv: проверка наличия, иначе создание ---
if (Test-Path $VenvPython) {
    Write-Host ".venv найден: $VenvDir"
} else {
    Write-Host ".venv не найден ($VenvPython) — создаю..." -ForegroundColor Yellow

    # Для создания venv нужен ЛЮБОЙ системный Python 3. Ищем без
    # строковой склейки: каждый кандидат пробуем через --version
    # (для py.exe — 'py -3 --version'). Заглушки Microsoft Store
    # отсекаются проверкой 'Python 3' в выводе.
    $pyCandidates = @(
        (Get-Command python -ErrorAction SilentlyContinue),
        (Get-Command py -ErrorAction SilentlyContinue),
        (Get-Command python3 -ErrorAction SilentlyContinue),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )

    $bootstrap = $null
    foreach ($candidate in $pyCandidates) {
        if ($null -eq $candidate) { continue }

        $exe = if ($candidate -is [string]) { $candidate } else { $candidate.Source }
        if ([string]::IsNullOrEmpty($exe) -or -not (Test-Path $exe)) { continue }

        $probe = if ($candidate -isnot [string] -and $candidate.Name -eq "py.exe") {
            @($exe, "-3", "--version")
        } else {
            @($exe, "--version")
        }

        $version = & $probe[0] $probe[1..($probe.Count - 1)] 2>$null
        if ($version -match "Python 3") {
            $bootstrap = $probe
            Write-Host "Системный Python для создания venv: $version ($exe)"
            break
        }
    }

    if ($null -eq $bootstrap) {
        throw "Python 3 не найден. Установите Python 3.10+ с python.org (галочка 'Add to PATH'), затем запустите скрипт снова."
    }

    & $bootstrap[0] $bootstrap[1..($bootstrap.Count - 1)] -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать .venv." }
    Write-Host ".venv создан: $VenvDir"
}

if (-not (Test-Path $VenvPython)) {
    throw ".venv\Scripts\python.exe не найден после создания — venv неполный. Пересоздайте: .\build_app.ps1 -CleanVenv"
}

# --- 2. Статус venv и обязательные пакеты ---
Write-Host "Статус venv: $(& $VenvPython --version)"

$missing = @()
foreach ($module in $RequiredModules) {
    & $VenvPython -c "import $module" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $module — OK"
    } else {
        Write-Host "  $module — НЕ найден, будет установлен" -ForegroundColor Yellow
        $missing += $module
    }
}

if ($missing.Count -gt 0) {
    Write-Host "Устанавливаю в .venv: $($missing -join ', ') ..."
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install --quiet @missing
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить пакеты: $($missing -join ', ')" }
}

# pyinstaller ставим при отсутствии (актуальная версия на момент сборки).
& $VenvPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Ставлю pyinstaller в .venv ..."
    & $VenvPython -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить pyinstaller." }
}

# --- 3. Сборка exe (onedir, окно без консоли) ---
Write-Host "Собираю exe (onedir) ..."
& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name $AppName `
    --hidden-import src `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой." }

$AppDir = Join-Path $PSScriptRoot "dist\$AppName"
if (-not (Test-Path (Join-Path $AppDir "$AppName.exe"))) {
    throw "Не найден $AppDir\$AppName.exe — сборка не удалась."
}

# --- 4. Пользовательские файлы рядом с exe ---
# REPO_ROOT в frozen-режиме = папка exe: там ищутся configs/, logs/,
# Saved data. Если configs/ уже есть в сборке (повторный запуск) —
# НЕ перезаписываем: настройки и калибровки пользователя сохраняются.
Write-Host "Раскладываю пользовательские файлы рядом с exe ..."

if (-not (Test-Path (Join-Path $AppDir "configs"))) {
    New-Item -ItemType Directory -Path (Join-Path $AppDir "configs") | Out-Null
}
foreach ($ini in @("configs\calibration.ini", "configs\app_settings.ini")) {
    $src = Join-Path $PSScriptRoot $ini
    $dst = Join-Path $AppDir $ini
    if ((Test-Path $src) -and -not (Test-Path $dst)) {
        Copy-Item $src $dst
    } elseif (-not (Test-Path $dst)) {
        # Файла-шаблона нет — создам пустой, приложение дополнит дефолтами.
        New-Item -ItemType File -Path $dst | Out-Null
    }
}

foreach ($dir in @("logs", "Saved data")) {
    $dst = Join-Path $AppDir $dir
    if (-not (Test-Path $dst)) {
        New-Item -ItemType Directory -Path $dst | Out-Null
    }
}

Write-Host ""
Write-Host "Готово: dist\$AppName\$AppName.exe" -ForegroundColor Green
Write-Host "Папку dist\$AppName можно переносить целиком (портативная сборка):"
Write-Host "  $AppName.exe              — запуск приложения"
Write-Host "  configs\*.ini             — калибровки и настройки (сохраняются между запусками)"
Write-Host "  logs\app_debug.log        — DEBUG-лог"
Write-Host "  Saved data\               — данные испытаний по умолчанию"
Write-Host "_internal\ и служебные файлы удалять нельзя."