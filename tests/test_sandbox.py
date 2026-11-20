"""战术沙盘测试：拖动输入、到达顺序置换、资源/位置审计、双方状态摘要。"""

import unittest

from lockstep.sandbox import TacticalSandbox, script_to_frames
from lockstep.scenarios import (blocked_script, build_blocked, build_door,
                                door_script)


class TestDragInput(unittest.TestCase):
    def setUp(self):
        self.box = TacticalSandbox(build_door, seed=1, strategy="dependency")
        self.frames = script_to_frames(door_script())

    def test_drag_moves_command_to_adjacent_tick(self):
        dragged = TacticalSandbox.drag_input(self.frames, 30, 1, 0, +1)
        src = [f for f in dragged if f["tick"] == 30 and f["player"] == 1]
        dst = [f for f in dragged if f["tick"] == 31 and f["player"] == 1]
        self.assertTrue(not src or not src[0]["cmds"])  # 原帧已空或消失
        self.assertEqual(len(dst), 1)
        self.assertEqual(dst[0]["cmds"], [{"t": "move", "u": 3, "x": 10, "y": 1}])
        # 原日志不被修改
        orig = [f for f in self.frames if f["tick"] == 30]
        self.assertEqual(len(orig[0]["cmds"]), 1)

    def test_drag_rejects_invalid_locator(self):
        with self.assertRaises(ValueError):
            TacticalSandbox.drag_input(self.frames, 99, 1, 0, +1)
        with self.assertRaises(ValueError):
            TacticalSandbox.drag_input(self.frames, 30, 1, 5, +1)
        with self.assertRaises(ValueError):
            TacticalSandbox.drag_input(self.frames, 0, 1, 0, -1)

    def test_first_difference_after_drag(self):
        """开门指令晚一拍：首个不同应出现在第 30 帧、门卫单位3。"""
        run_orig = self.box.run_frames(self.frames, 90)
        dragged = TacticalSandbox.drag_input(self.frames, 30, 1, 0, +1)
        run_drag = self.box.run_frames(dragged, 90)
        diff = TacticalSandbox.first_difference(run_orig, run_drag)
        self.assertIsNotNone(diff)
        self.assertEqual(diff["tick"], 30)
        self.assertEqual(diff["uid"], 3)
        self.assertEqual(diff["field"], "move_clock")
        # 拖动后的运行自身仍满足 优化==参照
        self.assertTrue(self.box.audit(run_drag)["ok"])

    def test_first_difference_none_for_identical_runs(self):
        run_a = self.box.run_frames(self.frames, 60)
        run_b = self.box.run_frames(self.frames, 60)
        self.assertIsNone(TacticalSandbox.first_difference(run_a, run_b))


class TestArrivalOrder(unittest.TestCase):
    def test_same_log_any_arrival_order_same_result(self):
        """相同日志换一批输入到达顺序，逐帧摘要必须一致。"""
        box = TacticalSandbox(build_door, seed=1, strategy="dependency")
        frames = script_to_frames(door_script())
        base = box.run_frames(frames, 90).digests_opt
        for seed in range(5):
            shuffled = box.run_frames(frames, 90, arrival_seed=seed).digests_opt
            self.assertEqual(base, shuffled, f"到达顺序 seed={seed} 结果不一致")

    def test_arrival_order_also_consistent_in_blocked_scenario(self):
        box = TacticalSandbox(build_blocked, seed=1, strategy="dependency")
        frames = script_to_frames(blocked_script())
        base = box.run_frames(frames, 80).digests_opt
        for seed in (7, 8, 9):
            self.assertEqual(base,
                             box.run_frames(frames, 80, arrival_seed=seed)
                             .digests_opt)


class TestAudit(unittest.TestCase):
    def test_door_audit_no_miss_and_savings(self):
        """连续数拍的位置与资源消费共同证明：无漏算，且求值显著减少。"""
        box = TacticalSandbox(build_door, seed=1, strategy="dependency")
        run = box.run_script(door_script(), 90)
        audit = box.audit(run)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["position_misses"], [])
        self.assertEqual(audit["cost_anomalies"], [])
        self.assertEqual(audit["digest_mismatches"], [])
        opt_evals, ref_evals = audit["total_evals"]
        self.assertLess(opt_evals, ref_evals // 2)
        opt_appr, ref_appr = audit["total_approaches"]
        self.assertLess(opt_appr, ref_appr // 4)
        # 连续数拍逐行可查
        self.assertEqual(len(audit["rows"]), 90)

    def test_blocked_audit_no_miss(self):
        box = TacticalSandbox(build_blocked, seed=1, strategy="dependency")
        run = box.run_script(blocked_script(), 80)
        audit = box.audit(run)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["position_misses"], [])

    def test_audit_flags_missed_wakeups(self):
        """influence 策略漏唤醒时，审计必须在连续帧上报位置漏算。"""
        box = TacticalSandbox(build_door, seed=1, strategy="influence",
                              influence_radius=3)
        run = box.run_script(door_script(), 90)
        audit = box.audit(run)
        self.assertFalse(audit["ok"])
        self.assertTrue(audit["position_misses"])
        self.assertEqual(audit["position_misses"],
                         sorted(audit["position_misses"]))


class TestSummaries(unittest.TestCase):
    def test_squad_summary(self):
        box = TacticalSandbox(build_door, seed=1, strategy="dependency")
        run = box.run_script(door_script(), 90)
        summary = TacticalSandbox.squad_summary(run.opt_world)
        self.assertEqual(set(summary), {0, 1})
        self.assertEqual(summary[0]["alive"], 3)
        self.assertEqual(summary[0]["hp"], 90)
        # 门后目标已被追猎小队消灭，只剩门卫
        self.assertEqual(summary[1]["alive"], 1)
        self.assertIn(3, summary[1]["units"])

    def test_wake_chain_text(self):
        box = TacticalSandbox(build_blocked, seed=1, strategy="dependency")
        run = box.run_script(blocked_script(), 80)
        lines = box.wake_chain(run, uid=0)
        self.assertTrue(any("单位3" in l and "通路让开" in l for l in lines),
                        "单位0 的唤醒溯源应指认堵路者单位3")


if __name__ == "__main__":
    unittest.main()
