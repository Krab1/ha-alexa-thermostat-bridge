"""Pure helper: keep Auto-mode heat/cool setpoints at least MIN_RANGE_GAP apart."""
from __future__ import annotations

from .const import MIN_RANGE_GAP


def enforce_gap(
    low: float,
    high: float,
    moved_high: bool,
    moved_low: bool,
    min_temp: float,
    max_temp: float,
) -> tuple[float, float]:
    """Return (low, high) with high - low >= MIN_RANGE_GAP.

    The handle the user did not move is pushed; if that hits a limit, the
    moved handle gives way instead. When it's unclear (both or neither
    moved), low is treated as the one the user moved.
    """
    if high - low >= MIN_RANGE_GAP:
        return low, high
    if moved_high and not moved_low:
        low = max(min_temp, high - MIN_RANGE_GAP)
        high = max(high, low + MIN_RANGE_GAP)
    else:
        high = min(max_temp, low + MIN_RANGE_GAP)
        low = min(low, high - MIN_RANGE_GAP)
    return low, high
