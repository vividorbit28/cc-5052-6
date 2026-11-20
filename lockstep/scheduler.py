"""单位调度优化：带明确唤醒依赖的候选集。

问题：完整逐步模拟（World.step）每帧对所有单位做移动/战斗结算。其中
追击不可达目标的单位（路径为空、目标未移动）会每拍重跑昂贵的邻域搜索
（_approach_path → 多次 BFS），结果却始终相同——这是确定输入步中的
无效单位更新。

做法：ScheduledWorld 在不改变任何规则的前提下裁剪每阶段的候选单位，
并为"上次搜索失败"的单位记录显式唤醒依赖（监视集 = 搜索时触碰到的
占用格）。两种唤醒策略可对比：

- "influence"  按输入影响范围激活：只唤醒指令落点/移动单位附近的单位。
               开门场景中，门后远处的队伍不在影响范围内，永远不会被
               唤醒重寻路——与参照模拟分歧（不可维持确定性语义）。
- "dependency" 按状态依赖反向唤醒：谁让开了被监视的格子，谁就唤醒
               依赖它的单位。开门 → 门格让出 → 门后队伍被唤醒 → 连锁
               移动，逐帧摘要与参照完全一致（默认策略）。

"reference" 策略不裁剪任何单位，只统计求值量，作为资源消费基线。

可观测性（供战术沙盘与测试）：
- eval_stats：每 tick 各阶段求值单位数、寻路次数、占位探测次数、跳过数；
- wake_log：唤醒溯源 (tick, 被唤醒uid, 唤醒源uid, 类别, 细节)，
  能说明"哪个邻居唤醒了谁"。
"""

from .world import ATTACK_RANGE_SQ, World

KIND_COMMAND = "指令"
KIND_TARGET_MOVED = "目标移动"
KIND_CELL_FREED = "邻格让出"
KIND_PATH_CLEARED = "通路让开"
KIND_INFLUENCE = "影响范围激活"


