#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMET2922/9922 Wearable PPG GUI (Tkinter + Matplotlib) + Serial backend + Drop Demo
- Single window showing BPM (bars+trend) or Pulse waveform (toggle)
- Visual alarms for high/low pulse; log window (timestamp format)
- 5 s "no packet" watchdog using a Remote indicator + log
- Accepts three backend sources:
    1) External command (NDJSON over stdout) via backend_cmd
    2) Serial port (NDJSON or plain BPM lines) via SERIAL_PORT
    3) Built-in simulator (when both are disabled)
- JSON lines:
    {"bpm":72.3}
    {"bpm":..., "samples":[50 ints], "seq":N, "flags":X, "t_mcu":ms}
- Demo helper: "Simulate Drop (5s)" button to force 6s data ignore → triggers watchdog.
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
from matplotlib.backends.backend_pdf import PdfPages

# ---- optional serial (pyserial may be absent) ----
try:
    import serial
except Exception:
    serial = None

# ===================== User Config =====================
# 串口（如果你走 Bluetooth SPP → 串口），把下面改成你 Mac 上的设备名
# 例如：'/dev/tty.ESP32_PPG-SerialPort' 或 '/dev/tty.usbmodem1101'
SERIAL_PORT: Optional[str] = None   # e.g. '/dev/tty.ESP32_PPG-SerialPort'
SERIAL_BAUD: int = 115200

# 外部命令（如果你想用管道方式，把串口当作“命令”来读）
# 例如：backend_cmd = "cat /dev/tty.ESP32_PPG-SerialPort"
BACKEND_CMD: Optional[str] = None

# -------- Config --------
HISTORY_SECONDS = 15
SAMPLE_HZ = 20
UI_UPDATE_MS = 200
BAR_BIN_SEC = 1.0
DEFAULT_LOW = 40.0
DEFAULT_HIGH = 90.0


# ===== Small widget: colored dot with label =====
class AlarmDot(ttk.Frame):
    def __init__(self, master, text, color="#222"):
        super().__init__(master)
        self.c = tk.Canvas(self, width=16, height=16, highlightthickness=0, bg=self._bg(master))
        self.oval = self.c.create_oval(2, 2, 14, 14, fill=color, outline="")
        self.c.pack(side="left", padx=(0, 6))
        ttk.Label(self, text=text).pack(side="left")

    def set_color(self, color):
        self.c.itemconfig(self.oval, fill=color)

    def _bg(self, w):
        try:
            return w.cget("background")
        except Exception:
            return "#f0f0f0"


# ===== Serial reader thread =====
class SerialReader(threading.Thread):
    """
    Read text lines from a serial port and push them to GUI queue as ('raw', line).
    Each line should end with '\n'. Supports NDJSON and '72.4' plain BPM lines.
    """
    def __init__(self, port: str, baudrate: int, q: queue.Queue):
        super().__init__(daemon=True)
        if serial is None:
            raise RuntimeError("pyserial not installed. Run `pip install pyserial` to enable SerialReader.")
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
                # swallow decoding/IO errors
                pass

    def stop(self):
        self.running = False
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass


# ===== Producer thread: reads external process or simulates data =====
class BackendThread(threading.Thread):
    """
    External command backend OR simulator.
    If cmd is None → simulator; otherwise read stdout lines and parse like SerialReader.
    """
    def __init__(self, q: queue.Queue, cmd: Optional[str] = None):
        super().__init__(daemon=True)
        self.q = q
        self.cmd = cmd
        self.stop_flag = False
        self.seq = 0

    def run(self):
        if self.cmd:
            # ---- External backend mode ----
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
                # 直接交给主线程解析：统一走 ('raw', line)
                self.q.put(("raw", line))

            self.q.put(("log", "[BACKEND] finished"))
            return

        # ---- Simulation mode ----
        self.q.put(("log", "[SIM] running mock BPM & packets"))
        t0 = time.time()
        last_pkt = 0.0

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


