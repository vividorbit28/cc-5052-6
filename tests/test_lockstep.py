"""锁步协议测试：帧率/延迟无关性、等待政策、分歧定位、断线政策、不丢帧。"""

import os
import subprocess
import sys
import unittest

from lockstep.commands import make_move
from lockstep.scripts import demo_script
from lockstep.session import LockstepSession
from lockstep.transport import ManualClock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_scripted(session, clock, script, ticks, frame_dt=0.05):
    script = sorted(script, key=lambda s: (s[0], s[1]))
    issued = 0
    guard = 0
    while session.min_tick() < ticks and guard < 400000 and not session.halted:
        t = session.min_tick()
        while issued < len(script) and script[issued][0] <= t:
            _, player, cmd = script[issued]
            session.issue(player, cmd)
            issued += 1
        clock.advance(frame_dt)
        session.update(frame_dt)
        guard += 1
    return session


class TestDeterminismAcrossConfigs(unittest.TestCase):
    def test_same_digests_across_latency_and_framerate(self):
        runs = []
        for latency_ms, frame_dt in ((0, 1 / 60), (250, 1 / 20), (80, 1 / 30)):
            clock = ManualClock()
            s = LockstepSession(seed=99, latency_ms=latency_ms, clock=clock)
            run_scripted(s, clock, demo_script(), 120, frame_dt)
            runs.append(dict(s.eps[0].digests))
        self.assertGreaterEqual(len(runs[0]), 120)
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], runs[2])

    def test_hash_seed_independence(self):
        outs = []
        for hash_seed in ("0", "1", "424242"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            proc = subprocess.run(
                [sys.executable, "-m", "lockstep", "--selftest"],
                capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outs.append(proc.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])


class TestPacing(unittest.TestCase):
    def test_slow_frames_never_skip_ticks(self):
        clock = ManualClock()
        s = LockstepSession(seed=1, latency_ms=0, clock=clock, tick_dt=0.1)
        clock.advance(10.0)
        s.update(10.0)  # 一次巨大的帧间隔：应开始追帧而不是丢帧
        progressed_first = s.min_tick()
        self.assertGreater(progressed_first, 0)
        for _ in range(400):
            if s.min_tick() >= 100:
                break
            clock.advance(0.2)
            s.update(0.2)
        self.assertGreaterEqual(s.min_tick(), 100)  # 10s / 0.1s = 100 帧一步不少

    def test_tick_rate_matches_schedule_not_frame_rate(self):
        for frame_dt in (1 / 60, 1 / 17):
            clock = ManualClock()
            s = LockstepSession(seed=1, latency_ms=0, clock=clock, tick_dt=0.1)
            wall = 0.0
            while wall < 5.0:
                clock.advance(frame_dt)
                s.update(frame_dt)
                wall += frame_dt
            self.assertGreaterEqual(s.min_tick(), 49)
            self.assertLessEqual(s.min_tick(), 52)


class TestWaitPolicy(unittest.TestCase):
    def test_wait_status_reports_source(self):
        clock = ManualClock()
        s = LockstepSession(seed=1, latency_ms=500, clock=clock)
        clock.advance(0.2)
        s.update(0.2)
        self.assertTrue(any("等待" in st for st in s.statuses()))
        self.assertIn("第0帧", " ".join(s.statuses()))
        for _ in range(200):
            clock.advance(0.05)
            s.update(0.05)
        self.assertGreater(s.min_tick(), 0)

    def test_wait_policy_blocks_on_disconnect(self):
        clock = ManualClock()
        s = LockstepSession(seed=2, latency_ms=0, clock=clock,
                            missing_policy="wait")
        run_scripted(s, clock, [], 10)
        s.links[1].close()
        before = s.min_tick()
        for _ in range(50):
            clock.advance(0.1)
            s.update(0.1)
        self.assertEqual(s.min_tick(), before)  # 明确等待，不用空输入冒充
        self.assertTrue(any("断线" in st or "等待" in st for st in s.statuses()))

    def test_zero_policy_on_disconnect(self):
        clock = ManualClock()
        s = LockstepSession(seed=2, latency_ms=0, clock=clock,
                            missing_policy="zero")
        # 脚本指令最迟在第 46 帧受理、第 48 帧结算；第 55 帧断线时已无在途指令
        run_scripted(s, clock, demo_script(), 55)
        s.links[1].close()
        run_scripted(s, clock, [], 80)
        a, b = s.eps
        self.assertTrue(a.degraded and b.degraded)
        common = set(a.digests) & set(b.digests)
        self.assertGreaterEqual(len(common), 80)
        self.assertTrue(all(a.digests[t] == b.digests[t] for t in common))


class TestDivergenceDetection(unittest.TestCase):
    def test_first_divergence_located(self):
        clock = ManualClock()
        s = LockstepSession(seed=5, latency_ms=0, clock=clock)
        run_scripted(s, clock, [], 10)
        s.eps[1].world.units[3].hp -= 5  # 绕过指令通道篡改端B
        run_scripted(s, clock, [], 40)
        div = s.divergence()
        self.assertIsNotNone(div)
        self.assertEqual(div["tick"], 10)  # 篡改后的下一帧即被检出
        self.assertEqual(div["first_uid"], 3)
        self.assertIn("hp", div["text"])
        self.assertTrue(s.halted)
        self.assertFalse(s.eps[0].try_advance())  # 停等，不再推进

    def test_clean_run_has_no_divergence(self):
        clock = ManualClock()
        s = LockstepSession(seed=5, latency_ms=30, clock=clock)
        run_scripted(s, clock, demo_script(), 60)
        self.assertIsNone(s.divergence())
        self.assertTrue(all(ok for _t, ok, _a, _b in s.checks()))


class TestCommandFeedback(unittest.TestCase):
    def test_invalid_command_rejected_with_reason(self):
        s = LockstepSession(seed=1, latency_ms=0, clock=ManualClock())
        ok, reason = s.issue(0, make_move(2, 12, 0))
        self.assertFalse(ok)
        self.assertIn("不可通行", reason)
        ok, reason = s.issue(0, make_move(8, 5, 5))
        self.assertFalse(ok)
        self.assertIn("不属于", reason)
        self.assertTrue(any("被拒" in line for line in s.drain_log()))

    def test_valid_command_takes_effect(self):
        clock = ManualClock()
        s = LockstepSession(seed=1, latency_ms=0, clock=clock)
        ok, _ = s.issue(0, make_move(2, 6, 7))
        self.assertTrue(ok)
        run_scripted(s, clock, [], 40)
        u = s.eps[0].world.units[2]
        self.assertEqual((u.x, u.y), (6, 7))
        # 两端视野一致
        self.assertEqual((s.eps[1].world.units[2].x, s.eps[1].world.units[2].y),
                         (6, 7))


if __name__ == "__main__":
    unittest.main()