class ScheduledWorld(World):
    """带候选集裁剪与唤醒溯源的 World；规则语义与 World 完全一致。"""

    def __init__(self, *args, strategy="dependency", influence_radius=3, **kwargs):
        super().__init__(*args, **kwargs)
        if strategy not in ("dependency", "influence", "reference"):
            raise ValueError(f"未知调度策略 {strategy!r}")
        self.strategy = strategy
        self.influence_radius = influence_radius
        self.eval_stats = []      # 每 tick 的求值统计（dict）
        self.wake_log = []        # (tick, woken_uid, by_uid|None, kind, detail)
        self._stat = self._new_stat()
        self._watch = {}          # uid -> 上次失败寻路触碰的占用格集合
        self._prev_pos = {u.uid: (u.x, u.y) for u in self.iter_units()}
        self._waiting = {}        # uid -> (下一格, 占位者uid)：移动阶段等待表
        self._freed_by = {}       # 本 tick cell -> 让出它的 uid
        self._pending_freed = {}  # 上一 tick 死亡让出的 cell -> uid（待消费）
        self._activations = []    # 本 tick 影响范围激活源 [(x, y)]
        self._recording = None    # 寻路期间记录占用格探测的集合

    # ---------- 统计与观测 ----------

    @staticmethod
    def _new_stat():
        return {"move_eval": 0, "move_skip": 0, "combat_eval": 0,
                "combat_skip": 0, "pathfinds": 0, "approaches": 0, "probes": 0}

    def unit_at(self, x, y):
        u = super().unit_at(x, y)
        self._stat["probes"] += 1
        if u is not None and self._recording is not None:
            self._recording.add((x, y))
        return u

    def find_path(self, start, goal):
        self._stat["pathfinds"] += 1
        return super().find_path(start, goal)

    def _approach_path(self, u, tgt):
        self._stat["approaches"] += 1
        if self.strategy == "reference":
            return super()._approach_path(u, tgt)
        self._recording = set()
        try:
            path = super()._approach_path(u, tgt)
        finally:
            recorded, self._recording = self._recording, None
        if path is None:
            # 搜索失败：结果只依赖这些占用格（空格变占用不会让失败变成功，
            # 只有被监视格让出才可能改变结果）——这就是显式唤醒依赖。
            self._watch[u.uid] = frozenset(recorded)
        else:
            self._watch.pop(u.uid, None)
        return path

    # ---------- 候选集 ----------

    def _phase_units(self, phase):
        units = self.iter_units()
        if self.strategy == "reference":
            key = "move_eval" if phase == "move" else "combat_eval"
            self._stat[key] += len(units)
            return units
        if phase == "move":
            # 无路径且 move_clock 已为 0 的单位在本阶段是无效更新（置 0 空操作）
            cands = [u for u in units if u.path or u.move_clock != 0]
            self._stat["move_eval"] += len(cands)
            self._stat["move_skip"] += len(units) - len(cands)
            return cands
        if phase == "combat":
            self._refresh_dirty()
            out = []
            for u in units:
                if u.target is None:
                    self._stat["combat_skip"] += 1
                    continue
                tgt = self.units.get(u.target)
                if tgt is None:
                    out.append(u)  # 目标已死，需清理（与参照一致）
                    continue
                d2 = (u.x - tgt.x) ** 2 + (u.y - tgt.y) ** 2
                if d2 <= ATTACK_RANGE_SQ:
                    out.append(u)  # 在射程内：攻击/清路径
                    continue
                if not u.path and u.chase == (tgt.x, tgt.y):
                    # 上次搜索失败且目标未动：可否复用失败结果？
                    if self._search_still_valid(u, tgt):
                        self._stat["combat_skip"] += 1
                        continue
                elif u.chase is not None and u.chase != (tgt.x, tgt.y):
                    self._log_wake(u.uid, tgt.uid, KIND_TARGET_MOVED,
                                   f"目标移至({tgt.x},{tgt.y})")
                out.append(u)
            self._stat["combat_eval"] += len(out)
            return out
        return units

    def _search_still_valid(self, u, tgt):
        """失败搜索是否可复用。可复用时参照语义下重搜结果仍是失败。"""
        if self.strategy == "influence":
            r2 = self.influence_radius ** 2
            for ax, ay in self._activations:
                if (u.x - ax) ** 2 + (u.y - ay) ** 2 <= r2:
                    self._log_wake(u.uid, None, KIND_INFLUENCE,
                                   f"激活源({ax},{ay})")
                    return False
                if (tgt.x - ax) ** 2 + (tgt.y - ay) ** 2 <= r2:
                    self._log_wake(u.uid, None, KIND_INFLUENCE,
                                   f"目标近激活源({ax},{ay})")
                    return False
            return True
        watch = self._watch.get(u.uid)
        if watch is None:
            return False  # 从未搜索过，必须评估
        for cell in sorted(watch):
            by = self._freed_by.get(cell)
            if by is not None:
                self._log_wake(u.uid, by, KIND_CELL_FREED,
                               f"监视格{cell}被让出，重新寻路")
                return False
        return True

    # ---------- 唤醒依赖跟踪 ----------

    def _refresh_dirty(self):
        """战斗阶段前调用：汇总自上一 tick 末以来的占位变化。

        移动让出由位置差直接看出；死亡让出无法从位置差看出（两端快照
        都没有死者），由上一 tick 末显式记录在 _pending_freed 中。
        """
        freed = dict(self._pending_freed)
        self._pending_freed = {}
        cur = {u.uid: (u.x, u.y) for u in self.iter_units()}
        for uid, pos in cur.items():
            old = self._prev_pos.get(uid)
            if old is not None and old != pos:
                freed[old] = uid  # 移动让出
                if self.strategy == "influence":
                    self._activations.append(pos)
        if self.strategy == "influence":
            self._activations.extend(freed)  # 让出的格子同样是激活源
        self._freed_by = freed

    def _log_wake(self, uid, by, kind, detail):
        self.wake_log.append((self.tick, uid, by, kind, detail))

    # ---------- tick 推进 ----------

    def step(self, merged_cmds):
        self._stat = self._new_stat()
        if self.strategy == "influence":
            self._activations = []
            for player, _seq, _idx, cmd in merged_cmds:
                if cmd.get("t") == "move":
                    u = self.units.get(cmd.get("u"))
                    if u is not None:
                        self._activations.append((u.x, u.y))
                    self._activations.append((cmd["x"], cmd["y"]))
                elif cmd.get("t") == "focus":
                    self._activations.append((cmd["x"], cmd["y"]))
        if self.strategy != "reference":
            for player, _seq, _idx, cmd in merged_cmds:
                ok, _reason = self.validate_command(player, cmd)
                if not ok:
                    continue  # 被拒指令不产生任何状态影响，不算唤醒源
                if cmd.get("t") == "move":
                    self._log_wake(cmd.get("u"), None, KIND_COMMAND,
                                   f"move→({cmd['x']},{cmd['y']})")
                elif cmd.get("t") == "focus":
                    self._log_wake(None, None, KIND_COMMAND,
                                   f"端{player} focus→({cmd['x']},{cmd['y']})")
        super().step(merged_cmds)
        if self.strategy != "reference":
            self._post_tick_tracking()
        self.eval_stats.append(self._stat)

    def _post_tick_tracking(self):
        """tick 末：识别移动连锁唤醒边，更新等待表与位置基线。

        等待表记录"单位正被谁堵在下一格"。格子让出后条目不立即丢弃：
        阶段内按 uid 顺序结算，让路者可能排在使用者之后移动，使用者
        要到下一拍才能走进空格——唤醒归因必须保留到它真正移动为止。
        """
        t = self.tick - 1
        cur = {u.uid: (u.x, u.y) for u in self.iter_units()}
        for uid, pos in cur.items():
            old = self._prev_pos.get(uid)
            if old is None or old == pos:
                continue
            wait = self._waiting.get(uid)
            if wait is not None and pos == wait[0]:
                # 上一拍还在等这格、这一拍走了进去：占位者让开了通路
                self.wake_log.append(
                    (t, uid, wait[1], KIND_PATH_CLEARED, f"进入{pos}"))
        waiting = {}
        for u in self.iter_units():
            if not u.path:
                continue
            occ = super().unit_at(*u.path[0])
            if occ is not None:
                waiting[u.uid] = (u.path[0], occ.uid)
            else:
                prev = self._waiting.get(u.uid)
                if prev is not None and prev[0] == u.path[0]:
                    waiting[u.uid] = prev  # 格已让出、尚未走入：保留归因
        self._waiting = waiting
        # 本 tick 清理阶段死亡的单位：其格让出，留给下一拍的依赖检查
        for uid, pos in self._prev_pos.items():
            if uid not in cur:
                self._pending_freed[pos] = uid
        self._prev_pos = cur

    # ---------- 沙盘查询 ----------

    def wake_explanation(self, uid):
        """指定单位被谁唤醒过（按 tick 升序的文字说明）。"""
        lines = []
        for t, woken, by, kind, detail in self.wake_log:
            if woken != uid:
                continue
            src = "指令" if by is None else f"单位{by}"
            lines.append(f"帧{t}：{src} {kind}（{detail}）")
        return lines
