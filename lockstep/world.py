"""确定性战术模拟内核。

确定性规则（违反任何一条都会在逐帧摘要比对中暴露）：
- 单位一律按 uid 升序遍历，绝不依赖 dict/set 的迭代顺序；
- 随机数只来自注入种子的 Rng，且只在 tick 内按固定顺序消耗；
- 空间查询结果按 (距离平方, uid) 排序，与插入顺序无关；
- 寻路为固定邻居顺序的 BFS；
- 指令结算、移动、攻击全部发生在 tick 内的固定阶段；网络线程、
  界面回调等异步来源不得在 tick 之外改写世界状态——它们只能排队，
  由 tick 边界统一应用（路径请求/动作完成不会"到达即生效"）。
"""

from collections import deque

from .rng import Rng

MOVE_TICKS = 3          # 每走一格所需 tick 数
ATTACK_RANGE_SQ = 9     # 攻击距离（平方，整数运算）
DAMAGE_BASE = 4
DAMAGE_SPREAD = 3       # 伤害 = BASE + rng.randint(0, SPREAD-1)
COOLDOWN_TICKS = 5
MAX_HP = 30

NEIGHBORS = ((0, -1), (1, 0), (0, 1), (-1, 0))  # 固定顺序：上右下左


class Unit:
    __slots__ = (
        "uid", "squad", "x", "y", "hp",
        "path", "move_clock", "target", "cooldown", "chase", "blocked",
    )

    def __init__(self, uid, squad, x, y):
        self.uid = uid
        self.squad = squad
        self.x = x
        self.y = y
        self.hp = MAX_HP
        self.path = []          # 剩余路径 [(x,y), ...]
        self.move_clock = 0     # 距上次移动的 tick 计数
        self.target = None      # 攻击目标 uid 或 None
        self.cooldown = 0
        self.chase = None       # 追击目标的上次位置（决定是否重寻路）
        self.blocked = False    # 本 tick 是否被占位挡住（仅用于事件去抖）


