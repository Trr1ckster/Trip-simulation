"""Расчёт маршрута: восстановление пропусков, уклон, скорость и расход топлива.

Модель намеренно простая — две формулы физики и одна политика движения:

1) сопротивление движению
       F = m·g·(C_r·cosθ + sinθ) + ½·ρ·C_d·A·v²
   (качение + подъём + аэродинамика, θ = arctan(уклон));

2) расход топлива
       q = P / (η(P) · Q_л) + расход холостого хода
   где P = F·v — мощность на колёсах, Q_л ≈ 32 МДж — теплота сгорания литра
   бензина, а КПД η(P) берётся из паспортной кривой удельного расхода (BSFC):
   на малой нагрузке двигатель неэкономичен. Если сила отрицательна (спуск),
   двигатель тормозит — режим ПХХ;

3) скорость: цель CRUISE_KMH − K_SLOPE_SPEED·уклон (на спуске растёт, на
   подъёме падает), сверху ограничена MAX_KMH, перед подъёмом — предзагрузка
   ×PREVIEW_PULSE; профиль сглажен разгоном, накатом и торможением.

Оптимальная скорость — это тот же расход, минимизированный по сетке скоростей
(optimal_speed). Точка входа — prepare_route(): она восстанавливает потерянные
точки и дополняет маршрут колонками dist_km, slope_pct, speed, optimal_speed,
fuel, fuel_total и time.
"""
import numpy as np
import pandas as pd
from geopy.distance import geodesic
from scipy.interpolate import PchipInterpolator

from constants import (
    A_ACC, A_BRAKE, ALT_SMOOTH, BSFC_G_KWH, BSFC_POWER_KW, CDA, CR0,
    CRUISE_KMH, FUEL_DENSITY, G, GRADE_UP_SLOW, IDLE_L_H, K_SLOPE_SPEED,
    LHV_MJ_KG, MASS_KG, MAX_KMH, MIN_KMH, OPTIMAL_STEP_KMH, OVERRUN_L_H,
    PREVIEW_M, PREVIEW_PULSE, RHO_AIR, SLOPE_SMOOTH, SLOPE_WINDOW_M,
)

# Колонки, в которых файл с потерями хранит пустые значения (NaN)
LOST_COLS = ("longitude", "latitude", "altitude")

SPEED_GRID_KMH = np.arange(MIN_KMH, MAX_KMH + 1, OPTIMAL_STEP_KMH, dtype=float)


# ── Потери маршрута ─────────────────────────────────────────

def loss_mask(route):
    """Булев массив: True — точка потеряна (нет координат или высоты)."""
    return ~np.isfinite(route[list(LOST_COLS)].to_numpy(float)).all(axis=1)


def loss_runs(mask):
    """Непрерывные участки потерь: список пар (первый индекс, последний)."""
    runs = []
    for i in np.flatnonzero(mask):
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = int(i)
        else:
            runs.append([int(i), int(i)])
    return [(a, b) for a, b in runs]


def restore_losses(route):
    """Восстанавливает потерянные точки по соседним сохранившимся.

    Координаты — линейная интерполяция по номеру точки: дорога между
    граничными точками разрыва почти прямая, поэтому хорда точнее и надёжнее
    сплайнов. Высота — монотонный кубический сплайн PCHIP: он проходит через
    все известные точки, не «выстреливает» между ними и не даёт излома
    профиля на границе разрыва.

    Число точек и их порядок не меняются; крайние потерянные точки получают
    значение ближайшей сохранившейся (экстраполяции нет). Маршрут без
    пропусков возвращается как есть.
    """
    df = route.copy().reset_index(drop=True)
    x = np.arange(len(df), dtype=float)
    for col in LOST_COLS:
        values = df[col].to_numpy(float)
        known = np.isfinite(values)
        if known.all():
            continue
        if known.sum() < 2:
            raise ValueError(f"в колонке '{col}' меньше двух известных точек")
        df[col] = (PchipInterpolator(x[known], values[known])(x) if col == "altitude"
                   else np.interp(x, x[known], values[known]))
    return df


# ── Геометрия маршрута ──────────────────────────────────────

def step_distances(lat_deg, lon_deg):
    """Расстояния между соседними точками, м (геодезическая линия WGS84)."""
    points = list(zip(np.asarray(lat_deg, float), np.asarray(lon_deg, float)))
    steps = np.zeros(len(points))
    for i, (prev, cur) in enumerate(zip(points, points[1:]), start=1):
        steps[i] = geodesic(prev, cur).meters
    return steps


