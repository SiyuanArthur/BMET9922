#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMET2922/9922 Wearable PPG GUI (Tkinter + Matplotlib)
- Single window showing BPM (bars+trend) or Pulse waveform (toggle)
- Visual alarms for high/low pulse; log window (timestamp format matches spec)
- 5 s "no packet" watchdog using a Remote indicator + log
- Backend thread accepts:
    * JSON lines with {"bpm":72.3}  -> queue: ("bpm",(t,bpm))
    * JSON lines with {"bpm":..., "samples":[50...], "seq":N, "flags":X, "t_mcu":ms}
                                   -> queue: ("pkt",(t_host,bpm,samples,seq,flags,t_mcu))
    * Plain lines like: 72.4
- In simulation mode (no external command), it pushes frequent bpm values and
  also emits a "pkt" once per second with 50 synthetic samples.
"""
import tkinter as tk #python标准库里面的GUI TKinter的根命名空间，用来创建窗口，Frame，等基础控件
from tkinter import ttk, messagebox #现代控件集和标准对话框
import threading, time, math, queue, json, subprocess, shlex#开后台线程（考验你cpu的时候到了），时间戳，基础数学功能，线程间的队列，json是后端进程stdout打下来的JSON行，subprocess外部后端程序，shlex把命令字符拆成argv列表
from collections import deque#双端队列，做唤醒缓冲合适，便于只保留最近N秒的数据
import numpy as np#数值运算库，快速做向量化运算

from matplotlib.figure import Figure#Matplotlib.Figure的图对象类。问题：为什么不使用plt.show（会弹出独立窗口并堵塞），而是创建Figure对象嵌入到Tk窗口
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg#把Matplotlib的Tkinter嵌入后端，把Figure变成一个TK控件
#Additional part
import os, csv
import sys, os
from collections import deque as _addon_deque
from matplotlib.backends.backend_pdf import PdfPages

import threading
import queue
import serial

class SerialReader(threading.Thread):
    def __init__(self, port, baudrate, queue):
        super().__init__(daemon=True)
        self.ser = serial.Serial(port, baudrate, timeout=1)
        self.queue = queue
        self.running = True

    def run(self):
        while self.running:
            line = self.ser.readline()
            if line:
                try:
                    text = line.decode('ascii').strip()
                    self.queue.put(text)
                except Exception:
                    # handle decoding errors if any
                    pass

    def stop(self):
        self.running = False
        if self.ser.is_open:
            self.ser.close()


# -------- Config --------
HISTORY_SECONDS = 15        # seconds visible in the chart 主图横轴显示的时间窗口，只保留最近 15 秒数据，想想为什么这样做？
SAMPLE_HZ = 20              # how often simulated bpm points are produced 模拟数据的产出频率（每秒 20 次）。
UI_UPDATE_MS = 200          # GUI refresh (ms)  -> >= 1 Hz requirement satisfied GUI 重绘周期，单位毫秒，5hz刷新率，远大于课程要求1hz，丝滑且稳定！
BAR_BIN_SEC = 1.0           # 1 bar per second 柱状图的时间分箱宽度（每根柱代表几秒的数据）。1 秒一柱刚好符合“每秒一个包/多个样本”的直觉，便于口头讲解（“这 15 根柱子就是 15 秒”）。
DEFAULT_LOW = 40.0          #低脉率阈值（bpm），GUI 启动初始值。40 bpm 常用作“过低”警戒线（运动员或睡眠可低于此，演示时明显）。通俗说就是一般人的心跳在正常情况下很难跌破40bpm
DEFAULT_HIGH = 90.0         #高脉率阈值，一般人如果心跳在正常情况下大于90bpm的话说明他可爱的小身体多少出了点问题了，一般来讲是不运动+心脏功能薄弱的人可能达到这个阈值，当然患病经常可以突破这个值，easily

# ===== Small widget: colored dot with label =====
class AlarmDot(ttk.Frame):#AlarmDot类的功能做一个带小圆点的标签，用来显示报警状态（变色），这个类继承ttk.Frame，做这个类的目的是把“圆点+文本"封装成一个可复用的小部件，外面当普通控件用（可pack/grid/place）
    def __init__(self, master, text, color="#222"):#构造函数参数，master父容器（必须）（为什么必须？）。text：显示在圆点右侧的文字，比如“High Pulse”。color=222：初始颜色深灰
        super().__init__(master)#初始化父类ttk.Frame把这个小部件挂到master上。
        self.c = tk.Canvas(self, width=16, height=16, highlightthickness=0, bg=self._bg(master))#用tk.Canvas而不是ttk因为ttk没有画组部件。highlightthickness=0修饰构图，去掉Canvas周围那圈浅色线条，bg=self._bg(master)：把 Canvas 背景色设成父容器的背景色（记得调参改一改然后看看会有什么不一样的地方）
        self.oval = self.c.create_oval(2, 2, 14, 14, fill=color, outline="")#画一个实心圆，坐标为其外接矩形的左上角和右下角，如果改动的话会发生什么？Fill=color用传入的初始颜色填充，outline=‘’去掉圆的描边，返回值是这个图形对象的item id，存到self.oval里面，后续改色用
        self.c.pack(side="left", padx=(0, 6))#（这个可能会考），把 Canvas 放到 AlarmDot 的左侧
        ttk.Label(self, text=text).pack(side="left")#（主题部件，保证风格统一）。
    def set_color(self, color):#这个方法是专门用来改色用的）
        self.c.itemconfig(self.oval, fill=color)#改色机制：并不重新画圆，而是通过已有的item调itemconfig改fill，效率更高，也不闪烁
    def _bg(self, w):
        try:
            return w.cget("background")
        except Exception:
            return "#f0f0f0"

# ===== Producer thread: reads external process or simulates data =====
class BackendThread(threading.Thread):                    #  定义后台线程类，继承 Thread，让它能在后台跑而不阻塞 GUI 主线程
    def __init__(self, q: queue.Queue, cmd: str | None = None):  #  构造器，q 是线程安全队列；cmd 是可选的“外部后端命令”
        super().__init__(daemon=True)                     #  把线程设为 daemon（守护）。主窗口退出时，不会因为此线程而卡住进程退出
        self.q = q                                        #  保存与主线程通信用的队列（生产者→消费者）
        self.cmd = cmd                                    #  保存外部命令；若为 None，则使用“模拟模式”
        self.stop_flag = False                            #  线程的停止开关；未来想优雅停止时置 True
        self.seq = 0                                      #  包序号计数器（模拟包或 JSON 不给 seq 时自增）


    def run(self):
        if self.cmd:                                      # 如果提供了外部命令，就走“外部后端模式”
            try:
                p = subprocess.Popen(                     #  启动外部进程作为数据源
                    shlex.split(self.cmd),                #  用 shlex 拆分命令字符串，正确处理带空格/引号的路径
                    stdout=subprocess.PIPE,               #  只读 stdout（标准输出）
                    stderr=subprocess.STDOUT,             #  把 stderr 合并到 stdout，避免两根管道读写死锁
                    text=True,                            #  文本模式：返回 str 而不是 bytes
                    bufsize=1                             #  行缓冲（仅在 text=True 时有效）。配合对方“按行 print + flush”
                )
                self.q.put(("log", f"[BACKEND] started: {self.cmd}"))   # 通知 GUI：后端已启动
            except Exception as e:
                self.q.put(("log", f"[BACKEND] start failed: {e}"))     #  启动失败，记录错误
                return                                                  #  线程结束（不再进入读取循环）

            for line in p.stdout:                           #  逐行读取子进程输出；此循环在本线程中，不会卡 GUI
                if self.stop_flag: break                    #  若被请求停止，则中断读取
                line = line.strip()                         #  去掉首尾空白/换行
                if not line: continue                       #  空行跳过（常见于对方多打了一个 print）

                try:#把外部后端输出的一行文本解析成结构化消息丢进队列，供 GUI 主线程消费；既兼容JSON 行（推荐），也兼容纯数字（临时/简单后端）。
                    if line.startswith("{"):                #  若这一行是 JSON（以“{”开头），按 JSON 解析，比直接 json.loads（可能抛异常）更省事、日志更干净；前面已经 strip() 过空白
                        obj = json.loads(line)              #  反序列化（为什么要反序列化？）
                        bpm = float(obj["bpm"])             #  必须包含 bpm，转成 float
                        if "samples" in obj:                #  若还带 samples（长度应为 50），就把它当“完整包”
                            samples = obj["samples"]        #  取 50 个样本（PPG 1 秒的波形）
                            seq = int(obj.get("seq", self.seq))   #  若 JSON 没给 seq，就用本地 seq；随后自增（seq是什么）？
                            self.seq = seq + 1
                            flags = int(obj.get("flags", 0))       #  状态位；默认 0
                            t_mcu = int(obj.get("t_mcu", 0))       #  MCU 毫秒时间戳；默认 0
                            # ↓ 投递“完整包”：t_host=当前主机时间，用于 GUI 看门狗与延迟估计
                            self.q.put(("pkt", (time.time(), bpm, samples, seq, flags, t_mcu)))
                        else:
                            # ↓ 只有 bpm，没有样本 → 投递“简包”，仅更新 BPM 曲线/柱状
                            self.q.put(("bpm", (time.time(), bpm)))
                    else:
                        # ↓ 非 JSON：允许是“72.3”或“72.3,其它字段” → 取第一段数字
                        bpm = float(line.split(",")[0])
                        self.q.put(("bpm", (time.time(), bpm)))
                except Exception:
                    # ↓ 解析失败：把原始行截断记录到日志，方便定位格式问题
                    self.q.put(("log", f"[PARSE] {line[:120]}"))

            self.q.put(("log", "[BACKEND] finished"))       #  子进程已结束/读完，写日志提示
            return                                          #  结束 run()，线程退出

        else:
            # ---- Simulation mode ----
            # ---- Simulation mode ----
            self.q.put(("log", "[SIM] running mock BPM & packets"))  # ← 日志：进入模拟模式
            t0 = time.time()                                         # ← 起点时间（用于生成平稳的正弦）
            last_pkt = 0.0                                           # ← 上一次发“完整包”的时刻（每秒 1 个包）

            while not self.stop_flag:                                # ← 模拟主循环
                t = time.time() - t0                                 # ← 相对时间（秒）
                # ↓ 用两路低频正弦叠加得到“起伏心率”：中心 71，幅度分别 3.5 与 1.0；频率 0.10 Hz 与 0.04 Hz
                bpm = 71.0 + 3.5 * math.sin(2 * math.pi * 0.10 * t) + 1.0 * math.sin(2 * math.pi * 0.04 * t)
                self.q.put(("bpm", (time.time(), bpm)))              # ← 高频（20 Hz）往队列投“简包”：用于趋势线/柱状的平滑显示

                # ---- emit a packet once per second with 50-sample sine waveform
                if time.time() - last_pkt >= 1.0:                    # ← 每满 1 秒，生成一个“完整包”（仿真 MCU 1 Hz 帧）
                    last_pkt = time.time()
                    x = np.arange(50, dtype=float) / 50.0            # ← 生成 [0,1) 的 50 个等分点，代表 1 秒内的 50 个样本（20 ms 间隔）
                    # ↓ 构造“类 PPG 波形”：1.2 Hz 正弦（≈72 bpm）+ 高斯噪声（幅度 60）
                    samples = (1000 * np.sin(2 * np.pi * 1.2 * x) + 60 * np.random.randn(50)).astype(int).tolist()
                    # ↓ 投递完整包：包含 bpm、50 点样本、序号、flags=0、t_mcu=相对毫秒
                    self.q.put(("pkt", (time.time(), bpm, samples, self.seq, 0, int((time.time()-t0)*1000))))
                    self.seq += 1                                     # ← 序号自增

                time.sleep(1.0 / SAMPLE_HZ)                          # ← 控制模拟产出速率（默认 20 Hz），避免占用过多 CPU
'''
为什么要“反序列化”（deserialize）
你从外部进程/设备收到的是一行文本，比如：
{"bpm":72.3,"samples":[...50个数...],"seq":123,"flags":0,"t_mcu":456789}
反序列化就是把这段文本变成 Python 的字典/列表/数字（obj = json.loads(line)），这样你就能直接用：
obj["bpm"] → 浮点数
obj["samples"] → 列表（50 个样本）
obj["seq"] → 整数
好处：安全、可靠、可扩展（以后多一个字段也能直接读），避免“用字符串切割出错”。
seq 是什么
seq = 序号（sequence number），每发一包就 +1。作用：
丢包检测：如果上次是 100，这次不是 101（比如到 103），说明中间掉了两包。
乱序/重复检测：发现回跳或重复，能记录或丢弃。
验证速率：看它是否在 1 秒间隔稳定递增（符合 1 Hz 要求）。
在你的 GUI 里，seq 随包一起被放进队列（"pkt"）；现在主要用于联调与验收（证明“每秒一个固定长度包、无丢包/乱序”），需要时可在日志里检查：            
'''
# ===== Main GUI =====
class App:  # 应用主类：负责窗口、图形、右侧面板、日志、与后台线程的协作
    def __init__(self, root: tk.Tk, backend_cmd: str | None = None):  # root 是 Tk 根窗口；backend_cmd 预留给外部数据源命令
        self.root = root  # 保存根窗口引用
        

        #self.queue = queue.Queue()
        '''
        self.serial_thread = SerialReader('COM#', 115200, self.queue)
        self.serial_thread.start()

        self.root.after(200, self.poll_serial)
        '''
        self.root.title("BMET2922 Pulse GUI")  # 设置窗口标题
        self.root.geometry("1080x680")  # 设置窗口初始大小（宽×高）

        outer = ttk.Frame(root)  # 最外层容器（整页布局外框）
        outer.pack(fill="both", expand=True, padx=12, pady=10)  # 填满窗口，四周留白

        ttk.Label(outer, text="Wearable PPG — Host GUI", font=("Segoe UI", 14, "bold")).pack(anchor="w")  # 顶部标题，靠左

        mid = ttk.Frame(outer)  # 中部容器：承载左右两列
        mid.pack(fill="both", expand=True, pady=(8, 6))  # 竖直方向填满，顶部/底部留白
        left = ttk.Frame(mid)  # 左列：放图形与数值卡片
        left.pack(side="left", fill="both", expand=True)  # 靠左、可随窗口伸缩
        right = ttk.Frame(mid, width=280)  # 右列：报警、按钮、阈值、日志控制
        right.pack(side="left", fill="y", padx=(12, 0))  # 仅竖向填充，与左列留 12px 间距

        # --- Matplotlib figure ---
        self.fig = Figure(figsize=(6, 4), dpi=100)  # 创建 Figure（逻辑画布），设置尺寸与 DPI
        self.ax = self.fig.add_subplot(111)  # 添加 1×1 网格中的第 1 个子图（唯一坐标轴）
        self.ax.set_title("BPM")  # 初始图标题
        self.ax.set_xlabel("Time (s)")  # X 轴标题
        self.ax.set_ylabel("BPM")  # Y 轴标题
        self.canvas = FigureCanvasTkAgg(self.fig, master=left)  # 用 TkAgg 将 Matplotlib 嵌入左列
        self.canvas.get_tk_widget().pack(fill="both", expand=True)  # 作为 Tk 控件加入布局，随左列伸缩

        # Metrics cards（均值/当前值小卡片）
        cards = ttk.Frame(left)  # 放两块数值卡片的容器
        cards.pack(fill="x", pady=(6, 0))  # 仅横向填充，顶部留白
        self.mean_var = tk.StringVar(value="--.-")  # 绑定变量：平均 BPM（默认占位）
        self.curr_var = tk.StringVar(value="--.-")  # 绑定变量：当前 BPM（默认占位）

        def card(parent, title, var):  # 小工厂：生成“标题 + 大号数字”的一行卡片
            f = ttk.Frame(parent)  # 卡片容器
            ttk.Label(f, text=title).pack(side="left", padx=(0, 6))  # 左侧标题
            ttk.Label(f, textvariable=var, font=("Segoe UI", 14, "bold")).pack(side="left")  # 右侧绑定变量显示
            return f  # 返回卡片容器供 pack

        card(cards, "mean", self.mean_var).pack(side="left", padx=(0, 20))  # 放置“mean”卡片，右侧多留 20px 间距
        card(cards, "Current", self.curr_var).pack(side="left")  # 放置“Current”卡片

        # ---- Right panel ----
        ttk.Label(right, text="ALARM", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))  # 右侧报警区标题
        self.dot_high = AlarmDot(right, "High Pulse")  # 高脉率指示灯（默认灰）
        self.dot_high.pack(anchor="w", pady=2)  # 靠左竖排
        self.dot_low = AlarmDot(right, "Low Pulse", "#1d8f2b")  # 低脉率指示灯（默认绿）
        self.dot_low.pack(anchor="w", pady=2)  # 靠左竖排
        self.dot_remote = AlarmDot(right, "Remote", "#1d8f2b")  # 远端/看门狗状态灯（默认绿）
        self.dot_remote.pack(anchor="w", pady=2)  # 靠左竖排

        # Buttons（信息/模式切换/退出）
        self.mode = tk.StringVar(value="BPM")  # 当前视图模式：BPM 或 PULSE
        btns = ttk.Frame(right)  # 按钮排布容器
        btns.pack(fill="x", pady=(8, 4))  # 横向填充，与上方留白
        ttk.Button(btns, text="Info",
                   command=lambda: messagebox.showinfo("Info", "BMET2922 Pulse GUI")).pack(side="left", padx=2)  # 信息弹窗
        ttk.Button(btns, text="BPM",
                   command=lambda: self.mode.set("BPM")).pack(side="left", padx=2)  # 切换到 BPM 视图
        ttk.Button(btns, text="PULSE",
                   command=lambda: self.mode.set("PULSE")).pack(side="left", padx=2)  # 切换到 PULSE 视图
        ttk.Button(btns, text="EXIT", command=self.on_close).pack(...) # 退出程序
       

        # Threshold sliders（阈值滑条）
        thr = ttk.Frame(right)  # 阈值控制容器
        thr.pack(fill="x", pady=(8, 4))  # 横向填充，与上方留白
        ttk.Label(thr, text="Low").grid(row=0, column=0, sticky="w")  # 第 1 行左列：Low 文本
        ttk.Label(thr, text="High").grid(row=1, column=0, sticky="w")  # 第 2 行左列：High 文本
        self.low_var = tk.DoubleVar(value=DEFAULT_LOW)  # 低阈值绑定变量（默认值见全局配置）
        self.high_var = tk.DoubleVar(value=DEFAULT_HIGH)  # 高阈值绑定变量
        s1 = ttk.Scale(thr, from_=30, to=100, variable=self.low_var, orient="horizontal")  # 低阈值滑条（30~100）
        s1.grid(row=0, column=1, sticky="ew", padx=6)  # 放在第 1 行第 2 列，水平方向可拉伸
        s2 = ttk.Scale(thr, from_=60, to=150, variable=self.high_var, orient="horizontal")  # 高阈值滑条（60~150）
        s2.grid(row=1, column=1, sticky="ew", padx=6)  # 放在第 2 行第 2 列，水平方向可拉伸
        thr.columnconfigure(1, weight=1)  # 让第 2 列（滑条）水平自适应拉伸
        ttk.Label(thr, textvariable=self.low_var, width=6).grid(row=0, column=2, sticky="e")  # 低阈值数字显示（右对齐）
        ttk.Label(thr, textvariable=self.high_var, width=6).grid(row=1, column=2, sticky="e")  # 高阈值数字显示（右对齐）

        # Log area（日志区域）
        ttk.Label(outer, text="Log").pack(anchor="w")  # 日志标题
        self.log = tk.Text(outer, height=8)  # Text 组件用作日志窗口
        self.log.pack(fill="both", expand=False)  # 固定高度，不随窗口纵向扩展
        self._log("GUI started")  # 写入第一条日志（含时间戳）

        # Buffers & backend（缓冲与后台线程）
        self.ts, self.bpm = deque(), deque()  # BPM 时间戳与数值缓冲（双端队列，便于裁剪旧数据）
        self.q = queue.Queue()  # 线程安全队列：后台生产、前台消费
        self.backend = BackendThread(self.q, backend_cmd)  # 创建后台线程（无命令→模拟数据；有命令→外部进程）
        self.backend.start()  # 启动后台线程

        # Watchdog & waveform buffers（看门狗与波形缓冲）
        self.last_pkt_ts = 0.0  # 最近一个“包”到达的主机时间（看门狗依据）
        self.miss_alarm_on = False  # 当前是否处于“>5s 无包”报警状态
        self.wave_t, self.wave_y = deque(), deque()  # 波形时间戳与样本缓冲（PULSE 视图使用）
        self._after_poll = None
        self._after_draw = None


        # schedule periodic tasks（定时任务：轮询队列、重绘图形）
        self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)
        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)



        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
    def on_close(self):
        """Graceful shutdown: stop threads, cancel timers, close window."""

        # (0) 告知其它循环“正在关闭”，避免再次续订 after
        self._closing = True

        # (1) 停后台线程（BackendThread）
        try:
            if hasattr(self, "backend") and self.backend is not None:
                self.backend.stop_flag = True
        except Exception:
            pass

        # (2) 停串口线程（如果后来又启用了 SerialReader 才会存在）
        try:
            if hasattr(self, "serial_thread") and self.serial_thread is not None:
                if hasattr(self.serial_thread, "stop"):
                    self.serial_thread.stop()
        except Exception:
            pass

        # (3) 取消 after 定时器（分别取消 poll / draw 两个 id）
        try:
            if hasattr(self, "_after_poll") and self._after_poll:
                self.root.after_cancel(self._after_poll)
                self._after_poll = None
            if hasattr(self, "_after_draw") and self._after_draw:
                self.root.after_cancel(self._after_draw)
                self._after_draw = None
        except Exception:
            pass

        # (4) 退出并销毁窗口
        try:
            self.root.quit()
        except Exception:
            pass
        self.root.destroy()

    # --- logging with exact timestamp format ---
    def _log(self, s: str):  # 写日志：带课程要求的时间戳格式
        ts = time.strftime("%a %b %d %H:%M:%S %Y")  # 示例格式：Thu Sep 19 17:46:50 2024
        self.log.insert("end", f"{ts}: {s}\n")  # 插入到文本末尾
        self.log.see("end")  # 自动滚动到末尾

    def _log_once(self, s: str):  # 只在内容变化时写日志（避免重复刷屏）
        if getattr(self, "_last_alarm", None) != s:  # 比较上一次的内容
            self._last_alarm = s  # 记录为最近一次
            self._log(s)  # 执行一次写入

    # --- queue polling: gather bpm and packets ---
    def _poll_queue(self):
    # 若正在关闭，直接返回，避免再次续订
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                typ, payload = self.q.get_nowait()  # 后台线程投的是 (typ, payload)

                if typ == "log":
                    self._log(payload)

                elif typ == "bpm":
                    t, v = payload
                    self.last_pkt_ts = t
                    self.ts.append(t); self.bpm.append(v)
                    cutoff = time.time() - HISTORY_SECONDS
                    while self.ts and self.ts[0] < cutoff:
                        self.ts.popleft(); self.bpm.popleft()

                elif typ == "pkt":
                    t_host, bpm, samples, seq, flags, t_mcu = payload
                    self.last_pkt_ts = t_host
                    self.ts.append(t_host); self.bpm.append(bpm)

                    base = t_host - 1.0  # 这 50 点代表上一秒
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
        finally:
            self._after_poll = self.root.after(UI_UPDATE_MS, self._poll_queue)

                             

    def process_serial_line(self, line):
        if line.startswith("BPM:"):
            bpm_value = float(line.split(':')[1])
            # update GUI plot with new bpm_value
        elif line.startswith("Recording:"):
            status = line.split(':')[1]
        # update recording indicator or state based on 'On'/'Off'
    # Add more elifs for your specific data types

    # --- periodic redraw ---
    

    def _redraw(self):  # 重绘图形与报警指示
        # Watchdog: 5s without packet -> alarm
        if getattr(self, "_closing", False):
            return
        now = time.time()  # 当前主机时间
        if self.last_pkt_ts and (now - self.last_pkt_ts > 5.0):  # 超过 5s 未收到包
            if not self.miss_alarm_on:  # 状态从“无报警”→“报警”
                self.miss_alarm_on = True
                self._log("No packet for > 5 s")  # 记日志（只记一次）
            self.dot_remote.set_color("#cc2121")  # 远端指示灯：红
        else:  # 收到包或刚恢复
            if self.miss_alarm_on and self.last_pkt_ts:  # 状态从“报警”→“恢复”
                self._log("Packet stream resumed")  # 记恢复日志
            self.miss_alarm_on = False
            self.dot_remote.set_color("#1d8f2b")  # 远端指示灯：绿

        mode = self.mode.get()  # 当前视图模式（BPM / PULSE）

        if mode == "PULSE":  # —— 波形视图 —— #
            if len(self.wave_t) > 2:  # 有足够数据再画
                t0 = self.wave_t[-1]  # 以最新点为 0 秒
                t_rel = np.array(self.wave_t, dtype=float) - t0  # 绝对时间 → 相对时间（右端 0）
                y = np.array(self.wave_y, dtype=float)  # 样本值数组（ADC 计数/幅值）

                self.ax.cla()  # 清空坐标轴
                self.ax.set_title("Pulse waveform")  # 标题
                self.ax.set_xlabel("Time (s)")  # X 轴标题
                self.ax.set_ylabel("ADC")  # Y 轴标题（原始计数/幅度）
                self.ax.plot(t_rel, y, linewidth=1.0)  # 画折线
                self.ax.set_xlim(-HISTORY_SECONDS, 0)  # 固定时间窗口：[-N, 0]

                if self.miss_alarm_on:  # 看门狗报警时在图上叠加提示
                    self.ax.text(0.02, 0.95, "NO PACKET > 5 s",
                                 transform=self.ax.transAxes,
                                 bbox=dict(facecolor='red', alpha=.3))
                self.canvas.draw()  # 重绘画布

            self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)

            return  # 结束本次（不再走 BPM 分支）

        # ---- BPM view ----
        if len(self.ts) > 2:  # 有足够数据再画 BPM
            t0 = self.ts[-1]  # 以最新时间为 0
            t_rel = np.array(self.ts, dtype=float) - t0  # 相对时间数组
            bpm_arr = np.array(self.bpm, dtype=float)  # BPM 数组

            bins = np.arange(-HISTORY_SECONDS, 0 + 1e-9, BAR_BIN_SEC)  # 按秒分箱（1 秒一箱）
            bar_vals, centers = [], []  # 柱子的高度与中心
            for i in range(len(bins) - 1):  # 遍历每个时间箱
                mask = (t_rel >= bins[i]) & (t_rel < bins[i + 1])  # 属于该箱的点
                bar_vals.append(float(np.mean(bpm_arr[mask])) if np.any(mask) else np.nan)  # 有点→均值，无→NaN
                centers.append((bins[i] + bins[i + 1]) / 2)  # 箱中心位置

            self.ax.cla()  # 清空坐标轴
            self.ax.set_title("BPM")  # 标题
            self.ax.set_xlabel("Time (s)"); self.ax.set_ylabel("BPM")  # 轴标题
            self.ax.bar(centers, bar_vals, width=0.8, align="center")  # 画柱状（每秒一根）
            self.ax.plot(t_rel, bpm_arr, linewidth=2.0)  # 画趋势线（高频点）
            self.ax.set_xlim(-HISTORY_SECONDS, 0)  # 固定时间窗口：[-N, 0]

            # cards（更新小卡片数值）
            mean_v = float(np.nanmean(bar_vals)) if np.any(~np.isnan(bar_vals)) else np.nan  # 柱子均值（跳过 NaN）
            curr_v = float(bpm_arr[-1])  # 最新 BPM
            self.mean_var.set(f"{mean_v:.1f}" if not np.isnan(mean_v) else "--.-")  # 平均值（1 位小数或占位）
            self.curr_var.set(f"{curr_v:.1f}")  # 当前值（1 位小数）

            # high/low alarms（阈值报警）
            low = float(self.low_var.get()); high = float(self.high_var.get())  # 读取滑条
            if curr_v < low:  # 低脉：低灯绿，高灯灰
                self.dot_low.set_color("#1d8f2b"); self.dot_high.set_color("#222"); self._log_once("Pulse LOW")
            elif curr_v > high:  # 高脉：高灯红，低灯灰
                self.dot_high.set_color("#cc2121"); self.dot_low.set_color("#222"); self._log_once("Pulse HIGH")
            else:  # 正常：高灯灰，低灯绿
                self.dot_high.set_color("#222"); self.dot_low.set_color("#1d8f2b")

            if self.miss_alarm_on:  # 看门狗报警时叠加提示
                self.ax.text(0.02, 0.95, "NO PACKET > 5 s",
                             transform=self.ax.transAxes,
                             bbox=dict(facecolor='red', alpha=.3))
            self.canvas.draw()  # 重绘

        self._after_draw = self.root.after(UI_UPDATE_MS, self._redraw)#预约下一次重绘

#Additional part
def _addon_ensure_researcher_window(self):
    """
    Create the Researcher toplevel window on-demand (if not already created).
    The window shows:
      - 4 live metrics (RMSSD, SDPPG, latency P95, loss %)
      - A 15 s spectrogram for the latest pulse waveform (Fs=50 Hz)
    """
    if getattr(self, "_researcher_win", None) and self._researcher_win.winfo_exists():
        return  # already created

    # Create a separate top-level window (non-modal)
    self._researcher_win = tk.Toplevel(self.root)
    self._researcher_win.title("Researcher")
    self._researcher_win.geometry("820x520")

    # Metrics row (StringVars were not present in the original App; we define them here)
    top = ttk.Frame(self._researcher_win); top.pack(fill="x", padx=8, pady=6)
    self._rmssd_var  = tk.StringVar(value="RMSSD: --.- ms")
    self._sdppg_var  = tk.StringVar(value="SDPPG: --.-")
    self._latp95_var = tk.StringVar(value="Latency P95: -- ms")
    self._loss_var   = tk.StringVar(value="Loss: 0.0%")
    for var in (self._rmssd_var, self._sdppg_var, self._latp95_var, self._loss_var):
        ttk.Label(top, textvariable=var, font=("Segoe UI", 10)).pack(side="left", padx=(0,16))

    # Matplotlib figure for spectrogram
    self._fig_res = Figure(figsize=(6,4), dpi=100)
    self._ax_res  = self._fig_res.add_subplot(111)
    self._ax_res.set_title("Spectrogram (last 15 s)")
    self._ax_res.set_xlabel("Time bins")
    self._ax_res.set_ylabel("Hz")
    self._canvas_res = FigureCanvasTkAgg(self._fig_res, master=self._researcher_win)
    self._canvas_res.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=4)

# -------------------------- CSV recording controls --------------------------
def _addon_toggle_recording(self):
    """
    Start/stop CSV recording into ./records/session_YYYYmmdd_HHMMSS.csv
    Each "pkt" row stores: t_host, t_mcu_ms, bpm, seq, flags, samples_json
    """
    if not getattr(self, "_recording", False):
        
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.dirname(os.path.abspath(sys.argv[0]))  # 脚本所在目录
        rec_dir = os.path.join(base, "records")
        os.makedirs(rec_dir, exist_ok=True)
        path = os.path.join(rec_dir, f"session_{ts}.csv")
        self._last_csv_path = path
        self._log(f"Recording started -> {os.path.abspath(path)}")
        
        try:
            self._rec_file = open(path, "w", newline="")
            self._rec_writer = csv.writer(self._rec_file)
            self._rec_writer.writerow(["t_host","t_mcu_ms","bpm","seq","flags","samples_json"])
            self._recording = True
            self._last_csv_path = path
            self._log(f"Recording started -> {path}")
        except Exception as e:
            self._log(f"[REC] open failed: {e}")
            self._recording = False
            self._rec_file = None; self._rec_writer = None
    else:
        # Stop
        try:
            if getattr(self, "_rec_file", None):
                self._rec_file.close()
        except Exception:
            pass
        self._recording = False
        self._rec_file = None; self._rec_writer = None
        self._log("Recording stopped")

def _addon_export_summary_pdf(self):
    """
    Export a 2-page PDF (BPM view + Spectrogram view) next to the last CSV, if any.
    This can be triggered manually from menu, or automatically on exit.
    """
    try:
        csv_path = getattr(self, "_last_csv_path", None)
        if not csv_path:
            self._log("[PDF] No CSV session found to summarize")
            return
        pdf_path = csv_path.replace(".csv", ".pdf")
        with PdfPages(pdf_path) as pdf:
            # Page 1: current main figure (BPM or Pulse view)
            try:
                self.canvas.draw()
                pdf.savefig(self.canvas.figure)
            except Exception as e:
                self._log(f"[PDF] main figure save failed: {e}")
            # Page 2: researcher spectrogram (if exists)
            if getattr(self, "_fig_res", None):
                try:
                    self._canvas_res.draw()
                    pdf.savefig(self._fig_res)
                except Exception as e:
                    self._log(f"[PDF] spectrogram save failed: {e}")
        self._log(f"Summary PDF saved -> {pdf_path}")
    except Exception as e:
        self._log(f"[PDF] export failed: {e}")

# -------------------------- Periodic researcher updater --------------------------
def _addon_update_researcher_timer(self):
    """
    Periodically update researcher metrics and spectrogram (if the researcher window exists).
    Scheduled every ~UI_UPDATE_MS; kept lightweight.
    """
    try:
        # Update metrics only if wave/bpm buffers have enough data
        # 1) RMSSD from BPM (PRV proxy): use last ~60s if available
        if len(self.bpm) >= 10:
            bpm_arr = np.array(self.bpm, dtype=float)
            bpm_arr = bpm_arr[bpm_arr > 1.0]               # guard against zeros/NaNs
            if getattr(self, "_rmssd_var", None) and len(bpm_arr) >= 10:
                ibi = 60.0 / bpm_arr                       # inter-beat interval in seconds
                diff = np.diff(ibi)
                rmssd = math.sqrt(np.mean(diff**2)) * 1000.0  # in ms
                self._rmssd_var.set(f"RMSSD: {rmssd:.1f} ms")

        # 2) SDPPG: RMS of 2nd derivative on last 15 s (50 Hz → 750 samples)
        if len(self.wave_y) > 60 and getattr(self, "_sdppg_var", None):
            y = np.array(self.wave_y, dtype=float)[-750:]
            if y.size >= 6:
                d2 = np.diff(y, n=2)
                sdppg = float(np.sqrt(np.mean(d2**2)))
                self._sdppg_var.set(f"SDPPG: {sdppg:.1f}")

        # 3) Latency P95 (ms) from t_host - t_mcu
        if getattr(self, "_lat_ms", None) and len(self._lat_ms) >= 5 and getattr(self, "_latp95_var", None):
            lat = np.array(self._lat_ms, dtype=float)
            p95 = float(np.percentile(lat, 95))
            self._latp95_var.set(f"Latency P95: {p95:.0f} ms")

        # 4) Loss %
        if getattr(self, "_total_pkts", 0) > 0 and getattr(self, "_loss_var", None):
            loss = 100.0 * self._missed / (self._missed + self._total_pkts)
            self._loss_var.set(f"Loss: {loss:.1f}%")

        # 5) Spectrogram: only if researcher window exists
        if getattr(self, "_researcher_win", None) and self._researcher_win.winfo_exists():
            if len(self.wave_y) > 200 and getattr(self, "_ax_res", None):
                y = np.array(self.wave_y, dtype=float)[-750:]  # 15 s window
                self._ax_res.cla()
                self._ax_res.set_title("Spectrogram (last 15 s)")
                self._ax_res.set_xlabel("Time bins"); self._ax_res.set_ylabel("Hz")
                # 128-point window @50 Hz → ~2.56 s; 50% overlap for smoothness
                self._ax_res.specgram(y, NFFT=128, Fs=50.0, noverlap=64, scale='dB')
                self._ax_res.set_ylim(0, 10)   # HR energy ~1–3 Hz; 10 Hz ceiling is enough
                self._canvas_res.draw()
    except Exception as e:
        # Never crash the UI from researcher updates
        try:
            self._log(f"[Researcher] update error: {e}")
        except Exception:
            pass
    finally:
        # Re-schedule itself
        try:
            self.root.after(UI_UPDATE_MS, lambda: _addon_update_researcher_timer(self))
        except Exception:
            pass

# -------------------------- Exit hook (summary-on-exit) --------------------------
def _addon_on_close(self):
    """
    Intercept window close and EXIT button: finalize recording and generate PDF summary.
    """
    # Close CSV if open
    try:
        if getattr(self, "_rec_file", None):
            self._rec_file.close()
            self._rec_file = None
            self._rec_writer = None
    except Exception:
        pass

    # Export PDF summary (optional; only if we recorded a session)
    try:
        _addon_export_summary_pdf(self)
    except Exception:
        pass

    # Stop serial reading thread and any other background work
    try:
        if hasattr(self, "serial_thread"):
            self.serial_thread.stop()
    except Exception:
        pass

    # Destroy root window (this closes the GUI)
    try:
        self.root.destroy()
    except Exception:
        pass


# -------------------------- Non-invasive augmentation of App --------------------------
# Save originals so we can delegate
_App__init_orig = App.__init__
_App__poll_queue_orig = App._poll_queue

def _App__init_addon(self, root: tk.Tk, backend_cmd: str | None = None):
    """
    Wrapper of App.__init__:
    - Call the original __init__ to build your existing UI untouched.
    - Then add menu, researcher/recording state, periodic updater, and close hook.
    """
    _App__init_orig(self, root, backend_cmd)  # build original UI

    # ---- Addon state (new fields; do not exist in original App) ----
    self._lat_ms = _addon_deque(maxlen=600)   # latency ms history (~10 min @1 Hz if you want more, increase)
    self._seq_prev = None
    self._missed = 0
    self._total_pkts = 0

    self._recording = False
    self._rec_file = None
    self._rec_writer = None
    self._last_csv_path = None

    # ---- Menu bar (Tools) ----
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

    # ---- Close hook: both window 'X' and EXIT button will run our on_close ----
    self._root_destroy_orig = self.root.destroy              # keep original destroy
    self.root.protocol("WM_DELETE_WINDOW", lambda: _addon_on_close(self))
    self.root.destroy = lambda: _addon_on_close(self)        # intercept EXIT button (which calls root.destroy)

    # ---- Start periodic researcher updater ----
    self.root.after(UI_UPDATE_MS, lambda: _addon_update_researcher_timer(self))
#
def _App__poll_queue_addon(self):
    """
    Wrapper of App._poll_queue:
    - Replicates original queue-drain logic (log/bpm/pkt) 1:1,
    - PLUS latency/loss stats and optional CSV recording on each "pkt".
    """
    try:
        while True:
            typ, payload = self.q.get_nowait()

            if typ == "log":
                self._log(payload)

            elif typ == "bpm":
                t, v = payload
                self.last_pkt_ts = t
                self.ts.append(t); self.bpm.append(v)
                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()

            elif typ == "pkt":
                t_host, bpm, samples, seq, flags, t_mcu = payload
                self.last_pkt_ts = t_host
                self.ts.append(t_host); self.bpm.append(bpm)

                # Waveform: expand 1 s @ 50 Hz into absolute time
                base = t_host - 1.0
                for i, y in enumerate(samples):
                    self.wave_t.append(base + i/50.0)
                    self.wave_y.append(y)

                # --- ADDON: latency & loss tracking ---
                try:
                    lat = (t_host - (t_mcu/1000.0)) * 1000.0   # ms
                    self._lat_ms.append(max(0.0, float(lat)))
                    if self._seq_prev is not None:
                        gap = (seq - self._seq_prev) % 65536
                        if gap > 1:
                            self._missed += (gap - 1)
                    self._seq_prev = seq
                    self._total_pkts += 1
                except Exception as e:
                    self._log(f"[Researcher] metric error: {e}")

                # --- ADDON: optional CSV recording ---
                try:
                    if getattr(self, "_recording", False) and self._rec_writer:
                        samples_str = "[" + ",".join(str(int(s)) for s in samples) + "]"
                        self._rec_writer.writerow([f"{t_host:.3f}", int(t_mcu), f"{bpm:.3f}", int(seq), int(flags), samples_str])
                except Exception as e:
                    self._log(f"[REC] write failed: {e}")

                # Trim buffers as original
                cutoff = time.time() - HISTORY_SECONDS
                while self.ts and self.ts[0] < cutoff:
                    self.ts.popleft(); self.bpm.popleft()
                while self.wave_t and self.wave_t[0] < cutoff:
                    self.wave_t.popleft(); self.wave_y.popleft()

    except queue.Empty:
        pass
    finally:
        self.root.after(UI_UPDATE_MS, self._poll_queue)  # keep the same scheduling cadence

# Activate the non-invasive augmentation
App.__init__     = _App__init_addon
App._poll_queue  = _App__poll_queue_addon

# ---- Entrypoint ----
def main():  # 程序入口：创建窗口、构造 App、进入事件循环
    root = tk.Tk()  # Tk 根窗口
    try:
        style = ttk.Style(); style.theme_use("clam")  # 尝试使用较现代的 clam 主题（失败则忽略）
    except Exception:
        pass
    app = App(root, backend_cmd=None)  # 创建 App；backend_cmd=None → 使用模拟数据；接真机时改成你的接收器命令
    root.mainloop()  # 进入 Tk 事件循环（阻塞，直到窗口关闭）

if __name__ == "__main__":  # 仅当直接运行该脚本时执行 main（被导入时不执行）
    main()