class SpatialIndex:
    """均匀网格空间索引。

    查询结果按 (距离平方, uid) 显式排序：无论单元格如何散列、
    单位以何种顺序插入，返回序列都唯一确定。
    """

    def __init__(self, units, cell=4):
        self.cell = cell
        self.map = {}
        for u in units:  # 调用方保证按 uid 升序传入
            key = (u.x // cell, u.y // cell)
            self.map.setdefault(key, []).append(u)

    def query(self, x, y, radius):
        r2 = radius * radius
        c = self.cell
        found = []
        for (kx, ky) in sorted(self.map):
            if kx * c > x + radius or kx * c + c - 1 < x - radius:
                continue
            if ky * c > y + radius or ky * c + c - 1 < y - radius:
                continue
            for u in self.map[(kx, ky)]:
                d2 = (u.x - x) ** 2 + (u.y - y) ** 2
                if d2 <= r2:
                    found.append((d2, u.uid))
        found.sort()
        return [uid for _, uid in found]


class World:
    def __init__(self, width, height, obstacles, units, seed):
        self.width = width
        self.height = height
        self.obstacles = frozenset(obstacles)
        self.units = {u.uid: u for u in units}
        self.rng = Rng(seed)
        self.tick = 0
        self.events = []  # 本 tick 内产生的动作事件（供界面日志，不入摘要）

    # ---------- 基础查询（全部确定性） ----------

    def iter_units(self):
        return [self.units[k] for k in sorted(self.units)]

    def unit_at(self, x, y):
        for u in self.iter_units():
            if u.x == x and u.y == y:
                return u
        return None

    def in_bounds(self, x, y):
        return 0 <= x < self.width and 0 <= y < self.height

    def is_blocked(self, x, y):
        return (not self.in_bounds(x, y)) or (x, y) in self.obstacles

    def units_within(self, x, y, radius):
        """空间查询：返回按 (距离平方, uid) 排序的 uid 列表。"""
        return SpatialIndex(self.iter_units()).query(x, y, radius)

    # ---------- 寻路 ----------

    def find_path(self, start, goal):
        """固定邻居顺序的 BFS；同一世界状态下结果唯一。"""
        sx, sy = start
        gx, gy = goal
        if start == goal:
            return []
        if self.is_blocked(gx, gy):
            return None
        occ = self.unit_at(gx, gy)
        if occ is not None and (occ.x, occ.y) != (sx, sy):
            return None
        came = {start: None}
        frontier = deque([start])
        while frontier:
            cur = frontier.popleft()
            for dx, dy in NEIGHBORS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in came or self.is_blocked(*nxt):
                    continue
                if self.unit_at(*nxt) is not None:
                    continue
                came[nxt] = cur
                if nxt == goal:
                    path = []
                    node = goal
                    while node != start:
                        path.append(node)
                        node = came[node]
                    path.reverse()
                    return path
                frontier.append(nxt)
        return None

    def _approach_path(self, u, tgt):
        """追击：在目标四周空格中按 (离u距离, x, y) 选第一个可达的。"""
        cands = []
        for dx, dy in NEIGHBORS:
            nx, ny = tgt.x + dx, tgt.y + dy
            if self.is_blocked(nx, ny) or self.unit_at(nx, ny) is not None:
                continue
            d2 = (u.x - nx) ** 2 + (u.y - ny) ** 2
            cands.append((d2, nx, ny))
        for _, nx, ny in sorted(cands):
            path = self.find_path((u.x, u.y), (nx, ny))
            if path is not None:
                return path
        return None

    # ---------- 指令 ----------

    def validate_command(self, player, cmd):
        """指令校验（受理时与结算时各做一次，结果反馈给发起端）。"""
        t = cmd.get("t")
        if t == "move":
            u = self.units.get(cmd.get("u"))
            if u is None:
                return False, f"单位{cmd.get('u')}不存在或已阵亡"
            if u.squad != player:
                return False, f"单位{u.uid}不属于本端小队"
            x, y = cmd["x"], cmd["y"]
            if self.is_blocked(x, y):
                return False, f"目标格({x},{y})不可通行"
            return True, ""
        if t == "focus":
            x, y = cmd["x"], cmd["y"]
            if not self.in_bounds(x, y):
                return False, "集火点超出地图"
            if not any(u.squad != player for u in self.iter_units()):
                return False, "没有可集火的敌方单位"
            return True, ""
        return False, f"未知指令类型 {t!r}"

    def _apply_command(self, player, cmd):
        ok, reason = self.validate_command(player, cmd)
        if not ok:
            self.events.append(f"指令驳回（端{player}）：{reason}")
            return
        t = cmd["t"]
        if t == "move":
            u = self.units[cmd["u"]]
            path = self.find_path((u.x, u.y), (cmd["x"], cmd["y"]))
            if path is None:
                self.events.append(f"单位{u.uid} 无法到达 ({cmd['x']},{cmd['y']})")
                return
            u.path = path
            u.target = None
            u.chase = None
            u.move_clock = 0
            u.blocked = False
            self.events.append(f"单位{u.uid} 前往 ({cmd['x']},{cmd['y']})")
        elif t == "focus":
            px, py = cmd["x"], cmd["y"]
            enemies = [e for e in self.iter_units() if e.squad != player]
            tgt = min(enemies, key=lambda e: ((e.x - px) ** 2 + (e.y - py) ** 2, e.uid))
            n = 0
            for u in self.iter_units():
                if u.squad != player:
                    continue
                u.target = tgt.uid
                u.path = []
                u.chase = None
                n += 1
            self.events.append(
                f"端{player} 小队集火 ({px},{py}) → 单位{tgt.uid}（{n} 个单位响应）"
            )

    # ---------- tick 推进：固定阶段 ----------

    def step(self, merged_cmds):
        """推进一个模拟帧。merged_cmds 必须来自 merge_frames 的唯一顺序。"""
        self.events = []
        for player, _seq, _idx, cmd in merged_cmds:
            self._apply_command(player, cmd)
        self._movement_phase()
        self._combat_phase()
        self._cleanup_phase()
        self.tick += 1

    def _movement_phase(self):
        for u in self.iter_units():
            if not u.path:
                u.move_clock = 0
                continue
            u.move_clock += 1
            if u.move_clock < MOVE_TICKS:
                continue
            nx, ny = u.path[0]
            if self.is_blocked(nx, ny) or self.unit_at(nx, ny) is not None:
                if not u.blocked:
                    self.events.append(f"单位{u.uid} 通道被堵，等待 ({nx},{ny})")
                    u.blocked = True
                continue  # 占位等待，不重置 move_clock，下 tick 重试
            u.x, u.y = nx, ny
            u.path.pop(0)
            u.move_clock = 0
            u.blocked = False
            if not u.path:
                self.events.append(f"单位{u.uid} 到达 ({nx},{ny})")

    def _combat_phase(self):
        strikes = []  # 先决策、后结算：伤害在阶段末按 uid 顺序统一生效
        for u in self.iter_units():
            if u.target is None:
                continue
            tgt = self.units.get(u.target)
            if tgt is None:
                u.target = None
                u.chase = None
                continue
            d2 = (u.x - tgt.x) ** 2 + (u.y - tgt.y) ** 2
            if d2 <= ATTACK_RANGE_SQ:
                if u.path:
                    u.path = []
                    u.chase = None
                if u.cooldown == 0:
                    dmg = DAMAGE_BASE + self.rng.randint(0, DAMAGE_SPREAD - 1)
                    strikes.append((u.uid, tgt.uid, dmg))
                    u.cooldown = COOLDOWN_TICKS
                    self.events.append(f"单位{u.uid} 攻击 单位{tgt.uid}（-{dmg}）")
            else:
                if u.chase != (tgt.x, tgt.y) or not u.path:
                    u.chase = (tgt.x, tgt.y)
                    path = self._approach_path(u, tgt)
                    u.path = path if path is not None else []
                    u.move_clock = 0
        for _src, dst, dmg in strikes:
            self.units[dst].hp -= dmg

    def _cleanup_phase(self):
        for uid in [u.uid for u in self.iter_units() if u.hp <= 0]:
            del self.units[uid]
            self.events.append(f"单位{uid} 被消灭")
        for u in self.iter_units():
            if u.target is not None and u.target not in self.units:
                u.target = None
                u.chase = None
            if u.cooldown > 0:
                u.cooldown -= 1


def build_world(seed, scenario="skirmish"):
    """从种子构建初始世界；同一种子在所有端产生完全相同的世界。"""
    if scenario != "skirmish":
        raise ValueError(f"未知场景 {scenario!r}")
    width, height = 24, 16
    obstacles = {(12, y) for y in range(height) if y not in (7, 8)}
    obstacles |= {(6, 4), (6, 11), (17, 4), (17, 11)}
    placement = [(2, 1), (2, 4), (2, 7), (2, 10), (2, 13), (4, 8)]
    units = []
    for i, (x, y) in enumerate(placement):
        units.append(Unit(i, 0, x, y))
    for i, (x, y) in enumerate(placement):
        units.append(Unit(6 + i, 1, width - 1 - x, y))
    return World(width, height, obstacles, units, seed)
