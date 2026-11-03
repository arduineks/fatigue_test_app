"""CycleAnalyzerMixin: детектор циклов (FSM), вынесен из
graph_widget.py механическим переносом (ADR-002/этап 0,
беклог №7). ForceGraphWidget наследует этот миксин; состояние
циклов остаётся на self (инициализация в ForceGraphWidget
__init__ не менялась).
"""

import math
import statistics
import time
from collections import deque

from src.logging_setup import logger


class CycleAnalyzerMixin:

    # Робастная оценка экстремумов: в течение фазы подъёма
    # (спада) ВСЕ кадры копятся в буфер; при подтверждении
    # перегиба рядом с RAW-кандидатом запоминается робастная
    # оценка — МЕДИАНА CYCLE_PEAK_TOP_N самых высоких (низких)
    # кадров фазы. Одиночный выброс (глитч, капля) вытесняет
    # один слот из топ-N и медианой отбрасывается; на чистом
    # синусе медиана топ-N лежит чуть ниже одиночного пика —
    # усреднение по диапазону точек пика. При 330 SPS и 20-25 Гц
    # цикл — всего ~14 кадров, поэтому полоса-«площадка»
    # ненадёжна (у пика 1-2 кадра), а топ-N работает при любой
    # плотности.
    #
    # ВАЖНО (реплей стендовых данных 02-11-2026): робастные
    # значения используются ТОЛЬКО в отчётных атрибутах
    # (карточки MAX/MIN/СРЕДНЕЕ, линия MID, CSV, DS-регулятор).
    # ДЕТЕКЦИЯ циклов (порог pending_max - pending_min >=
    # cycle_tolerance, пересечение MID, счётчик) работает на
    # СЫРЫХ экстремумах — иначе на мелких шумовых колебаниях
    # (амплитуда ~ допуска) робастное сжатие пиков меняет
    # семантику счётчика (реплей: OLD 1253 / NEW 1181 цикла —
    # счётчик занижался на 5.7%, недопустимо для останова по
    # целевому числу циклов).
    CYCLE_PEAK_TOP_N = 5
    # Ограничение буфера фазы (кадров): у медленных циклов фаза
    # между подтверждёнными перегибами длиннее — 512 кадров
    # (~1.5 с при 330 SPS) достаточно, при переполнении
    # deque выталкивает самые старые точки.
    CYCLE_PEAK_BUF_MAX = 512
    # Адаптивный порог подтверждения перегиба: откат от
    # кандидата должен превышать max(cycle_tolerance, эта доля
    # РАЗМАХА последнего завершённого цикла). Причина (реплей
    # стендовых данных 02-11-2026, эталон пользователя: 19
    # полных смен в точках 0-2000): фиксированный порог
    # cycle_tolerance (0.03 Н) у дна синусоиды ловил ШУМОВЫЕ
    # wiggle ~0.06 Н как «перегиб» — детектор насчитывал
    # мусорные микро-циклы (MAX=0.317 при реальном пике 1.15),
    # терял при этом реальные циклы (18 вместо 19 на эталоне)
    # и «разносил» поддержание среднего. Реальные перегибы
    # (откат ~0.9 Н) подтверждаются с запасом.
    CYCLE_TURN_CONFIRM_FRAC = 0.2
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
        self.cycle_pending_max_robust = None
        self.cycle_pending_min_robust = None

        # Адаптивный порог подтверждения перегиба (см.
        # CYCLE_TURN_CONFIRM_FRAC): до первого завершённого цикла
        # — фиксированный допуск, дальше — доля размаха.
        self.cycle_turn_pullback = self.cycle_tolerance

        # Буферы фаз текущего полупериода (робастная оценка
        # MAX/MIN, см. CYCLE_PEAK_TOP_N).
        self.cycle_max_plateau = deque(
            maxlen=self.CYCLE_PEAK_BUF_MAX
        )
        self.cycle_min_plateau = deque(
            maxlen=self.CYCLE_PEAK_BUF_MAX
        )

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
            self.cycle_pending_max_robust = None
            self.cycle_pending_min_robust = None
            self.cycle_turn_pullback = self.cycle_tolerance
            self.cycle_max_plateau.clear()
            self.cycle_min_plateau.clear()

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
        self.cycle_pending_max_robust = None
        self.cycle_pending_min_robust = None
        self.cycle_turn_pullback = self.cycle_tolerance
        self.cycle_max_plateau.clear()
        self.cycle_min_plateau.clear()

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
        # Буферы фаз для робастной оценки экстремумов
        # (см. CYCLE_PEAK_TOP_N): собираются ВСЕ кадры фазы,
        # до dead-band скипа.
        if self.cycle_last_direction > 0:
            if force > self.cycle_candidate_max:
                self.cycle_candidate_max = force

            self.cycle_max_plateau.append(force)

        if self.cycle_last_direction < 0:
            if force < self.cycle_candidate_min:
                self.cycle_candidate_min = force

            self.cycle_min_plateau.append(force)

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
                    >= self.cycle_turn_pullback
            ):
                # Подтверждён перегиб-пик текущего цикла:
                # сила упала на допуск от кандидата, который
                # к этому моменту дошёл до настоящего пика.
                # В линию MAX попадёт при завершении цикла.
                # Порог детекции — по RAW-кандидату (семантика
                # счётчика неизменна, см. комментарий у
                # CYCLE_PEAK_TOP_N); рядом запоминается робастная
                # оценка величины пика (медиана топ-N кадров
                # фазы) — она пойдёт в карточки/CSV/MID.
                self.cycle_pending_max = (
                        self.cycle_candidate_max
                )
                self.cycle_pending_max_robust = (
                    self._plateau_top_median(
                        self.cycle_max_plateau,
                        self.cycle_candidate_max,
                        is_max=True,
                    )
                )
                self.cycle_max_plateau.clear()

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
                    >= self.cycle_turn_pullback
            ):
                # Подтверждён перегиб-впадина текущего цикла.
                # Порог детекции — по RAW-кандидату; рядом
                # робастная оценка величины впадины
                # (симметрично пику).
                self.cycle_pending_min = (
                        self.cycle_candidate_min
                )
                self.cycle_pending_min_robust = (
                    self._plateau_top_median(
                        self.cycle_min_plateau,
                        self.cycle_candidate_min,
                        is_max=False,
                    )
                )
                self.cycle_min_plateau.clear()

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
                # Счётчик и частота — всегда (гейтинг влияет
                # только на карточки MAX/MIN/MID/AMP).
                self.cycle_count += 1

                if self.cycles_evaluating:
                    # Завершение цикла в гейте: фиксируем
                    # подтверждённые перегибы в линии MAX/MIN
                    # и в СРЕДНЕЕ. В ОТЧЁТНЫЕ значения идут
                    # РОБАСТНЫЕ оценки (медиана топ-N кадров
                    # фазы пика), детекция выше работала на
                    # RAW — счётчик не менялся (см.
                    # CYCLE_PEAK_TOP_N). Робастная оценка
                    # недоступна (перегиб без буфера) — откат
                    # к RAW-значению.
                    if self.cycle_pending_max is not None:
                        self.cycle_max_value = (
                            self.cycle_pending_max_robust
                            if self.cycle_pending_max_robust
                            is not None
                            else self.cycle_pending_max
                        )

                    if self.cycle_pending_min is not None:
                        self.cycle_min_value = (
                            self.cycle_pending_min_robust
                            if self.cycle_pending_min_robust
                            is not None
                            else self.cycle_pending_min
                        )

                    # Страховка от инверсии: на мусорных
                    # микро-структурах фазы обучения (начальный
                    # спад, реплей 02-11-2026 кадр 56) робастные
                    # оценки могут дать MAX < MIN — такие оценки
                    # бракуются в пользу RAW- pending (те упорядо-
                    # чены по построению: кандидат-максимум не
                    # ниже кандидата-минимума текущего полупериода).
                    if (
                        self.cycle_max_value is not None
                        and self.cycle_min_value is not None
                        and self.cycle_max_value
                        < self.cycle_min_value
                    ):
                        self.cycle_max_value = (
                            self.cycle_pending_max
                        )
                        self.cycle_min_value = (
                            self.cycle_pending_min
                        )

                    self.cycle_max = self.cycle_max_value
                    self.cycle_min = self.cycle_min_value

                    # Отчётный MID — середина РОБАСТНЫХ пиков
                    # (средняя линия от робастных MIN/MAX,
                    # как запрошено); FSM-пересечения выше
                    # детектируются по сырому mid_value.
                    self.cycle_mid = (
                        (self.cycle_max + self.cycle_min)
                        / 2.0
                    )

                    # Сглаженная средняя линия: EMA по
                    # робастному mid завершённых циклов.
                    if self.cycle_mid_display is None:
                        self.cycle_mid_display = (
                                self.cycle_mid
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

                    # Адаптивный порог следующего перегиба:
                    # доля размаха только что завершённого
                    # цикла, не ниже фиксированного допуска
                    # (см. CYCLE_TURN_CONFIRM_FRAC).
                    self.cycle_turn_pullback = max(
                        self.cycle_tolerance,
                        self.CYCLE_TURN_CONFIRM_FRAC
                        * (self.cycle_max - self.cycle_min),
                    )

                # Начинаем новый цикл с уже известного MID.
                self.cycle_max_value = force
                self.cycle_min_value = force
                self.cycle_pending_max = None
                self.cycle_pending_min = None
                self.cycle_pending_max_robust = None
                self.cycle_pending_min_robust = None
                self.cycle_max_plateau.clear()
                self.cycle_min_plateau.clear()
                self.cycle_candidate_max = force
                self.cycle_candidate_min = force
                self.cycle_state = "SEARCH_DIRECTION"
                self.cycle_last_direction = 1
                self.cycle_turn_count = 0

                if self.cycles_evaluating:
                    logger.debug(
                            f"CYCLE #{self.cycle_count}: "
                            f"MAX={self.cycle_max:.3f} N, "
                            f"MIN={self.cycle_min:.3f} N, "
                            f"MID={self.cycle_mid:.3f} N, "
                            f"AMP={self.cycle_amplitude:.3f} N"
                    )
                else:
                    logger.debug(
                            f"CYCLE #{self.cycle_count}: "
                            f"вне гейта — счёт только "
                            f"(MID={self.cycle_mid_value:.3f} N)"
                    )

        self.cycle_prev_force = force

    def _plateau_top_median(self, plateau, candidate, is_max):
        # Робастная оценка экстремума: медиана CYCLE_PEAK_TOP_N
        # самых высоких (is_max=True) или самых низких (False)
        # кадров фазы. Пустой буфер (кандидат был установлен до
        # первого кадра фазы) — откат к старому поведению
        # (кандидат как есть).
        #
        # Для ЧЁТНОГО числа точек медиана — среднее двух средних,
        # и на буфере вида [выброс, реальный кадр] даёт мусор
        # посередине (реплей: [1.23, 0.68] -> 0.95). Поэтому
        # смещаем оценку ВНУТРЬ сигнала: для MAX — median_low
        # (нижний из двух средних), для MIN — median_high. При
        # нечётном топ-N (штатный случай) это обычная медиана.
        try:
            points = sorted(
                plateau,
                reverse=bool(is_max),
            )[: self.CYCLE_PEAK_TOP_N]
        except TypeError:
            return candidate

        if not points:
            return candidate

        if is_max:
            return statistics.median_low(points)

        return statistics.median_high(points)

