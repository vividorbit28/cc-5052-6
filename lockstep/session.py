"""单进程双端会话：两端通过延迟链路交换序列化消息，模拟真实联机锁步。

界面与测试共用的驱动接口：issue / update / step_once / worlds / statuses /
checks / divergence / drain_log / set_latency / tamper / save_recording。
"""

import time

from .digest import diff_snapshots
from .endpoint import Endpoint
from .transport import LatentLink
from .world import build_world

MAX_CATCHUP = 64  # 单次 update 的最大追帧数；追不完的 backlog 保留，绝不丢弃


class LockstepSession:
    title = "策略锁步演练（本机双端）"
    view_labels = ("端A 模拟视图", "端B 模拟视图")
    local_player = None  # 两端都可指挥
    latency_editable = True

    def __init__(self, seed=20261118, latency_ms=60, input_delay=2, tick_dt=0.1,
                 clock=time.monotonic, missing_policy="wait", scripted_frames=None):
        self.seed = seed
        self.input_delay = input_delay
        self.tick_dt = tick_dt
        self.clock = clock
        link_a, link_b = LatentLink.pair(latency_ms / 1000.0, clock)
        self.links = (link_a, link_b)
        self.frames = []  # 录制的输入脚本（两端发出的全部帧）
        self.log = []
        self._acc = 0.0
        self.eps = []
        for player, name, peer, link in (
            (0, "端A", "端B", link_a),
            (1, "端B", "端A", link_b),
        ):
            self.eps.append(Endpoint(
                name, peer, player, build_world(seed), link,
                input_delay=input_delay, missing_policy=missing_policy,
                on_frame=self.frames.append, scripted_frames=scripted_frames,
            ))

    # ---------- 指令 ----------

    def issue(self, player, cmd):
        ok, msg = self.eps[player].issue(cmd)
        tag = "受理" if ok else "被拒"
        self.log.append(f"[{self.eps[player].name}] 指令{tag}：{msg}")
        return ok, msg

    # ---------- 推进 ----------

    def pump(self):
        for ep in self.eps:
            ep.pump()

    def _pump_step_once(self):
        """一轮"收信→尝试推进"，做两趟以覆盖零延迟下的当轮互投。"""
        progressed = False
        for _ in range(2):
            for ep in self.eps:
                ep.pump()
            for ep in self.eps:
                if ep.try_advance():
                    progressed = True
                    self._drain_events(ep)
        return progressed

    def _drain_events(self, ep):
        # 两端事件流确定一致，只收端A的以免重复
        if ep is not self.eps[0]:
            return
        t = ep.world.tick - 1
        for ev in ep.world.events:
            self.log.append(f"[帧{t}] {ev}")

    def min_tick(self):
        return min(ep.world.tick for ep in self.eps)

    def step_once(self):
        """手动单步：尝试让双端各推进一帧；缺输入时等待来源会显示在界面上。"""
        self._pump_step_once()

    def update(self, dt):
        """按真实流逝时间推进：累加器以固定步长消费。

        渲染慢只会落后、不会少走——没追上的 backlog 留在累加器里，
        等待对端输入时同样保留，绝不偷偷丢帧。
        """
        self._acc += dt
        budget = MAX_CATCHUP
        while self._acc >= self.tick_dt and budget > 0:
            before = self.min_tick()
            self._pump_step_once()
            if self.min_tick() == before:
                break  # 等待对端输入帧；累加器保留
            self._acc -= self.tick_dt
            budget -= 1
        self.pump()

    # ---------- 状态查询 ----------

    def worlds(self):
        return [ep.world for ep in self.eps]

    def statuses(self):
        return [ep.status() for ep in self.eps]

    def tick_info(self):
        return f"帧 {self.min_tick()}（端A:{self.eps[0].world.tick} 端B:{self.eps[1].world.tick}）"

    def checks(self):
        a, b = self.eps
        out = []
        for t in sorted(a.digests):
            if t in b.digests:
                out.append((t, a.digests[t] == b.digests[t],
                            a.digests[t], b.digests[t]))
        return out

    @property
    def halted(self):
        return any(ep.halted for ep in self.eps)

    def divergence(self):
        div = next((ep.divergence for ep in self.eps if ep.divergence), None)
        if div is None:
            return None
        t = div["tick"]
        return {
            "tick": t,
            "first_uid": div["first_uid"],
            "text": diff_snapshots(self.eps[0].snapshots.get(t),
                                   self.eps[1].snapshots.get(t)),
            "digest_a": self.eps[0].digests.get(t),
            "digest_b": self.eps[1].digests.get(t),
        }

    def drain_log(self):
        out, self.log = self.log, []
        return out

    # ---------- 演示/测试钩子 ----------

    def set_latency(self, ms):
        for link in self.links:
            link.set_latency(ms / 1000.0)

    def tamper(self):
        """测试注入：绕过指令通道直接篡改端B状态，验证分歧检测。"""
        w = self.eps[1].world
        for u in w.iter_units():
            u.hp = max(1, u.hp - 7)
            msg = f"已注入扰动：端B 单位{u.uid} hp→{u.hp}（绕过指令通道）"
            self.log.append(msg)
            return msg
        return "无可扰动单位"

    def save_recording(self, path):
        from .replay import save_recording
        save_recording(self, path)
