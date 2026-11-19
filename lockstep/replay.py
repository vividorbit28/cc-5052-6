"""输入脚本录制与回放校验。

录制内容只有：初始配置 + 双端输入帧 + 逐帧摘要（用于校验）。
回放用同样的锁步内核重放输入帧；帧率与延迟只改变送达时机，
不得改变逐帧摘要——compare_configurations 正是这样验证的。
"""

import json

from .session import LockstepSession
from .transport import ManualClock


def save_recording(session, path):
    rec = {
        "config": {
            "seed": session.seed,
            "input_delay": session.input_delay,
            "tick_dt": session.tick_dt,
        },
        "frames": session.frames,
        "digests": {
            ep.name: {str(t): d for t, d in ep.digests.items()}
            for ep in session.eps
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)


def load_recording(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_replay(rec, latency_ms=0.0, frame_dt=0.05):
    """按录制帧重放，返回 {tick: digest}。不同帧率/延迟下结果必须相同。"""
    clock = ManualClock()
    scripted = {}
    for frame in rec["frames"]:
        scripted[(frame["player"], frame["tick"])] = frame
    cfg = rec["config"]
    session = LockstepSession(
        seed=cfg["seed"], latency_ms=latency_ms,
        input_delay=cfg["input_delay"], tick_dt=cfg["tick_dt"],
        clock=clock, scripted_frames=scripted,
    )
    recorded = rec["digests"]["端A"]
    max_tick = max(int(t) for t in recorded)
    guard = 0
    while session.min_tick() <= max_tick and guard < 400000:
        clock.advance(frame_dt)
        session.update(frame_dt)
        guard += 1
    if guard >= 400000:
        raise RuntimeError("回放停滞：锁步未能在预期内汇齐输入")
    return dict(session.eps[0].digests)


def verify(rec, latency_ms=0.0, frame_dt=0.05):
    """重放并与录制摘要逐帧比对。返回 (ok, 首个不一致帧, 描述)。"""
    got = run_replay(rec, latency_ms, frame_dt)
    recorded = {int(t): d for t, d in rec["digests"]["端A"].items()}
    for t in sorted(recorded):
        g = got.get(t)
        if g is None:
            return False, t, f"回放缺失第 {t} 帧"
        if g != recorded[t]:
            return False, t, (
                f"第 {t} 帧摘要不一致：录制={recorded[t][:16]} 回放={g[:16]}"
            )
    return True, None, f"回放一致：{len(recorded)} 帧摘要全部吻合"


def compare_configurations(rec, configs):
    """同一脚本在多种 (延迟ms, 帧步长s) 下重放，逐帧比对，定位首次分歧。"""
    runs = [((lat, dt), run_replay(rec, lat, dt)) for lat, dt in configs]
    base_cfg, base = runs[0]
    for cfg, digests in runs[1:]:
        for t in sorted(base):
            if t not in digests:
                return False, f"配置 {cfg} 缺失第 {t} 帧"
            if digests[t] != base[t]:
                return False, (
                    f"首次分歧在第 {t} 帧：{base_cfg} 与 {cfg} 摘要不同"
                )
    return True, f"{len(runs)} 种帧率/延迟配置 × {len(base)} 帧，摘要全部一致"
