"""Small statistics for the test plan: percentiles, bootstrap intervals and spreads (TEST_PLAN.md 7.4)."""

import random
import statistics
from collections.abc import Callable, Sequence
from typing import Any


def pct(xs: Sequence[float], q: float) -> float | None:
    """The ``q`` quantile (0 to 1) by linear interpolation; None for no data."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


def dist(xs: Sequence[float], qs: Sequence[float] = (0.5, 0.95)) -> dict[str, Any]:
    """n, the requested percentiles and the maximum; p99 and p99.9 only where n allows (section 3)."""
    out: dict[str, Any] = {"n": len(xs)}
    for q in qs:
        if q >= 0.99 and len(xs) < 1000:
            continue
        v = pct(xs, q)
        out[f"p{q * 100:g}"] = None if v is None else round(v, 3)
    out["max"] = round(max(xs), 3) if xs else None
    out["min"] = round(min(xs), 3) if xs else None
    return out


def bootstrap(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = statistics.fmean,
    n: int = 2000,
    seed: int = 1,
) -> dict[str, Any]:
    """Mean and 95 percent bootstrap interval over ``values`` (one value per seed)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    rng = random.Random(seed)
    boots = sorted(stat([rng.choice(vals) for _ in vals]) for _ in range(n))
    return {
        "mean": round(stat(vals), 6),
        "lo": round(boots[int(0.025 * (n - 1))], 6),
        "hi": round(boots[int(0.975 * (n - 1))], 6),
        "n": len(vals),
    }


def spread(values: dict[str, float]) -> dict[str, Any]:
    """Minimum, median and maximum across cells, with the worst (lowest) cell named."""
    vals = {k: v for k, v in values.items() if v is not None}
    if not vals:
        return {"min": None, "median": None, "max": None, "worst": None}
    worst = min(vals, key=lambda k: vals[k])
    return {
        "min": round(min(vals.values()), 6),
        "median": round(statistics.median(vals.values()), 6),
        "max": round(max(vals.values()), 6),
        "worst": worst,
    }
