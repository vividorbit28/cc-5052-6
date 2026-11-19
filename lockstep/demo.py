"""无头演示：把任务要求的验收点全部跑一遍并打印结果。

1) 同一脚本在不同帧率/延迟下逐帧摘要一致；
2) 录制输入脚本可回放、且回放对帧率/延迟免疫；
3) 注入扰动后能定位首次分歧（帧、单位、字段）；
4) 对端断线时"空输入替代"政策让双端继续且保持一致；
5) 慢帧率不丢步。
"""

from .replay import compare_configurations, verify
from .scripts import SELFTEST_TICKS, sorted_script
from .session import LockstepSession
from .transport import ManualClock


def _run_scripted(seed, latency_ms, frame_dt, ticks, policy="wait",
                  tamper_at=None, close_at=None):
    clock = ManualClock()
    session = LockstepSession(seed=seed, latency_ms=latency_ms, clock=clock,
                              missing_policy=policy)
    script = sorted_script()
    issued = 0
    guard = 0
    while session.min_tick() < ticks and guard < 400000 and not session.halted:
        t = session.min_tick()
        while issued < len(script) and script[issued][0] <= t:
            _, player, cmd = script[issued]
            session.issue(player, cmd)
            issued += 1
        if tamper_at is not None and t >= tamper_at:
            session.tamper()
            tamper_at = None
        if close_at is not None and t >= close_at:
            session.links[1].close()
            close_at = None
        clock.advance(frame_dt)
        session.update(frame_dt)
        guard += 1
    return session


def run(out=print):
    ok_all = True

    def check(name, ok, detail=""):
        nonlocal ok_all
        ok_all = ok_all and ok
        out(f"[{'PASS' if ok else 'FAIL'}] {name}{('：' + detail) if detail else ''}")

    # 1) 帧率/延迟无关性
    base = _run_scripted(7, 0, 1 / 60, SELFTEST_TICKS)
    fast = _run_scripted(7, 300, 1 / 20, SELFTEST_TICKS)
    slow = _run_scripted(7, 120, 1 / 15, SELFTEST_TICKS)
    d0 = dict(base.eps[0].digests)
    check("不同帧率/延迟下逐帧摘要一致",
          d0 == dict(fast.eps[0].digests) == dict(slow.eps[0].digests)
          and len(d0) >= SELFTEST_TICKS,
          f"{len(d0)} 帧 × 3 种配置")

    # 2) 录制 + 回放
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "rec.json")
    base.save_recording(path)
    from .replay import load_recording
    rec = load_recording(path)
    ok, _t, msg = verify(rec)
    check("输入脚本回放一致", ok, msg)
    ok2, msg2 = compare_configurations(rec, [(0, 1 / 60), (300, 1 / 20), (120, 1 / 30)])
    check("回放对帧率/延迟免疫", ok2, msg2)

    # 3) 分歧定位
    div_session = _run_scripted(7, 0, 1 / 60, 200, tamper_at=60)
    div = div_session.divergence()
    check("注入扰动后定位首次分歧",
          div is not None and div["tick"] == 60 and div["first_uid"] is not None,
          f"帧{div['tick']} 单位{div['first_uid']}：{div['text']}" if div else "未检出")

    # 4) 断线替代政策（第 60 帧断线，此时脚本指令已全部结算、无在途指令）
    drop = _run_scripted(7, 0, 1 / 60, 90, policy="zero", close_at=60)
    a, b = drop.eps
    common = set(a.digests) & set(b.digests)
    check("断线后空输入替代且双端一致",
          a.degraded and b.degraded and len(common) >= 90
          and all(a.digests[t] == b.digests[t] for t in common),
          f"{len(common)} 帧一致")

    # 5) 慢帧率不丢步：一次巨大的帧间隔后追平，一步不少
    clock = ManualClock()
    s = LockstepSession(seed=7, latency_ms=0, clock=clock, tick_dt=0.1)
    clock.advance(10.0)
    s.update(10.0)
    for _ in range(400):
        if s.min_tick() >= 100:
            break
        clock.advance(0.2)
        s.update(0.2)
    check("慢机器不偷偷少执行步", s.min_tick() >= 100,
          f"10 秒 backlog 追平到第 {s.min_tick()} 帧")

    out("演示结束：" + ("全部通过" if ok_all else "存在失败项"))
    return ok_all
