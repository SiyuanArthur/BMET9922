#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMET2922 - Pulse Monitor (Tkinter + Matplotlib)
- 左侧主图：BPM 柱状 + 趋势线，展示最近 15 s
- 右侧侧栏：ALARM 指示灯、按钮（Info/BPM/PULSE/EXIT）、阈值滑条
- 下方：Log 日志窗口
- 内置模拟数据；未来可替换为 C 后端（读取 stdout）对接
"""
import tkinter as tk
from tkinter import ttk, messagebox
import threading, time, math, queue, subprocess, shlex
from collections import deque
from typing import Optional
import numpy as np

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# --------------------------- 可调参数 ---------------------------
HISTORY_SECONDS = 15          # 主图显示最近 15 秒
SAMPLE_HZ = 20                # 模拟数据频率（后端替换时不影响）
UI_UPDATE_MS = 200
BAR_BIN_SEC = 1.0             # 1 秒一个柱
DEFAULT_LOW_BPM = 40
DEFAULT_HIGH_BPM = 90
# ---------------------------------------------------------------

class AlarmDot(ttk.Frame):
    """一个小圆点指示灯"""
    def __init__(self, master, text):
        super().__init__(master)
        self.canvas = tk.Canvas(self, width=16, height=16, highlightthickness=0, bg="white")
        self.canvas.grid(row=0, column=0, padx=(0,6))
        self.lbl = ttk.Label(self, text=text, width=10)
        self.lbl.grid(row=0, column=1)
        self.oval = self.canvas.create_oval(2,2,14,14, fill="#222", outline="#111")

    def set_color(self, color_hex):
        self.canvas.itemconfig(self.oval, fill=color_hex)

class BackendThread(threading.Thread):
    """可切换：模拟数据 或 运行外部可执行文件读取 stdout 行"""
    def __init__(self, q:queue.Queue, cmd:Optional[str]=None):
        super().__init__(daemon=True); self.q=q; self.cmd=cmd; self.stop_flag=False

    def run(self):
        if self.cmd:
            try:
                proc = subprocess.Popen(shlex.split(self.cmd), stdout=subprocess.PIPE,
                                        universal_newlines=True, bufsize=1)
            except Exception as e:
                self.q.put(("log", f"[ERR] start backend failed: {e}"))
                return
            self.q.put(("log", f"[OK] backend: {self.cmd}"))
            for line in proc.stdout:
                if self.stop_flag: break
                line=line.strip()
                # 允许两种格式：json {"bpm":72.3} 或 纯数字 72.3
                try:
                    if line.startswith("{"):
                        import json
                        bpm=float(json.loads(line)["bpm"])
                    else:
                        bpm=float(line.split(",")[0])
                    t=time.time()
                    self.q.put(("bpm", (t, bpm)))
                except Exception:
                    self.q.put(("log", f"[PARSE] {line[:80]}"))
        else:
            # 模拟数据
            t0=time.time(); self.q.put(("log","[SIM] running mock BPM"))
            while not self.stop_flag:
                t=time.time()-t0
                bpm=71.0 + 3.5*math.sin(2*math.pi*0.07*t) + 1.0*math.sin(2*math.pi*0.23*t)
                self.q.put(("bpm",(time.time(), bpm)))
                time.sleep(1.0/SAMPLE_HZ)

class App:
    def __init__(self, root, backend_cmd: Optional[str]=None):
        self.root = root
        root.title("BMET2922 - Pulse Monitor")
        root.geometry("1080x720")
        try: root.iconbitmap("")  # 可换图标；留空不报错
        except: pass

        # 整体布局：上标题，中间左右两列，下日志
        outer = ttk.Frame(root, padding=8); outer.pack(fill="both", expand=True)
        title = ttk.Label(outer, text="BMET2922 - Pulse Monitor", anchor="center",
                          font=("Arial", 18, "bold"))
        title.pack(fill="x", pady=(0,8))

        mid = ttk.Frame(outer); mid.pack(fill="both", expand=True)
        left = ttk.Frame(mid); left.pack(side="left", fill="both", expand=True, padx=(0,8))
        right = ttk.Frame(mid, width=260); right.pack(side="right", fill="y")

        # ---- 左侧：主图（柱状 + 趋势线） ----
        fig = Figure(figsize=(6,4), dpi=100)
        self.ax = fig.add_subplot(111)
        self.ax.set_title("BPM")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("BPM")
        self.canvas = FigureCanvasTkAgg(fig, master=left)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # mean / current 数字卡片（用 Tk 叠在画布下方）
        cards = ttk.Frame(left); cards.pack(fill="x", pady=(6,0))
        self.mean_var = tk.StringVar(value="--.-")
        self.curr_var = tk.StringVar(value="--.-")
        mean_box = ttk.Label(cards, textvariable=self.mean_var, width=8,
                             anchor="center", padding=6, font=("Arial",16,"bold"),
                             background="#e8f1ff", foreground="#1e5db3")
        curr_box = ttk.Label(cards, textvariable=self.curr_var, width=8,
                             anchor="center", padding=6, font=("Arial",16,"bold"),
                             background="#e8fff0", foreground="#1a8f4a")
        ttk.Label(cards, text="mean").pack(side="left", padx=(8,6))
        mean_box.pack(side="left")
        ttk.Label(cards, text="Current").pack(side="right", padx=(6,8))
        curr_box.pack(side="right")

        # ---- 右侧：ALARM + 按钮 + 滑条 ----
        alarms_title = ttk.Label(right, text="ALARM", font=("Arial",14,"bold"))
        alarms_title.pack(pady=(4,6))

        self.dot_high = AlarmDot(right, "High Pulse"); self.dot_high.pack(anchor="w", pady=2)
        self.dot_low  = AlarmDot(right, "Low Pulse");  self.dot_low.pack(anchor="w", pady=2)
        self.dot_remote = AlarmDot(right, "Remote");   self.dot_remote.pack(anchor="w", pady=2)
        self.dot_remote.set_color("#1d8f2b")  # 远程/本地随意演示

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=10)

        def btn(s, cmd): 
            b=ttk.Button(right, text=s, command=cmd); b.pack(fill="x", pady=4); return b
        btn("Info", lambda: messagebox.showinfo("Info","BMET2922 Pulse Monitor\nTkinter 版本演示"))
        self.mode = tk.StringVar(value="BPM")
        btn("BPM",  lambda: self.mode.set("BPM"))
        btn("PULSE",lambda: self.mode.set("PULSE"))  # 预留：切换到 PPG（可后续扩展）
        btn("EXIT", root.destroy)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=10)
        slider_frame = ttk.Frame(right); slider_frame.pack(fill="x", pady=4)
        ttk.Label(slider_frame, text="Low Pulse").grid(row=0, column=0, sticky="w")
        self.low_var = tk.DoubleVar(value=DEFAULT_LOW_BPM)
        self.high_var = tk.DoubleVar(value=DEFAULT_HIGH_BPM)
        low_slider = ttk.Scale(slider_frame, from_=30, to=100, variable=self.low_var)
        low_slider.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(slider_frame, textvariable=self.low_var, width=4).grid(row=0, column=2)
        ttk.Label(slider_frame, text="High Pulse").grid(row=1, column=0, sticky="w", pady=(6,0))
        high_slider = ttk.Scale(slider_frame, from_=60, to=150, variable=self.high_var)
        high_slider.grid(row=1, column=1, sticky="ew", padx=6, pady=(6,0))
        ttk.Label(slider_frame, textvariable=self.high_var, width=4).grid(row=1, column=2, pady=(6,0))
        slider_frame.columnconfigure(1, weight=1)

        # ---- 下方：Log ----
        log_box = ttk.Label(outer, text="Log", font=("Arial",12,"bold"))
        log_box.pack(anchor="w", pady=(8,0))
        self.log = tk.Text(outer, height=8)
        self.log.pack(fill="both", expand=False)
        self._log("GUI started")

        # ---- 数据缓冲 ----
        self.ts = deque()
        self.bpm = deque()

        # 后端线程（默认模拟）
        self.q = queue.Queue()
        self.backend = BackendThread(self.q, cmd=backend_cmd)
        self.backend.start()

        # 定时更新
        self.root.after(UI_UPDATE_MS, self._poll_queue)
        self.root.after(UI_UPDATE_MS, self._redraw)

    def _log(self, s: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log.insert("end", f"{ts}  {s}\n")
        self.log.see("end")

    def _poll_queue(self):
        drained = 0
        while True:
            try:
                typ, payload = self.q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if typ == "log":
                self._log(payload)
            elif typ == "bpm":
                t, v = payload
                self.ts.append(t)
                self.bpm.append(v)
                # 限定在最近 HISTORY_SECONDS
                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()
        self.root.after(UI_UPDATE_MS, self._poll_queue)

    def _redraw(self):
        # 生成柱状数据（每 1 秒一柱）
        if len(self.ts) > 2:
            # 转成相对时间
            t0 = self.ts[-1]
            t_rel = np.array(self.ts) - t0
            bpm_arr = np.array(self.bpm)

            # 分箱
            bins = np.arange(-HISTORY_SECONDS, 0+1e-6, BAR_BIN_SEC)
            # 为每个 bin 计算平均 BPM
            bar_vals = []
            centers = []
            for i in range(len(bins)-1):
                mask = (t_rel >= bins[i]) & (t_rel < bins[i+1])
                if np.any(mask):
                    bar_vals.append(float(np.mean(bpm_arr[mask])))
                else:
                    bar_vals.append(np.nan)
                centers.append((bins[i]+bins[i+1])/2)

            # 绘图
            self.ax.cla()
            self.ax.set_title("BPM")
            self.ax.set_xlabel("Time (s)")
            self.ax.set_ylabel("BPM")
            # 绿色柱
            self.ax.bar(centers, bar_vals, width=0.8, align="center")
            # 蓝色趋势线
            self.ax.plot(t_rel, bpm_arr, linewidth=2)
            self.ax.set_xlim(-HISTORY_SECONDS, 0)

            # mean / current
            mean_v = float(np.nanmean(bar_vals)) if np.any(~np.isnan(bar_vals)) else np.nan
            curr_v = float(bpm_arr[-1])
            self.mean_var.set(f"{mean_v:.1f}" if not np.isnan(mean_v) else "--.-")
            self.curr_var.set(f"{curr_v:.1f}")

            # 报警指示灯
            low = float(self.low_var.get()); high = float(self.high_var.get())
            if curr_v < low:
                self.dot_low.set_color("#1d8f2b")     # 绿灯亮
                self.dot_high.set_color("#222")
                self._log_once("Pulse LOW")
            elif curr_v > high:
                self.dot_high.set_color("#cc2121")    # 红灯亮
                self.dot_low.set_color("#222")
                self._log_once("Pulse HIGH")
            else:
                self.dot_high.set_color("#222")
                self.dot_low.set_color("#1d8f2b")
            self.canvas.draw()
        self.root.after(UI_UPDATE_MS, self._redraw)

    # 简单“去抖”：避免每次都刷日志
    _last_alarm = ""
    def _log_once(self, msg: str):
        if msg != self._last_alarm:
            self._last_alarm = msg
            self._log(msg)

def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except: pass
    app = App(root, backend_cmd=None)
    root.mainloop()

if __name__ == "__main__":
    main()
