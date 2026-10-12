#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сравнение xlsx-записей capture_*.xlsx: кривая, экстремумы, статы, график.

Консольный скрипт-компаньон к capture_3s.py:

    python analyze_captures.py test/capture_330_....xlsx test/capture_600_....xlsx
    python analyze_captures.py a.xlsx            # одна запись — RAW vs FILTERED
    python analyze_captures.py a.xlsx b.xlsx -o out.png

Что делает:
  1. Читает xlsx (лист «Кадры»: unix, время с, RAW, FILTERED, Сила Н, Позиция мм).
  2. «Детектирует кривую»: отфильтровывает дрейф (скользящее среднее ~1 с),
     находит локальные экстремумы цикла (с гистерезисом) → MAX/MIN/MID по циклам.
  3. Считает min / max / median / mean по каналам и по детектированной кривой.
  4. Рисует PNG: кривые с отмеченными экстремумами; если файлов два —
     сверху вниз (RAW vs FILTERED внутри файла, файл против файла),
     плюс сводная таблица min/max/median/mean и дельты между файлами.
  5. Печатает таблицу сравнения в консоль.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent

CHANNELS = [
    ("RAW", 2),
    ("FILTERED", 3),
    ("Сила,Н", 4),
    ("Позиция,мм", 5),
]


