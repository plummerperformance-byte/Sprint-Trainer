"""hires.py — upgrade a just-recorded run to its high-resolution trace.

Live reps are captured at 10 Hz (Modbus poll). Straight after a rep ends we
want to replace that coarse trace with the rig's real high-frequency datalog
(~1 kHz) and re-run the analysis on it — so the coach view sharpens from blocky
to smooth within a couple of seconds, automatically.

The high-res DATA SOURCE is pluggable (a "provider"):
  - At the rig, register a provider that fetches the just-finished rep's 1 kHz
    trace from the HMI datalog (192.168.88.222) — the real source.
  - Until that's wired, `simulated_provider` synthesises a dense trace by
    spline-interpolating the coarse live samples, so the whole upgrade FLOW
    (fetch -> replace -> re-analyse -> UI sharpens) is exercisable today.

A provider is:  fn(ctx: dict) -> list[dict] | None
  ctx carries: rep_id, session_id, started_at, duration_s, and `coarse` (the
  rep's live samples: [{t_ms, v_mps, F_N, pos_m}, ...]).
  It returns dense samples in the SAME shape, or None if no high-res is available
  (the rep then just keeps its live trace — never worse than before).

Nothing here touches the DB or the app; ppa_service wires it in.
"""
from __future__ import annotations

from typing import Callable, Optional

# provider registry -------------------------------------------------------
_provider: Optional[Callable[[dict], Optional[list]]] = None


def register_provider(fn: Optional[Callable[[dict], Optional[list]]]) -> None:
    """Install the high-res source. Pass None to disable auto-upgrade."""
    global _provider
    _provider = fn


def is_configured() -> bool:
    """True when a high-res source is registered (auto-upgrade should fire)."""
    return _provider is not None


def fetch(ctx: dict) -> Optional[list]:
    """Call the registered provider for one rep. Returns dense samples or None.
    Never raises — a provider failure just means 'no upgrade this time'."""
    if _provider is None:
        return None
    try:
        return _provider(ctx)
    except Exception:
        return None


# spline helpers (pure python; no numpy) ----------------------------------
def _natural_cubic(xs, ys):
    """Natural cubic spline coefficients for strictly increasing xs."""
    n = len(xs)
    if n < 3:
        return None
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    alpha = [0.0] * n
    for i in range(1, n - 1):
        alpha[i] = (3.0 / h[i]) * (ys[i + 1] - ys[i]) - (3.0 / h[i - 1]) * (ys[i] - ys[i - 1])
    l = [1.0] + [0.0] * (n - 1)
    mu = [0.0] * n
    z = [0.0] * n
    for i in range(1, n - 1):
        l[i] = 2.0 * (xs[i + 1] - xs[i - 1]) - h[i - 1] * mu[i - 1]
        if l[i] == 0:
            l[i] = 1e-9
        mu[i] = h[i] / l[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]
    l[n - 1] = 1.0
    c = [0.0] * n
    b = [0.0] * n
    d = [0.0] * n
    for j in range(n - 2, -1, -1):
        c[j] = z[j] - mu[j] * c[j + 1]
        b[j] = (ys[j + 1] - ys[j]) / h[j] - h[j] * (c[j + 1] + 2.0 * c[j]) / 3.0
        d[j] = (c[j + 1] - c[j]) / (3.0 * h[j])
    return b, c, d


def _spline_interp(xs, ys, xq):
    """Evaluate a natural cubic spline (fitted on xs,ys) at query points xq.
    Falls back to linear if too few points."""
    n = len(xs)
    coeff = _natural_cubic(xs, ys)
    out = []
    if coeff is None:  # linear fallback
        for x in xq:
            j = 0
            while j < n - 2 and xs[j + 1] < x:
                j += 1
            span = (xs[j + 1] - xs[j]) or 1e-9
            out.append(ys[j] + (ys[j + 1] - ys[j]) * (x - xs[j]) / span)
        return out
    b, c, d = coeff
    j = 0
    for x in xq:
        while j < n - 2 and xs[j + 1] < x:
            j += 1
        while j > 0 and xs[j] > x:
            j -= 1
        dx = x - xs[j]
        out.append(ys[j] + b[j] * dx + c[j] * dx * dx + d[j] * dx * dx * dx)
    return out


# simulated provider ------------------------------------------------------
def simulate_dense(coarse: list, target_hz: int = 500) -> Optional[list]:
    """Synthesise a dense trace from coarse live samples by spline interpolation.
    Demo stand-in for the real HMI datalog — makes the upgrade flow visible now."""
    pts = [s for s in coarse if s.get("t_ms") is not None]
    if len(pts) < 4:
        return None
    pts.sort(key=lambda s: s["t_ms"])
    # de-duplicate identical timestamps (spline needs strictly increasing x)
    xs, keep = [], []
    last = None
    for s in pts:
        t = s["t_ms"] / 1000.0
        if last is not None and t <= last:
            t = last + 1e-4
        xs.append(t)
        keep.append(s)
        last = t
    t0, t1 = xs[0], xs[-1]
    dur = t1 - t0
    if dur <= 0:
        return None
    n_out = max(int(dur * target_hz), len(xs))
    tq = [t0 + dur * i / (n_out - 1) for i in range(n_out)]

    def chan(key):
        ys = [float(s.get(key) or 0.0) for s in keep]
        return _spline_interp(xs, ys, tq)

    v = chan("v_mps")
    f = chan("F_N")
    p = chan("pos_m")
    out = []
    for i, t in enumerate(tq):
        out.append({
            "t_ms": round((t - t0) * 1000.0, 1),
            "v_mps": round(v[i], 4),
            "F_N": round(f[i], 2),
            "pos_m": round(p[i], 4),
        })
    return out


def simulated_provider(ctx: dict) -> Optional[list]:
    """Provider wrapper around simulate_dense — reads ctx['coarse']."""
    return simulate_dense(ctx.get("coarse") or [], target_hz=ctx.get("target_hz", 500))


if __name__ == "__main__":
    # a coarse 10 Hz, 2 s rep -> dense 500 Hz
    import math
    coarse = [{"t_ms": i * 100, "v_mps": 8 * (1 - math.exp(-(i * 0.1) / 1.2)),
               "F_N": 600 * math.exp(-(i * 0.1) / 1.2) + 40,
               "pos_m": 8 * (i * 0.1) - 8 * 1.2 * (1 - math.exp(-(i * 0.1) / 1.2))}
              for i in range(21)]
    dense = simulate_dense(coarse, 500)
    print(f"coarse: {len(coarse)} samples ({len(coarse)/2:.0f} Hz over 2 s)")
    print(f"dense : {len(dense)} samples ({len(dense)/2:.0f} Hz)")
    print(f"peak v coarse {max(s['v_mps'] for s in coarse):.3f} -> dense {max(s['v_mps'] for s in dense):.3f}")
    print("provider configured?", is_configured())
    register_provider(simulated_provider)
    print("after register:", is_configured(),
          "| fetch ->", len(fetch({"coarse": coarse})), "samples")
