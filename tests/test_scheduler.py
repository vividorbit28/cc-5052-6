"""调度器测试：候选集裁剪必须与完整逐步模拟逐帧一致。

覆盖：
- reference 策略（不裁剪）与原生 World 完全一致；
- dependency 策略在开门/堵塞/随机指令流下逐帧一致且显著省求值；
- influence 策略在开门场景漏唤醒、与参照分歧（故不采用）；
- 唤醒溯源能说明哪个邻居唤醒了谁。
"""

import os
import random
import subprocess
import sys
import unittest

from lockstep.commands import make_focus, make_frame, make_move, merge_frames
from lockstep.digest import world_digest
from lockstep.scheduler import (KIND_CELL_FREED, KIND_PATH_CLEARED,
                                ScheduledWorld)
from lockstep.scenarios import (blocked_script, build_blocked, build_door,
                                door_script)
from lockstep.world import Unit, World, build_world

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_pair(builder, frames_by_tick, ticks, **kw):
    """同步推进 优化端/参照端/原生World，返回逐帧摘要三元组。"""
    opt = builder(ScheduledWorld, 1, strategy="dependency", **kw)
    ref = builder(ScheduledWorld, 1, strategy="reference")
    raw = builder(World, 1)
    out = []
    for t in range(ticks):
        merged = merge_frames(frames_by_tick.get(t, []))
        opt.step(merged)
        ref.step(merged)
        raw.step(merged)
        out.append((world_digest(opt), world_digest(ref), world_digest(raw)))
    return opt, ref, raw, out


def frames_of(script):
    from lockstep.sandbox import script_to_frames
    by_tick = {}
    for f in script_to_frames(script):
        by_tick.setdefault(f["tick"], []).append(f)
    return by_tick


class TestReferenceStrategy(unittest.TestCase):
    def test_reference_strategy_equals_plain_world(self):
        """统计钩子不得改变任何语义。"""
        _opt, _ref, _raw, digests = run_pair(build_door, frames_of(door_script()), 90)
        for t, (_d_opt, d_ref, d_raw) in enumerate(digests):
            self.assertEqual(d_ref, d_raw, f"第 {t} 帧 reference 策略与原生 World 不符")


