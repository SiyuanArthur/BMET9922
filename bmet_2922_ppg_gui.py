#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMET2922/9922 Wearable PPG GUI (Tkinter + Matplotlib)
- 单窗口：BPM（柱状+趋势）与 PULSE 波形（按钮切换）
- 高/低阈值报警灯：低于低阈值 → Low 红/High 灰；高于高阈值 → High 红/Low 灰；正常 → 两灯均灰
- 日志时间戳格式：'Thu Sep 19 17:46:50 2024: ...'；低阈值日志关键字为 'Pulse Low'（L 大写）
- 5 秒无包看门狗：右侧 Remote 灯变红 + 图上遮罩 + 日志提示
- 后端数据三选一：
    1) 串口（Bluetooth SPP 或 USB）：NDJSON 或纯数值 BPM 行
    2) 外部命令（stdout 行流）：例如 cat 某串口
    3) 内置模拟（若1/2都未配置）
- 一键演示：Simulate Drop (5s) 人为屏蔽 6s 数据，必触发“5s 无包”
"""

import os
import sys
import csv
import math
import time
import json
import shlex
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from collections import deque
from typing import Optional

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ============ 可配置区域 ============
# 串口设备名（推荐：蓝牙 SPP 配对后出现在 /dev/tty.*）
# 示例：'/dev/tty.ESP32_PPG-SerialPort' 或 '/dev/tty.usbserial-0001'
SERIAL_PORT: Optional[str] = None
SERIAL_BAUD: int = 115200

# 外部命令（可选）：例如把串口当命令读： "cat /dev/tty.ESP32_PPG-SerialPort"
BACKEND_CMD: Optional[str] = None

# 显示与逻辑参数
HISTORY_SECONDS = 15
UI_UPDATE_MS = 200
BAR_BIN_SEC = 1.0
DEFAULT_LOW = 40.0
DEFAULT_HIGH = 90.0

# 依赖串口的库（可选）
try:
    import serial  # type: ignore
except Exception:
    serial = None


# ========= 小部件：有色圆点指示 + 文本 =========
class AlarmDot(ttk.Frame):
    def __init__(self, master, text, color="#222"):
        super().__init__(master)
        self.c = tk.Canvas(self, width=16, height=16, highlightthickness=0, bg=self._bg(master))
        self.oval = self.c.create_oval(2, 2, 14, 14, fill=color, outline="")
        self.c.pack(side="left", padx=(0, 6))
        ttk.Label(self, text=text).pack(side="left")

    def set_color(self, color: str):
        self.c.itemconfig(self.oval, fill=color)

    @staticmethod
    def _bg(w):
        try:
            return w.cget("background")
        except Exception:
            return "#f0f0f0"


# ========= 串口读取线程：逐行入队 ('raw', line) =========
class SerialReader(threading.Thread):
    def __init__(self, port: str, baudrate: int, q: queue.Queue):
        super().__init__(daemon=True)
        if serial is None:
            raise RuntimeError("pyserial 未安装；执行 `pip install pyserial` 开启串口读取。")
        self.ser = serial.Serial(port, baudrate, timeout=1)
        self.q = q
        self.running = True

    def run(self):
        while self.running:
            try:
                line = self.ser.readline()
                if not line:
                    continue
                text = line.decode('ascii', errors='ignore').strip()
                if text:
                    self.q.put(("raw", text))
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass


# ========= 外部命令后端 / 内置模拟（统一往队列投消息）=========
class BackendThread(threading.Thread):
    def __init__(self, q: queue.Queue, cmd: Optional[str] = None):
        super().__init__(daemon=True)
        self.q = q
        self.cmd = cmd
        self.stop_flag = False
        self.seq = 0

    def run(self):
        if self.cmd:
            # 外部命令模式：逐行读 stdout，交给主线程解析
            try:
                p = subprocess.Popen(
                    shlex.split(self.cmd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self.q.put(("log", f"[BACKEND] started: {self.cmd}"))
            except Exception as e:
                self.q.put(("log", f"[BACKEND] start failed: {e}"))
                return

            assert p.stdout is not None
            for line in p.stdout:
                if self.stop_flag:
                    break
                line = (line or "").strip()
                if not line:
                    continue
                self.q.put(("raw", line))
            self.q.put(("log", "[BACKEND] finished"))
            return

        # 模拟模式：20Hz 简包 + 每秒 1 个“完整包”含 50 个样本
        self.q.put(("log", "[SIM] running mock BPM & packets"))
        t0 = time.time()
        last_pkt = 0.0
        SAMPLE_HZ = 20.0

        while not self.stop_flag:
            t = time.time() - t0
            bpm = 71.0 + 3.5 * math.sin(2 * math.pi * 0.10 * t) + 1.0 * math.sin(2 * math.pi * 0.04 * t)
            self.q.put(("bpm", (time.time(), bpm)))

            if time.time() - last_pkt >= 1.0:
                last_pkt = time.time()
                x = np.arange(50, dtype=float) / 50.0
                samples = (1000 * np.sin(2 * math.pi * 1.2 * x) + 60 * np.random.randn(50)).astype(int).tolist()
                self.q.put(("pkt", (time.time(), bpm, samples, self.seq, 0, int((time.time() - t0) * 1000))))
                self.seq += 1

            time.sleep(1.0 / SAMPLE_HZ)


# ========= 主应用 =========
class App:
    def __init__(self, root: tk.Tk, backend_cmd: Optional[str] = None):
        self.root = root
        self._closing = False

        self.root.title("BMET2922 Pulse GUI")
        self.root.geometry("1080x680")

        outer = ttk.Frame(root); outer.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(outer, text="Wearable PPG — Host GUI", font=("Segoe UI", 14, "bold")).pack(anchor="w")

        mid = ttk.Frame(outer); mid.pack(fill="both", expand=True, pady=(8, 6))
        left = ttk.Frame(mid); left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(mid, width=280); right.pack(side="left", fill="y", padx=(12, 0))

        # 图形
        self.fig = Figure(figsize=(6, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("BPM"); self.ax.set_xlabel("Time (s)"); self.ax.set_ylabel("BPM")
        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # 数值卡片
        cards = ttk.Frame(left); cards.pack(fill="x", pady=(6, 0))
        self.mean_var = tk.StringVar(value="--.-"); self.curr_var = tk.StringVar(value="--.-")
        def card(parent, title, var):
            f = ttk.Frame(parent)
            ttk.Label(f, text=title).pack(side="left", padx=(0, 6))
            ttk.Label(f, textvariable=var, font=("Segoe UI", 14, "bold")).pack(side="left")
            return f
        card(cards, "mean", self.mean_var).pack(side="left", padx=(0, 20))
        card(cards, "Current", self.curr_var).pack(side="left")

        # 右侧：报警灯
        ttk.Label(right, text="ALARM", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))
        self.dot_high = AlarmDot(right, "High Pulse"); self.dot_high.pack(anchor="w", pady=2)
        self.dot_low  = AlarmDot(right, "Low Pulse");  self.dot_low.pack(anchor="w", pady=2)
        self.dot_remote = AlarmDot(right, "Remote", "#1d8f2b"); self.dot_remote.pack(anchor="w", pady=2)

        # 模式 & 按钮
        self.mode = tk.StringVar(value="BPM")
        btns = ttk.Frame(right); btns.pack(fill="x", pady=(8, 4))
        ttk.Button(btns, text="Info",  command=lambda: messagebox.showinfo("Info", "BMET2922 Pulse GUI")).pack(side="left", padx=2)
        ttk.Button(btns, text="BPM",   command=lambda: self.mode.set("BPM")).pack(side="left", padx=2)
        ttk.Button(btns, text="PULSE", command=lambda: self.mode.set("PULSE")).pack(side="left", padx=2)
        ttk.Button(btns, text="Simulate Drop (5s)", command=lambda: self._simulate_drop(6)).pack(side="left", padx=2)
        ttk.Button(btns, text="EXIT",  command=lambda: self.root.destroy()).pack(side="left", padx=2)

        # 阈值滑条
        thr = ttk.Frame(right); thr.pack(fill="x", pady=(8, 4))
        ttk.Label(thr, text="Low").grid(row=0, column=0, sticky="w")
        ttk.Label(thr, text="High").grid(row=1, column=0, sticky="w")
        self.low_var = tk.DoubleVar(value=DEFAULT_LOW)
        self.high_var = tk.DoubleVar(value=DEFAULT_HIGH)
        s1 = ttk.Scale(thr, from_=30, to=100, variable=self.low_var, orient="horizontal"); s1.grid(row=0, column=1, sticky="ew", padx=6)
        s2 = ttk.Scale(thr, from_=60, to=150, variable=self.high_var, orient="horizontal"); s2.grid(row=1, column=1, sticky="ew", padx=6)
        thr.columnconfigure(1, weight=1)
        ttk.Label(thr, textvariable=self.low_var, width=6).grid(row=0, column=2, sticky="e")
        ttk.Label(thr, textvariable=self.high_var, width=6).grid(row=1, column=2, sticky="e")

        # 日志
        ttk.Label(outer, text="Log").pack(anchor="w")
        self.log = tk.Text(outer, height=8); self.log.pack(fill="both", expand=False)
        self._log("GUI started")

        # 缓冲与后端
        self.ts, self.bpm = deque(), deque()
        self.q = queue.Queue()
        self.backend = BackendThread(self.q, backend_cmd)
        self.backend.start()

        # 串口（可选）
        self.serial_thread = None
        if SERIAL_PORT is not None:
            if serial is None:
                self._log(f"[Serial] pyserial not installed; cannot open {SERIAL_PORT}")
            else:
                try:
                    self.serial_thread = SerialReader(SERIAL_PORT, SERIAL_BAUD, self.q)
                    self.serial_thread.start()
                    self._log(f"[Serial] opened {SERIAL_PORT} @ {SERIAL_BAUD}")
                except Exception as e:
                    self._log(f"[Serial] open failed: {e}")

        # 看门狗 & 波形缓冲
        self.last_pkt_ts = 0.0
        self.miss_alarm_on = False
        self.wave_t, self.wave_y = deque(), deque()
        self._drop_until = 0.0  # 断流演示闸门

        # 定时任务
        self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)
        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # 日志：精确到秒的要求格式
    def _log(self, s: str):
        ts = time.strftime("%a %b %d %H:%M:%S %Y")
        self.log.insert("end", f"{ts}: {s}\n")
        self.log.see("end")

    def _log_once(self, s: str):
        if getattr(self, "_last_alarm", None) != s:
            self._last_alarm = s
            self._log(s)

    # 断流演示：N 秒内忽略入口数据
    def _simulate_drop(self, seconds=6):
        self._drop_until = time.time() + float(seconds)
        self._log(f"[Demo] Simulating no packets for {int(seconds)} s")

    # 队列轮询：收集 bpm 与包
    def _poll_queue(self):
        if self._closing:
            return
        try:
            while True:
                typ, payload = self.q.get_nowait()

                if typ == "log":
                    self._log(payload)

                elif typ == "raw":
                    # 解析一行文本：NDJSON 或 72.4
                    line = (payload or "").strip()
                    try:
                        if line.startswith("{"):
                            if time.time() < self._drop_until:
                                continue
                            obj = json.loads(line)
                            bpm = float(obj["bpm"])
                            if "samples" in obj:
                                samples = obj["samples"]
                                t_host = time.time()
                                self.last_pkt_ts = t_host
                                self.ts.append(t_host); self.bpm.append(bpm)
                                base = t_host - 1.0
                                for i, y in enumerate(samples):
                                    self.wave_t.append(base + i/50.0)
                                    self.wave_y.append(y)
                            else:
                                t = time.time()
                                self.last_pkt_ts = t
                                self.ts.append(t); self.bpm.append(bpm)
                        else:
                            if time.time() < self._drop_until:
                                continue
                            bpm = float(line.split(",")[0])
                            t = time.time()
                            self.last_pkt_ts = t
                            self.ts.append(t); self.bpm.append(bpm)
                    except Exception:
                        self._log(f"[PARSE] {line[:120]}")
                    self._trim_buffers()

                elif typ == "bpm":
                    if time.time() < self._drop_until:
                        continue
                    t, v = payload
                    self.last_pkt_ts = t
                    self.ts.append(t); self.bpm.append(v)
                    self._trim_buffers()

                elif typ == "pkt":
                    if time.time() < self._drop_until:
                        continue
                    t_host, bpm, samples, seq, flags, t_mcu = payload
                    self.last_pkt_ts = t_host
                    self.ts.append(t_host); self.bpm.append(bpm)
                    base = t_host - 1.0
                    for i, y in enumerate(samples):
                        self.wave_t.append(base + i / 50.0)
                        self.wave_y.append(y)
                    self._trim_buffers()

        except queue.Empty:
            pass
        finally:
            self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)

    def _trim_buffers(self):
        cutoff = time.time() - HISTORY_SECONDS
        while self.ts and self.ts[0] < cutoff:
            self.ts.popleft(); self.bpm.popleft()
        while self.wave_t and self.wave_t[0] < cutoff:
            self.wave_t.popleft(); self.wave_y.popleft()

    # 定时重绘
    def _redraw(self):
        if self._closing:
            return

        # 5s 看门狗
        now = time.time()
        if self.last_pkt_ts and (now - self.last_pkt_ts > 5.0):
            if not self.miss_alarm_on:
                self.miss_alarm_on = True
                self._log("No packet for > 5 s")
            self.dot_remote.set_color("#cc2121")
        else:
            if self.miss_alarm_on and self.last_pkt_ts:
                self._log("Packet stream resumed")
            self.miss_alarm_on = False
            self.dot_remote.set_color("#1d8f2b")

        mode = self.mode.get()

        if mode == "PULSE":
            if len(self.wave_t) > 2:
                t0 = self.wave_t[-1]
                t_rel = np.array(self.wave_t, dtype=float) - t0
                y = np.array(self.wave_y, dtype=float)

                self.ax.cla()
                self.ax.set_title("Pulse waveform")
                self.ax.set_xlabel("Time (s)")
                self.ax.set_ylabel("ADC")
                self.ax.plot(t_rel, y, linewidth=1.0)
                self.ax.set_xlim(-HISTORY_SECONDS, 0)

                if self.miss_alarm_on:
                    self.ax.text(0.02, 0.95, "NO PACKET > 5 s",
                                 transform=self.ax.transAxes,
                                 bbox=dict(facecolor='red', alpha=.3))
                self.canvas.draw()

            self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)
            return

        # BPM 视图
        if len(self.ts) > 2:
            t0 = self.ts[-1]
            t_rel = np.array(self.ts, dtype=float) - t0
            bpm_arr = np.array(self.bpm, dtype=float)

            bins = np.arange(-HISTORY_SECONDS, 0 + 1e-9, BAR_BIN_SEC)
            bar_vals, centers = [], []
            for i in range(len(bins) - 1):
                mask = (t_rel >= bins[i]) & (t_rel < bins[i + 1])
                bar_vals.append(float(np.mean(bpm_arr[mask])) if np.any(mask) else np.nan)
                centers.append((bins[i] + bins[i + 1]) / 2)

            self.ax.cla()
            self.ax.set_title("BPM")
            self.ax.set_xlabel("Time (s)"); self.ax.set_ylabel("BPM")
            self.ax.bar(centers, bar_vals, width=0.8, align="center")
            self.ax.plot(t_rel, bpm_arr, linewidth=2.0)
            self.ax.set_xlim(-HISTORY_SECONDS, 0)

            mean_v = float(np.nanmean(bar_vals)) if np.any(~np.isnan(bar_vals)) else np.nan
            curr_v = float(bpm_arr[-1])
            self.mean_var.set(f"{mean_v:.1f}" if not np.isnan(mean_v) else "--.-")
            self.curr_var.set(f"{curr_v:.1f}")

            # 阈值报警（按要求：正常→两灯灰；低→Low红/High灰；高→High红/Low灰）
            low = float(self.low_var.get()); high = float(self.high_var.get())
            if curr_v < low:
                self.dot_low.set_color("#cc2121")
                self.dot_high.set_color("#222")
                self._log_once("Pulse Low")
            elif curr_v > high:
                self.dot_high.set_color("#cc2121")
                self.dot_low.set_color("#222")
                self._log_once("Pulse High")
            else:
                self.dot_high.set_color("#222")
                self.dot_low.set_color("#222")

            if self.miss_alarm_on:
                self.ax.text(0.02, 0.95, "NO PACKET > 5 s",
                             transform=self.ax.transAxes,
                             bbox=dict(facecolor='red', alpha=.3))
            self.canvas.draw()

        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)

    def on_close(self):
        self._closing = True
        try:
            if hasattr(self, "backend") and self.backend is not None:
                self.backend.stop_flag = True
        except Exception:
            pass
        try:
            if hasattr(self, "serial_thread") and self.serial_thread is not None:
                self.serial_thread.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_after_poll") and self._after_poll:
                self.root.after_cancel(self._after_poll)
                self._after_poll = None
            if hasattr(self, "_after_draw") and self._after_draw:
                self.root.after_cancel(self._after_draw)
                self._after_draw = None
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        self.root.destroy()


# ========= 入口 =========
def main():
    root = tk.Tk()
    try:
        style = ttk.Style(); style.theme_use("clam")
    except Exception:
        pass
    app = App(root, backend_cmd=BACKEND_CMD)
    root.mainloop()

if __name__ == "__main__":
    main()
