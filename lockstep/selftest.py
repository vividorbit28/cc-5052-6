"""无头自检：固定脚本跑固定帧数，逐帧打印状态摘要。

用于跨进程、跨 PYTHONHASHSEED 验证确定性：
    PYTHONHASHSEED=0 python3 -m lockstep --selftest > a.txt
    PYTHONHASHSEED=1 python3 -m lockstep --selftest > b.txt
    diff a.txt b.txt   # 必须完全相同
"""

from .scripts import SELFTEST_TICKS, sorted_script
from .session import LockstepSession
from .transport import ManualClock


def run(seed=20261118, latency_ms=120, ticks=SELFTEST_TICKS, out=print):
    clock = ManualClock()
    session = LockstepSession(seed=seed, latency_ms=latency_ms, clock=clock)
    script = sorted_script()
    issued = 0
    guard = 0
    while session.min_tick() < ticks and guard < 400000 and not session.halted:
        t = session.min_tick()
        while issued < len(script) and script[issued][0] <= t:
            _, player, cmd = script[issued]
            session.issue(player, cmd)
            issued += 1
        clock.advance(0.05)
        session.update(0.05)
        guard += 1
    for t, ok, da, _db in session.checks():
        out(f"tick {t:3d} {'OK ' if ok else 'DIV'} {da[:16]}")
    div = session.divergence()
    if div is not None:
        out(f"DIVERGED tick={div['tick']} uid={div['first_uid']} {div['text']}")
        return False
    out(f"SELFTEST OK ticks={session.min_tick()} checks={len(session.checks())}")
    return True
