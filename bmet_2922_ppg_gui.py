#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMET2922/9922 Wearable PPG GUI (Tkinter + Matplotlib + Serial)
- 后端线程: 串口读取 MCU JSON 行，解析后放入队列
- 前端: 1Hz+ 刷新，显示 BPM 文本 + PPG 波形；5s 无包触发告警
"""

import json, time, threading, queue, sys
from dataclasses import dataclass
from typing import Optional
import tkinter as tk
from tkinter import ttk
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# 串口
import serial
import serial.tools.list_ports

# ============ 配置 ============
BAUD = 115200
COM_PORT: Optional[str] = None  # 设成 "COM5" 或留 None 让程序自动找
WATCHDOG_SEC = 5.0              # 5秒未收到包 -> 告警
TARGET_FPS = 20                 # 前端绘图刷新上限
PPG_BUFFER_SEC = 8              # 显示最近8秒波形

# ============ 数据结构 ============
@dataclass
class Sample:
    ts_ms: int
    bpm: float
    ppg: int

# ============ 串口后台线程 ============
class SerialReader(threading.Thread):
    def __init__(self, q: queue.Queue, port: Optional[str], baud: int):
        super().__init__(daemon=True)
        self.q = q
        self.port = port
        self.baud = baud
        self.stop_flag = False
        self.ser = None

    def _auto_pick_port(self) -> Optional[str]:
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return None
        # 简单策略：挑第一个含 Arduino/USB 的
        for p in ports:
            name = (p.description or "") + " " + (p.device or "")
            if "Arduino" in name or "USB" in name or "ACM" in name or "COM" in name:
                return p.device
        return ports[0].device

    def run(self):
        try:
            port = self.port or self._auto_pick_port()
            if not port:
                self.q.put(("log", f"[ERROR] 未找到可用串口，请手动设置 COM_PORT"))
                return
            self.q.put(("log", f"[INFO] 尝试连接串口: {port} @ {self.baud}"))
            self.ser = serial.Serial(port=port, baudrate=self.baud, timeout=1.0)
            self.q.put(("log", f"[OK] 串口已连接"))

            while not self.stop_flag:
                line = self.ser.readline()  # 按行读取
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8").strip())
                    ts = int(obj.get("ts", int(time.time() * 1000)))
                    bpm = float(obj.get("bpm", 0.0))
                    ppg = int(obj.get("ppg", 0))
                    self.q.put(("data", Sample(ts, bpm, ppg)))
                except Exception as e:
                    # 容错：也许是 CSV 行；可加备选解析
                    s = line.decode("utf-8", errors="ignore").strip()
                    self.q.put(("log", f"[WARN] 解析失败: {s} ({e})"))
        except Exception as e:
            self.q.put(("log", f"[ERROR] 串口线程异常: {e}"))
        finally:
            if self.ser:
                try:
                    self.ser.close()
                except:
                    pass
            self.q.put(("log", f"[INFO] 串口线程退出"))

    def stop(self):
        self.stop_flag = True

# ============ GUI ============
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Wearable PPG GUI (Serial)")
        self.geometry("920x600")

        self.q = queue.Queue()
        self.reader = SerialReader(self.q, COM_PORT, BAUD)
        self.last_packet_time = 0.0
        self.latest_bpm = 0.0

        # 上部：BPM + 告警
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        self.bpm_var = tk.StringVar(value="BPM: --.-")
        self.bpm_label = ttk.Label(top, textvariable=self.bpm_var, font=("Segoe UI", 28, "bold"))
        self.bpm_label.pack(side="left")

        self.alarm_var = tk.StringVar(value="OK")
        self.alarm_label = ttk.Label(top, textvariable=self.alarm_var, font=("Segoe UI", 16))
        self.alarm_label.pack(side="left", padx=20)

        # 中部：Matplotlib 图（PPG）
        fig = plt.Figure(figsize=(8, 3), dpi=100)
        self.ax = fig.add_subplot(111)
        self.ax.set_title("Pulse Waveform (PPG)")
        self.ax.set_xlabel("Time (s, recent)")
        self.ax.set_ylabel("PPG (a.u.)")
        self.line, = self.ax.plot([], [], lw=1.2)
        self.ax.grid(True)

        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True, padx=8, pady=6)

        # 底部：日志
        bottom = ttk.Frame(self)
        bottom.pack(fill="both", padx=8, pady=6)
        self.log = tk.Text(bottom, height=8)
        self.log.pack(fill="both", expand=True)
        self._log("GUI started")

        # 数据缓存（ts秒、ppg）
        self.buf_t = []
        self.buf_ppg = []

        # 启动串口线程
        self.reader.start()

        # 定时轮询队列 + 刷新绘图
        self._last_draw = 0.0
        self.after(10, self._poll_queue)
        self.after(200, self._tick_watchdog)

    # 轮询后台队列
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "data":
                    self._on_sample(payload)
        except queue.Empty:
            pass
        # 控制刷新频率
        now = time.time()
        if now - self._last_draw >= 1.0 / TARGET_FPS:
            self._redraw()
            self._last_draw = now
        self.after(10, self._poll_queue)

    # 收到一条数据
    def _on_sample(self, s: Sample):
        self.last_packet_time = time.time()
        self.latest_bpm = s.bpm
        self.bpm_var.set(f"BPM: {s.bpm:0.1f}")

        # 维护固定时长窗口
        t_now = self.last_packet_time
        self.buf_t.append(t_now)
        self.buf_ppg.append(s.ppg)

        # 删除过旧数据（只保留最近 PPG_BUFFER_SEC 秒）
        t_cut = t_now - PPG_BUFFER_SEC
        while self.buf_t and self.buf_t[0] < t_cut:
            self.buf_t.pop(0); self.buf_ppg.pop(0)

    # 绘图刷新
    def _redraw(self):
        if not self.buf_t:
            return
        t0 = self.buf_t[-1]
        xs = [t - t0 for t in self.buf_t]  # 相对时间（秒，负数到0）
        self.line.set_data(xs, self.buf_ppg)
        self.ax.set_xlim(-PPG_BUFFER_SEC, 0)
        if self.buf_ppg:
            vmin = min(self.buf_ppg); vmax = max(self.buf_ppg)
            if vmin == vmax:
                vmin -= 1; vmax += 1
            pad = (vmax - vmin) * 0.1
            self.ax.set_ylim(vmin - pad, vmax + pad)
        self.canvas.draw_idle()

    # 5秒看门狗（无包 -> 告警）
    def _tick_watchdog(self):
        now = time.time()
        if self.last_packet_time == 0 or (now - self.last_packet_time) > WATCHDOG_SEC:
            self.alarm_var.set("ALARM: No packet > 5s")
            self.alarm_label.configure(foreground="red")
        else:
            self.alarm_var.set("OK")
            self.alarm_label.configure(foreground="green")
        self.after(500, self._tick_watchdog)

    def _log(self, msg: str):
        ts = time.strftime("%a %b %d %H:%M:%S")
        self.log.insert("end", f"{ts}: {msg}\n")
        self.log.see("end")

    def on_close(self):
        try:
            self.reader.stop()
        except:
            pass
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
