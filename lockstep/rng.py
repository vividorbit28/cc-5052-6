"""确定性随机流（splitmix64）。

模拟内核只允许使用该流，且只能在 tick 内按固定顺序消耗；
rng.state 计入状态摘要，任何消耗顺序差异都会立刻暴露。
"""

MASK = (1 << 64) - 1


class Rng:
    __slots__ = ("state",)

    def __init__(self, seed: int):
        self.state = seed & MASK

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return (z ^ (z >> 31)) & MASK

    def randint(self, lo: int, hi: int) -> int:
        """闭区间整数。"""
        return lo + self.next_u64() % (hi - lo + 1)