# ===== Main GUI =====
class App:
    def __init__(self, root: tk.Tk, backend_cmd: Optional[str] = None):
        self.root = root
        self._closing = False

        self.root.title("BMET2922 Pulse GUI")
        self.root.geometry("1080x680")

        outer = ttk.Frame(root)
        outer.pack(fill="both", expand=True, padx=12, pady=10)

        ttk.Label(outer, text="Wearable PPG — Host GUI", font=("Segoe UI", 14, "bold")).pack(anchor="w")

        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True, pady=(8, 6))
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(mid, width=280)
        right.pack(side="left", fill="y", padx=(12, 0))

        # --- Matplotlib figure ---
        self.fig = Figure(figsize=(6, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("BPM")
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("BPM")
        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Metrics cards
        cards = ttk.Frame(left)
        cards.pack(fill="x", pady=(6, 0))
        self.mean_var = tk.StringVar(value="--.-")
        self.curr_var = tk.StringVar(value="--.-")

        def card(parent, title, var):
            f = ttk.Frame(parent)
            ttk.Label(f, text=title).pack(side="left", padx=(0, 6))
            ttk.Label(f, textvariable=var, font=("Segoe UI", 14, "bold")).pack(side="left")
            return f

        card(cards, "mean", self.mean_var).pack(side="left", padx=(0, 20))
        card(cards, "Current", self.curr_var).pack(side="left")

        # ---- Right panel ----
        ttk.Label(right, text="ALARM", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))
        self.dot_high = AlarmDot(right, "High Pulse")
        self.dot_high.pack(anchor="w", pady=2)
        self.dot_low = AlarmDot(right, "Low Pulse")
        self.dot_low.pack(anchor="w", pady=2)
        self.dot_remote = AlarmDot(right, "Remote", "#1d8f2b")
        self.dot_remote.pack(anchor="w", pady=2)

        # Buttons
        self.mode = tk.StringVar(value="BPM")
        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(8, 4))
        ttk.Button(btns, text="Info",
                   command=lambda: messagebox.showinfo("Info", "BMET2922 Pulse GUI")).pack(side="left", padx=2)
        ttk.Button(btns, text="BPM",
                   command=lambda: self.mode.set("BPM")).pack(side="left", padx=2)
        ttk.Button(btns, text="PULSE",
                   command=lambda: self.mode.set("PULSE")).pack(side="left", padx=2)
        # ---- Demo button: simulate 5s+ drop ----
        ttk.Button(btns, text="Simulate Drop (5s)",
                   command=lambda: self._simulate_drop(6)).pack(side="left", padx=2)
        ttk.Button(btns, text="EXIT", command=lambda: self.root.destroy()).pack(side="left", padx=2)

        # Threshold sliders
        thr = ttk.Frame(right)
        thr.pack(fill="x", pady=(8, 4))
        ttk.Label(thr, text="Low").grid(row=0, column=0, sticky="w")
        ttk.Label(thr, text="High").grid(row=1, column=0, sticky="w")
        self.low_var = tk.DoubleVar(value=DEFAULT_LOW)
        self.high_var = tk.DoubleVar(value=DEFAULT_HIGH)
        s1 = ttk.Scale(thr, from_=30, to=100, variable=self.low_var, orient="horizontal")
        s1.grid(row=0, column=1, sticky="ew", padx=6)
        s2 = ttk.Scale(thr, from_=60, to=150, variable=self.high_var, orient="horizontal")
        s2.grid(row=1, column=1, sticky="ew", padx=6)
        thr.columnconfigure(1, weight=1)
        ttk.Label(thr, textvariable=self.low_var, width=6).grid(row=0, column=2, sticky="e")
        ttk.Label(thr, textvariable=self.high_var, width=6).grid(row=1, column=2, sticky="e")

        # Log area
        ttk.Label(outer, text="Log").pack(anchor="w")
        self.log = tk.Text(outer, height=8)
        self.log.pack(fill="both", expand=False)
        self._log("GUI started")

        # Buffers & backend
        self.ts, self.bpm = deque(), deque()
        self.q = queue.Queue()
        self.backend = BackendThread(self.q, backend_cmd)
        self.backend.start()

        # Serial (optional)
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

        # Watchdog & waveform buffers
        self.last_pkt_ts = 0.0
        self.miss_alarm_on = False
        self.wave_t, self.wave_y = deque(), deque()
        self._after_poll = None
        self._after_draw = None

        # ---- Demo gate: ignore incoming packets until this timestamp (seconds) ----
        self._drop_until = 0.0

        # schedule periodic tasks
        self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)
        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        self._closing = True
        try:
            if hasattr(self, "backend") and self.backend is not None:
                self.backend.stop_flag = True
        except Exception:
            pass
        try:
            if hasattr(self, "serial_thread") and self.serial_thread is not None:
                if hasattr(self.serial_thread, "stop"):
                    self.serial_thread.stop()
        except Exception:
            pass
        try:
            if hasattr(self, "_after_poll") and self._after_poll:
                self.root.after_cancel(self._after_poll); self._after_poll = None
            if hasattr(self, "_after_draw") and self._after_draw:
                self.root.after_cancel(self._after_draw); self._after_draw = None
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        self.root.destroy()

    # --- logging with exact timestamp format ---
    def _log(self, s: str):
        ts = time.strftime("%a %b %d %H:%M:%S %Y")
        self.log.insert("end", f"{ts}: {s}\n")
        self.log.see("end")

    def _log_once(self, s: str):
        if getattr(self, "_last_alarm", None) != s:
            self._last_alarm = s
            self._log(s)

    # --- demo: simulate N seconds of drop (ignore incoming data) ---
    def _simulate_drop(self, seconds=6):
        self._drop_until = time.time() + float(seconds)
        self._log(f"[Demo] Simulating no packets for {int(seconds)} s")

    # --- queue polling: gather bpm and packets ---
    def _poll_queue(self):
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                typ, payload = self.q.get_nowait()

                if typ == "log":
                    self._log(payload)

                elif typ == "raw":
                    # Parse a single text line (from Serial or external cmd): NDJSON or '72.4'
                    line = (payload or "").strip()
                    try:
                        if line.startswith("{"):
                            obj = json.loads(line)
                            bpm = float(obj["bpm"])
                            if time.time() < getattr(self, "_drop_until", 0.0):
                                # demo gate: ignore during drop window
                                continue
                            if "samples" in obj:
                                samples = obj["samples"]
                                seq = int(obj.get("seq", 0))
                                flags = int(obj.get("flags", 0))
                                t_mcu = int(obj.get("t_mcu", 0))
                                t_host = time.time()
                                # handle as 'pkt'
                                self.last_pkt_ts = t_host
                                self.ts.append(t_host); self.bpm.append(bpm)
                                base = t_host - 1.0
                                for i, y in enumerate(samples):
                                    self.wave_t.append(base + i/50.0)
                                    self.wave_y.append(y)
                            else:
                                # handle as 'bpm'
                                t = time.time()
                                self.last_pkt_ts = t
                                self.ts.append(t); self.bpm.append(bpm)
                        else:
                            # plain BPM line
                            if time.time() < getattr(self, "_drop_until", 0.0):
                                continue
                            bpm = float(line.split(",")[0])
                            t = time.time()
                            self.last_pkt_ts = t
                            self.ts.append(t); self.bpm.append(bpm)
                    except Exception:
                        # bad line → record trimmed content for debugging
                        self._log(f"[PARSE] {line[:120]}")

                    # trim buffers (same as below)
                    cutoff = time.time() - HISTORY_SECONDS
                    while self.ts and self.ts[0] < cutoff:
                        self.ts.popleft(); self.bpm.popleft()
                    while self.wave_t and self.wave_t[0] < cutoff:
                        self.wave_t.popleft(); self.wave_y.popleft()

                elif typ == "bpm":
                    # demo gate
                    if time.time() < getattr(self, "_drop_until", 0.0):
                        continue
                    t, v = payload
                    self.last_pkt_ts = t
                    self.ts.append(t); self.bpm.append(v)
                    cutoff = time.time() - HISTORY_SECONDS
                    while self.ts and self.ts[0] < cutoff:
                        self.ts.popleft(); self.bpm.popleft()

                elif typ == "pkt":
                    # demo gate
                    if time.time() < getattr(self, "_drop_until", 0.0):
                        continue
                    t_host, bpm, samples, seq, flags, t_mcu = payload
                    self.last_pkt_ts = t_host
                    self.ts.append(t_host); self.bpm.append(bpm)
                    base = t_host - 1.0
                    for i, y in enumerate(samples):
                        self.wave_t.append(base + i / 50.0)
                        self.wave_y.append(y)
                    cutoff = time.time() - HISTORY_SECONDS
                    while self.ts and self.ts[0] < cutoff:
                        self.ts.popleft(); self.bpm.popleft()
                    while self.wave_t and self.wave_t[0] < cutoff:
                        self.wave_t.popleft(); self.wave_y.popleft()

        except queue.Empty:
            pass
        except Exception as e:
            try:
                self._log(f"[poll] error: {e}")
            except Exception:
                pass
        finally:
            self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)

    # --- periodic redraw ---
    def _redraw(self):
        if getattr(self, "_closing", False):
            return

        # Watchdog: 5s without packet -> alarm
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

        # ---- BPM view ----
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

            # --- high/low alarms（阈值报警：按你要求） ---
            low = float(self.low_var.get())
            high = float(self.high_var.get())

            if curr_v < low:
                # 低阈值报警：低灯红，高灯灰；日志：Pulse Low
                self.dot_low.set_color("#cc2121")
                self.dot_high.set_color("#222")
                self._log_once("Pulse Low")
            elif curr_v > high:
                # 高阈值报警：高灯红，低灯灰；日志：Pulse High
                self.dot_high.set_color("#cc2121")
                self.dot_low.set_color("#222")
                self._log_once("Pulse High")
            else:
                # 正常：两灯均灰
                self.dot_high.set_color("#222")
                self.dot_low.set_color("#222")

            if self.miss_alarm_on:
                self.ax.text(0.02, 0.95, "NO PACKET > 5 s",
                             transform=self.ax.transAxes,
                             bbox=dict(facecolor='red', alpha=.3))
            self.canvas.draw()

        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)


