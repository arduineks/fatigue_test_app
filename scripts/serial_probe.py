#!/usr/bin/env python
"""Диагностика serial-порта без GUI: открывает порт, читает N секунд.

Показывает: занятые байты, скорость потока, время потока CPU
(process_time) против реального времени — если чтение крутится
busy-loop'ом, CPU-время ≈ реальному (100% ядра).

Запуск:
    python scripts/serial_probe.py COM5        # Windows
    python scripts/serial_probe.py /dev/ttyACM0

Опционально: --seconds 15 --baud 115200
"""
import argparse
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=0.02,
                    help="таймаут чтения, с (как в SerialWorker)")
    args = ap.parse_args()

    print(f"Открываю {args.port} @ {args.baud}, timeout={args.timeout}")
    t_open0 = time.perf_counter()
    s = serial.Serial(port=args.port, baudrate=args.baud,
                      timeout=args.timeout)
    print(f"Открыто за {time.perf_counter() - t_open0:.3f} с")

    # Фиксация состояний линий: DTR/RTS как их выставил pyserial.
    print(f"DTR={s.dtr} RTS={s.rts}")

    t0 = time.perf_counter()
    cpu0 = time.process_time()
    total = 0
    reads = 0
    empty_instant = 0
    next_report = t0 + 2.0
    try:
        while time.perf_counter() - t0 < args.seconds:
            t_read0 = time.perf_counter()
            data = s.read(4096)
            dt = time.perf_counter() - t_read0
            if data:
                total += len(data)
                reads += 1
                empty_instant = 0
            else:
                if dt < 0.002:
                    empty_instant += 1
            now = time.perf_counter()
            if now >= next_report:
                cpu = time.process_time() - cpu0
                wall = now - t0
                print(
                    f"  t={wall:5.1f} c: байт={total:8d} "
                    f"чтений={reads:5d} "
                    f"CPU={cpu / wall * 100:5.1f}% "
                    f"мгнов.пустых подряд={empty_instant}"
                )
                next_report += 2.0
    except KeyboardInterrupt:
        pass
    finally:
        s.close()

    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    print()
    print(f"Итого: {total} байт за {wall:.2f} с "
          f"({total / max(wall, 1e-6):.0f} Б/с), "
          f"CPU потока {cpu / max(wall, 1e-6) * 100:.1f}%")
    if total == 0:
        print("Данных нет: поток не запущен (нужен START) или устройство не отвечает — это не тормоз.")
    if cpu / max(wall, 1e-6) > 0.8:
        print("ВНИМАНИЕ: чтение крутит ядро на 100% — таймаут чтения "
              "не работает на этой машине (busy-loop).")


if __name__ == "__main__":
    main()
