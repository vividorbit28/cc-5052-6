"""演示/自检共用的脚本化指令序列。

(at_tick, player, cmd)：当会话推进到 at_tick 时受理该指令。
"""

from .commands import make_focus, make_move

SELFTEST_TICKS = 140


def demo_script():
    return [
        (2, 0, make_move(2, 10, 7)),    # 端A 前锋压向中央缺口
        (2, 0, make_move(5, 10, 8)),
        (3, 1, make_move(8, 14, 7)),    # 端B 前锋迎战
        (3, 1, make_move(11, 14, 8)),
        (18, 0, make_focus(14, 7)),     # 端A 小队集火（协作动作）
        (20, 1, make_focus(10, 8)),     # 端B 反集火
        (45, 0, make_move(0, 6, 3)),    # 侧翼机动
        (45, 1, make_move(6, 17, 3)),
        (46, 0, make_move(4, 6, 12)),
        (46, 1, make_move(10, 17, 12)),
    ]


def sorted_script():
    """按 (tick, player) 排序的脚本；指令 dict 本身不参与比较。"""
    return sorted(demo_script(), key=lambda s: (s[0], s[1]))
