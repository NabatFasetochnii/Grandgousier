from __future__ import annotations

import math


def select_separated_candidates(
    scored: list[tuple[float, int, int, float]],
    *,
    max_sources: int,
    min_separation_pix: float,
) -> list[tuple[float, float, float, float]]:
    """Apply the legacy greedy separation rule using a uniform spatial hash.

    Candidate order and the strict ``distance < min_separation`` rejection test
    are identical to the historical all-previous-sources scan. Only nearby
    accepted candidates are inspected, removing the O(N_selected^2) Python loop.
    """

    if max_sources <= 0:
        return []
    if min_separation_pix <= 0:
        return [
            (snr, float(y_int), float(x_int), peak)
            for snr, y_int, x_int, peak in scored[:max_sources]
        ]

    cell_size = float(min_separation_pix)
    sep2 = cell_size * cell_size
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    chosen: list[tuple[float, float, float, float]] = []

    for snr, y_int, x_int, peak in scored:
        if len(chosen) >= max_sources:
            break
        x = float(x_int)
        y = float(y_int)
        cx = int(math.floor(x / cell_size))
        cy = int(math.floor(y / cell_size))

        too_close = False
        for ix in range(cx - 1, cx + 2):
            for iy in range(cy - 1, cy + 2):
                for x0, y0 in grid.get((ix, iy), ()):
                    if (x - x0) ** 2 + (y - y0) ** 2 < sep2:
                        too_close = True
                        break
                if too_close:
                    break
            if too_close:
                break
        if too_close:
            continue

        chosen.append((snr, y, x, peak))
        grid.setdefault((cx, cy), []).append((x, y))

    return chosen
