"""战术沙盘：优化调度器与完整逐步模拟的对照实验台。

一次 run 同时推进两个世界：
- 优化端：ScheduledWorld（候选集 + 唤醒依赖）；
- 参照端：完整逐步模拟（不裁剪任何单位）。
两端吃同一份输入帧日志，逐帧比对摘要——优化不得改变任何结果。

沙盘能力：
- run_script / run_frames：执行输入日志，记录逐帧摘要、每步求值数量、
  唤醒溯源、逐帧位置；
- drag_input：把某条输入拖动到相邻步（tick±1），重跑后可读取首个不同
  的单位状态；
- 到达顺序置换：同一日志打乱送达顺序重跑，结果必须一致；
- audit：用连续数拍的资源消费与位置共同检查是否漏算；
- squad_summary：双方（各小队）状态摘要。
"""

import random

from .commands import make_frame, merge_frames
from .digest import UNIT_FIELDS, canonical_world, world_digest
from .scheduler import ScheduledWorld


class RunResult:
    def __init__(self, frames, ticks, strategy):
        self.frames = frames
        self.ticks = ticks
        self.strategy = strategy
        self.digests_opt = {}     # tick -> 优化端摘要
        self.digests_ref = {}     # tick -> 参照端摘要
        self.states = {}          # tick -> 优化端规范快照（拖动比较用）
        self.positions_opt = {}   # tick -> {uid: (x, y)}
        self.positions_ref = {}
        self.eval_stats = []      # 优化端每步求值统计
        self.eval_stats_ref = []  # 参照端每步求值统计（全量基线）
        self.wake_log = []        # 优化端唤醒溯源
        self.mismatches = []      # 优化端与参照端摘要不符的 tick
        self.opt_world = None
        self.ref_world = None

    def consistent(self):
        return not self.mismatches


def script_to_frames(script):
    """把 (tick, player, cmd) 脚本转成输入帧日志（每 (tick,player) 一帧）。

    seq 按该玩家此前帧数单调递增，与 merge_frames 的排序键一致。
    """
    groups = {}
    for tick, player, cmd in script:
        groups.setdefault((tick, player), []).append(cmd)
    seqs = {}
    frames = []
    for (tick, player) in sorted(groups):
        seq = seqs.get(player, 0)
        seqs[player] = seq + 1
        frames.append(make_frame(tick, player, seq, groups[(tick, player)]))
    return frames


