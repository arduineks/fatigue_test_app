#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Анализ xlsx-записей capture_*.xlsx: детекция кривой, статистика,
график сходимости по частоте дискретизации.

Консольный скрипт-компаньон к capture_3s.py:

    python analyze_captures.py test/capture_150_*.xlsx test/capture_330_*.xlsx \
           test/capture_600_*.xlsx test/capture_1000_*.xlsx
    python analyze_captures.py a.xlsx b.xlsx        # 2 файла — попарные дельты
    python analyze_captures.py a.xlsx b.xlsx c.xlsx # 3+ — режим СХОДИМОСТИ

Режимы:
  - 1 файл: кривая с экстремумами + статистика.
  - 2 файла: то же + попарные дельты b−a.
  - 3+ файла (число в имени = SPS): ГРАФИК СХОДИМОСТИ — для каждого
    канала один график: oX = sample_seconds (число из имени файла:
    150, 330, 600, ...), oY = значение; серии min/max/median/mean и
    кривая MAX/MIN/MID; в консоли — отклонение от самой высокой SPS
    (сходимость измерения с ростом частоты дискретизации).
"""

import argparse
import re
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

PARAMS = ["min", "max", "median", "mean",
          "кривая MAX", "кривая MIN", "кривая MID"]


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


def per_cycle_stats(x, peaks, troughs):
    """min/max/median ДЛЯ КАЖДОГО цикла: сегменты по последовательным
    пикам (внутри сегмента один минимум — синусоидный цикл).
    Возвращает список dict: (i0, i1, min, max, median).
    """
    cycles = []
    for a, b in zip(peaks[:-1], peaks[1:]):
        seg = x[a:b + 1]
        cycles.append({
            "i0": int(a), "i1": int(b),
            "min": float(np.min(seg)),
            "max": float(np.max(seg)),
            "median": float(np.median(seg)),
        })
    return cycles


def fmt(v):
    return "—" if v is None else f"{v:.3f}"


def compare(a, b, keys):
    """Дельты одноимённых каналов между файлами."""
    out = []
    for name in keys:
        sa = a["stats"][name]
        sb = b["stats"][name]
        for key in PARAMS:
            if sa[key] is not None and sb[key] is not None:
                out.append((name, key, sa[key], sb[key], sb[key] - sa[key]))
    return out


def sps_of(meta_label):
    """Число из имени capture_<SPS>_... → int или None."""
    m = re.match(r"capture_(\d+)_", meta_label)
    return int(m.group(1)) if m else None


def plot_pair(folders, out_png):
    """1-2 файла: кривые с экстремумами + таблица статистики."""
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
            ax.plot(d["t"][peaks], x[peaks], "rv", ms=4,
                    label="MAX" if row == 0 else None)
            ax.plot(d["t"][troughs], x[troughs], "b^", ms=4,
                    label="MIN" if row == 0 else None)
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


def plot_convergence(folders, out_png):
    """3+ файла: график сходимости — oX = SPS из имени файла,
    oY = значение параметра; поsubplot на канал."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Сортировка по SPS.
    folders = sorted(folders, key=lambda fd: sps_of(fd[1]["метка"]) or 0)
    sps = [sps_of(d["метка"]) for _f, d in folders]
    x_labels = [str(s) for s in sps]

    series_colors = {
        "min": "#1a7f4f", "max": "#d0442c",
        "median": "#333333", "mean": "#777777",
        "кривая MAX": "#c9308f", "кривая MIN": "#2f5fd0",
        "кривая MID": "#7a5fd0",
    }
    series_markers = {
        "min": "v", "max": "^", "median": "o", "mean": "s",
        "кривая MAX": "P", "кривая MIN": "X", "кривая MID": "D",
    }

    fig, axes = plt.subplots(
        len(CHANNELS), 1,
        figsize=(9, 3.0 * len(CHANNELS)),
        squeeze=False,
    )

    for row, (name, _) in enumerate(CHANNELS):
        ax = axes[row][0]
        st_list = [d["stats"][name] for _f, d in folders]
        plotted = False
        for key in PARAMS:
            vals = [st[key] for st in st_list]
            if any(v is None for v in vals):
                continue
            spread = max(vals) - min(vals)
            if spread <= 0:
                spread = 1.0
            ax.plot(range(len(sps)), vals,
                    color=series_colors.get(key, "#555"),
                    marker=series_markers.get(key, "o"),
                    lw=1.2, ms=6,
                    label=key)
            plotted = True
        # Поцикловые точки (синусоида: min/max ≈ ±амплитуда цикла,
        # median ≈ midline) — разброс точек по вертикали показывает,
        # сходится ли измерение от цикла к циклу при данной SPS.
        if name in ("RAW", "FILTERED"):
            for ci, (_f, d) in enumerate(folders):
                cyc = d.get("cycles", {}).get(name, [])
                for key, color, marker in (
                        ("max", "#d0442c", "."),
                        ("min", "#1a7f4f", "."),
                        ("median", "#333333", "."),
                ):
                    vals = [c[key] for c in cyc]
                    if vals:
                        ax.scatter([ci] * len(vals), vals,
                                   s=8, alpha=0.25, color=color,
                                   marker=marker, zorder=0)
        ax.set_xticks(range(len(sps)))
        ax.set_xticklabels(x_labels)
        ax.set_xlabel("sample_seconds (SPS)")
        ax.set_ylabel(name)
        if name in ("RAW", "FILTERED"):
            # Нормировать ось вокруг среднего — ряды в отсчётах АЦП.
            center = np.mean([st["mean"] for st in st_list])
            lo, hi = ax.get_ylim()
            span = max(hi - lo, 1e-9)
            ax.set_ylim(center - 1.2 * span / 2, center + 1.2 * span / 2)
        ax.grid(alpha=0.35)
        if row == 0:
            ax.legend(fontsize=8, ncol=4)

    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def convergence_report(folders):
    """Консоль: значения параметров по SPS + отклонение от max SPS;
    поцикловые min/max/median (медиана по циклам ± разброс)."""
    folders = sorted(folders, key=lambda fd: sps_of(fd[1]["метка"]) or 0)
    sps = [sps_of(d["метка"]) for _f, d in folders]
    top_sps = sps[-1]

    print("\n=== СХОДИМОСТЬ: значение по SPS (Δ от максимальной SPS "
          f"{top_sps}) ===")
    for name in keys_names():
        print(f"\n--- {name} ---")
        header = f"{'параметр':<12}" + "".join(f"{s:>12}" for s in sps) + f"{'Δ(макс)':>12}"
        print(header)
        st_list = [d["stats"][name] for _f, d in folders]
        for key in PARAMS:
            vals = [st[key] for st in st_list]
            if any(v is None for v in vals):
                continue
            line = f"{key:<12}"
            for v in vals:
                line += f"{fmt(v):>12}"
            delta = vals[-1] - vals[0] if vals[0] is not None else None
            line += f"{fmt(delta):>12}"
            print(line)

    # Поцикловые min/max/median: сигнала — синусоида, поэтому для
    # синуса median цикла ≈ midline, min/max ≈ ±амплитуда.
    print("\n=== ПОЦИКЛОВО (синусоида: median цикла ≈ середина, "
          "min/max ≈ ±амплитуда) ===")
    for name in ("RAW", "FILTERED"):
        print(f"\n--- {name}: поциклово (медиана по циклам [min..max]) ---")
        header = f"{'параметр':<12}" + "".join(f"{s:>22}" for s in sps)
        print(header)
        for key in ("min", "max", "median"):
            cells = []
            for _f, d in folders:
                cyc = d.get("cycles", {}).get(name, [])
                if not cyc:
                    cells.append("—")
                    continue
                vals = [c[key] for c in cyc]
                med = float(np.median(vals))
                lo, hi = min(vals), max(vals)
                cells.append(f"{med:.1f} [{lo:.1f}..{hi:.1f}]")
            line = f"{key:<12}" + "".join(f"{c:>22}" for c in cells)
            print(line)
        # Амплитуда цикла (max−min)/2 по циклам и её разброс —
        # мера сходимости: чем плотнее к одной величине, тем лучше.
        line = f"{'амп (max-min)/2':<12}"
        for _f, d in folders:
            cyc = d.get("cycles", {}).get(name, [])
            if not cyc:
                line += f"{'—':>22}"
                continue
            amps = [(c["max"] - c["min"]) / 2 for c in cyc]
            med = float(np.median(amps))
            line += f"{med:.1f} [{min(amps):.1f}..{max(amps):.1f}]".rjust(22)
        print(line)


