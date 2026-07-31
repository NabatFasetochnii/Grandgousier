from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from grandgousier.quality.lightcurve_quality import ObjectScatterRow, build_rms_mag_summary


@dataclass
class RmsMagPlotResult:
    output_path: str
    n_objects: int
    n_plotted: int
    n_bins: int
    calibration_name: str | None
    y_key: str


def export_rms_mag_plot(
    object_rows: list[ObjectScatterRow],
    path: str | Path,
    *,
    title: str | None = None,
    dpi: int = 150,
    y_key: str = "mag_rms",
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    show_binned: bool = True,
    ylim_max: float | None = None,
) -> RmsMagPlotResult:
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    xs: list[float] = []
    ys: list[float] = []
    for row in object_rows:
        x = row.mag_median
        y = getattr(row, y_key, None)
        if x is None or y is None:
            continue
        try:
            xf = float(x)
            yf = float(y)
        except Exception:
            continue
        if not math.isfinite(xf) or not math.isfinite(yf):
            continue
        xs.append(xf)
        ys.append(yf)

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=dpi)
    ax.scatter(xs, ys, s=18, alpha=0.7)

    summary = []
    if show_binned:
        summary = build_rms_mag_summary(object_rows, bin_width=bin_width, min_objects_per_bin=min_objects_per_bin)
        bx = [row["mag_bin_center"] for row in summary if row.get("rms_median") is not None]
        by = [row["rms_median"] for row in summary if row.get("rms_median") is not None]
        if bx and by:
            ax.plot(bx, by, marker="o", linewidth=1.6, label="Binned median RMS")
            ax.legend(loc="best")

    calibration_name = object_rows[0].calibration_name if object_rows else None
    ax.set_title(title or f"RMS–magnitude: {calibration_name or 'unknown calibration'}")
    ax.set_xlabel("Median calibrated magnitude")
    ax.set_ylabel("RMS, mag" if y_key == "mag_rms" else "MAD, mag")
    ax.grid(True, alpha=0.3)
    if ylim_max is not None and math.isfinite(float(ylim_max)) and float(ylim_max) > 0:
        ax.set_ylim(0, float(ylim_max))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return RmsMagPlotResult(
        output_path=str(path),
        n_objects=len(object_rows),
        n_plotted=len(xs),
        n_bins=len(summary),
        calibration_name=calibration_name,
        y_key=y_key,
    )
