"""状态摘要与快照差异定位。

摘要覆盖：tick、RNG 状态、每个单位的全部规则字段（按 uid 升序）。
逐帧比对摘要即可证明"过程一致"，而不是只看最终单位数量。
"""

import hashlib

from .commands import canonical_bytes

UNIT_FIELDS = (
    "uid", "squad", "x", "y", "hp", "target",
    "cooldown", "move_clock", "blocked", "chase", "path",
)


def canonical_unit(u):
    return [
        u.uid, u.squad, u.x, u.y, u.hp, u.target,
        u.cooldown, u.move_clock, int(u.blocked),
        list(u.chase) if u.chase else None,
        [list(p) for p in u.path],
    ]


def canonical_world(world):
    return {
        "tick": world.tick,
        "rng": world.rng.state,
        "units": [canonical_unit(u) for u in world.iter_units()],
    }


def world_digest(world) -> str:
    return hashlib.sha256(canonical_bytes(canonical_world(world))).hexdigest()


def unit_digests(world):
    """逐单位短摘要：分歧时用于定位第一个不一致的单位。"""
    return {
        u.uid: hashlib.sha256(canonical_bytes(canonical_unit(u))).hexdigest()[:10]
        for u in world.iter_units()
    }


def diff_snapshots(a, b) -> str:
    """比较两份规范快照，返回第一处差异的描述。"""
    if a is None or b is None:
        return "缺少一侧快照，无法定位"
    if a["tick"] != b["tick"]:
        return f"tick 不同：{a['tick']} vs {b['tick']}"
    if a["rng"] != b["rng"]:
        return f"RNG 状态不同：{a['rng']} vs {b['rng']}"
    ua, ub = a["units"], b["units"]
    if len(ua) != len(ub):
        return f"单位数量不同：{len(ua)} vs {len(ub)}"
    for ca, cb in zip(ua, ub):
        if ca == cb:
            continue
        for i, name in enumerate(UNIT_FIELDS):
            if ca[i] != cb[i]:
                return f"单位{ca[0]} 字段 {name}: {ca[i]!r} vs {cb[i]!r}"
        return f"单位{ca[0]} 内容不同"
    return "快照一致"
