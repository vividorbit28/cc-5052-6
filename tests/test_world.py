"""模拟内核确定性规则测试。"""

import unittest

from lockstep.commands import make_focus, make_move
from lockstep.digest import world_digest
from lockstep.rng import Rng
from lockstep.world import Unit, World, build_world


class TestRng(unittest.TestCase):
    def test_stream_repeatable(self):
        a, b = Rng(42), Rng(42)
        self.assertEqual([a.next_u64() for _ in range(50)],
                         [b.next_u64() for _ in range(50)])

    def test_different_seeds_differ(self):
        a, b = Rng(1), Rng(2)
        self.assertNotEqual([a.next_u64() for _ in range(5)],
                            [b.next_u64() for _ in range(5)])


class TestSpatialQuery(unittest.TestCase):
    def test_result_sorted_by_dist_then_uid(self):
        w = build_world(1)
        result = w.units_within(12, 7, 50)
        keys = []
        for uid in result:
            u = w.units[uid]
            keys.append(((u.x - 12) ** 2 + (u.y - 7) ** 2, uid))
        self.assertEqual(keys, sorted(keys))

    def test_insertion_order_irrelevant(self):
        w1 = build_world(1)
        r1 = w1.units_within(12, 7, 50)
        units = [Unit(u.uid, u.squad, u.x, u.y) for u in w1.iter_units()]
        units.reverse()  # 故意用相反顺序插入
        w2 = World(w1.width, w1.height, w1.obstacles, units, 1)
        self.assertEqual(r1, w2.units_within(12, 7, 50))


class TestPathfinding(unittest.TestCase):
    def test_path_deterministic_across_instances(self):
        w1, w2 = build_world(7), build_world(7)
        self.assertEqual(w1.find_path((2, 7), (20, 7)),
                         w2.find_path((2, 7), (20, 7)))

    def test_path_respects_obstacles(self):
        w = build_world(7)
        path = w.find_path((2, 7), (20, 7))
        self.assertIsNotNone(path)
        for cell in path:
            self.assertNotIn(cell, w.obstacles)

    def test_unreachable_goal_returns_none(self):
        w = build_world(7)
        self.assertIsNone(w.find_path((2, 7), (12, 0)))  # 障碍格


class TestCommandRules(unittest.TestCase):
    def test_validate_rejects_blocked_cell(self):
        w = build_world(1)
        ok, reason = w.validate_command(0, make_move(2, 12, 0))
        self.assertFalse(ok)
        self.assertIn("不可通行", reason)

    def test_validate_rejects_foreign_unit(self):
        w = build_world(1)
        ok, reason = w.validate_command(0, make_move(8, 5, 5))
        self.assertFalse(ok)
        self.assertIn("不属于", reason)

    def test_focus_tiebreak_prefers_lowest_uid(self):
        # 两个敌人与集火点等距时必须按 uid 决胜
        w = World(10, 10, [], [Unit(0, 0, 0, 0), Unit(5, 1, 6, 2), Unit(3, 1, 4, 4)], 1)
        w.step([(0, 0, 0, make_focus(5, 3))])
        self.assertEqual(w.units[0].target, 3)


class TestStepDeterminism(unittest.TestCase):
    def test_same_commands_same_digest(self):
        from lockstep.commands import merge_frames, make_frame
        frames_a = [make_frame(0, 0, 0, [make_move(2, 10, 7)]),
                    make_frame(0, 1, 0, [make_focus(10, 7)])]
        digests = []
        for _ in range(2):
            w = build_world(11)
            for t in range(40):
                frames = [make_frame(t, 0, t, [make_move(2, 10, 7)] if t == 0 else []),
                          make_frame(t, 1, t, [make_focus(10, 7)] if t == 0 else [])]
                w.step(merge_frames(frames))
            digests.append(world_digest(w))
        self.assertEqual(digests[0], digests[1])


if __name__ == "__main__":
    unittest.main()
