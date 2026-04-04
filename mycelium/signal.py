"""Signal field computation — the heart of stigmergy.

Tasks emit signals. Agents perceive and move toward the strongest signal.
Signal strength grows with time (urgency) and amplifies on failure.
Claimed or blocked tasks emit zero signal.
"""

import math
import time


def compute_signal(
    priority: float,
    failures: int,
    available_at: float,
    now: float | None = None,
) -> float:
    """Compute the signal strength for a ready task.

    Signal = urgency * temporal_pressure
    - urgency: base priority amplified by failure count
    - temporal_pressure: logarithmic growth with wait time (never decays)
    """
    if now is None:
        now = time.time()
    age = max(0.0, now - available_at)
    urgency = priority * (1.0 + 0.5 * failures)
    temporal = math.log1p(age / 30.0)  # ramps up over ~2 minutes
    return round(urgency * temporal, 4)