# -------------------------- CSV recording / Researcher add-ons --------------------------
def _addon_ensure_researcher_window(self: App):
    if getattr(self, "_researcher_win", None) and self._researcher_win.winfo_exists():
        return

    self._researcher_win = tk.Toplevel(self.root)
    self._researcher_win.title("Researcher")
    self._researcher_win.geometry("820x520")

    top = ttk.Frame(self._researcher_win); top.pack(fill="x", padx=8, pady=6)
    self._rmssd_var  = tk.StringVar(value="RMSSD: --.- ms")
    self._sdppg_var  = tk.StringVar(value="SDPPG: --.-")
    self._latp95_var = tk.StringVar(value="Latency P95: -- ms")
    self._loss_var   = tk.StringVar(value="Loss: 0.0%")
    for var in (self._rmssd_var, self._sdppg_var, self._latp95_var, self._loss_var):
        ttk.Label(top, textvariable=var, font=("Segoe UI", 10)).pack(side="left", padx=(0,16))

    self._fig_res = Figure(figsize=(6,4), dpi=100)
    self._ax_res  = self._fig_res.add_subplot(111)
    self._ax_res.set_title("Spectrogram (last 15 s)")
    self._ax_res.set_xlabel("Time bins")
    self._ax_res.set_ylabel("Hz")
    self._canvas_res = FigureCanvasTkAgg(self._fig_res, master=self._researcher_win)
    self._canvas_res.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=4)


