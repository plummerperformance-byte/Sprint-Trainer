"""Sprint force-velocity model — trustworthy F0/V0/Pmax from a single trace.

Pure-Python port of the tether model from the `shorts` R package
(Jovanovic; mono-exponential sprint model of Furusawa-Hill-Parkinson /
Chelly-Denis / Samozino-Morin). No scipy dependency: Lambert W by Halley
iteration, curve fit by Levenberg-Marquardt with a numerical Jacobian.

The point of this module (vs the app's earlier naive avg-force-vs-avg-speed
regression) is that it fits the *shape* of the whole velocity trace and then
routes every derived value through reference_norms.validity_check(), so a bad
fit is reported as invalid instead of as a wrong number.

Core model (TAU = MSS/MAC):
    v(t) = MSS * (1 - e^(-t/TAU))
    t(d) = TAU*W(-e^(-d/(MSS*TAU) - 1)) + d/MSS + TAU
    v(d) = v(t(d))
    PMAX_rel = MSS*MAC/4    F0_rel = MAC (N/kg)    V0 = MSS

Public API:
    model_tether(distance, velocity)            -> {MSS, MAC, TAU, PMAX, r2, ...}
    model_tether_DC(distance, velocity)         -> as above + DC (start offset)
    create_fvp(MSS, MAC, bodymass)              -> {F0, V0, Pmax, FV_slope, ...}
    profile_from_trace(t, distance, velocity, bodymass, resisted=True)
                                                -> full gated coach-ready dict
"""
from __future__ import annotations

import math
from typing import Sequence, Optional

try:
    from reference_norms import validity_check
except Exception:  # allow standalone use / testing without the loader
    def validity_check(metric, value):
        return {"ok": True, "metric": metric, "value": value, "gate": None, "reason": "no gate"}

_E = math.e


# --------------------------------------------------------------------------
# Lambert W (principal branch W0), valid for x in [-1/e, 0)
# --------------------------------------------------------------------------
def lambert_w0(x: float) -> float:
    if x < -1.0 / _E - 1e-12:
        raise ValueError(f"lambert_w0 out of domain: {x}")
    if x >= -1.0 / _E and x < -1.0 / _E + 1e-9:
        return -1.0
    if x == 0.0:
        return 0.0
    # initial guess
    if x < -0.2:
        # near the branch point: series expansion in p = sqrt(2(e x + 1))
        p = math.sqrt(2.0 * (_E * x + 1.0))
        w = -1.0 + p - p * p / 3.0 + 11.0 * p ** 3 / 72.0
    else:
        w = x  # small |x|, W(x) ~ x
    # Halley iteration
    for _ in range(60):
        ew = math.exp(w)
        f = w * ew - x
        denom = ew * (w + 1.0) - (w + 2.0) * f / (2.0 * w + 2.0)
        if denom == 0.0:
            break
        dw = f / denom
        w -= dw
        if abs(dw) <= 1e-13 * (1.0 + abs(w)):
            break
    return w


# --------------------------------------------------------------------------
# Forward model
# --------------------------------------------------------------------------
def predict_velocity_at_time(t: float, MSS: float, MAC: float) -> float:
    TAU = MSS / MAC
    return MSS * (1.0 - math.exp(-t / TAU))


def predict_time_at_distance(d: float, MSS: float, MAC: float) -> float:
    TAU = MSS / MAC
    arg = -math.exp(-d / (MSS * TAU) - 1.0)
    return TAU * lambert_w0(arg) + d / MSS + TAU


def predict_velocity_at_distance(d: float, MSS: float, MAC: float) -> float:
    return predict_velocity_at_time(predict_time_at_distance(d, MSS, MAC), MSS, MAC)


def create_sprint_trace(MSS: float, MAC: float, times: Sequence[float], DC: float = 0.0):
    """Synthetic (distance, velocity) trace — used for round-trip testing."""
    TAU = MSS / MAC
    out = []
    for t in times:
        v = predict_velocity_at_time(t, MSS, MAC)
        d = MSS * t + MSS * TAU * (math.exp(-t / TAU) - 1.0) + DC
        out.append((d, v))
    return out


