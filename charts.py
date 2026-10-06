"""Графики показателей маршрута (Plotly).

Каждый график — линия по колонке DataFrame из calculations.prepare_route,
общая ось X — расстояние в километрах. Маркер pos_km отмечает текущую
позицию автомобиля.
"""
import numpy as np
import plotly.graph_objects as go

# Оформление
MARKER = dict(size=8, color="#d62828")
LINE = dict(width=2)
HOVER = "%{x:.2f} км<br>%{y:.2f}<extra></extra>"
LAYOUT = dict(
    hovermode="x unified",
    height=320,
    margin=dict(l=20, r=20, t=50, b=40),
    uirevision=True,      # сохранять зум между обновлениями
    dragmode="pan",
)


def _line(df, column, name, dash=None):
    """Линия по колонке маршрута с общей подсказкой."""
    return go.Scatter(
        x=df["dist_km"],
        y=df[column],
        mode="lines",
        name=name,
        line=dict(LINE, dash=dash),
        hovertemplate=HOVER,
    )


def _chart(df, traces, title, y_title, pos_km=None):
    """Общая сборка: линии, маркер текущей позиции, оси и оформление."""
    fig = go.Figure(traces)
    if pos_km is not None:
        i = int(np.argmin(np.abs(df["dist_km"].to_numpy() - pos_km)))
        fig.add_trace(go.Scatter(
            x=[df["dist_km"].iloc[i]],
            y=[traces[0].y[i]],
            mode="markers",
            marker=MARKER,
            showlegend=False,
            hoverinfo="skip",
        ))
    fig.update_layout(title=title, **LAYOUT)
    fig.update_xaxes(title_text="Расстояние, км",
                     range=[0, float(df["dist_km"].iloc[-1])])
    fig.update_yaxes(title_text=y_title)
    return fig


def elevation_chart(df, pos_km=None):
    """Профиль высоты, м."""
    return _chart(df, [_line(df, "altitude", "Высота")],
                  "Высота", "Высота, м", pos_km)


def slope_chart(df, pos_km=None):
    """Продольный уклон, % (с линией нуля)."""
    fig = _chart(df, [_line(df, "slope_pct", "Уклон")],
                 "Уклон", "Уклон, %", pos_km)
    fig.add_hline(y=0, line_width=1, line_color="gray")
    return fig


def speed_chart(df, pos_km=None):
    """Фактическая скорость и справочная оптимальная, км/ч."""
    traces = [_line(df, "speed", "Текущая<br>скорость"),
              _line(df, "optimal_speed", "Оптимальная<br>скорость", dash="dash")]
    return _chart(df, traces, "Скорость", "км/ч", pos_km)


def fuel_chart(df, pos_km=None):
    """Мгновенный расход топлива, л/100км."""
    return _chart(df, [_line(df, "fuel", "Расход")],
                  "Расход топлива", "л/100км", pos_km)