class TacticalSandbox:
    def __init__(self, builder, seed=1, strategy="dependency", **sched_kw):
        self.builder = builder
        self.seed = seed
        self.strategy = strategy
        self.sched_kw = sched_kw

    # ---------- 执行 ----------

    def run_script(self, script, ticks, arrival_seed=None):
        return self.run_frames(script_to_frames(script), ticks, arrival_seed)

    def run_frames(self, frames, ticks, arrival_seed=None):
        """双端推进同一输入日志；arrival_seed 打乱帧的送达顺序。"""
        frames = [dict(f, cmds=[dict(c) for c in f["cmds"]]) for f in frames]
        if arrival_seed is not None:
            random.Random(arrival_seed).shuffle(frames)
        by_tick = {}
        for f in frames:
            by_tick.setdefault(f["tick"], []).append(f)
        opt = self.builder(ScheduledWorld, self.seed,
                           strategy=self.strategy, **self.sched_kw)
        ref = self.builder(ScheduledWorld, self.seed, strategy="reference")
        run = RunResult(frames, ticks, self.strategy)
        for t in range(ticks):
            merged = merge_frames(by_tick.get(t, []))
            opt.step(merged)
            ref.step(merged)
            run.digests_opt[t] = world_digest(opt)
            run.digests_ref[t] = world_digest(ref)
            run.states[t] = canonical_world(opt)
            run.positions_opt[t] = {u.uid: (u.x, u.y) for u in opt.iter_units()}
            run.positions_ref[t] = {u.uid: (u.x, u.y) for u in ref.iter_units()}
            if run.digests_opt[t] != run.digests_ref[t]:
                run.mismatches.append(t)
        run.eval_stats = list(opt.eval_stats)
        run.eval_stats_ref = list(ref.eval_stats)
        run.wake_log = list(opt.wake_log)
        run.opt_world = opt
        run.ref_world = ref
        return run

    # ---------- 输入编辑 ----------

    @staticmethod
    def drag_input(frames, tick, player, cmd_index, delta):
        """把指定帧内第 cmd_index 条指令拖动到相邻步（tick+delta）。

        目标 tick 的该玩家帧不存在则新建（seq 接续该玩家已有帧）。
        返回新的帧日志；原日志不被修改。
        """
        frames = [dict(f, cmds=list(f["cmds"])) for f in frames]
        src = next((f for f in frames
                    if f["tick"] == tick and f["player"] == player), None)
        if src is None or not (0 <= cmd_index < len(src["cmds"])):
            raise ValueError(f"找不到帧(tick={tick}, player={player})"
                             f"内第 {cmd_index} 条指令")
        cmd = src["cmds"].pop(cmd_index)
        new_tick = tick + delta
        if new_tick < 0:
            raise ValueError("不能拖到第 0 帧之前")
        dst = next((f for f in frames
                    if f["tick"] == new_tick and f["player"] == player), None)
        if dst is None:
            seq = 1 + max((f["seq"] for f in frames if f["player"] == player),
                          default=-1)
            dst = make_frame(new_tick, player, seq, [])
            frames.append(dst)
        dst["cmds"].append(cmd)
        return frames

    # ---------- 比较与审计 ----------

    @staticmethod
    def first_difference(run_a, run_b):
        """两次运行首个不同的单位状态；一致返回 None。"""
        for t in sorted(set(run_a.states) & set(run_b.states)):
            ua, ub = run_a.states[t]["units"], run_b.states[t]["units"]
            if ua == ub:
                continue
            for ca, cb in zip(ua, ub):
                if ca == cb:
                    continue
                for i, name in enumerate(UNIT_FIELDS):
                    if ca[i] != cb[i]:
                        return {"tick": t, "uid": ca[0], "field": name,
                                "a": ca[i], "b": cb[i]}
                return {"tick": t, "uid": ca[0], "field": None,
                        "a": ca, "b": cb}
            return {"tick": t, "uid": None, "field": "unit_count",
                    "a": len(ua), "b": len(ub)}
        return None

    @staticmethod
    def audit(run):
        """用连续数拍的资源消费与位置共同判断是否漏算。

        位置：优化端每拍每个单位的坐标必须与参照一致；
        资源：优化端每拍求值数不得超过参照（裁剪只许减不许增）。
        """
        rows = []
        pos_misses = []
        cost_anomalies = []
        for t in range(run.ticks):
            po, pr = run.positions_opt[t], run.positions_ref[t]
            so, sr = run.eval_stats[t], run.eval_stats_ref[t]
            pos_ok = po == pr
            cost_ok = (so["move_eval"] + so["combat_eval"]
                       <= sr["move_eval"] + sr["combat_eval"]
                       and so["approaches"] <= sr["approaches"])
            if not pos_ok:
                pos_misses.append(t)
            if not cost_ok:
                cost_anomalies.append(t)
            rows.append({"tick": t, "positions_ok": pos_ok,
                         "opt_evals": so["move_eval"] + so["combat_eval"],
                         "ref_evals": sr["move_eval"] + sr["combat_eval"],
                         "opt_approaches": so["approaches"],
                         "ref_approaches": sr["approaches"]})
        total_opt = sum(r["opt_evals"] for r in rows)
        total_ref = sum(r["ref_evals"] for r in rows)
        appr_opt = sum(r["opt_approaches"] for r in rows)
        appr_ref = sum(r["ref_approaches"] for r in rows)
        return {
            "ok": not pos_misses and not cost_anomalies and run.consistent(),
            "position_misses": pos_misses,
            "cost_anomalies": cost_anomalies,
            "digest_mismatches": list(run.mismatches),
            "total_evals": (total_opt, total_ref),
            "total_approaches": (appr_opt, appr_ref),
            "rows": rows,
        }

    @staticmethod
    def squad_summary(world):
        """双方（各小队）状态摘要：存活数、总血量、逐单位坐标。"""
        out = {}
        for u in world.iter_units():
            s = out.setdefault(u.squad, {"alive": 0, "hp": 0, "units": {}})
            s["alive"] += 1
            s["hp"] += u.hp
            s["units"][u.uid] = (u.x, u.y)
        return out

    @staticmethod
    def wake_chain(run, uid=None):
        """唤醒溯源的可读文本；uid 给定时只看该单位被谁唤醒。"""
        lines = []
        for t, woken, by, kind, detail in run.wake_log:
            if uid is not None and woken != uid:
                continue
            src = "指令" if by is None else f"单位{by}"
            who = "全队" if woken is None else f"单位{woken}"
            lines.append(f"帧{t}：{src} {kind} → {who}（{detail}）")
        return lines


