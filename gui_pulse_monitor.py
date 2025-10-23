#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pulse Monitor GUI (PySimpleGUI 5.x + Matplotlib)
- 单窗口：大号 BPM + 曲线（PPG / BPM 趋势切换）
- ≥1Hz 更新；阈值报警；系统日志；延迟<2s
- 与 C 程序通过 stdout 按行通讯（JSON 或 CSV）
- 无 --backend 时使用模拟数据，便于测试
"""
import sys, json, time, math, queue, threading, subprocess, shlex
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Deque, Tuple
import numpy as np
import PySimpleGUI as sg
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

BUFFER_SECONDS = 30
UI_UPDATE_MS = 200
PLOT_UPDATE_MS = 500
DEFAULT_LOW_BPM = 50.0
DEFAULT_HIGH_BPM = 120.0
LOG_MAX_LINES = 5000

@dataclass
class Sample:
    t: float; bpm: float; ppg: float

@dataclass
class RingBuffer:
    seconds: float
    ts: Deque[float] = field(default_factory=deque)
    bpm: Deque[float] = field(default_factory=deque)
    ppg: Deque[float] = field(default_factory=deque)
    def append(self, s: Sample):
        self.ts.append(s.t); self.bpm.append(s.bpm); self.ppg.append(s.ppg)
        cutoff = s.t - self.seconds
        while self.ts and self.ts[0] < cutoff:
            self.ts.popleft(); self.bpm.popleft(); self.ppg.popleft()
    def arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (np.fromiter(self.ts, float),
                np.fromiter(self.bpm, float),
                np.fromiter(self.ppg, float))

class BackendReader(threading.Thread):
    def __init__(self, out_q: queue.Queue, cmd: Optional[str]):
        super().__init__(daemon=True); self.q = out_q; self.cmd = cmd
        self.proc = None; self.stop_flag = threading.Event(); self.sim = cmd is None
    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            try: self.proc.terminate()
            except Exception: pass
    def run(self):
        self._run_sim() if self.sim else self._run_proc()
    def _run_proc(self):
        try:
            self.proc = subprocess.Popen(shlex.split(self.cmd),
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         bufsize=1, universal_newlines=True)
            self._log(f"[OK] backend: {self.cmd}")
        except Exception as e:
            self._log(f"[ERR] start backend failed: {e}"); return
        t0 = time.time()
        for line in self.proc.stdout:
            if self.stop_flag.is_set(): break
            line = line.strip()
            s = self._parse_line(line, t0)
            if s: self.q.put(("data", s))
        if self.proc:
            err = self.proc.stderr.read()
            if err:
                for ln in err.splitlines():
                    self._log(f"[BACKEND] {ln}")
    def _run_sim(self):
        self._log("[SIM] running mock data (no --backend)")
        t0 = time.time(); f = 1.4
        while not self.stop_flag.is_set():
            t = time.time() - t0
            ppg = 0.9*math.sin(2*math.pi*f*t) + 0.15*math.sin(2*math.pi*3.2*t)
            bpm = 72.0 + 4.0*math.sin(2*math.pi*0.1*t)
            self.q.put(("data", Sample(t=t, bpm=bpm, ppg=ppg)))
            time.sleep(0.02)  # 50 Hz
    def _parse_line(self, line: str, t0: float) -> Optional[Sample]:
        # JSON
        try:
            obj = json.loads(line)
            t_raw = obj.get("ts", time.time()); bpm = float(obj["bpm"]); ppg = float(obj["ppg"])
            t = self._normalize_ts(float(t_raw), t0); return Sample(t, bpm, ppg)
        except Exception: pass
        # CSV ts,bpm,ppg
        try:
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 3:
                t = self._normalize_ts(float(p[0]), t0); return Sample(t, float(p[1]), float(p[2]))
        except Exception:
            self._log(f"[PARSE] bad line: {line[:100]}")
        return None
    def _normalize_ts(self, t_raw: float, t0: float) -> float:
        if t_raw > 1e12: t_sec = t_raw/1000.0
        elif t_raw > 1e9: t_sec = t_raw
        else: t_sec = t0 + t_raw
        return t_sec - t0
    def _log(self, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.q.put(("log", f"{ts}: {msg}"))

def _draw(canvas_elem, fig):
    agg = FigureCanvasTkAgg(fig, canvas_elem.TKCanvas)
    agg.draw(); agg.get_tk_widget().pack(side="top", fill="both", expand=1)
    return agg

def _smooth(a: np.ndarray, win: int = 5) -> np.ndarray:
    if a.size < 2 or win <= 1: return a
    win = min(win, a.size); k = np.ones(win)/win
    return np.convolve(a, k, mode="same")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=str, default=None, help="C 可执行文件（不填=模拟）")
    parser.add_argument("--args", type=str, default="", help="传给后端的参数串")
    parser.add_argument("--buffer", type=float, default=BUFFER_SECONDS)
    args = parser.parse_args()

    sg.set_options(font=("Inter", 12))
    buf = RingBuffer(args.buffer); q = queue.Queue()
    mode = sg.Combo(["PPG Waveform","BPM Trend"], default_value="PPG Waveform", key="-MODE-", readonly=True)
    auto = sg.Checkbox("Auto Y", key="-AUTO-", default=True)
    y_min = sg.Input("-1.5", key="-YMIN-", size=(6,1), justification="right")
    y_max = sg.Input("1.5",  key="-YMAX-", size=(6,1), justification="right")
    bpm_big = sg.Text("—.—", key="-BPM-", font=("Inter", 48, "bold"), size=(8,1), justification="right")
    status = sg.Text(" NORMAL ", key="-STAT-", font=("Inter", 16, "bold"),
                     text_color="white", background_color="#22aa22")
    canvas = sg.Canvas(key="-CANVAS-", size=(800, 400), background_color="black")
    logbox = sg.Multiline(key="-LOG-", size=(48, 24), autoscroll=True, disabled=True)
    low_in = sg.Input(f"{DEFAULT_LOW_BPM:.1f}", key="-LOW-", size=(6,1), justification="right")
    high_in= sg.Input(f"{DEFAULT_HIGH_BPM:.1f}",key="-HIGH-",size=(6,1), justification="right")
    layout = [
        [sg.Text("Pulse Monitor", font=("Inter",18,"bold")), sg.Push(), bpm_big, sg.Push(), status],
        [[sg.Text("Display:"), mode, auto, sg.Text("Y:"), y_min, y_max], [canvas],],
        [sg.Text("System Log", font=("Inter",14,"bold")),],
        [logbox],
        [sg.Text("Low BPM:"), low_in, sg.Text("High BPM:"), high_in]
    ]
    win = sg.Window("BMET Pulse GUI", layout, finalize=True, resizable=True)

    fig = Figure(figsize=(8,3), dpi=100); ax = fig.add_subplot(111); agg = _draw(win["-CANVAS-"], fig)
    last_redraw = 0.0
    cmd = None if args.backend is None else (args.backend if not args.args else f"{args.backend} {args.args}")
    reader = BackendReader(q, cmd); reader.start()

    def log(s: str):
        cur = win["-LOG-"].get()
        if cur.count("\n") > LOG_MAX_LINES: win["-LOG-"].update("")
        win["-LOG-"].update(s+"\n", append=True)

    last_bpm = None
    while True:
        ev, vals = win.read(timeout=UI_UPDATE_MS)
        if ev in (sg.WINDOW_CLOSED, "Exit"): break
        # drain queue
        drained = 0
        while True:
            try: typ, payload = q.get_nowait()
            except queue.Empty: break
            if typ == "log": log(payload)
            elif typ == "data":
                buf.append(payload); last_bpm = payload.bpm; drained += 1
        # BPM display + alarm
        try:
            low = float(vals["-LOW-"]); high = float(vals["-HIGH-"])
        except Exception:
            low, high = DEFAULT_LOW_BPM, DEFAULT_HIGH_BPM
        if last_bpm is not None:
            win["-BPM-"].update(f"{last_bpm:.1f}")
            if last_bpm < low:  win["-STAT-"].update(" LOW ",  background_color="#ffaa00")
            elif last_bpm > high: win["-STAT-"].update(" HIGH ", background_color="#cc2222")
            else: win["-STAT-"].update(" NORMAL ", background_color="#22aa22")
        # redraw (rate limited)
        now = time.time()
        if now - last_redraw >= PLOT_UPDATE_MS/1000.0:
            last_redraw = now
            ts, bpm, ppg = buf.arrays()
            ax.cla()
            if ts.size:
                x = ts - ts[-1]
                if vals["-MODE-"] == "PPG Waveform":
                    ax.plot(x, ppg, linewidth=1.0)
                    ax.set_ylabel("Amplitude")
                    if not vals["-AUTO-"]:
                        try: ax.set_ylim(float(vals["-YMIN-"]), float(vals["-YMAX-"]))
                        except Exception: pass
                else:
                    ax.plot(x, _smooth(bpm, 5), linewidth=1.5)
                    ax.set_ylabel("BPM")
                ax.set_xlim(-buf.seconds, 0.0)
                ax.set_title(f"{vals['-MODE-']} (last {buf.seconds:.0f}s)")
                ax.set_xlabel("Time (s)"); ax.grid(True, alpha=0.3)
            agg.draw()
    try: reader.stop()
    except Exception: pass
    win.close()

if __name__ == "__main__":
    main()
