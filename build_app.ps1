<#
.SYNOPSIS
    Сборка портативной сборки Fatigue Test App: папка с exe и всеми
    зависимостями (как современное приложение), рядом configs/, logs/,
    Saved data/.

.DESCRIPTION
    Скрипт:
      1. Создаёт изолированный venv сборки (.build-venv) и ставит туда
         PyQt5, pyserial, pyinstaller (в системный python ничего не ставит).
      2. Собирает onedir-сборку PyInstaller (окно без консоли).
      3. Раскладывает рядом с exe пользовательские файлы:
         configs/ (calibration.ini, app_settings.ini), logs/, Saved data/.

    Результат: dist\FatigueTestApp\FatigueTestApp.exe — можно копировать
    папку целиком на любую машину с Windows (x64), установка не нужна.

.PARAMETER Clean
    Удалить сборочные артефакты (build/, dist/, .build-venv/) перед сборкой.

.EXAMPLE
    .\build_app.ps1            # обычная сборка
    .\build_app.ps1 -Clean     # сборка с нуля
#>
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$AppName = "FatigueTestApp"
$VenvDir = Join-Path $PSScriptRoot ".build-venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "=== Fatigue Test App: сборка портативной сборки ===" -ForegroundColor Cyan

# --- 0. Чистка ---
if ($Clean) {
    foreach ($dir in @("build", "dist", ".build-venv")) {
        if (Test-Path $dir) {
            Write-Host "Удаляю $dir ..."
            Remove-Item -Recurse -Force $dir
        }
    }
}

# --- 1. Python ---
# Ищем рабочий Python 3 среди типовых вариантов; вызываем через
# & (без Invoke-Expression и разбиения строк — py-лаунчер часто
# отсутствует, а 'py -3' как строка даёт CommandNotFoundException).
$pyCandidates = @(
    @(Get-Command python -ErrorAction SilentlyContinue),
    @(Get-Command py -ErrorAction SilentlyContinue),
    @(Get-Command python3 -ErrorAction SilentlyContinue),
    @(Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    @(Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    @(Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
)

$pyCmd = $null
foreach ($candidate in $pyCandidates) {
    if ($null -eq $candidate) { continue }

    $exe = if ($candidate -is [string]) { $candidate } else { $candidate.Source }
    if ([string]::IsNullOrEmpty($exe) -or -not (Test-Path $exe)) { continue }

    # py-лаунчер: нужен именно 'py -3' (запуск Python 3)
    $probe = if ($candidate -isnot [string] -and $candidate.Name -eq "py.exe") {
        @($exe, "-3", "--version")
    } else {
        @($exe, "--version")
    }

    $version = & $probe[0] $probe[1..($probe.Count-1)] 2>$null
    if ($version -match "Python 3") {
        $pyCmd = $probe
        Write-Host "Python: $version"
        break
    }
}

if ($null -eq $pyCmd) {
    throw "Python 3 не найден. Установите Python 3.10+ с python.org (галочка 'Add to PATH')."
}

# --- 2. Venv сборки + зависимости ---
if (-not (Test-Path $VenvPython)) {
    Write-Host "Создаю venv сборки: .build-venv ..."
    & $pyCmd[0] $pyCmd[1..($pyCmd.Count-1)] -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать venv." }
}

Write-Host "Ставлю зависимости (PyQt5, pyserial, pyinstaller) ..."
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install --quiet PyQt5 pyserial pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }

# --- 3. PyInstaller: onedir, окно без консоли ---
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