"""CycleAnalyzerMixin: детектор циклов (FSM), вынесен из
graph_widget.py механическим переносом (ADR-002/этап 0,
беклог №7). ForceGraphWidget наследует этот миксин; состояние
циклов остаётся на self (инициализация в ForceGraphWidget
__init__ не менялась).
"""

import math
import time

from logging_setup import logger


class CycleAnalyzerMixin:
        # =====================================================
        # CYCLE ANALYSIS
        # =====================================================

    def reset_cycle_analysis(self):

        self.cycle_count = 0
        self.cycle_max = None
        self.cycle_min = None
        self.cycle_mid = None
        self.cycle_amplitude = None

        # Сглаженная средняя линия (EMA по mid завершённых
        # циклов): линия «плывёт» вместо скачков между
        # отдельными циклами.
        self.cycle_mid_display = None
        self.cycle_mid_ema_alpha = 0.35

        self.cycle_state = "SEARCH_DIRECTION"
        self.cycle_prev_force = None
        self.cycle_candidate_max = None
        self.cycle_candidate_min = None
        self.cycle_max_value = None
        self.cycle_min_value = None
        self.cycle_mid_value = None
        self.cycle_turn_count = 0
        self.cycle_last_direction = 0

        # Подтверждённые перегибы текущего цикла (пики/
        # впадины). В линии MAX/MIN и в СРЕДНЕЕ попадают
        # только по завершении цикла — линии не дёргаются
        # в середине цикла.
        self.cycle_pending_max = None
        self.cycle_pending_min = None

    @property
    def macro_moving(self):
        # Флаг макро-перемещения траверсы (для карточек UI).
        return self._macro_moving

    @property
    def cycles_evaluating(self):
        # Циклы сейчас оцениваются (учитываются карточками).
        return (
                self._cycles_enabled
                and not self._macro_moving
        )

    def set_cycles_enabled(self, enabled):
        # Порог начала оценки циклов: включается, когда
        # управляемая величина в допуске от цели. На границе
        # включения состояние анализа сбрасывается — MID
        # учится заново на текущем уровне (счётчик циклов
        # не сбрасывается).
        enabled = bool(enabled)
        if enabled == self._cycles_enabled:
            return
        self._cycles_enabled = enabled
        if enabled:
            self.cycle_max = None
            self.cycle_min = None
            self.cycle_mid = None
            self.cycle_amplitude = None
            self.cycle_mid_display = None
            self.cycle_state = "SEARCH_DIRECTION"
            self.cycle_prev_force = None
            self.cycle_candidate_max = None
            self.cycle_candidate_min = None
            self.cycle_max_value = None
            self.cycle_min_value = None
            self.cycle_mid_value = None
            self.cycle_turn_count = 0
            self.cycle_last_direction = 0
            self.cycle_pending_max = None
            self.cycle_pending_min = None

    def set_traverse_speed(self, speed_mm_s):
        # Гейтинг циклов: при макро-перемещении траверсы
        # анализ циклов приостанавливается (показания
        # нестабильны), после остановки состояние
        # перезапускается — MID заново учится на новом уровне.
        # Небольшие подстройки регулятора во время осцилляции
        # гейтинг не трогают. Счётчик циклов не сбрасывается.
        try:
            speed = None if speed_mm_s is None else float(speed_mm_s)
        except (TypeError, ValueError):
            speed = None
        self.traverse_speed_mm_s = speed
        moving = (
                speed is not None
                and abs(speed) > self.macro_move_speed
        )
        if moving == self._macro_moving:
            return
        self._macro_moving = moving
        if moving:
            return
        # Конец макро-перемещения: сброс состояния анализа
        # (без счётчика циклов).
        self.cycle_max = None
        self.cycle_min = None
        self.cycle_mid = None
        self.cycle_amplitude = None
        self.cycle_mid_display = None
        self.cycle_state = "SEARCH_DIRECTION"
        self.cycle_prev_force = None
        self.cycle_candidate_max = None
        self.cycle_candidate_min = None
        self.cycle_max_value = None
        self.cycle_min_value = None
        self.cycle_mid_value = None
        self.cycle_turn_count = 0
        self.cycle_last_direction = 0
        self.cycle_pending_max = None
        self.cycle_pending_min = None

    def process_cycle(self, force):

        force = float(force)

        if self.cycle_prev_force is None:
            self.cycle_prev_force = force
            self.cycle_candidate_max = force
            self.cycle_candidate_min = force
            return

        delta = force - self.cycle_prev_force

        # Трекинг экстремумов — ДО dead-band скипа: у пика
        # синусоиды подъём за кадр меньше dead-band, и при
        # пропуске кадров candidate замирает ниже настоящего
        # пика (лесенка сверху на графике).
        if (
                self.cycle_last_direction > 0
                and force > self.cycle_candidate_max
        ):
            self.cycle_candidate_max = force

        if (
                self.cycle_last_direction < 0
                and force < self.cycle_candidate_min
        ):
            self.cycle_candidate_min = force

        if abs(delta) < self.cycle_tolerance * 0.15:
            self.cycle_prev_force = force
            return

        direction = 1 if delta > 0 else -1

        # Первое реальное направление движения.
        if self.cycle_last_direction == 0:
            self.cycle_last_direction = direction
            self.cycle_turn_count = 0
            if direction > 0:
                self.cycle_candidate_max = force
            else:
                self.cycle_candidate_min = force
            self.cycle_prev_force = force
            return

        # -------------------------------------------------
        # Движение вверх: ищем максимум (перегиб).
        # Подтверждение перегиба — по ВЕЛИЧИНЕ отката от
        # кандидата (>= cycle_tolerance), а не по числу
        # кадров против направления: у зашумлённого синуса
        # пара ниспадающих кадров бывает до пика, и при
        # покадровом подтверждении MAX замирает ниже
        # настоящего пика (лесенка сверху на графике).
        # -------------------------------------------------
        if self.cycle_last_direction > 0:

            if force > self.cycle_candidate_max:
                self.cycle_candidate_max = force

            if (
                    self.cycle_candidate_max - force
                    >= self.cycle_tolerance
            ):
                # Подтверждён перегиб-пик текущего цикла:
                # сила упала на допуск от кандидата, который
                # к этому моменту дошёл до настоящего пика.
                # В линию MAX попадёт при завершении цикла.
                self.cycle_pending_max = (
                        self.cycle_candidate_max
                )

                self.cycle_candidate_min = force
                self.cycle_last_direction = -1
                self.cycle_turn_count = 0

        # -------------------------------------------------
        # Движение вниз: ищем минимум (перегиб).
        # -------------------------------------------------
        else:

            if force < self.cycle_candidate_min:
                self.cycle_candidate_min = force

            if (
                    force - self.cycle_candidate_min
                    >= self.cycle_tolerance
            ):
                # Подтверждён перегиб-впадина текущего цикла.
                self.cycle_pending_min = (
                        self.cycle_candidate_min
                )

                # Порог детекции — по экстремумам ТЕКУЩЕГО
                # цикла (pending), как и раньше.
                if (
                        self.cycle_pending_max is not None
                        and (
                                self.cycle_pending_max
                                - self.cycle_pending_min
                                >= self.cycle_tolerance
                        )
                ):
                    self.cycle_mid_value = (
                                                   self.cycle_pending_max
                                                   + self.cycle_pending_min
                                           ) / 2.0

                    self.cycle_state = "WAIT_MID_UP"

                self.cycle_candidate_max = force
                self.cycle_last_direction = 1
                self.cycle_turn_count = 0

        # -------------------------------------------------
        # MID -> MAX -> MID -> MIN -> MID
        # -------------------------------------------------
        # После первого найденного MAX/MIN получаем реальный MID.
        # Далее цикл фиксируется при прохождении MID вверх после MIN.
        if (
                self.cycle_state == "WAIT_MID_UP"
                and self.cycle_mid_value is not None
                and (
                        self.cycle_pending_min is not None
                        or self.cycle_min_value is not None
                )
        ):
            if (
                    self.cycle_prev_force
                    < self.cycle_mid_value
                    and force >= self.cycle_mid_value
            ):
                self.cycle_count += 1

                # Завершение цикла: фиксируем подтверждённые
                # перегибы в линии MAX/MIN и в СРЕДНЕЕ.
                if self.cycle_pending_max is not None:
                    self.cycle_max_value = (
                            self.cycle_pending_max
                    )

                if self.cycle_pending_min is not None:
                    self.cycle_min_value = (
                            self.cycle_pending_min
                    )

                self.cycle_max = self.cycle_max_value
                self.cycle_min = self.cycle_min_value
                self.cycle_mid = self.cycle_mid_value

                # Сглаженная средняя линия: EMA по mid
                # завершённых циклов.
                if self.cycle_mid_display is None:
                    self.cycle_mid_display = (
                            self.cycle_mid_value
                    )
                else:
                    self.cycle_mid_display = (
                            self.cycle_mid_ema_alpha
                            * self.cycle_mid_value
                            + (
                                    1.0
                                    - self.cycle_mid_ema_alpha
                            )
                            * self.cycle_mid_display
                    )

                self.cycle_amplitude = (
                                               self.cycle_max
                                               - self.cycle_min
                                       ) / 2.0

                # Начинаем новый цикл с уже известного MID.
                self.cycle_max_value = force
                self.cycle_min_value = force
                self.cycle_pending_max = None
                self.cycle_pending_min = None
                self.cycle_candidate_max = force
                self.cycle_candidate_min = force
                self.cycle_state = "SEARCH_DIRECTION"
                self.cycle_last_direction = 1
                self.cycle_turn_count = 0

                logger.debug(
                        f"CYCLE #{self.cycle_count}: "
                        f"MAX={self.cycle_max:.3f} N, "
                        f"MIN={self.cycle_min:.3f} N, "
                        f"MID={self.cycle_mid:.3f} N, "
                        f"AMP={self.cycle_amplitude:.3f} N"
                )

        self.cycle_prev_force = force

