"""联机场景窗口（tkinter）。

布局：
  顶部状态栏：帧号、双端状态（含等待来源）、同步结论；
  中部：两端阵形画布（点击下达移动/集火指令）；
  控制条：控制端、指令模式、单步、自动、延迟、保存脚本、回放校验、注入分歧；
  底部：逐帧摘要比对表 + 单位动作日志。

窗口只是锁步内核的视图与入口：指令经内核校验受理（拒绝会反馈到日志），
推进由内核驱动，分歧由内核检出并停等——界面不绕开锁步做任何状态修补。
"""

import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .commands import make_focus, make_move
from .world import MAX_HP

CELL = 30
SQUAD_COLORS = ("#3f8cff", "#ff5546")
OBSTACLE_COLOR = "#4a4a4a"
GRID_COLOR = "#2c2c2c"
BG_COLOR = "#1a1a1a"


class LockstepWindow:
    def __init__(self, driver):
        self.driver = driver
        self.root = tk.Tk()
        self.root.title(driver.title)

        self.selected_uid = None
        self.mode = tk.StringVar(value="select")
        initial = driver.local_player if driver.local_player is not None else 0
        self.control = tk.IntVar(value=initial)
        self.auto = tk.BooleanVar(value=False)

        self._last = time.monotonic()
        self._checks_shown = 0
        self._div_shown = False

        self._build()
        self.root.after(16, self._loop)

    # ---------------- 界面构建 ----------------

    def _build(self):
        world = self.driver.worlds()[0]
        w, h = world.width * CELL, world.height * CELL

        status = ttk.Frame(self.root)
        status.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        self.tick_lbl = ttk.Label(status, text="", width=28)
        self.tick_lbl.grid(row=0, column=0, sticky="w")
        self.end_lbls = []
        for i in range(2):
            lbl = ttk.Label(status, text="", width=34)
            lbl.grid(row=0, column=1 + i, sticky="w", padx=(12, 0))
            self.end_lbls.append(lbl)
        self.sync_lbl = ttk.Label(status, text="", width=30)
        self.sync_lbl.grid(row=0, column=3, sticky="w", padx=(12, 0))

        boards = ttk.Frame(self.root)
        boards.grid(row=1, column=0, padx=6)
        self.canvases = []
        for i, label in enumerate(self.driver.view_labels):
            frame = ttk.Frame(boards)
            frame.grid(row=0, column=i, padx=4)
            ttk.Label(frame, text=label).grid(row=0, column=0)
            cv = tk.Canvas(frame, width=w, height=h, bg=BG_COLOR,
                           highlightthickness=1, highlightbackground="#666")
            cv.grid(row=1, column=0)
            cv.bind("<Button-1>", lambda ev, idx=i: self._on_click(idx, ev))
            self.canvases.append(cv)

        ctrl = ttk.Frame(self.root)
        ctrl.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        ttk.Label(ctrl, text="控制端:").grid(row=0, column=0)
        for i, name in enumerate(("端A", "端B")):
            rb = ttk.Radiobutton(ctrl, text=name, value=i, variable=self.control)
            rb.grid(row=0, column=1 + i)
            if self.driver.local_player is not None:
                rb.state(["disabled"])
        ttk.Label(ctrl, text="  模式:").grid(row=0, column=3)
        for j, (val, name) in enumerate((("select", "选择"), ("move", "移动"),
                                         ("focus", "集火"))):
            ttk.Radiobutton(ctrl, text=name, value=val,
                            variable=self.mode).grid(row=0, column=4 + j)
        ttk.Button(ctrl, text="单步", command=self._on_step).grid(
            row=0, column=7, padx=(12, 2))
        ttk.Checkbutton(ctrl, text="自动运行",
                        variable=self.auto).grid(row=0, column=8)
        ttk.Label(ctrl, text="  延迟(ms):").grid(row=0, column=9)
        self.latency_var = tk.StringVar(value="")
        ent = ttk.Entry(ctrl, textvariable=self.latency_var, width=6)
        ent.grid(row=0, column=10)
        if not self.driver.latency_editable:
            ent.state(["disabled"])
        ttk.Button(ctrl, text="设定", command=self._on_latency).grid(
            row=0, column=11)
        ttk.Button(ctrl, text="保存脚本", command=self._on_save).grid(
            row=0, column=12, padx=(12, 2))
        ttk.Button(ctrl, text="回放校验", command=self._on_replay).grid(
            row=0, column=13)
        ttk.Button(ctrl, text="注入分歧(测试)", command=self._on_tamper).grid(
            row=0, column=14, padx=(12, 2))

        bottom = ttk.Frame(self.root)
        bottom.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        ttk.Label(bottom, text="检查结果（逐帧状态摘要比对）").grid(
            row=0, column=0, sticky="w")
        ttk.Label(bottom, text="单位动作 / 内核反馈").grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        cols = ("tick", "result", "digest_a", "digest_b")
        self.tree = ttk.Treeview(bottom, columns=cols, show="headings", height=9)
        for col, name, width in (("tick", "帧", 60), ("result", "结果", 70),
                                 ("digest_a", "端A摘要", 150),
                                 ("digest_b", "端B摘要", 150)):
            self.tree.heading(col, text=name)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("bad", foreground="#ff4444")
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(bottom, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=0, sticky="nse")

        self.log_text = tk.Text(bottom, width=64, height=10, state="disabled",
                                bg="#111", fg="#ddd")
        self.log_text.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        sb2 = ttk.Scrollbar(bottom, orient="vertical",
                            command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb2.set)
        sb2.grid(row=1, column=1, sticky="nse")
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(1, weight=1)

    # ---------------- 交互 ----------------

    def _on_click(self, end_idx, event):
        x, y = event.x // CELL, event.y // CELL
        world = self.driver.worlds()[end_idx]
        if not world.in_bounds(x, y):
            return
        player = self.control.get()
        mode = self.mode.get()
        if mode == "select":
            u = world.unit_at(x, y)
            self.selected_uid = u.uid if (u and u.squad == player) else None
        elif mode == "move":
            if self.selected_uid is None:
                self._append_log("[操控] 请先在“选择”模式下点选本方单位")
                return
            self.driver.issue(player, make_move(self.selected_uid, x, y))
        elif mode == "focus":
            self.driver.issue(player, make_focus(x, y))

    def _on_step(self):
        self.auto.set(False)
        self.driver.step_once()

    def _on_latency(self):
        try:
            ms = max(0, int(self.latency_var.get()))
        except ValueError:
            self._append_log("[操控] 延迟需为非负整数毫秒")
            return
        self.driver.set_latency(ms)
        self._append_log(f"[操控] 链路延迟已设为 {ms}ms")

    def _on_save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("锁步输入脚本", "*.json")])
        if not path:
            return
        self.driver.save_recording(path)
        self._append_log(f"[操控] 输入脚本已保存：{path}")

    def _on_replay(self):
        path = filedialog.askopenfilename(
            filetypes=[("锁步输入脚本", "*.json")])
        if not path:
            return
        from .replay import compare_configurations, load_recording, verify
        rec = load_recording(path)
        ok, _t, msg = verify(rec)
        ok2, msg2 = compare_configurations(
            rec, [(0, 1 / 60), (300, 1 / 20), (120, 1 / 30)])
        self._append_log(f"[回放] {msg}")
        self._append_log(f"[回放] {msg2}")
        messagebox.showinfo("回放校验", f"{msg}\n{msg2}",
                            icon="info" if (ok and ok2) else "error")

    def _on_tamper(self):
        self._append_log(f"[测试] {self.driver.tamper()}")

    # ---------------- 主循环 ----------------

    def _loop(self):
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if self.auto.get() and not self.driver.halted:
            self.driver.update(dt)
        self._redraw()
        self._refresh_status()
        for line in self.driver.drain_log():
            self._append_log(line)
        self._refresh_checks()
        self._check_divergence()
        self.root.after(16, self._loop)

    def _redraw(self):
        for idx, canvas in enumerate(self.canvases):
            self._draw_world(canvas, self.driver.worlds()[idx])

    def _draw_world(self, canvas, world):
        canvas.delete("all")
        for gx in range(world.width + 1):
            canvas.create_line(gx * CELL, 0, gx * CELL, world.height * CELL,
                               fill=GRID_COLOR)
        for gy in range(world.height + 1):
            canvas.create_line(0, gy * CELL, world.width * CELL, gy * CELL,
                               fill=GRID_COLOR)
        for (ox, oy) in world.obstacles:
            canvas.create_rectangle(ox * CELL, oy * CELL,
                                    (ox + 1) * CELL, (oy + 1) * CELL,
                                    fill=OBSTACLE_COLOR, outline="")
        for u in world.iter_units():
            cx, cy = u.x * CELL + CELL // 2, u.y * CELL + CELL // 2
            r = CELL * 0.38
            if u.target is not None and u.target in world.units:
                tgt = world.units[u.target]
                canvas.create_line(cx, cy, tgt.x * CELL + CELL // 2,
                                   tgt.y * CELL + CELL // 2,
                                   fill="#aa3333", dash=(3, 3))
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                               fill=SQUAD_COLORS[u.squad], outline="")
            hpw = CELL * 0.8 * max(0, u.hp) / MAX_HP
            canvas.create_rectangle(cx - CELL * 0.4, cy - r - 7,
                                    cx - CELL * 0.4 + hpw, cy - r - 4,
                                    fill="#44cc44", outline="")
            canvas.create_text(cx, cy, text=str(u.uid), fill="white",
                               font=("TkDefaultFont", 8, "bold"))
            if u.uid == self.selected_uid:
                canvas.create_rectangle(u.x * CELL + 1, u.y * CELL + 1,
                                        (u.x + 1) * CELL - 1,
                                        (u.y + 1) * CELL - 1,
                                        outline="#ffdd33", width=2)
                for (px, py) in u.path:
                    canvas.create_oval(px * CELL + CELL // 2 - 2,
                                       py * CELL + CELL // 2 - 2,
                                       px * CELL + CELL // 2 + 2,
                                       py * CELL + CELL // 2 + 2,
                                       fill="#ffdd33", outline="")

    def _refresh_status(self):
        self.tick_lbl.config(text=self.driver.tick_info())
        for lbl, text in zip(self.end_lbls, self.driver.statuses()):
            lbl.config(text=text)
        div = self.driver.divergence()
        if div is not None:
            self.sync_lbl.config(text=f"同步：分歧@帧{div['tick']}")
            return
        checks = self.driver.checks()
        bad = sum(1 for _t, ok, _a, _b in checks if not ok)
        waiting = any("等待" in s for s in self.driver.statuses())
        if bad:
            self.sync_lbl.config(text=f"同步：{bad} 帧不一致")
        elif waiting:
            self.sync_lbl.config(text="同步：等待对端输入")
        else:
            self.sync_lbl.config(text=f"同步：逐帧一致（{len(checks)} 帧）")

    def _refresh_checks(self):
        checks = self.driver.checks()
        for t, ok, da, db in checks[self._checks_shown:]:
            self.tree.insert(
                "", "end",
                values=(t, "一致" if ok else "分歧", da[:16], db[:16]),
                tags=() if ok else ("bad",))
        if len(checks) > self._checks_shown:
            self._checks_shown = len(checks)
            children = self.tree.get_children()
            if children:
                self.tree.see(children[-1])

    def _check_divergence(self):
        div = self.driver.divergence()
        if div is None or self._div_shown:
            return
        self._div_shown = True
        self.auto.set(False)
        self._append_log(
            f"[锁步] 首次分歧@帧{div['tick']} 单位{div['first_uid']}：{div['text']}")
        messagebox.showerror(
            "锁步分歧",
            f"双端在第 {div['tick']} 帧首次分歧，会话已停等。\n"
            f"首个不一致单位：{div['first_uid']}\n{div['text']}\n"
            f"端A摘要 {str(div['digest_a'])[:16]} / 端B摘要 {str(div['digest_b'])[:16]}")

    def _append_log(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run(self):
        self.root.mainloop()
