#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pulse Monitor GUI - Tkinter + Matplotlib
完全免费，不依赖 PySimpleGUI 私有源。
支持实时模拟数据（PPG + BPM）
"""
import tkinter as tk
from tkinter import ttk
import threading, time, math
from collections import deque
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

BUFFER_SECONDS = 30
SAMPLE_HZ = 50

class PulseGUI:
    def __init__(self, root):
        self.root = root
        root.title("Pulse Monitor (Tkinter Version)")
        root.geometry("900x600")
        self.bpm_var = tk.StringVar(value="--.-")
        self.status_var = tk.StringVar(value="NORMAL")

        # 顶部标题栏
        top = ttk.Frame(root)
        ttk.Label(top, text="Pulse Monitor", font=("Arial", 16, "bold")).pack(side="left", padx=10)
        ttk.Label(top, textvariable=self.bpm_var, font=("Arial", 48, "bold")).pack(side="right", padx=10)
        top.pack(fill="x", pady=5)

        # 绘图区域
        fig = Figure(figsize=(8,3), dpi=100)
        self.ax = fig.add_subplot(111)
        self.ax.set_title("PPG Waveform (sim)")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Amplitude")
        self.canvas = FigureCanvasTkAgg(fig, master=root)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, pady=10)

        # 状态栏
        bottom = ttk.Frame(root)
        ttk.Label(bottom, textvariable=self.status_var, font=("Arial", 14, "bold")).pack(side="left", padx=10)
        bottom.pack(fill="x", pady=5)

        # 数据缓存
        self.ts = deque()
        self.ppg = deque()
        self.bpm = deque()
        self.running = True
        threading.Thread(target=self._simulate_data, daemon=True).start()
        self._update_gui()

    def _simulate_data(self):
        t0 = time.time()
        f = 1.4
        while self.running:
            t = time.time() - t0
            ppg = 0.9 * math.sin(2*math.pi*f*t) + 0.15 * math.sin(2*math.pi*3.2*t)
            bpm = 72 + 4 * math.sin(2*math.pi*0.1*t)
            self.ts.append(t)
            self.ppg.append(ppg)
            self.bpm.append(bpm)
            while self.ts and self.ts[0] < t - BUFFER_SECONDS:
                self.ts.popleft(); self.ppg.popleft(); self.bpm.popleft()
            time.sleep(1.0 / SAMPLE_HZ)

    def _update_gui(self):
        if len(self.ts) > 1:
            t = np.array(self.ts)
            p = np.array(self.ppg)
            b = np.array(self.bpm)
            self.ax.cla()
            self.ax.plot(t - t[-1], p, linewidth=1.0)
            self.ax.set_xlim(-BUFFER_SECONDS, 0)
            self.ax.set_title("PPG Waveform (sim)")
            self.ax.grid(True, alpha=0.3)
            self.canvas.draw()
            bpm_now = b[-1]
            self.bpm_var.set(f"{bpm_now:.1f}")
            if bpm_now < 50:
                self.status_var.set("LOW")
            elif bpm_now > 120:
                self.status_var.set("HIGH")
            else:
                self.status_var.set("NORMAL")
        if self.running:
            self.root.after(200, self._update_gui)

    def stop(self):
        self.running = False

if __name__ == "__main__":
    root = tk.Tk()
    app = PulseGUI(root)
    try:
        root.mainloop()
    finally:
        app.stop()