def _addon_toggle_recording(self: App):
    if not getattr(self, "_recording", False):
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        rec_dir = os.path.join(base, "records")
        os.makedirs(rec_dir, exist_ok=True)
        path = os.path.join(rec_dir, f"session_{ts}.csv")
        try:
            self._rec_file = open(path, "w", newline="")
            self._rec_writer = csv.writer(self._rec_file)
            self._rec_writer.writerow(["t_host","t_mcu_ms","bpm","seq","flags","samples_json"])
            self._recording = True
            self._last_csv_path = path
            self._log(f"Recording started -> {os.path.abspath(path)}")
        except Exception as e:
            self._log(f"[REC] open failed: {e}")
            self._recording = False
            self._rec_file = None; self._rec_writer = None
    else:
        try:
            if getattr(self, "_rec_file", None):
                self._rec_file.close()
        except Exception:
            pass
        self._recording = False
        self._rec_file = None; self._rec_writer = None
        self._log("Recording stopped")


def _addon_export_summary_pdf(self: App):
    try:
        csv_path = getattr(self, "_last_csv_path", None)
        if not csv_path:
            self._log("[PDF] No CSV session found to summarize")
            return
        pdf_path = csv_path.replace(".csv", ".pdf")
        with PdfPages(pdf_path) as pdf:
            try:
                self.canvas.draw()
                pdf.savefig(self.canvas.figure)
            except Exception as e:
                self._log(f"[PDF] main figure save failed: {e}")
            if getattr(self, "_fig_res", None):
                try:
                    self._canvas_res.draw()
                    pdf.savefig(self._fig_res)
                except Exception as e:
                    self._log(f"[PDF] spectrogram save failed: {e}")
        self._log(f"Summary PDF saved -> {pdf_path}")
    except Exception as e:
        self._log(f"[PDF] export failed: {e}")


