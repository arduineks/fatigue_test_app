"""SerialWorker — чтение/запись COM-порта в отдельном потоке.

Вынесено из GUI-потока (было: QTimer poll_serial каждые 5 мс —
чтение + парсинг + add_frame + repaint в GUI-потоке, из-за чего
UI фризил и терял кадры). Теперь:

- порт открывается в GUI-потоке (serial.Serial, чтобы ошибка
  открытия всплывала синхронно, как раньше), затем объект
  передаётся воркеру;
- воркер ТОЛЬКО читает байты блокирующим read(timeout) и шлёт
  их в GUI сигналом ``data_received`` (queued). Разбор кадров,
  FSM, обновление карточек — как и прежде в GUI-слоте;
- передача команд (TX) — через потокобезопасную очередь:
  GUI кладёт строку в очередь, воркер пишет её в порт и
  подтверждает сигналом ``tx_done`` (лог SND/RCV сохраняется);
- serial-объект трогает ТОЛЬКО воркер (кроме открытия и
  закрытия-страховки), поэтому гонок нет.

Все исключения внутри воркера ловятся и уходят сигналом
``error`` — GUI не роняем.
"""

import queue
import threading

from PyQt5.QtCore import QThread, pyqtSignal

from src.logging_setup import logger


# Таймаут блокирующего чтения порта в воркере (с). Порт открыт
# с SERIAL_TIMEOUT=0 (неблокирующий) — для воркера нужен
# короткий блокирующий таймаут, чтобы не крутиться в busy-loop.
WORKER_READ_TIMEOUT_S = 0.02


class SerialWorker(QThread):
    # Сырые байты из порта (доставляются в GUI-поток как
    # queued-сигнал). object — чтобы не связываться с
    # типами QByteArray/Python bytes.
    data_received = pyqtSignal(object)
    # Команда записана в порт: (команда, успех).
    tx_done = pyqtSignal(str, bool)
    # Ошибка ввода/вывода (текст) — для лога, GUI не роняем.
    error = pyqtSignal(str)

    READ_CHUNK = 4096

    def __init__(self, serial_port, parent=None):
        super().__init__(parent)
        self._serial = serial_port
        self._stop = threading.Event()
        self._tx_queue = queue.Queue()
        # Блокирующий read с коротким таймаутом: воркер спит в
        # порту, а не крутится в busy-loop, и при этом быстро
        # реагирует на stop/TX.
        try:
            self._serial.timeout = WORKER_READ_TIMEOUT_S
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def enqueue_tx(self, command):
        # Потокобезопасно: положить команду в очередь на запись.
        self._tx_queue.put(command)

    def stop(self, wait_ms=2000):
        # Попросить воркер завершиться; перед выходом он ещё
        # раз сбросит очередь TX (чтобы STOP успел уйти).
        self._stop.set()
        if not self.wait(wait_ms):
            logger.warning("SerialWorker: не завершился за %d мс", wait_ms)

    # ------------------------------------------------------------------
    def _drain_tx(self):
        while True:
            try:
                command = self._tx_queue.get_nowait()
            except queue.Empty:
                return

            try:
                data = command.encode("ascii")
                self._serial.write(data)
                self._serial.flush()
                self.tx_done.emit(command, True)
            except Exception as e:  # noqa: BLE001
                logger.exception("SND FAIL: %s", e)
                self.tx_done.emit(command, False)
                self.error.emit(f"TX FAIL: {e}")

    # ------------------------------------------------------------------
    def run(self):
        try:
            while True:
                self._drain_tx()

                if self._stop.is_set():
                    break

                try:
                    data = self._serial.read(self.READ_CHUNK)
                except Exception as e:  # noqa: BLE001
                    logger.exception("RCV FAIL: %s", e)
                    self.error.emit(f"RX FAIL: {e}")
                    # Пауза, чтобы не крутиться в цикле при
                    # отвалившемся порте.
                    self.msleep(50)
                    continue

                if data:
                    self.data_received.emit(bytes(data))
        except Exception as e:  # noqa: BLE001
            logger.exception("SerialWorker упал: %s", e)
            self.error.emit(f"WORKER FAIL: {e}")
        finally:
            # Последний сброс TX (STOP на отключении).
            try:
                self._drain_tx()
            except Exception:  # noqa: BLE001
                pass

            try:
                if self._serial is not None:
                    self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None
