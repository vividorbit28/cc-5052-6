"""指令与输入帧：定义、规范序列化、确定顺序合并。

线上只传输入帧（指令）与状态摘要；两端收到同一组帧后，
merge_frames 给出与到达先后无关的唯一指令序列。
"""

import json


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_move(uid: int, x: int, y: int) -> dict:
    return {"t": "move", "u": uid, "x": x, "y": y}


def make_focus(x: int, y: int) -> dict:
    """协作动作：本端小队集火离 (x,y) 最近的敌人。"""
    return {"t": "focus", "x": x, "y": y}


def make_frame(tick: int, player: int, seq: int, cmds) -> dict:
    return {"tick": tick, "player": player, "seq": seq, "cmds": list(cmds)}


def merge_frames(frames):
    """把同一 tick 的各端输入帧合并成唯一确定的指令流。

    排序键 (player, seq, 帧内序号) 与网络到达顺序无关。
    返回 [(player, seq, idx, cmd), ...]
    """
    out = []
    for frame in sorted(frames, key=lambda f: (f["player"], f["seq"])):
        for idx, cmd in enumerate(frame["cmds"]):
            out.append((frame["player"], frame["seq"], idx, cmd))
    return out