def _addon_update_researcher_timer(self: App):
    try:
        if len(self.bpm) >= 10:
            bpm_arr = np.array(self.bpm, dtype=float)
            bpm_arr = bpm_arr[bpm_arr > 1.0]
            if getattr(self, "_rmssd_var", None) and len(bpm_arr) >= 10:
                ibi = 60.0 / bpm_arr
                diff = np.diff(ibi)
                rmssd = math.sqrt(np.mean(diff**2)) * 1000.0
                self._rmssd_var.set(f"RMSSD: {rmssd:.1f} ms")

        if len(self.wave_y) > 60 and getattr(self, "_sdppg_var", None):
            y = np.array(self.wave_y, dtype=float)[-750:]
            if y.size >= 6:
                d2 = np.diff(y, n=2)
                sdppg = float(np.sqrt(np.mean(d2**2)))
                self._sdppg_var.set(f"SDPPG: {sdppg:.1f}")

        if getattr(self, "_lat_ms", None) and len(self._lat_ms) >= 5 and getattr(self, "_latp95_var", None):
            lat = np.array(self._lat_ms, dtype=float)
            p95 = float(np.percentile(lat, 95))
            self._latp95_var.set(f"Latency P95: {p95:.0f} ms")

        if getattr(self, "_total_pkts", 0) > 0 and getattr(self, "_loss_var", None):
            loss = 100.0 * self._missed / (self._missed + self._total_pkts)
            self._loss_var.set(f"Loss: {loss:.1f}%")

        if getattr(self, "_researcher_win", None) and self._researcher_win.winfo_exists():
            if len(self.wave_y) > 200 and getattr(self, "_ax_res", None):
                y = np.array(self.wave_y, dtype=float)[-750:]
                self._ax_res.cla()
                self._ax_res.set_title("Spectrogram (last 15 s)")
                self._ax_res.set_xlabel("Time bins"); self._ax_res.set_ylabel("Hz")
                self._ax_res.specgram(y, NFFT=128, Fs=50.0, noverlap=64, scale='dB')
                self._ax_res.set_ylim(0, 10)
                self._canvas_res.draw()
    except Exception as e:
        try:
            self._log(f"[Researcher] update error: {e}")
        except Exception:
            pass
    finally:
        try:
            self.root.after(UI_UPDATE_MS, lambda: _addon_update_researcher_timer(self))
        except Exception:
            pass


def _addon_on_close(self: App):
    try:
        if getattr(self, "_rec_file", None):
            self._rec_file.close()
            self._rec_file = None
            self._rec_writer = None
    except Exception:
        pass
    try:
        _addon_export_summary_pdf(self)
    except Exception:
        pass
    try:
        if hasattr(self, "serial_thread"):
            self.serial_thread.stop()
    except Exception:
        pass
    try:
        self.root.quit()
    except Exception:
        pass
    try:
        self._closing = True
        self.root.destroy()
    except Exception:
        pass


# -------------------------- Non-invasive augmentation of App --------------------------
_App__init_orig = App.__init__
_App__poll_queue_orig = App._poll_queue

