"""调度器验收场景：开门连锁、道路堵塞。

每个场景提供：
- build_*(world_cls, seed, **kw)：构造一个全新世界（优化端与参照端各自调用，
  得到互不共享状态的两份世界）；
- *_script()：(at_tick, player, cmd) 指令脚本，由沙盘转成输入帧。

door：追猎小队被门格挡住，搜索持续失败；开门后按状态依赖应连锁唤醒。
blocked：纵队收到移动指令但被前方占位堵住，静止等待让路者。
"""

from .commands import make_focus, make_move
from .world import Unit


def build_door(world_cls, seed=1, **kw):
    """开门场景：墙中一扇门，门后追猎小队依赖门格让出。

    单位：0,1,2 = 小队0 追猎者（墙左）；3 = 小队1 门卫；4 = 小队1 目标。
    """
    width, height = 20, 12
    obstacles = {(12, y) for y in range(height) if y != 6}  # 门 = (12,6)
    units = [
        Unit(0, 0, 4, 4), Unit(1, 0, 4, 6), Unit(2, 0, 4, 8),
        Unit(3, 1, 9, 6),   # 门卫：先占住门格，稍后奉命让开
        Unit(4, 1, 16, 6),  # 追击目标：原地不动
    ]
    return world_cls(width, height, obstacles, units, seed, **kw)


def door_script():
    return [
        (0, 1, make_move(3, 12, 6)),    # 门卫占门（堵住唯一通道）
        (12, 0, make_focus(16, 6)),     # 追猎小队集火门后目标 → 搜索持续失败
        (30, 1, make_move(3, 10, 1)),   # 门卫让开（开门）→ 应唤醒门后小队
    ]


def build_blocked(world_cls, seed=1, **kw):
    """道路堵塞场景：开阔区漏斗汇入唯一门格，纵队被占位者堵住。

    单位：0,1,2 = 小队0 纵队（按预计到达门格的先后排 uid）；
    3 = 小队1 堵路者（先占门，稍后奉命让开）。
    """
    width, height = 20, 8
    obstacles = {(10, y) for y in range(height) if y != 3}  # 门 = (10,3)
    units = [
        Unit(0, 0, 2, 4), Unit(1, 0, 2, 2), Unit(2, 0, 2, 6),
        Unit(3, 1, 7, 3),
    ]
    return world_cls(width, height, obstacles, units, seed, **kw)


def blocked_script():
    return [
        (0, 1, make_move(3, 10, 3)),    # 堵路者抢占门格并停下
        (0, 0, make_move(0, 16, 3)),    # 纵队收到移动指令（门将在途中被堵）
        (0, 0, make_move(1, 15, 3)),
        (0, 0, make_move(2, 14, 3)),
        (40, 1, make_move(3, 13, 1)),   # 堵路者让开 → 纵队应被逐个唤醒
    ]