# --------------------------------------------------------------------------
# Levenberg-Marquardt fit (pure python, numerical Jacobian)
# --------------------------------------------------------------------------
def _solve(A, b):
    """Solve small linear system A x = b by Gaussian elimination."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-18:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def _lm_fit(model, params, xs, ys, lower, upper, iters=200):
    """Generic LM. model(x, params)->y. Returns (params, r2)."""
    def resid(p):
        return [model(x, p) - y for x, y in zip(xs, ys)]

    def sse(p):
        return sum(r * r for r in resid(p))

    p = list(params)
    lam = 1e-3
    cur = sse(p)
    npar = len(p)
    for _ in range(iters):
        r = resid(p)
        # numerical Jacobian
        J = [[0.0] * npar for _ in xs]
        for j in range(npar):
            h = 1e-6 * (1.0 + abs(p[j]))
            pj = p[:]
            pj[j] += h
            rj = resid(pj)
            for i in range(len(xs)):
                J[i][j] = (rj[i] - r[i]) / h
        # normal equations
        JtJ = [[sum(J[i][a] * J[i][b] for i in range(len(xs))) for b in range(npar)] for a in range(npar)]
        Jtr = [sum(J[i][a] * r[i] for i in range(len(xs))) for a in range(npar)]
        improved = False
        for _ in range(30):
            A = [[JtJ[a][b] + (lam * JtJ[a][a] if a == b else 0.0) for b in range(npar)] for a in range(npar)]
            step = _solve(A, [-v for v in Jtr])
            if step is None:
                lam *= 10
                continue
            cand = [min(max(p[k] + step[k], lower[k]), upper[k]) for k in range(npar)]
            s = sse(cand)
            if s < cur:
                p, cur = cand, s
                lam = max(lam / 3.0, 1e-9)
                improved = True
                break
            lam *= 10
            if lam > 1e12:
                break
        if not improved:
            break
    # r^2
    ybar = sum(ys) / len(ys)
    ss_tot = sum((y - ybar) ** 2 for y in ys) or 1e-12
    r2 = 1.0 - cur / ss_tot
    return p, r2


# --------------------------------------------------------------------------
# Public models
# --------------------------------------------------------------------------
def _clean(distance, velocity):
    xs, ys = [], []
    for d, v in zip(distance, velocity):
        if d is None or v is None:
            continue
        try:
            d = float(d); v = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(d) or math.isnan(v):
            continue
        xs.append(d); ys.append(v)
    return xs, ys


def model_tether(distance, velocity, use_observed_mss: bool = False) -> dict:
    """Fit MSS, MAC to a distance-velocity trace (tether device)."""
    xs, ys = _clean(distance, velocity)
    if len(xs) < 5:
        return {"ok": False, "reason": "too few samples"}
    mss0 = max(ys) if use_observed_mss else 7.0
    (MSS, MAC), r2 = _lm_fit(
        lambda d, p: predict_velocity_at_distance(d, p[0], p[1]),
        [mss0, 7.0], xs, ys, lower=[0.1, 0.1], upper=[20.0, 40.0],
    )
    TAU = MSS / MAC
    return {"ok": True, "MSS": MSS, "MAC": MAC, "TAU": TAU,
            "PMAX": MSS * MAC / 4.0, "r2": r2, "n": len(xs), "DC": 0.0}


def model_tether_DC(distance, velocity) -> dict:
    """As model_tether, plus a distance-correction DC for the trace's start
    offset (cable slack / where zero sits) — recommended for resisted reps."""
    xs, ys = _clean(distance, velocity)
    if len(xs) < 6:
        return {"ok": False, "reason": "too few samples"}
    (MSS, MAC, DC), r2 = _lm_fit(
        lambda d, p: predict_velocity_at_distance(max(d - p[2], 1e-6), p[0], p[1]),
        [7.0, 7.0, 0.0], xs, ys,
        lower=[0.1, 0.1, -5.0], upper=[20.0, 40.0, 5.0],
    )
    TAU = MSS / MAC
    return {"ok": True, "MSS": MSS, "MAC": MAC, "TAU": TAU,
            "PMAX": MSS * MAC / 4.0, "r2": r2, "n": len(xs), "DC": DC}


def create_fvp(MSS: float, MAC: float, bodymass: float = 75.0) -> dict:
    """MSS/MAC -> horizontal force-velocity profile (air resistance ignored;
    the dominant terms for a cable rig). F0_rel in N/kg == MAC by F=ma."""
    F0_rel = MAC                      # N/kg
    V0 = MSS                          # m/s
    Pmax_rel = MSS * MAC / 4.0        # W/kg
    F0 = F0_rel * bodymass
    Pmax = Pmax_rel * bodymass
    FV_slope = -F0_rel / V0 if V0 else None
    return {"F0": F0, "F0_rel": F0_rel, "V0": V0,
            "Pmax": Pmax, "Pmax_rel": Pmax_rel, "FV_slope": FV_slope}


def _rf_metrics(MSS, MAC, bodymass, g=9.81):
    """RFmax and DRF from the modelled RF-velocity relationship.
    RF(v) = Fh(v)/sqrt(Fh^2 + (m g)^2), Fh = m*(MAC - MAC/MSS * v)."""
    rows = []
    for k in range(3, 60):
        v = MSS * k / 60.0
        a = MAC - (MAC / MSS) * v
        Fh = bodymass * a
        if Fh <= 0:
            continue
        rf = Fh / math.sqrt(Fh * Fh + (bodymass * g) ** 2)
        rows.append((v, rf * 100.0))
    if len(rows) < 3:
        return None, None
    rfmax = max(r for _, r in rows)
    # DRF = slope of RF vs v (linear)
    n = len(rows)
    mx = sum(v for v, _ in rows) / n
    my = sum(r for _, r in rows) / n
    num = sum((v - mx) * (r - my) for v, r in rows)
    den = sum((v - mx) ** 2 for v, _ in rows) or 1e-9
    return rfmax, num / den


def profile_from_trace(t, distance, velocity, bodymass: float = 75.0,
                       resisted: bool = True) -> dict:
    """Fit a rep's trace and return a coach-ready, VALIDITY-GATED profile.

    Returns {ok, valid, model:{...}, fvp:{...}, checks:{metric:gate}, rejected:[...]}.
    `valid` is False if any gated metric fails — the UI should then show a flag,
    not the numbers.
    """
    fit = model_tether_DC(distance, velocity) if resisted else model_tether(distance, velocity)
    if not fit.get("ok"):
        return {"ok": False, "valid": False, "reason": fit.get("reason", "fit failed")}
    fvp = create_fvp(fit["MSS"], fit["MAC"], bodymass)
    rfmax, drf = _rf_metrics(fit["MSS"], fit["MAC"], bodymass)
    fvp["RFmax_pct"] = rfmax
    fvp["DRF_pct"] = drf

    # The physiological gates are FREE-SPRINT bounds (Samozino/Morin field
    # data). A resisted fit legitimately reads V0/F0/Pmax/RF outside them —
    # the load itself shifts the numbers — so applying free-sprint bounds to
    # a resisted rep rejects ordinary good reps (the same apples-to-oranges
    # hazard rep_analysis documents for the Haugen bands). Resisted reps are
    # therefore gated on fit quality only (tau sanity + r2 below); free reps
    # get the full physiological gate.
    if resisted:
        checks = {"tau_s": validity_check("tau_s", fit["TAU"])}
    else:
        checks = {
            "f0_nkg": validity_check("f0_nkg", fvp["F0_rel"]),
            "v0_ms": validity_check("v0_ms", fvp["V0"]),
            "pmax_wkg": validity_check("pmax_wkg", fvp["Pmax_rel"]),
            "rfmax_pct": validity_check("rfmax_pct", rfmax),
            "tau_s": validity_check("tau_s", fit["TAU"]),
        }
    rejected = [m for m, c in checks.items() if not c.get("ok")]
    # a very poor fit is itself a rejection
    if fit["r2"] < 0.80:
        rejected.append("r2")
    return {
        "ok": True, "valid": len(rejected) == 0,
        "model": fit, "fvp": fvp, "checks": checks, "rejected": rejected,
    }


# --------------------------------------------------------------------------
# Self-validation
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("== round-trip: recover known MSS=8.0, MAC=6.0 ==")
    times = [i * 0.02 for i in range(int(6 / 0.02) + 1)]
    trace = create_sprint_trace(8.0, 6.0, times)
    d = [p[0] for p in trace]; v = [p[1] for p in trace]
    m = model_tether(d, v)
    print(f"  MSS={m['MSS']:.4f} (exp 8.0)  MAC={m['MAC']:.4f} (exp 6.0)  r2={m['r2']:.5f}")
    assert abs(m["MSS"] - 8.0) < 0.02 and abs(m["MAC"] - 6.0) < 0.02, "round-trip failed"

    print("== round-trip with start offset DC=3.0 ==")
    trace = create_sprint_trace(8.5, 7.0, times, DC=3.0)
    d = [p[0] for p in trace]; v = [p[1] for p in trace]
    m = model_tether_DC(d, v)
    print(f"  MSS={m['MSS']:.3f} (exp 8.5)  MAC={m['MAC']:.3f} (exp 7.0)  DC={m['DC']:.3f} (exp 3.0)  r2={m['r2']:.5f}")

    print("== create_fvp(8.0, 6.0, 80kg) ==")
    print("  ", {k: round(x, 3) for k, x in create_fvp(8.0, 6.0, 80).items() if x is not None})

    print("== gated profile on a clean synthetic rep ==")
    p = profile_from_trace([0], d, v, bodymass=80, resisted=False)
    print("  valid:", p["valid"], "| rejected:", p["rejected"],
          "| F0_rel", round(p["fvp"]["F0_rel"], 2), "V0", round(p["fvp"]["V0"], 2),
          "RFmax", round(p["fvp"]["RFmax_pct"], 1))
    print("all good.")