def demo(out=print):
    """无头验收：两种唤醒策略对比、拖动输入、到达顺序、堵塞审计。"""
    from .scenarios import (blocked_script, build_blocked, build_door,
                            door_script)

    ok_all = True

    def check(name, ok, detail=""):
        nonlocal ok_all
        ok_all = ok_all and ok
        out(f"[{'PASS' if ok else 'FAIL'}] {name}"
            f"{('：' + detail) if detail else ''}")

    # 1) 开门场景：状态依赖反向唤醒与参照完全一致，且显著省求值
    box = TacticalSandbox(build_door, seed=1, strategy="dependency")
    run = box.run_script(door_script(), 90)
    audit = box.audit(run)
    check("开门场景：依赖唤醒逐帧一致", audit["ok"],
          f"求值 {audit['total_evals'][0]}/{audit['total_evals'][1]}，"
          f"邻域搜索 {audit['total_approaches'][0]}/{audit['total_approaches'][1]}")
    chain = [l for l in box.wake_chain(run) if "邻格让出" in l or "通路让开" in l]
    check("开门场景：唤醒链可溯源", len(chain) > 0, chain[0] if chain else "无")

    # 2) 同场景：按输入影响范围激活会漏唤醒，与参照分歧（故不采用）
    box_inf = TacticalSandbox(build_door, seed=1, strategy="influence",
                              influence_radius=3)
    run_inf = box_inf.run_script(door_script(), 90)
    check("开门场景：影响范围激活暴露漏唤醒", not run_inf.consistent(),
          f"首个分歧帧 {run_inf.mismatches[0] if run_inf.mismatches else '-'}")

    # 3) 拖动输入到相邻步：读取首个不同的单位状态
    frames = script_to_frames(door_script())
    dragged = TacticalSandbox.drag_input(frames, 30, 1, 0, +1)  # 开门晚一拍
    run_drag = box.run_frames(dragged, 90)
    diff = TacticalSandbox.first_difference(run, run_drag)
    check("拖动输入：定位首个不同单位状态",
          diff is not None and diff["uid"] == 3 and diff["tick"] == 30,
          f"帧{diff['tick']} 单位{diff['uid']} 字段{diff['field']}"
          if diff else "无差异")
    check("拖动后优化端仍与参照一致", box.audit(run_drag)["ok"])

    # 4) 相同日志换到达顺序，结果一致
    variants = {tuple(sorted(box.run_frames(frames, 90, arrival_seed=s)
                             .digests_opt.items()))
                for s in range(5)}
    check("到达顺序置换后结果一致", len(variants) == 1, "5 种顺序")

    # 5) 堵塞场景：静止队伍依赖让路者，唤醒链与审计
    box_b = TacticalSandbox(build_blocked, seed=1, strategy="dependency")
    run_b = box_b.run_script(blocked_script(), 80)
    audit_b = box_b.audit(run_b)
    wakes = [l for l in box_b.wake_chain(run_b) if "通路让开" in l]
    check("堵塞场景：逐帧一致且无漏算", audit_b["ok"],
          f"求值 {audit_b['total_evals'][0]}/{audit_b['total_evals'][1]}")
    check("堵塞场景：说明哪个邻居唤醒了谁", len(wakes) >= 3,
          wakes[0] if wakes else "无")

    out("沙盘演示结束：" + ("全部通过" if ok_all else "存在失败项"))
    return ok_all
