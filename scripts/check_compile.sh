#!/usr/bin/env bash
# Проверка окружения и компиляция (Linux / macOS).
# Проверяет: python3.14, venv, пакеты, право записи; недостающее ставит
# или печатает готовую команду для решения проблемы.
set -u
cd "$(dirname "$0")/.."

fail=0

# ---------- 1. Python 3.14 ----------
PY=""
for cand in python3.14 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if [ "$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = "3.14" ]; then
            PY="$cand"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "[X] Python 3.14 не найден."
    if command -v apt >/dev/null 2>&1; then
        echo "    Установить (Ubuntu/Debian, deadsnakes):"
        echo "      sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update && sudo apt install -y python3.14 python3.14-venv"
    elif command -v brew >/dev/null 2>&1; then
        echo "    Установить (macOS):"
        echo "      brew install python@3.14"
    else
        echo "    Установите Python 3.14 с https://www.python.org/downloads/"
    fi
    fail=1
    PY=""  # дальше компилировать нечем
else
    echo "[OK] Python 3.14: $(command -v "$PY")"
fi

# ---------- 2. Право записи в репо ----------
if [ -n "$PY" ] && [ ! -w . ]; then
    echo "[X] Нет права записи в каталог проекта ($(pwd))."
    echo "    Выполните: sudo chown -R \"$USER\" \"$(pwd)\""
    fail=1
fi

# ---------- 3. venv ----------
if [ -n "$PY" ]; then
    if [ ! -x .venv/bin/python ]; then
        echo "[..] venv отсутствует — создаю .venv"
        "$PY" -m venv .venv || { echo "[X] Не удалось создать venv (нужен пакет venv)."; fail=1; }
    fi
fi

if [ -x .venv/bin/python ]; then
    PY=".venv/bin/python"
    echo "[OK] venv: $PY"

    # ---------- 4. Пакеты ----------
    if ! "$PY" -c "import PyQt5, serial" >/dev/null 2>&1; then
        echo "[..] PyQt5/pyserial отсутствуют — устанавливаю"
        "$PY" -m pip install --upgrade pip >/dev/null 2>&1
        "$PY" -m pip install PyQt5 pyserial || {
            echo "[X] pip install не удался. Вручную:"
            echo "      $PY -m pip install PyQt5 pyserial"
            if command -v apt >/dev/null 2>&1; then
                echo "    (если venv не создавался системным python без venv-пакета: sudo apt install -y python3.14-venv)"
            fi
            fail=1
        }
    fi
    "$PY" -c "import PyQt5, serial" >/dev/null 2>&1 && echo "[OK] Пакеты: PyQt5, pyserial" || fail=1
fi

# ---------- 5. Компиляция ----------
if [ -n "$PY" ]; then
    echo "[..] python -m compileall src main.py"
    if "$PY" -m compileall -q src main.py; then
        echo "[OK] Компиляция успешна"
    else
        echo "[X] Ошибки компиляции — см. сообщения выше"
        fail=1
    fi
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "Готово: всё на месте, компиляция прошла."
else
    echo "Есть проблемы — выполните напечатанные выше команды и перезапустите скрипт."
fi
exit "$fail"