def main():
    parser = argparse.ArgumentParser(
        description="Анализ xlsx-записей: кривая, min/max/median, сходимость"
    )
    parser.add_argument("files", nargs="+", help="xlsx-файлы capture_*.xlsx")
    parser.add_argument("-o", "--out", default=None, help="PNG результата")
    args = parser.parse_args()
    if len(args.files) > 4:
        sys.exit("Максимум 4 файла (по одному на SPS)")

    folders = []
    for path in args.files:
        d = load_capture(path)
        d["stats"] = {}
        folders.append((path, d))

    # Статистика по всем каналам. Fs — по SPAN (t[-1]-t[0]): кадры
    # в xlsx таймстампированы моментом ВЫЧИТКИ пачки — dt
    # бимодальный (почти 0 внутри пачки, пауза между пачками),
    # медиана dt завышает Fs на порядки.
    for path, d in folders:
        fs = (len(d["t"]) - 1) / (d["t"][-1] - d["t"][0]) \
            if len(d["t"]) > 1 and d["t"][-1] > d["t"][0] else 600.0
        d["cycles"] = {}
        for name, _ in CHANNELS:
            x = d[name]
            baseline, peaks, troughs, _ = detect_curve(x, fs)
            d["stats"][name] = stats(x, baseline, peaks, troughs)
            if name in ("RAW", "FILTERED"):
                d["cycles"][name] = per_cycle_stats(x, peaks, troughs)

    # Консольный вывод.
    for path, d in folders:
        print(f"\n=== {d['метка']} ===")
        print(f"{'канал':<12} {'min':>12} {'max':>12} {'median':>12} "
              f"{'mean':>12} {'циклов':>7}")
        for name in keys_names():
            st = d["stats"][name]
            print(f"{name:<12} {fmt(st['min']):>12} {fmt(st['max']):>12} "
                  f"{fmt(st['median']):>12} {fmt(st['mean']):>12} "
                  f"{st['циклов']:>7}")
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
            print(f"{name:<12} {key:<10} {fmt(va):>12} {fmt(vb):>12} "
                  f"{fmt(delta):>12}")
    elif len(folders) > 2:
        convergence_report(folders)

    # PNG: 1-2 файла — кривые; 3+ — сходимость.
    tags = [Path(p).stem.split("_", 1)[1].split("_")[0] for p, _ in folders]
    out = args.out or str(
        REPO_ROOT / "test" / (
            ("compare_" + "_vs_".join(tags) if len(folders) <= 2
             else "convergence_" + "_".join(tags)) + ".png"
        )
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if len(folders) > 2:
        plot_convergence(folders, out)
    else:
        plot_pair(folders, out)
    print(f"\nГрафик: {out}")


def keys_names():
    return [name for name, _ in CHANNELS]


if __name__ == "__main__":
    main()
