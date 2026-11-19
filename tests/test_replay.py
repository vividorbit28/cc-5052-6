"""输入脚本录制/回放测试。"""

import copy
import unittest

from lockstep.replay import (compare_configurations, load_recording,
                             save_recording, verify)
from lockstep.scripts import demo_script
from lockstep.session import LockstepSession
from lockstep.transport import ManualClock


def make_recording(path, seed=42, ticks=100):
    clock = ManualClock()
    s = LockstepSession(seed=seed, latency_ms=90, clock=clock)
    script = sorted(demo_script(), key=lambda s: (s[0], s[1]))
    issued = 0
    guard = 0
    while s.min_tick() < ticks and guard < 400000 and not s.halted:
        t = s.min_tick()
        while issued < len(script) and script[issued][0] <= t:
            _, player, cmd = script[issued]
            s.issue(player, cmd)
            issued += 1
        clock.advance(0.05)
        s.update(0.05)
        guard += 1
    save_recording(s, str(path))
    return load_recording(str(path))


class TestReplay(unittest.TestCase):
    def test_verify_ok(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            rec = make_recording(pathlib.Path(td) / "rec.json")
        ok, t, msg = verify(rec)
        self.assertTrue(ok, msg)
        self.assertIsNone(t)

    def test_replay_immune_to_latency_and_framerate(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            rec = make_recording(pathlib.Path(td) / "rec.json")
        ok, msg = compare_configurations(
            rec, [(0, 1 / 60), (300, 1 / 20), (150, 1 / 15), (40, 1 / 30)])
        self.assertTrue(ok, msg)

    def test_tampered_recording_detected_at_right_tick(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            rec = make_recording(pathlib.Path(td) / "rec.json")
        bad = copy.deepcopy(rec)
        target = next(f for f in bad["frames"] if f["cmds"])
        target["cmds"][0]["x"] += 1  # 篡改一条指令
        ok, t, msg = verify(bad)
        self.assertFalse(ok)
        self.assertEqual(t, target["tick"])  # 首个不一致帧 = 被篡改帧
        self.assertIn("不一致", msg)


if __name__ == "__main__":
    unittest.main()