class TestDependencyStrategy(unittest.TestCase):
    def test_door_scenario_exact_and_cheaper(self):
        opt, ref, _raw, digests = run_pair(build_door, frames_of(door_script()), 90)
        for t, (d_opt, d_ref, _d) in enumerate(digests):
            self.assertEqual(d_opt, d_ref, f"第 {t} 帧优化端与参照分歧")
        approaches_opt = sum(s["approaches"] for s in opt.eval_stats)
        approaches_ref = sum(s["approaches"] for s in ref.eval_stats)
        self.assertLess(approaches_opt, approaches_ref // 4,
                        f"邻域搜索应大幅减少：{approaches_opt} vs {approaches_ref}")

    def test_failed_search_skipped_while_door_closed(self):
        """门关闭期间（13..31 帧）优化端零搜索，参照端每拍都在重搜。"""
        opt, ref, _raw, _d = run_pair(build_door, frames_of(door_script()), 32)
        for t in range(13, 32):
            self.assertEqual(opt.eval_stats[t]["approaches"], 0,
                             f"第 {t} 帧应复用失败结果")
            self.assertGreater(ref.eval_stats[t]["approaches"], 0,
                               f"第 {t} 帧参照端应重搜")
            self.assertGreater(opt.eval_stats[t]["combat_skip"], 0)

    def test_door_open_wakes_chasers_via_door_cell(self):
        """开门：门卫让出门格 → 三个追猎者都被它唤醒。"""
        opt, _ref, _raw, _d = run_pair(build_door, frames_of(door_script()), 90)
        wakes = [(t, woken, by) for (t, woken, by, kind, _d2) in opt.wake_log
                 if kind == KIND_CELL_FREED]
        woken_by_doorkeeper = {woken for (t, woken, by) in wakes if by == 3}
        self.assertEqual(woken_by_doorkeeper, {0, 1, 2})
        # 两轮唤醒：帧32 让出门格(12,6)，帧35 让出身位格(11,6)——
        # 每轮都精确对应门卫的一次移动，没有误唤醒也没有漏唤醒
        self.assertEqual({t for (t, _w, by) in wakes if by == 3}, {32, 35})

    def test_blocked_scenario_wake_chain(self):
        """堵塞：静止纵队虽不换格，仍依赖让路者；唤醒链逐级可溯源。"""
        opt, _ref, _raw, digests = run_pair(
            build_blocked, frames_of(blocked_script()), 80)
        for t, (d_opt, d_ref, _d) in enumerate(digests):
            self.assertEqual(d_opt, d_ref, f"第 {t} 帧优化端与参照分歧")
        chain = [(t, woken, by) for (t, woken, by, kind, _d) in opt.wake_log
                 if kind == KIND_PATH_CLEARED]
        # 第一环：堵路者 3 让开门格唤醒单位0；随后 0→1、1→2 逐级传递
        self.assertIn((43, 0, 3), chain)
        self.assertIn((43, 1, 0), chain)
        later = [(t, w, b) for (t, w, b) in chain if w == 2 and b == 1]
        self.assertTrue(later, "单位2 应由单位1 唤醒")
        # 唤醒前它们确实静止了十几拍（依赖下一拍谁让路，而非自身时钟）
        self.assertTrue(all(t >= 43 for (t, _w, _b) in chain))
        # 世界侧查询接口同样能说明"谁唤醒了单位0"
        self.assertTrue(any("单位3" in l for l in opt.wake_explanation(0)))

    def test_random_command_stream_exact(self):
        """随机指令流（含集火/减员）下逐帧一致——裁剪不改变任何语义。"""
        rng = random.Random(20261120)
        w0 = build_world(7)
        free = [(x, y) for x in range(w0.width) for y in range(w0.height)
                if not w0.is_blocked(x, y)]
        frames_by_tick = {}
        for t in range(120):
            cmds = {0: [], 1: []}
            for player in (0, 1):
                for _ in range(rng.randint(0, 2)):
                    if rng.random() < 0.3:
                        x, y = free[rng.randrange(len(free))]
                        cmds[player].append(make_focus(x, y))
                    else:
                        uid = rng.choice([u.uid for u in w0.iter_units()
                                          if u.squad == player])
                        x, y = free[rng.randrange(len(free))]
                        cmds[player].append(make_move(uid, x, y))
            frames_by_tick[t] = [make_frame(t, 0, t, cmds[0]),
                                 make_frame(t, 1, t, cmds[1])]

        def builder(cls, seed, **kw):
            units = [Unit(u.uid, u.squad, u.x, u.y) for u in w0.iter_units()]
            return cls(w0.width, w0.height, w0.obstacles, units, 7, **kw)

        opt, ref, raw, digests = run_pair(builder, frames_by_tick, 120)
        for t, (d_opt, d_ref, d_raw) in enumerate(digests):
            self.assertEqual(d_opt, d_raw, f"第 {t} 帧优化端与原生 World 分歧")
            self.assertEqual(d_ref, d_raw, f"第 {t} 帧参照端与原生 World 分歧")
        # 确认指令流真的重度触发了追击重寻路（被优化的对象）
        self.assertGreater(sum(s["approaches"] for s in ref.eval_stats), 100)

    def test_death_frees_watched_cell(self):
        """监视格的占位者被消灭，同样要唤醒依赖者（死亡让出路径）。"""
        from lockstep.world import Unit
        obstacles = {(12, y) for y in range(12) if y != 6}

        def builder(cls, seed, **kw):
            units = [Unit(0, 0, 4, 6),    # 追猎者：目标在门后，搜索将失败
                     Unit(1, 0, 10, 5),   # 攻击者：射程内但不遮蔽门格
                     Unit(2, 1, 12, 6),   # 门卫：hp=1，即将被消灭
                     Unit(3, 1, 16, 6)]   # 门后目标
            w = cls(20, 12, obstacles, units, seed, **kw)
            w.units[2].hp = 1
            w.units[0].target = 3  # 直接布置状态（确定性单测，不走指令通道）
            w.units[1].target = 2
            return w

        opt = builder(ScheduledWorld, 1, strategy="dependency")
        ref = builder(ScheduledWorld, 1, strategy="reference")
        for t in range(40):
            opt.step([])
            ref.step([])
            if t == 0:
                # 首次搜索失败，门格（被门卫占用）已进入监视集
                self.assertIn((12, 6), opt._watch[0])
            self.assertEqual(world_digest(opt), world_digest(ref),
                             f"第 {t} 帧优化端与参照分歧")
        self.assertNotIn(2, opt.units)  # 门卫已被消灭
        wakes = [(t, w, by) for (t, w, by, kind, _d) in opt.wake_log
                 if kind == KIND_CELL_FREED]
        self.assertIn((1, 0, 2), wakes,
                      "门卫被消灭让出门格，应在下一拍唤醒追猎者")
        # 被唤醒后重新寻路成功，追猎者最终穿门逼近目标
        self.assertGreater(opt.units[0].x, 12)

    def test_eval_stats_cover_every_tick(self):
        opt, _ref, _raw, _d = run_pair(build_door, frames_of(door_script()), 40)
        self.assertEqual(len(opt.eval_stats), 40)
        for s in opt.eval_stats:
            for key in ("move_eval", "move_skip", "combat_eval",
                        "combat_skip", "pathfinds", "approaches", "probes"):
                self.assertIn(key, s)


class TestInfluenceStrategy(unittest.TestCase):
    def test_influence_diverges_in_door_scenario(self):
        """按输入影响范围激活：门后队伍不在影响范围内，漏唤醒 → 分歧。"""
        from lockstep.digest import world_digest as wd
        opt = build_door(ScheduledWorld, 1, strategy="influence",
                         influence_radius=3)
        ref = build_door(ScheduledWorld, 1, strategy="reference")
        frames = frames_of(door_script())
        first_div = None
        for t in range(90):
            merged = merge_frames(frames.get(t, []))
            opt.step(merged)
            ref.step(merged)
            if wd(opt) != wd(ref):
                first_div = t
                break
        self.assertIsNotNone(first_div, "影响范围策略应在开门场景暴露漏唤醒")
        # 分歧点应在开门（30 帧指令、32 帧门格让出）之后不久
        self.assertGreaterEqual(first_div, 32)

    def test_dependency_chosen_for_determinism(self):
        """同一场景下 dependency 保持逐帧一致——这是采用它的理由。"""
        _opt, _ref, _raw, digests = run_pair(
            build_door, frames_of(door_script()), 90)
        self.assertTrue(all(d_opt == d_ref for d_opt, d_ref, _ in digests))


class TestSchedulerHashSeedIndependence(unittest.TestCase):
    def test_sandbox_output_hash_seed_independent(self):
        """调度器内部用集合做依赖跟踪，输出不得受哈希种子影响。"""
        outs = []
        for hash_seed in ("0", "1", "424242"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            proc = subprocess.run(
                [sys.executable, "-m", "lockstep", "--sandbox"],
                capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outs.append(proc.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])


if __name__ == "__main__":
    unittest.main()