def smooth(values, window):
    """Скользящее среднее по окну точек (центрированное)."""
    return (pd.Series(values)
            .rolling(window, center=True, min_periods=1)
            .mean()
            .to_numpy())


def slope_pct(dist_m, alt_m):
    """Уклон, %, — перепад высоты на участке SLOPE_WINDOW_M к его длине.

    Окно ±150 м вокруг точки усредняет рельеф и убирает шум высоты, который
    дал бы разброс в десятки процентов на соседних точках.
    """
    d_lo = np.maximum(dist_m - SLOPE_WINDOW_M, dist_m[0])
    d_hi = np.minimum(dist_m + SLOPE_WINDOW_M, dist_m[-1])
    drop = np.interp(d_hi, dist_m, alt_m) - np.interp(d_lo, dist_m, alt_m)
    span = np.maximum(d_hi - d_lo, 1e-6)
    return smooth(drop / span * 100, SLOPE_SMOOTH)


def slope_ahead(slope, dist_m, window_m=PREVIEW_M):
    """Максимальный уклон в окне window_m впереди, % — предсказание рельефа.

    Для каждой точки берётся окно [s_i, s_i + window_m] по расстоянию и
    находится самый крутой подъём: по нему решается, разгоняться ли заранее.
    """
    last = len(dist_m) - 1
    ahead = np.empty(len(dist_m))
    for i, s in enumerate(dist_m):
        end = min(int(np.searchsorted(dist_m, s + window_m)), last)
        ahead[i] = slope[i:end + 1].max()
    return ahead


# ── Силы и топливо ──────────────────────────────────────────

def resistance(v_ms, grade):
    """Сопротивление движению, Н: качение + подъём + аэродинамика."""
    theta = np.arctan(grade)
    rolling = MASS_KG * G * CR0 * np.cos(theta)
    hill = MASS_KG * G * np.sin(theta)
    drag = 0.5 * RHO_AIR * CDA * v_ms ** 2
    return rolling + hill + drag


def efficiency(power_kw):
    """КПД двигателя по удельному расходу топлива (BSFC), доли.

    Удельный расход берётся из паспортной кривой BSFC_POWER_KW/BSFC_G_KWH:
    на малой нагрузке двигатель неэкономичен, минимум расхода — на средней.
    КПД = полезная энергия / затраченная:
        3.6 МДж/ч (1 кВт) / (BSFC, кг/кВт·ч · теплота сгорания, МДж/кг).
    """
    power_kw = np.asarray(power_kw, float)
    bsfc = np.interp(np.maximum(power_kw, 0.0), BSFC_POWER_KW, BSFC_G_KWH)
    return 3.6 / (bsfc / 1000 * LHV_MJ_KG)


def fuel_l100km(v_kmh, slope):
    """Расход топлива, л/100км: топливо на тягу + холостой ход или ПХХ.

    Мощность на колёсах P = F·v делится на КПД при этой нагрузке — столько
    мощности должен дать двигатель; деление на теплоту сгорания литра даёт
    литры в час, а на 100 км это же, делённое на скорость. Если сила
    отрицательна (спуск), двигатель тормозит — расход падает до OVERRUN_L_H.
    """
    v_kmh = np.asarray(v_kmh, float)
    v_ms = np.maximum(v_kmh, 1.0) / 3.6
    force = resistance(v_ms, np.asarray(slope, float) / 100)
    power_kw = np.maximum(force, 0.0) * v_ms / 1000

    energy_mj_l = LHV_MJ_KG * FUEL_DENSITY              # ≈ 32 МДж в литре
    fuel_l_h = power_kw / efficiency(power_kw) * 3.6 / energy_mj_l
    idle_l_h = np.where(force <= 0, OVERRUN_L_H, IDLE_L_H)
    return (np.where(force > 0, fuel_l_h, 0.0) + idle_l_h) \
        / np.maximum(v_kmh, 1.0) * 100


# ── Скорость ────────────────────────────────────────────────

def target_speed(slope, up_ahead):
    """Целевая скорость, км/ч: падает на подъёме, растёт на спуске.

    Если впереди (до PREVIEW_M) подъём круче GRADE_UP_SLOW, а сейчас ещё
    ровно, цель умножается на PREVIEW_PULSE — разгоняемся заранее, до горки.
    """
    target = np.clip(CRUISE_KMH - K_SLOPE_SPEED * slope, MIN_KMH, MAX_KMH)
    before_climb = (up_ahead > GRADE_UP_SLOW) & (slope <= GRADE_UP_SLOW)
    target = np.where(before_climb, target * PREVIEW_PULSE, target)
    return np.clip(target, MIN_KMH, MAX_KMH)


