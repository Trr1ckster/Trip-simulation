"""Восстановление пропущенных точек маршрута.

Читает route_with_losses.csv — трек, в котором у 18 участков пустые
longitude/latitude/altitude, — восстанавливает эти точки и сохраняет
результат в каталог OUT_DIR (по умолчанию текущий) со столбцом dist_km
(накопленное расстояние от начала маршрута).

Координаты восстанавливаются линейно по номеру точки, высота — PCHIP
(см. calculations.restore_losses). Число строк и их порядок не меняются,
исходный файл с потерями не изменяется.

Запуск:
    python restore_route.py            # route_restored.csv рядом со скриптом
    python restore_route.py out        # out/route_restored.csv
"""
import pathlib
import sys

import pandas as pd

from calculations import LOST_COLS, loss_mask, loss_runs, restore_losses, \
    step_distances

LOSSES = "route_with_losses.csv"     # вход: маршрут с пропущенными точками
RESTORED_NAME = "route_restored.csv"  # имя выходного файла


def main():
    out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / RESTORED_NAME

    losses = pd.read_csv(LOSSES)
    runs = loss_runs(loss_mask(losses))
    restored = restore_losses(losses)

    if loss_mask(restored).any():
        raise RuntimeError("после восстановления остались пропущенные точки")

    route = restored.copy()
    step_m = step_distances(restored["latitude"], restored["longitude"])
    route["dist_km"] = step_m.cumsum() / 1000
    route.to_csv(target, index=False)

    lost = sum(b - a + 1 for a, b in runs)
    print(f"{LOSSES}: {len(losses)} точек")
    print(f"Восстановлено: {lost} точек ({lost / len(losses) * 100:.1f}%) "
          f"в {len(runs)} участках")
    print("Координаты — линейно по номеру точки, высота — PCHIP\n")

    for k, (a, b) in enumerate(runs, 1):
        print(f"  участок {k:>2}: индексы {a}-{b} ({b - a + 1} точек)")

    print(f"\nДлина маршрута: {route['dist_km'].iloc[-1]:.1f} км")
    print(f"Записано: {target} ({len(route)} строк, колонки "
          f"{', '.join([*LOST_COLS, 'dist_km'])})")


if __name__ == "__main__":
    main()