def _App__init_addon(self: App, root: tk.Tk, backend_cmd: Optional[str] = None):
    _App__init_orig(self, root, backend_cmd)

    from collections import deque as _addon_deque
    self._lat_ms = _addon_deque(maxlen=600)
    self._seq_prev = None
    self._missed = 0
    self._total_pkts = 0

    self._recording = False
    self._rec_file = None
    self._rec_writer = None
    self._last_csv_path = None

    try:
        menubar = tk.Menu(self.root)
        m_tools = tk.Menu(menubar, tearoff=False)
        m_tools.add_command(label="Open Researcher Window", command=lambda: (_addon_ensure_researcher_window(self)))
        m_tools.add_command(label="Start/Stop Recording (CSV)", command=lambda: (_addon_toggle_recording(self)))
        m_tools.add_command(label="Export Summary PDF Now", command=lambda: (_addon_export_summary_pdf(self)))
        menubar.add_cascade(label="Tools", menu=m_tools)
        self.root.config(menu=menubar)
    except Exception as e:
        self._log(f"[Menu] build failed: {e}")

    self.root.protocol("WM_DELETE_WINDOW", lambda: _addon_on_close(self))
    self.root.destroy = lambda: _addon_on_close(self)  # EXIT 按钮调用 root.destroy() 即触发

    self.root.after(UI_UPDATE_MS, lambda: _addon_update_researcher_timer(self))


def _App__poll_queue_addon(self: App):
    try:
        while True:
            typ, payload = self.q.get_nowait()
            if typ == "log":
                self._log(payload)

            elif typ == "bpm":
                if time.time() < getattr(self, "_drop_until", 0.0):
                    continue
                t, v = payload
                self.last_pkt_ts = t
                self.ts.append(t); self.bpm.append(v)
                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()

            elif typ == "pkt":
                if time.time() < getattr(self, "_drop_until", 0.0):
                    continue
                t_host, bpm, samples, seq, flags, t_mcu = payload
                self.last_pkt_ts = t_host
                self.ts.append(t_host); self.bpm.append(bpm)

                base = t_host - 1.0
                for i, y in enumerate(samples):
                    self.wave_t.append(base + i/50.0)
                    self.wave_y.append(y)

                # latency & loss tracking (optional metrics)
                try:
                    lat = (t_host - (t_mcu/1000.0)) * 1000.0
                    self._lat_ms.append(max(0.0, float(lat)))
                    if self._seq_prev is not None:
                        gap = (seq - self._seq_prev) % 65536
                        if gap > 1:
                            self._missed += (gap - 1)
                    self._seq_prev = seq
                    self._total_pkts += 1
                except Exception as e:
                    self._log(f"[Researcher] metric error: {e}")

                # trim buffers
                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()
                while self.wave_t and self.wave_t[0] < cutoff:
                    self.wave_t.popleft(); self.wave_y.popleft()

            elif typ == "raw":
                line = (payload or "").strip()
                try:
                    if line.startswith("{"):
                        obj = json.loads(line)
                        bpm = float(obj["bpm"])
                        if time.time() < getattr(self, "_drop_until", 0.0):
                            continue
                        if "samples" in obj:
                            samples = obj["samples"]
                            seq = int(obj.get("seq", 0))
                            flags = int(obj.get("flags", 0))
                            t_mcu = int(obj.get("t_mcu", 0))
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
                        if time.time() < getattr(self, "_drop_until", 0.0):
                            continue
                        bpm = float(line.split(",")[0])
                        t = time.time()
                        self.last_pkt_ts = t
                        self.ts.append(t); self.bpm.append(bpm)
                except Exception:
                    self._log(f"[PARSE] {line[:120]}")

                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()
                while self.wave_t and self.wave_t[0] < cutoff:
                    self.wave_t.popleft(); self.wave_y.popleft()

    except queue.Empty:
        pass
    finally:
        self.root.after(UI_UPDATE_MS, self._poll_queue)


# Activate the non-invasive augmentation
App.__init__     = _App__init_addon
App._poll_queue  = _App__poll_queue_addon


# ---- Entrypoint ----
def main():
    root = tk.Tk()
    try:
        style = ttk.Style(); style.theme_use("clam")
    except Exception:
        pass
    app = App(root, backend_cmd=BACKEND_CMD)  # 可选：外部命令；也可只用 SERIAL_PORT 或模拟
    root.mainloop()

if __name__ == "__main__":
    main()