def apply_accel_limits(target_kmh, step_m, grade):
    """Профиль скорости, км/ч: тяга к цели, свободный накат, торможение.

    Прямой проход: если сопротивления достаточно, чтобы катиться, двигатель
    не работает и скорость «выкатывается» сама; иначе тянемся к цели с
    разгоном ≤ A_ACC. Обратный проход: подвод к ограничениям торможением
    ≤ A_BRAKE. Сверху всегда MAX_KMH, снизу MIN_KMH.
    """
    target = np.maximum(target_kmh, MIN_KMH) / 3.6
    v_top, v_min = MAX_KMH / 3.6, MIN_KMH / 3.6

    v = np.empty_like(target)
    v[0] = min(target[0], v_top)
    for i in range(1, len(v)):
        prev = v[i - 1]
        # накат: замедляет только сопротивление движению
        a_free = -resistance(prev, grade[i]) / MASS_KG
        v_free = np.sqrt(max(prev ** 2 + 2 * a_free * step_m[i], v_min ** 2))
        if v_free >= min(target[i], prev):              # катимся (выбег/спуск)
            v[i] = min(v_free, max(target[i], prev), v_top)
        else:                                           # нужна тяга к цели
            v[i] = min(np.sqrt(prev ** 2 + 2 * A_ACC * step_m[i]), target[i], v_top)
        v[i] = max(v[i], v_min)

    for i in range(len(v) - 2, -1, -1):                 # подвод к точке
        v_brake = np.sqrt(v[i + 1] ** 2 + 2 * A_BRAKE * step_m[i + 1])
        v[i] = min(v[i], max(v_brake, v_min))
    return v * 3.6


def optimal_speed(slope, limit_kmh):
    """Скорость с минимальным расходом на данном уклоне, км/ч.

    Перебор сетки MIN_KMH…MAX_KMH по той же формуле расхода fuel_l100km:
    для каждой скорости считается расход и берётся минимум. Скорости выше
    текущего ограничения (целевой скорости) отбрасываются — быстрее ехать
    всё равно нельзя. На итоговый расход функция не влияет, она строит
    только справочную линию на графике.
    """
    slope = np.asarray(slope, float)
    speeds, grades = np.meshgrid(SPEED_GRID_KMH, slope, indexing="xy")
    consumption = fuel_l100km(speeds, grades)

    allowed = SPEED_GRID_KMH[None, :] <= np.asarray(limit_kmh, float)[:, None] + 1e-9
    return SPEED_GRID_KMH[np.argmin(np.where(allowed, consumption, np.inf), axis=1)]


# ── Итоговый расчёт ─────────────────────────────────────────

def prepare_route(route):
    """Добавляет к маршруту расчётные колонки по всему пути."""
    df = restore_losses(route)
    n = len(df)

    step_m = step_distances(df["latitude"], df["longitude"])
    dist_m = np.cumsum(step_m)
    dist_m[0] = 0.0

    slope = slope_pct(dist_m, smooth(df["altitude"], ALT_SMOOTH))
    target = target_speed(slope, slope_ahead(slope, dist_m))
    speed = apply_accel_limits(target, step_m, slope / 100)
    v_ms = speed / 3.6

    consumption = fuel_l100km(speed, slope)
    fuel_seg = np.zeros(n)
    fuel_seg[1:] = (consumption[1:] + consumption[:-1]) / 2 * step_m[1:] / 1e5
    dt_s = np.zeros(n)
    dt_s[1:] = step_m[1:] / np.maximum((v_ms[1:] + v_ms[:-1]) / 2, 0.5)

    df["dist_km"] = dist_m / 1000
    df["slope_pct"] = slope
    df["speed"] = speed
    df["optimal_speed"] = optimal_speed(slope, target)
    df["fuel"] = np.where(speed > 1, consumption, np.nan)
    df["fuel_total"] = np.cumsum(fuel_seg)
    df["time"] = np.cumsum(dt_s) / 60
    return df


def trip_summary(df):
    """Итоги поездки по расчётным колонкам."""
    distance_km = float(df["dist_km"].iloc[-1])
    time_h = float(df["time"].iloc[-1]) / 60
    fuel_l = float(df["fuel_total"].iloc[-1])
    return {
        "distance_km": distance_km,
        "time_h": time_h,
        "fuel_l": fuel_l,
        "avg_consumption": fuel_l / distance_km * 100,
        "avg_speed": distance_km / time_h,
    }
