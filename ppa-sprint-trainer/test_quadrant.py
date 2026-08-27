"""Unit tests for quadrant.py — each of the 8 quadrant cells is reachable and
the point/center coordinates are sane. Pure stdlib; run: python test_quadrant.py"""
import quadrant as Q

def _rep(**kw): return kw

# rugby "back" splits: F0/kg good=6.5, V0 good=9.5, Vmax good=9.8, tau split=1.15
FV_CASES = {
    "Well-rounded":       _rep(f0_rel_nkg=8.0, v0_mps=10.0),
    "Force-oriented":     _rep(f0_rel_nkg=8.0, v0_mps=8.0),
    "Velocity-oriented":  _rep(f0_rel_nkg=5.0, v0_mps=10.0),
    "Under-powered":      _rep(f0_rel_nkg=5.0, v0_mps=8.0),
}
AM_CASES = {
    "Complete":     _rep(max_v_ms=10.0, tau_s=0.9),
    "Accelerator":  _rep(max_v_ms=9.0,  tau_s=0.9),
    "Speedster":    _rep(max_v_ms=10.0, tau_s=1.3),
    "Developing":   _rep(max_v_ms=9.0,  tau_s=1.3),
}

def run():
    passed = failed = 0
    def check(name, got, want):
        nonlocal passed, failed
        ok = got == want
        passed += ok; failed += (not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got!r}, want {want!r}")

    print("FV quadrant:")
    for want, rep in FV_CASES.items():
        q = Q.fv_quadrant(rep, "back")
        check(want, q and q["quadrant"], want)
        # point/center within [0,1]
        for k in ("x", "y"):
            assert 0.0 <= q["point"][k] <= 1.0 and 0.0 <= q["center"][k] <= 1.0

    print("Accel-MaxV quadrant:")
    for want, rep in AM_CASES.items():
        q = Q.accel_maxv_quadrant(rep, "back")
        check(want, q and q["quadrant"], want)

    print("Missing-input safety:")
    check("no V0 -> fv None", Q.fv_quadrant(_rep(f0_rel_nkg=8.0), "back"), None)
    check("no tau -> am None", Q.accel_maxv_quadrant(_rep(max_v_ms=10.0), "back"), None)
    both = Q.quadrants(_rep(f0_rel_nkg=8.0, v0_mps=10.0, max_v_ms=10.0, tau_s=0.9), "back")
    check("quadrants() returns both", both["fv"]["quadrant"] == "Well-rounded"
          and both["accel_maxv"]["quadrant"] == "Complete", True)

    # forward norms differ (Vmax good=9.0) -> a 9.2 top speed flips strong
    q_back = Q.accel_maxv_quadrant(_rep(max_v_ms=9.2, tau_s=0.9), "back")      # <9.8 -> Accelerator
    q_fwd  = Q.accel_maxv_quadrant(_rep(max_v_ms=9.2, tau_s=0.9), "forward")   # >=9.0 -> Complete
    check("position norms applied", (q_back["quadrant"], q_fwd["quadrant"]),
          ("Accelerator", "Complete"))

    print(f"\n{passed}/{passed+failed} passed")
    return failed == 0

if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