def load_capture(path):
    """Читает xlsx → dict с каналами и временем (с)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if "Кадры" in wb.sheetnames:
        ws = wb["Кадры"]
    else:
        ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    rows = [r for r in rows if r and r[0] is not None]
    if not rows:
        raise SystemExit(f"ОШИБКА: {path} — лист «Кадры» пуст")
    t0 = rows[0][0]
    t = np.array([r[0] - t0 for r in rows], dtype=float)
    data = {"t": t, "метка": Path(path).stem}
    for name, col in CHANNELS:
        data[name] = np.array([float(r[col]) for r in rows], dtype=float)
    wb.close()
    return data


def moving_average(x, window_s, fs):
    """Скользящее среднее (окно нечётное, отражение краёв)."""
    w = max(3, int(round(window_s * fs)) | 1)
    if w >= len(x):
        return np.full_like(x, np.mean(x))
    pad = w // 2
    xp = np.concatenate((x[pad::-1], x, x[:-pad - 1:-1]))
    kernel = np.ones(w) / w
    return np.convolve(xp, kernel, mode="valid")[:len(x)]


def detect_curve(x, fs, min_period_s=0.05):
    """Детекция кривой цикла: дрейф (скользящее среднее 1 с) убирается,
    в остатке — локальные экстремумы с гистерезисом по амплитуде.

    Возвращает (baseline, peaks_idx, troughs_idx, osc_amplitude).
    """
    baseline = moving_average(x, 1.0, fs)
    osc = x - baseline
    osc_smooth = moving_average(osc, min(0.02, 4.0 / fs), fs)
    amp = float(np.percentile(np.abs(osc_smooth), 95)) or 1e-9
    hyst = 0.3 * amp

    peaks = []
    troughs = []
    state = None          # "up" ищем максимум, "down" ищем минимум
    cur_max = cur_min = osc_smooth[0]
    cur_max_i = cur_min_i = 0
    for i in range(1, len(osc_smooth)):
        v = osc_smooth[i]
        if state is None:
            if v > cur_max:
                cur_max, cur_max_i = v, i
            if v < cur_min:
                cur_min, cur_min_i = v, i
            if cur_max - osc_smooth[cur_min_i] > hyst:
                state = "up" if osc_smooth[cur_min_i] < v else "down"
                if state == "down":
                    peaks.append(cur_max_i)
                    cur_min, cur_min_i = v, i
                else:
                    troughs.append(cur_min_i)
                    cur_max, cur_max_i = v, i
        elif state == "up":
            if v > cur_max:
                cur_max, cur_max_i = v, i
            elif v < cur_max - hyst:
                peaks.append(cur_max_i)
                state = "down"
                cur_min, cur_min_i = v, i
        else:
            if v < cur_min:
                cur_min, cur_min_i = v, i
            elif v > cur_min + hyst:
                troughs.append(cur_min_i)
                state = "up"
                cur_max, cur_max_i = v, i

    # Слить соседние дубли.
    def dedup(arr):
        out = []
        for i in arr:
            if not out or i - out[-1] > min_period_s * fs:
                out.append(i)
        return out

    return baseline, dedup(peaks), dedup(troughs), 2 * amp


def stats(x, baseline, peaks, troughs):
    """min/max/median/mean по каналу и по детектированной кривой."""
    det_max = float(np.mean(x[peaks])) if len(peaks) else None
    det_min = float(np.mean(x[troughs])) if len(troughs) else None
    return {
        "min": float(np.min(x)),
        "max": float(np.max(x)),
        "median": float(np.median(x)),
        "mean": float(np.mean(x)),
        "кривая MAX": det_max,
        "кривая MIN": det_min,
        "кривая MID": (det_max + det_min) / 2 if det_max is not None else None,
        "циклов": len(peaks) + len(troughs),
    }


def fmt(v):
    return "—" if v is None else f"{v:.3f}"


def compare(a, b, keys):
    """Дельты одноимённых каналов между файлами."""
    out = []
    for name in keys:
        sa = a["stats"][name]
        sb = b["stats"][name]
        for key in ("min", "max", "median", "mean"):
            if sa[key] is not None and sb[key] is not None:
                out.append((name, key, sa[key], sb[key], sb[key] - sa[key]))
        for key in ("кривая MAX", "кривая MIN", "кривая MID"):
            if sa[key] is not None and sb[key] is not None:
                out.append((name, key, sa[key], sb[key], sb[key] - sa[key]))
    return out


def plot(folders, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_files = len(folders)
    fig, axes = plt.subplots(
        n_files + 1, 1,
        figsize=(11, 3.2 * (n_files + 1)),
        squeeze=False,
        gridspec_kw={"height_ratios": [2.2] * n_files + [1.0]},
    )

    for row, (path, d) in enumerate(folders):
        ax = axes[row][0]
        fs = 1.0 / np.median(np.diff(d["t"])) if len(d["t"]) > 1 else 600
        for name, color in (("RAW", "#d64"), ("FILTERED", "#19E6FF")):
            x = d[name]
            baseline, peaks, troughs, _ = detect_curve(x, fs)
            d["stats"].setdefault(name, stats(x, baseline, peaks, troughs))
            ax.plot(d["t"], x, color=color, lw=0.8, label=name)
            ax.plot(d["t"], baseline, color="#888", lw=1.0, ls="--",
                    label="baseline (дрейф)")
            ax.plot(d["t"][peaks], x[peaks], "rv", ms=4, label="MAX" if row == 0 else None)
            ax.plot(d["t"][troughs], x[troughs], "b^", ms=4, label="MIN" if row == 0 else None)
        ax.set_title(d["метка"], fontsize=10, loc="left")
        ax.grid(alpha=0.3)
        if row == 0:
            ax.legend(fontsize=8, ncol=4)

    # Сводная таблица min/max/median/mean.
    ax = axes[n_files][0]
    ax.axis("off")
    keys = [name for name, _ in CHANNELS]
    cols = ["канал", "min", "max", "median", "mean"]
    cell = []
    for name in keys:
        for _f, d in folders:
            st = d["stats"][name]
            cell.append([f"{d['метка']}·{name}", fmt(st["min"]), fmt(st["max"]),
                         fmt(st["median"]), fmt(st["mean"])])
    table = ax.table(cellText=cell, colLabels=cols, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(7)
    table.scale(1, 1.15)

    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="Сравнение xlsx-записей: кривая, min/max/median, график"
    )
    parser.add_argument("files", nargs="+", help="xlsx-файлы capture_*.xlsx (1-2)")
    parser.add_argument("-o", "--out", default=None, help="PNG результата")
    args = parser.parse_args()
    if not (1 <= len(args.files) <= 2):
        sys.exit("Укажите 1 или 2 xlsx-файла")

    folders = []
    for path in args.files:
        d = load_capture(path)
        d["stats"] = {}
        folders.append((path, d))

    # Статистика по всем каналам. Fs — по SPAN (t[-1]-t[0]): кадры
    # в xlsx тиспмтампированы моментом ВЫЧИТКИ пачки — dt
    # бимодальный (почти 0 внутри пачки, пауза между пачками),
    # медиана/медиана-dt завышает Fs на порядки.
    for path, d in folders:
        fs = (len(d["t"]) - 1) / (d["t"][-1] - d["t"][0]) \
            if len(d["t"]) > 1 and d["t"][-1] > d["t"][0] else 600.0
        for name, _ in CHANNELS:
            x = d[name]
            baseline, peaks, troughs, _ = detect_curve(x, fs)
            d["stats"][name] = stats(x, baseline, peaks, troughs)

    # Консольный вывод.
    for path, d in folders:
        print(f"\n=== {d['метка']} ===")
        print(f"{'канал':<12} {'min':>12} {'max':>12} {'median':>12} {'mean':>12} {'циклов':>7}")
        for name in keys_names():
            st = d["stats"][name]
            print(f"{name:<12} {fmt(st['min']):>12} {fmt(st['max']):>12} "
                  f"{fmt(st['median']):>12} {fmt(st['mean']):>12} {st['циклов']:>7}")
        for name in ("RAW", "FILTERED"):
            st = d["stats"][name]
            print(f"  кривая {name}: MAX={fmt(st['кривая MAX'])} "
                  f"MIN={fmt(st['кривая MIN'])} MID={fmt(st['кривая MID'])}")

    if len(folders) == 2:
        (pa, a), (pb, b) = folders
        print(f"\n=== ДЕЛЬТЫ: {b['метка']} − {a['метка']} ===")
        rows = compare(a, b, keys_names())
        print(f"{'канал':<12} {'параметр':<10} {'a':>12} {'b':>12} {'Δ':>12}")
        for name, key, va, vb, delta in rows:
            print(f"{name:<12} {key:<10} {fmt(va):>12} {fmt(vb):>12} {fmt(delta):>12}")

    # PNG.
    out = args.out or str(
        REPO_ROOT / "test" / (
            "compare_" + "_vs_".join(Path(p).stem.split("_", 1)[1].split("_")[0]
                                     for p, _ in folders) + ".png"
        )
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    plot(folders, out)
    print(f"\nГрафик: {out}")


def keys_names():
    return [name for name, _ in CHANNELS]


if __name__ == "__main__":
    main()
