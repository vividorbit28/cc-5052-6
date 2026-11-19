"""锁步端点：输入帧交换、输入延迟、缺席政策、逐帧摘要比对。

每个模拟帧必须汇齐双端输入帧后，两端各自按 merge_frames 的唯一顺序
推进本地模拟；随后交换该帧状态摘要。任何分歧都会让端点 halted，
并把首个不一致的 tick/单位反馈给会话层——失败回流到锁步内核，
而不是被状态同步悄悄盖掉。
"""

import json

from .commands import canonical_bytes, make_frame, merge_frames
from .digest import canonical_world, unit_digests, world_digest


class Endpoint:
    def __init__(self, name, peer_name, player, world, link, input_delay=2,
                 missing_policy="wait", on_frame=None, scripted_frames=None):
        self.name = name
        self.peer_name = peer_name
        self.player = player
        self.world = world
        self.link = link
        self.input_delay = input_delay
        self.missing_policy = missing_policy  # "wait" 明确等待 | "zero" 断线空输入替代
        self.on_frame = on_frame              # 录制回调
        self.scripted_frames = scripted_frames  # 回放：{(player, tick): frame}

        self.out_seq = 0
        self.pending = {}         # tick -> [cmd] 已受理待排入的本地指令
        self.sent = {}            # tick -> 本端输入帧
        self.recv = {}            # tick -> 对端输入帧
        self.digests = {}         # tick -> 本端摘要
        self.unit_hashes = {}     # tick -> {uid: 短摘要}
        self.peer_digests = {}    # tick -> 对端摘要
        self.peer_unit_hashes = {}
        self.snapshots = {}       # tick -> 规范快照（分歧定位用）
        self.checks = []          # (tick, ok)
        self._checked = set()

        self.waiting_for = None   # (peer_name, tick) 当前等待来源
        self.divergence = None    # {"tick","mine","theirs","first_uid"}
        self.peer_snapshot = None # 分歧后对端发来的诊断快照
        self.halted = False
        self.degraded = False     # 断线替代政策已生效

    # ---------- 指令入口：只排队，由 tick 边界统一结算 ----------

    def issue(self, cmd):
        ok, reason = self.world.validate_command(self.player, cmd)
        if not ok:
            return False, reason
        tick = self.world.tick + self.input_delay
        self.pending.setdefault(tick, []).append(cmd)
        return True, f"已排入第 {tick} 帧"

    # ---------- 网络 ----------

    def pump(self):
        for data in self.link.poll():
            self._handle(json.loads(data.decode("utf-8")))

    def _handle(self, msg):
        kind = msg["kind"]
        if kind == "frame":
            frame = msg["frame"]
            self.recv[frame["tick"]] = frame
        elif kind == "digest":
            t = msg["tick"]
            self.peer_digests[t] = msg["digest"]
            self.peer_unit_hashes[t] = {int(k): v for k, v in msg["units"].items()}
            self._compare(t)
        elif kind == "detail":
            self.peer_snapshot = msg

    def _send(self, obj):
        try:
            self.link.send(canonical_bytes(obj))
        except (OSError, RuntimeError):
            self.link.peer_closed = True

    def _ensure_frame(self, tick):
        if tick in self.sent:
            return
        if self.scripted_frames is not None:
            frame = self.scripted_frames.get((self.player, tick))
            if frame is None:
                frame = make_frame(tick, self.player, self.out_seq, [])
        else:
            frame = make_frame(tick, self.player, self.out_seq,
                               self.pending.pop(tick, []))
        self.out_seq += 1
        self.sent[tick] = frame
        self._send({"kind": "frame", "frame": frame})
        if self.on_frame is not None:
            self.on_frame(frame)

    # ---------- 锁步推进 ----------

    def try_advance(self):
        """汇齐本帧双端输入则推进一步；否则明确等待（或按政策替代）。"""
        if self.halted:
            return False
        t = self.world.tick
        self._ensure_frame(t)
        peer_frame = self.recv.pop(t, None)
        if peer_frame is None:
            if self.link.peer_closed and self.missing_policy == "zero":
                # 断线替代：双端对同一 tick 用相同的空帧才能保持一致，
                # 因此该政策要求断线点没有在途/待排入的指令（否则未送达
                # 的指令本就无法挽救，分歧是真实结果而非政策缺陷）。
                peer_frame = make_frame(t, 1 - self.player, -1, [])
                self.degraded = True
            else:
                self.waiting_for = (self.peer_name, t)
                return False
        self.waiting_for = None
        merged = merge_frames([self.sent[t], peer_frame])
        self.world.step(merged)
        digest = world_digest(self.world)
        units = unit_digests(self.world)
        self.digests[t] = digest
        self.unit_hashes[t] = units
        self.snapshots[t] = canonical_world(self.world)
        self._send({"kind": "digest", "tick": t, "digest": digest, "units": units})
        self._compare(t)
        return True

    def _compare(self, t):
        if t in self._checked:
            return
        mine = self.digests.get(t)
        theirs = self.peer_digests.get(t)
        if mine is None or theirs is None:
            return
        self._checked.add(t)
        ok = mine == theirs
        self.checks.append((t, ok))
        if not ok and not self.halted:
            first_uid = None
            my_uh = self.unit_hashes.get(t, {})
            their_uh = self.peer_unit_hashes.get(t, {})
            for uid in sorted(set(my_uh) | set(their_uh)):
                if my_uh.get(uid) != their_uh.get(uid):
                    first_uid = uid
                    break
            self.divergence = {
                "tick": t, "mine": mine, "theirs": theirs, "first_uid": first_uid,
            }
            self.halted = True
            # 仅在分歧发生后交换一次诊断快照用于定位，不参与模拟
            self._send({"kind": "detail", "tick": t,
                        "snapshot": self.snapshots.get(t)})

    def status(self):
        if self.divergence is not None:
            return f"分歧@帧{self.divergence['tick']}，已停等"
        if self.degraded:
            return f"对端断线→空输入替代（tick={self.world.tick}）"
        if self.link.peer_closed:
            return "对端断线（政策=等待，已停等）"
        if self.waiting_for is not None:
            return f"等待{self.waiting_for[0]}第{self.waiting_for[1]}帧"
        return f"运行中 tick={self.world.tick}"
