"""Streamlit-приложение: анимация поездки по маршруту с потерянными точками.

Приложение читает route_with_losses.csv, само восстанавливает пропущенные
точки (calculations.prepare_route) и показывает карту, метрики текущего
положения и графики по всему маршруту.
"""
import time

import pandas as pd
import pydeck as pdk
import streamlit as st

from calculations import loss_mask, loss_runs, prepare_route, trip_summary
from charts import elevation_chart, fuel_chart, slope_chart, speed_chart
from constants import CRUISE_KMH

ROUTE_FILE = "route_with_losses.csv"   # маршрут с пропущенными точками
FRAME_DELAY_S = 0.2                    # пауза между кадрами анимации
STEP_OPTIONS = (1, 2, 5, 10, 25, 50)   # сколько точек маршрута проходит за кадр
CHARTS = (elevation_chart, slope_chart, speed_chart, fuel_chart)

# Кнопка и подпись состояния для каждого режима анимации
STATES = {
    "stopped": ("Start", "⚪ Stopped"),
    "running": ("Pause", "🟢 Started"),
    "paused": ("Continue", "🟡 Paused"),
}


@st.cache_data
def load_route():
    """Читает маршрут с потерями; prepare_route восстанавливает пропуски."""
    losses = pd.read_csv(ROUTE_FILE)
    return losses, prepare_route(losses)


def metrics_row(containers, row):
    """Показывает метрики текущего положения в подготовленных контейнерах."""
    speed_m, time_m, fuel_m, fuel_total_m, opt_m = containers
    speed_m.metric("Текущая скорость", f"{row.speed:.0f} км/ч")
    time_m.metric("Время в пути", f"{row.time:.0f} мин")
    fuel_m.metric("Расход топлива", f"{row.fuel:.1f} л/100км")
    fuel_total_m.metric("Израсходовано", f"{row.fuel_total:.1f} л")
    opt_m.metric("Оптимальная скорость", f"{row.optimal_speed:.0f} км/ч")


def route_map(df, current_path, point):
    """Карта pydeck: весь маршрут, пройденная часть и текущая точка."""
    path = [{"path": list(zip(df.longitude, df.latitude))}]
    driven = [{"path": list(zip(current_path.longitude, current_path.latitude))}]
    layers = [
        pdk.Layer("PathLayer", data=path, get_path="path",
                  get_color=[180, 180, 180, 150], get_width=3,
                  width_min_pixels=2, width_max_pixels=4),
        pdk.Layer("PathLayer", data=driven, get_path="path",
                  get_color=[0, 128, 0], get_width=4,
                  width_min_pixels=2, width_max_pixels=5),
        pdk.Layer("ScatterplotLayer",
                  data=[{"longitude": point.longitude, "latitude": point.latitude}],
                  get_position="[longitude, latitude]", get_color=[255, 0, 0],
                  get_radius=5, radius_units="pixels",
                  radius_min_pixels=3, radius_max_pixels=5),
    ]
    view = pdk.ViewState(latitude=df.latitude.mean(), longitude=df.longitude.mean(),
                         zoom=5, pitch=0)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view),
                    width="stretch")


st.set_page_config(layout="wide")
st.title("Trip simulation")

st.session_state.setdefault("anim_state", "stopped")
st.session_state.setdefault("frame", 0)
st.session_state.setdefault("step", STEP_OPTIONS[0])

try:
    losses, df = load_route()
except FileNotFoundError:
    st.error(f"Файл '{ROUTE_FILE}' не найден. Пожалуйста, проверьте путь.")
    st.stop()
except KeyError:
    st.error("Убедитесь, что в CSV-файле есть колонки 'latitude' и 'longitude'.")
    st.stop()

loss_points = loss_mask(losses)
loss_sections = loss_runs(loss_points)
summary = trip_summary(df)
total_frames = len(df)

st.info(STATES[st.session_state.anim_state][1])

start_column, reset_column = st.columns(2)
with start_column:
    # Одна кнопка на все три состояния: Start → Pause → Continue
    label = STATES[st.session_state.anim_state][0]
    if st.button(label):
        if label == "Start":
            st.session_state.frame = 0
            st.session_state.anim_state = "running"
        elif label == "Pause":
            st.session_state.anim_state = "paused"
        else:
            st.session_state.anim_state = "running"
        st.rerun()
with reset_column:
    if st.button("Reset"):
        st.session_state.anim_state = "stopped"
        st.session_state.frame = 0
        st.rerun()

with st.sidebar:
    st.subheader("Показатели поездки")
    containers = []
    for _ in range(5):
        containers.append(st.empty())
        st.divider()
    st.caption(
        f"Заданная скорость: **{CRUISE_KMH} км/ч**\n\n"
        f"Маршрут: **{summary['distance_km']:.0f} км**, "
        f"время в пути **{summary['time_h']:.1f} ч**, "
        f"средний расход **{summary['avg_consumption']:.1f} л/100км**, "
        f"всего **{summary['fuel_l']:.0f} л**"
    )
    st.divider()
    st.select_slider(
        "Шаг анимации, точек за кадр",
        options=STEP_OPTIONS,
        key="step",
        help="Меняет только скорость показа: расчёт и графики от шага не зависят.",
    )
    st.divider()
    st.caption(
        f"Файл `{ROUTE_FILE}`: потеряно **{int(loss_points.sum())}** точек "
        f"в **{len(loss_sections)}** участках — координаты восстановлены "
        f"линейно, высота — PCHIP."
    )


@st.fragment
def animation_fragment():
    """Кадр анимации: карта, метрики, графики и переход к следующему кадру.

    Шаг влияет только на то, сколько точек маршрута добавляется за один кадр.
    Расчёт (df), метрики и графики от шага не зависят: графики всегда строятся
    по всему маршруту, а pos_km указывает на текущую точку.
    """
    step = int(st.session_state.step)
    index = min(st.session_state.frame * step, total_frames - 1)
    index = max(1, index)                      # хотя бы одна точка уже пройдена
    current_path = df.iloc[:index]
    point = df.iloc[index - 1]

    metrics_row(containers, point)
    route_map(df, current_path, point)

    pos_km = float(point["dist_km"])
    for chart in CHARTS:
        st.plotly_chart(chart(df, pos_km), width="stretch",
                        config={"scrollZoom": True})

    if st.session_state.anim_state != "running":
        return
    if index < total_frames - 1:
        # кадр за кадром: st.rerun() перезапускает скрипт и рисует следующий кадр
        time.sleep(FRAME_DELAY_S)
        st.session_state.frame += 1
        st.rerun()
    else:
        st.session_state.anim_state = "stopped"
        st.success("Маршрут завершен!")


animation_fragment()
