"""窗口冒烟测试：用桩 tkinter 驱动 LockstepWindow 的完整交互链路。

本环境没有显示服务器，无法真实开窗；该测试验证界面装配与
"点击 → 指令受理 → 单步推进 → 注入分歧 → 界面检出"的接线。
"""

import sys
import types
import unittest
from types import SimpleNamespace


class _Var:
    def __init__(self, value=None):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _Widget:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.items = []

    def __getattr__(self, name):
        # 未显式实现的控件方法一律视为无操作（布局、样式等）
        return lambda *a, **k: None

    def grid(self, *a, **k):
        pass

    def config(self, **k):
        self.kwargs.update(k)

    configure = config

    def bind(self, *a, **k):
        pass

    def state(self, *a, **k):
        pass

    def delete(self, *a, **k):
        pass

    def see(self, *a, **k):
        pass

    def yview(self, *a, **k):
        pass

    def insert(self, *a, **k):
        self.items.append((a, k))
        return len(self.items)

    def get_children(self, *a, **k):
        return list(range(len(self.items)))

    def create_line(self, *a, **k):
        return 0

    create_rectangle = create_line
    create_oval = create_line
    create_text = create_line

    def heading(self, *a, **k):
        pass

    def column(self, *a, **k):
        pass

    def tag_configure(self, *a, **k):
        pass

    def set(self, *a, **k):
        pass


class _Tk(_Widget):
    def __init__(self, *a, **k):
        super().__init__()
        self._after = []

    def title(self, t):
        self._title = t

    def after(self, ms, cb=None):
        self._after.append(cb)
        return len(self._after)

    def mainloop(self):
        pass

    def rowconfigure(self, *a, **k):
        pass

    columnconfigure = rowconfigure


def install_fake_tk():
    existing = sys.modules.get("tkinter")
    if existing is not None:
        return existing  # 幂等：lockstep.ui 可能已绑定首次安装的桩模块
    tk = types.ModuleType("tkinter")
    tk.Tk = _Tk
    tk.Canvas = _Widget
    tk.Text = _Widget
    tk.StringVar = _Var
    tk.IntVar = _Var
    tk.BooleanVar = _Var
    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Button", "Radiobutton", "Checkbutton",
                 "Entry", "Treeview", "Scrollbar"):
        setattr(ttk, name, _Widget)
    fd = types.ModuleType("tkinter.filedialog")
    fd.asksaveasfilename = lambda **k: ""
    fd.askopenfilename = lambda **k: ""
    mb = types.ModuleType("tkinter.messagebox")
    mb.showerror = lambda *a, **k: None
    mb.showinfo = lambda *a, **k: None
    tk.ttk = ttk
    tk.filedialog = fd
    tk.messagebox = mb
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = fd
    sys.modules["tkinter.messagebox"] = mb
    return tk


class TestWindowSmoke(unittest.TestCase):
    def test_full_interaction_chain(self):
        install_fake_tk()
        from lockstep.session import LockstepSession
        from lockstep.transport import ManualClock
        from lockstep.ui import CELL, LockstepWindow

        clock = ManualClock()
        session = LockstepSession(seed=9, latency_ms=0, clock=clock)
        win = LockstepWindow(session)

        # 选择端A单位2（初始位于 (2,7)）
        win.mode.set("select")
        win.control.set(0)
        win._on_click(0, SimpleNamespace(x=2 * CELL + 1, y=7 * CELL + 1))
        self.assertEqual(win.selected_uid, 2)

        # 移动指令经内核受理
        win.mode.set("move")
        win._on_click(0, SimpleNamespace(x=6 * CELL + 1, y=7 * CELL + 1))
        self.assertTrue(any("受理" in m for m in session.log))

        # 非法指令被内核驳回并反馈
        win._on_click(0, SimpleNamespace(x=12 * CELL + 1, y=0 * CELL + 1))
        self.assertTrue(any("被拒" in m for m in session.log))

        # 单步 + 主循环推进
        for _ in range(12):
            clock.advance(0.2)
            win._on_step()
            win._loop()
        self.assertGreater(session.min_tick(), 0)

        # 集火（协作动作）
        win.mode.set("focus")
        win._on_click(0, SimpleNamespace(x=14 * CELL + 1, y=7 * CELL + 1))
        self.assertTrue(any("集火" in m or "受理" in m for m in session.log))

        # 延迟设置
        win.latency_var.set("150")
        win._on_latency()
        self.assertAlmostEqual(session.links[0].latency, 0.15)
        win.latency_var.set("abc")
        win._on_latency()  # 不崩溃，日志提示

        # 检查结果表已累积逐帧比对行
        self.assertGreater(len(session.checks()), 0)

        # 注入分歧 → 主循环检出并停等
        win._on_tamper()
        for _ in range(12):
            clock.advance(0.2)
            session.set_latency(0)
            win._on_step()
            win._loop()
        self.assertIsNotNone(session.divergence())
        self.assertTrue(win._div_shown)
        self.assertTrue(session.halted)

    def test_save_and_replay_via_window(self):
        tk = install_fake_tk()
        from lockstep.session import LockstepSession
        from lockstep.transport import ManualClock
        from lockstep.ui import LockstepWindow
        import tempfile, os

        clock = ManualClock()
        session = LockstepSession(seed=11, latency_ms=0, clock=clock)
        win = LockstepWindow(session)
        for _ in range(10):
            clock.advance(0.2)
            win._on_step()
        path = os.path.join(tempfile.mkdtemp(), "ui_rec.json")
        tk.filedialog.asksaveasfilename = lambda **k: path
        win._on_save()
        self.assertTrue(os.path.exists(path))
        tk.filedialog.askopenfilename = lambda **k: path
        win._on_replay()  # 回放校验不抛异常即通过


if __name__ == "__main__":
    unittest.main()
