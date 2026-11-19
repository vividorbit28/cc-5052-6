"""TCP 联机驱动：每端一个进程，各自只发输入帧与逐帧摘要。

与 LockstepSession 实现同一套驱动接口，界面无需区分本机双端还是联机。
"""

import json
import time

from .commands import canonical_bytes
from .digest import diff_snapshots
from .endpoint import Endpoint
from .transport import TcpLink
from .world import build_world

MAX_CATCHUP = 64


class NetDriver:
    latency_editable = False

    def __init__(self, link, player, seed, input_delay=2, tick_dt=0.1):
        self.link = link
        self.player = player
        self.local_player = player
        self.seed = seed
        self.input_delay = input_delay
        self.tick_dt = tick_dt
        role = "主机" if player == 0 else "客户端"
        self.title = f"策略锁步演练（联机 {role}，指挥小队{player}）"
        self.view_labels = ("本端模拟 · 小队0视角", "本端模拟 · 小队1视角")
        self.ep = Endpoint(f"端{chr(65 + player)}", "对端", player,
                           build_world(seed), link, input_delay=input_delay)
        self.frames = []
        self.ep.on_frame = self.frames.append
        self.log = []
        self._acc = 0.0

    @classmethod
    def listen(cls, port, seed, input_delay=2, timeout=60):
        link = TcpLink.listen(port, timeout)
        link.send(canonical_bytes({
            "kind": "hello", "seed": seed, "input_delay": input_delay,
        }))
        return cls(link, 0, seed, input_delay)

    @classmethod
    def connect(cls, host, port, timeout=60):
        link = TcpLink.connect(host, port, timeout)
        hello = json.loads(link.recv_blocking(timeout).decode("utf-8"))
        if hello.get("kind") != "hello":
            raise RuntimeError("握手失败：对端不是锁步主机")
        return cls(link, 1, hello["seed"], hello["input_delay"])

    # ---------- 驱动接口 ----------

    def issue(self, player, cmd):
        if player != self.player:
            msg = "联机模式只能指挥本端小队"
            self.log.append(f"[{self.ep.name}] 指令被拒：{msg}")
            return False, msg
        ok, msg = self.ep.issue(cmd)
        tag = "受理" if ok else "被拒"
        self.log.append(f"[{self.ep.name}] 指令{tag}：{msg}")
        return ok, msg

    def _pump_step(self):
        self.ep.pump()
        progressed = self.ep.try_advance()
        self.ep.pump()
        if progressed:
            t = self.ep.world.tick - 1
            for ev in self.ep.world.events:
                self.log.append(f"[帧{t}] {ev}")
        return progressed

    def step_once(self):
        self._pump_step()

    def update(self, dt):
        self._acc += dt
        budget = MAX_CATCHUP
        while self._acc >= self.tick_dt and budget > 0:
            before = self.ep.world.tick
            self._pump_step()
            if self.ep.world.tick == before:
                break
            self._acc -= self.tick_dt
            budget -= 1
        self.ep.pump()

    def worlds(self):
        return [self.ep.world, self.ep.world]

    def statuses(self):
        peer = "断线" if self.link.peer_closed else "联机中"
        return [self.ep.status(), f"对端：{peer}"]

    def tick_info(self):
        return f"帧 {self.ep.world.tick}"

    def checks(self):
        out = []
        for t in sorted(self.ep.digests):
            if t in self.ep.peer_digests:
                out.append((t, self.ep.digests[t] == self.ep.peer_digests[t],
                            self.ep.digests[t], self.ep.peer_digests[t]))
        return out

    @property
    def halted(self):
        return self.ep.halted

    def divergence(self):
        div = self.ep.divergence
        if div is None:
            return None
        t = div["tick"]
        peer_snap = None
        if self.ep.peer_snapshot is not None:
            peer_snap = self.ep.peer_snapshot.get("snapshot")
        return {
            "tick": t,
            "first_uid": div["first_uid"],
            "text": diff_snapshots(self.ep.snapshots.get(t), peer_snap),
            "digest_a": self.ep.digests.get(t),
            "digest_b": self.ep.peer_digests.get(t),
        }

    def drain_log(self):
        out, self.log = self.log, []
        return out

    def set_latency(self, ms):
        pass  # 真实网络延迟不可调

    def tamper(self):
        w = self.ep.world
        for u in w.iter_units():
            u.hp = max(1, u.hp - 7)
            msg = f"已注入扰动：本端 单位{u.uid} hp→{u.hp}（绕过指令通道）"
            self.log.append(msg)
            return msg
        return "无可扰动单位"

    def save_recording(self, path):
        rec = {
            "config": {"seed": self.seed, "input_delay": self.input_delay,
                       "tick_dt": self.tick_dt},
            "frames": self.frames,
            "digests": {"端A": {str(t): d for t, d in self.ep.digests.items()}},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
