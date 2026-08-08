# -*- coding: utf-8 -*-
"""
AeroBridge 主窗口类
整合：连接页（首页） → 工作区（侧边栏 + 地图）
两阶段布局：连接前只显示连接页，连接成功后才解锁操作面板
"""
import json
import asyncio
import socket
import os
import threading
from pathlib import Path
from typing import Optional

# 诊断日志：写入 %APPDATA%/AeroBridge/diag.log
_DIAG_LOG = None

def _diag(msg: str) -> None:
    """诊断日志，写入文件便于 GUI 模式调试。"""
    global _DIAG_LOG
    if _DIAG_LOG is None:
        try:
            log_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "AeroBridge"
            log_dir.mkdir(parents=True, exist_ok=True)
            _DIAG_LOG = open(str(log_dir / "diag.log"), "a", encoding="utf-8", buffering=1)
        except Exception:
            _DIAG_LOG = False
            return
    if _DIAG_LOG:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
        _DIAG_LOG.write(f"[{ts}] {msg}\n")
        _DIAG_LOG.flush()

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QStatusBar, QLabel, QPushButton, QMessageBox,
    QStackedWidget, QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

from core.dll_bridge import DLLBridge
from core.transponder_controller import TransponderController
from core.resource_utils import get_asset_path, get_config_path
from core.mock_server import MockServer


class AsyncWorker(QThread):
    """后台线程，运行 asyncio 事件循环，避免阻塞 UI 主线程"""

    _instance = None
    task_ready = pyqtSignal(object)  # coroutine

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = None
        self._running = False
        self.daemon = True
        AsyncWorker._instance = self

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._running = True
        self.task_ready.connect(self._execute_task)
        self._loop.run_forever()

    def _execute_task(self, coro):
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            # 捕获未处理异常，防止静默失败
            def _log_exc(f):
                try:
                    f.result()
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"[AsyncWorker] Unhandled task exception: {e}")
            future.add_done_callback(_log_exc)

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._running = False

    @staticmethod
    def instance():
        return AsyncWorker._instance

    @staticmethod
    def run_async(coro):
        worker = AsyncWorker._instance
        if worker and worker._running:
            worker.task_ready.emit(coro)
        else:
            def _run():
                try:
                    asyncio.run(coro)
                except Exception as e:
                    print(f"Async fallback error: {e}")
            threading.Thread(target=_run, daemon=True).start()


class MainWindow(QMainWindow):
    """
    主窗口 — 两阶段布局
    Page 0: ConnectPage（宽敞连接表单 + 地图）
    Page 1: Workspace（侧边栏面板 + 地图）
    """

    # === 信号定义 ===
    telemetry_updated = pyqtSignal(dict)
    connection_status_changed = pyqtSignal(str, str)
    atc_message_received = pyqtSignal(str, str, str)
    traffic_updated = pyqtSignal(list)
    transponder_status_changed = pyqtSignal(str, str, bool)

    def __init__(self):
        super().__init__()

        self.setWindowTitle("AeroBridge - Aerofly FS 4 联机客户端")
        self.setMinimumSize(1200, 800)

        # === 初始化核心模块 ===
        self.dll_bridge = DLLBridge()
        self.transponder = TransponderController(self.dll_bridge)
        # 应答机状态变化时同步更新状态栏
        self.transponder.on_state_change = self._on_transponder_state_change
        self.fsd_client = None
        self._fsd_generation = 0       # 代际计数器：防止旧 client 的延迟信号污染新 client
        self._user_disconnect = False  # 区分主动断开 vs 意外断开

        # === Mock 服务器（非正版 AF4 的 DLL 替代方案）===
        self._mock_server: Optional[MockServer] = None
        self._mock_enabled = False
        self._last_map_data: Optional[dict] = None  # 节流用：缓存最近一次位置

        # === 初始化 UI ===
        self._init_ui()
        self._init_timers()
        self._connect_signals()

        # === 启动后台异步线程 ===
        self._async_worker = AsyncWorker(self)
        self._async_worker.start()

        # === 连接 DLL ===
        AsyncWorker.run_async(self.dll_bridge.connect())

        # === 加载用户配置 ===
        self._load_settings()

    # ──────────────────────────────────────────────
    # 界面初始化
    # ──────────────────────────────────────────────

    def _init_ui(self):
        """初始化两阶段布局 — QSplitter 可拖拽，字段随窗口自适应"""
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # === QSplitter: 左面板 + 地图，比例可调 ===
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(3)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: #2a2a2a; }")

        # 左侧：QStackedWidget（两页切换）
        self.left_stack = QStackedWidget()
        self.left_stack.setMinimumWidth(340)

        # ── Page 0: 连接页 ──
        from ui.connect_page import ConnectPage
        self.connect_page = ConnectPage()
        self.left_stack.addWidget(self.connect_page)  # index 0

        # ── Page 1: 工作侧边栏 ──
        self.workspace_sidebar = self._create_workspace_sidebar()
        self.left_stack.addWidget(self.workspace_sidebar)  # index 1

        self.left_stack.setCurrentIndex(0)

        # 右侧：地图（两页共享）
        self.map_view = self._create_map_view()

        self.splitter.addWidget(self.left_stack)
        self.splitter.addWidget(self.map_view)
        # stretch: 左面板 2 : 地图 3（窗口缩放时按比例分配）
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)

        outer.addWidget(self.splitter)

        # === 底部状态栏 ===
        self.status_bar = self._create_status_bar()
        self.setStatusBar(self.status_bar)

        # 初始 splitter 比例（等布局完成后设置）
        QTimer.singleShot(50, self._set_connect_split)

    def _set_connect_split(self):
        """连接页模式：左面板占 ~42%，地图占 ~58%"""
        w = self.splitter.width() or self.width()
        self.splitter.setSizes([int(w * 0.42), int(w * 0.58)])

    def _set_workspace_split(self):
        """工作区模式：侧边栏占 ~30%，地图占 ~70%"""
        w = self.splitter.width() or self.width()
        self.splitter.setSizes([int(w * 0.30), int(w * 0.70)])

    def _create_workspace_sidebar(self):
        """创建工作区侧边栏（Page 1）：连接状态 + 应答机 + 飞行计划 + 日志"""
        sidebar = QWidget()
        sidebar.setStyleSheet("background-color: #1a1a1a;")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 连接状态条（紧凑）──
        conn_bar = QFrame()
        conn_bar.setStyleSheet("""
            QFrame#connBar {
                background-color: #262626;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        conn_bar.setObjectName("connBar")
        conn_layout = QVBoxLayout(conn_bar)
        conn_layout.setContentsMargins(12, 10, 12, 10)
        conn_layout.setSpacing(8)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)

        self.conn_status_icon = QLabel("●")
        self.conn_status_icon.setStyleSheet("color: #4CAF50; font-size: 14px;")
        info_row.addWidget(self.conn_status_icon)

        self.conn_status_label = QLabel("已连接")
        self.conn_status_label.setStyleSheet("color: #e0e0e0; font-size: 13px; font-weight: bold;")
        info_row.addWidget(self.conn_status_label)

        info_row.addStretch()

        lbl_srv = QLabel("")
        lbl_srv.setObjectName("connServerLabel")
        lbl_srv.setStyleSheet("color: #777; font-size: 11px;")
        info_row.addWidget(lbl_srv)
        self.conn_server_label = lbl_srv

        conn_layout.addLayout(info_row)

        self.btn_sidebar_disconnect = QPushButton("断开连接")
        self.btn_sidebar_disconnect.setStyleSheet("""
            QPushButton {
                background-color: #c0392b;
                color: white;
                padding: 6px;
                font-size: 12px;
                font-weight: bold;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #e74c3c;
            }
        """)
        self.btn_sidebar_disconnect.clicked.connect(self._on_disconnect)
        conn_layout.addWidget(self.btn_sidebar_disconnect)

        layout.addWidget(conn_bar)

        # ── 应答机面板 ──
        from ui.transponder_panel import TransponderPanel
        self.transponder_panel = TransponderPanel()
        layout.addWidget(self.transponder_panel)

        # ── 飞行计划面板 ──
        from ui.flightplan_panel import FlightPlanPanel
        self.flightplan_panel = FlightPlanPanel()
        layout.addWidget(self.flightplan_panel)

        # ── 通讯日志面板（可拉伸）──
        from ui.log_panel import LogPanel
        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel, stretch=1)

        return sidebar

    def _create_map_view(self):
        """创建地图视图（QWebEngineView 加载 Leaflet）"""
        view = QWebEngineView()

        # 启用本地文件访问远程资源（瓦片服务器）和本地文件
        page = view.page()
        settings = page.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )

        # 页面加载完成后设标志并回放积压的 JS 调用
        self._map_loaded = False
        self._map_pending_ownship = None   # 待回放的 ownship 数据
        self._map_pending_traffic = None   # 待回放的 traffic 数据

        def _on_page_loaded(ok: bool):
            self._map_loaded = True
            _diag("map page loaded, flushing pending JS calls")
            if self._map_pending_ownship:
                self._update_map_ownship(self._map_pending_ownship)
                self._map_pending_ownship = None
            if self._map_pending_traffic:
                self._update_map_traffic(self._map_pending_traffic)
                self._map_pending_traffic = None

        page.loadFinished.connect(_on_page_loaded)

        map_path = get_asset_path("assets/map.html")
        if map_path.exists():
            view.load(QUrl(map_path.as_uri()))
        else:
            view.setHtml(
                "<html><body style='background:#0a0a0a;color:#fff;display:flex;"
                "align-items:center;justify-content:center;'>"
                "<h2>地图文件未找到<br>请确认 assets/map.html 存在</h2></body></html>"
            )
            self._map_loaded = True  # 错误页也算"已加载"
        return view

    def _create_status_bar(self):
        """创建底部状态栏"""
        bar = QStatusBar()
        bar.setFixedHeight(28)
        bar.setStyleSheet(
            "QStatusBar { background-color: #1a1a1a; color: #e0e0e0; font-size: 12px; }"
        )

        self.status_connection = QLabel("● 未连接")
        self.status_connection.setStyleSheet("color: gray; padding: 0 10px;")
        bar.addWidget(self.status_connection)

        sep1 = QLabel("|")
        sep1.setStyleSheet("color: #444; padding: 0 5px;")
        bar.addWidget(sep1)

        self.status_xpdr = QLabel("应答机: STBY 7000")
        self.status_xpdr.setStyleSheet("color: #e0e0e0; padding: 0 5px;")
        bar.addWidget(self.status_xpdr)

        sep2 = QLabel("|")
        sep2.setStyleSheet("color: #444; padding: 0 5px;")
        bar.addWidget(sep2)

        self.status_flight = QLabel("高度: ---ft  地速: ---kts")
        self.status_flight.setStyleSheet("color: #e0e0e0; padding: 0 5px;")
        bar.addWidget(self.status_flight)

        sep3 = QLabel("|")
        sep3.setStyleSheet("color: #444; padding: 0 5px;")
        bar.addWidget(sep3)

        self.status_callsign = QLabel("呼号: ---")
        self.status_callsign.setStyleSheet("color: #ff9800; padding: 0 5px;")
        bar.addWidget(self.status_callsign)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bar.addWidget(spacer)

        self.status_dll = QLabel("DLL: 检测中...")
        self.status_dll.setStyleSheet("color: gray; padding: 0 10px;")
        bar.addWidget(self.status_dll)

        # Mock DLL 开关按钮
        self.btn_mock = QPushButton("模拟DLL")
        self.btn_mock.setFixedHeight(22)
        self.btn_mock.setCheckable(True)
        self.btn_mock.setStyleSheet(
            "QPushButton {"
            "  background-color: #333; color: #888; border: 1px solid #555;"
            "  border-radius: 3px; padding: 0 8px; font-size: 11px;"
            "}"
            "QPushButton:checked {"
            "  background-color: #1b5e20; color: #4CAF50; border-color: #4CAF50;"
            "}"
        )
        self.btn_mock.setToolTip(
            "非正版 AF4 不支持 external DLL API 时，可开启此模拟服务器"
        )
        self.btn_mock.clicked.connect(self._toggle_mock_server)
        bar.addWidget(self.btn_mock)

        return bar

    def _init_timers(self):
        """初始化所有定时器"""
        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self._read_telemetry)
        self.telemetry_timer.start(100)  # 10Hz 遥测读取

        self.map_update_timer = QTimer(self)
        self.map_update_timer.timeout.connect(self._throttled_map_update)
        self.map_update_timer.start(200)  # 5Hz 地图刷新（避免 runJavaScript 过频）

        self.position_report_timer = QTimer(self)
        self.position_report_timer.timeout.connect(self._send_position_report)

        self.sync_check_timer = QTimer(self)
        self.sync_check_timer.timeout.connect(self._check_transponder_sync)

        self.dll_health_timer = QTimer(self)
        self.dll_health_timer.timeout.connect(self._check_dll_health)
        self.dll_health_timer.start(1000)

    # ──────────────────────────────────────────────
    # 信号连接
    # ──────────────────────────────────────────────

    def _connect_signals(self):
        """连接所有信号槽"""
        # 连接页信号
        self.connect_page.connect_clicked.connect(self._on_connect)
        self.connect_page.disconnect_clicked.connect(self._on_disconnect)

        # 应答机面板信号
        self.transponder_panel.mode_changed.connect(self._on_xpdr_mode_change)
        self.transponder_panel.code_changed.connect(self._on_xpdr_code_change)
        self.transponder_panel.ident_clicked.connect(self._on_ident)

        # 飞行计划面板信号
        self.flightplan_panel.flight_plan_submitted.connect(self._on_flight_plan_submit)

        # Callsign + Realname 同步到飞行计划面板（来自连接页）
        self.connect_page.input_callsign_widget.textChanged.connect(
            self.flightplan_panel.set_callsign
        )
        self.connect_page.input_realname_widget.textChanged.connect(
            self.flightplan_panel.set_pilot_name
        )

        # 自身信号
        self.telemetry_updated.connect(self._on_telemetry_update)
        self.connection_status_changed.connect(self._on_connection_status_change)
        self.atc_message_received.connect(self._on_atc_message)
        self.traffic_updated.connect(self._on_traffic_update)
        self.transponder_status_changed.connect(self._on_transponder_status_change)

    # ──────────────────────────────────────────────
    # 连接管理
    # ──────────────────────────────────────────────

    def _on_connect(self):
        """用户点击 Connect（来自 ConnectPage）"""
        config = self.connect_page.get_config()

        if not config["callsign"] or not config["cid"]:
            QMessageBox.warning(self, "配置不完整", "请填写呼号和 CID")
            return

        if not config["server"]:
            QMessageBox.warning(self, "配置不完整", "请填写服务器地址")
            return

        # 重置断连标志
        self._user_disconnect = False

        # 保存配置
        self._save_settings()

        # 递增代际，使旧 client 的延迟信号（如 disconnected）被自动忽略
        self._fsd_generation += 1
        gen = self._fsd_generation

        # 创建 FSD 客户端
        from core.fsd_client import FSDClient
        # 注入 Mock 位置作为 FSD 初始坐标（避免硬编码上海浦东）
        try:
            config["init_lat"] = float(config.get("mock_lat") or "51.4775")
            config["init_lon"] = float(config.get("mock_lon") or "-0.4614")
        except ValueError:
            config["init_lat"] = 51.4775
            config["init_lon"] = -0.4614
        try:
            config["init_alt"] = float(config.get("mock_alt") or "3500")
        except ValueError:
            config["init_alt"] = 3500.0
        self.fsd_client = FSDClient(config)
        self.fsd_client.message_received.connect(self.atc_message_received.emit)
        self.fsd_client.traffic_updated.connect(self.traffic_updated.emit)
        # 代际闭包：旧 client 发出的 status_changed 会被静默丢弃
        self.fsd_client.status_changed.connect(
            lambda s, m, g=gen: self._on_connection_status_change_if_current(s, m, g)
        )
        self.fsd_client.debug_line.connect(
            lambda line: self.log_panel.add_message("FSD", "DEBUG", line)
        )

        # 注入 TransponderController
        self.transponder.fsd_client = self.fsd_client

        # 异步连接（结果由 _on_connection_status_change 处理）
        AsyncWorker.run_async(self.fsd_client.connect())

        # 设置「正在连接...」状态（不切页！等连接成功后再切）
        self.connect_page.set_connecting(True)

        # 启动定时器
        self.position_report_timer.start(1000)  # 1Hz 位置报告（VATSIM/Swift 标准频率，2s 太慢导致地图跳跃）
        self.sync_check_timer.start(5000)

        # 同步飞行计划面板的 Callsign/Pilot
        self.flightplan_panel.set_callsign(config.get("callsign", ""))
        self.flightplan_panel.set_pilot_name(config.get("realname", ""))

        # 更新状态栏
        self.status_connection.setText("● 连接中...")
        self.status_connection.setStyleSheet("color: orange; padding: 0 10px;")

    def _on_disconnect(self):
        """用户点击 Disconnect"""
        self._user_disconnect = True
        # 递增代际，使旧 client 的延迟 disconnected 信号被忽略
        self._fsd_generation += 1

        if self.fsd_client:
            AsyncWorker.run_async(self.fsd_client.disconnect())
            self.fsd_client = None
            self.transponder.fsd_client = None

        self.position_report_timer.stop()
        self.sync_check_timer.stop()

        # 清空飞行计划，避免重连后残留旧数据
        self.flightplan_panel.reset_fields()

        # 更新连接页状态
        self.connect_page.set_connected(False)
        self.connect_page.set_connecting(False)

        # 切换回连接页（Page 0）
        self.left_stack.setCurrentIndex(0)
        self._set_connect_split()

        self.status_connection.setText("● 未连接")
        self.status_connection.setStyleSheet("color: gray; padding: 0 10px;")
        self.status_flight.setText("高度: ---ft  地速: ---kts")
        self.status_callsign.setText("呼号: ---")

    def _cleanup_fsd_connection(self):
        """清理 FSD 连接状态（不尝试断开，因为可能已经断开）"""
        if self.fsd_client:
            self.fsd_client = None
            self.transponder.fsd_client = None
        self.position_report_timer.stop()
        self.sync_check_timer.stop()

    # ──────────────────────────────────────────────
    # 定时器回调
    # ──────────────────────────────────────────────

    def _read_telemetry(self):
        try:
            t = self.dll_bridge.latest_telemetry
            if not t and self.fsd_client:
                # Fallback: 无 DLL 遥测时使用配置的初始位置，确保本地地图也显示飞机
                from core.dll_bridge import Telemetry
                t = Telemetry(
                    lat=self.fsd_client._init_lat,
                    lon=self.fsd_client._init_lon,
                    alt_m=self.fsd_client._init_alt_m,
                    hdg_true=0.0,
                    gs_kts=0.0,
                    on_ground=True,
                )
            if t:
                data = {
                    "lat": t.lat,
                    "lon": t.lon,
                    "alt_m": t.alt_m,
                    "hdg_true": t.hdg_true,
                    "gs_kts": t.gs_kts,
                }
                self.telemetry_updated.emit(data)
                # 诊断：每 10 次打印一次遥测
                if not hasattr(self, "_diag_cnt"):
                    self._diag_cnt = 0
                self._diag_cnt += 1
                if self._diag_cnt % 10 == 0:
                    _diag(f"telemetry_updated #{self._diag_cnt}: lat={t.lat:.4f} lon={t.lon:.4f} alt={t.alt_m:.0f}m gs={t.gs_kts:.0f}kt")
            else:
                if not hasattr(self, "_diag_no_telem"):
                    self._diag_no_telem = 0
                self._diag_no_telem += 1
                if self._diag_no_telem <= 5:
                    _diag(f"_read_telemetry: latest_telemetry=None (#{self._diag_no_telem})")
        except Exception as e:
            _diag(f"_read_telemetry ERROR: {e}")
            import traceback; _diag(traceback.format_exc())

    def _send_position_report(self):
        if not self.fsd_client:
            return

        t = self.dll_bridge.latest_telemetry
        if not t:
            # Fallback: 无 DLL 遥测时使用配置的初始位置继续发送位置报告
            # 确保飞机在连飞地图上显示在配置的位置，而非永远停在希思罗默认坐标
            if not hasattr(self, "_diag_posrpt_no_telem"):
                self._diag_posrpt_no_telem = 0
            self._diag_posrpt_no_telem += 1
            if self._diag_posrpt_no_telem <= 3 or self._diag_posrpt_no_telem % 60 == 0:
                _diag(f"pos_report #{self._diag_posrpt_no_telem}: latest_telemetry=None, "
                      f"using init fallback lat={self.fsd_client._init_lat:.5f} "
                      f"lon={self.fsd_client._init_lon:.5f} "
                      f"alt={self.fsd_client._init_alt_m:.0f}m")
            # 构造 fallback 遥测
            from core.dll_bridge import Telemetry
            t = Telemetry(
                lat=self.fsd_client._init_lat,
                lon=self.fsd_client._init_lon,
                alt_m=self.fsd_client._init_alt_m,
                hdg_true=0.0,
                gs_kts=0.0,
                on_ground=True,
            )

        xpdr_code = self.transponder.get_current_xpdr_for_ap()
        # 诊断：显示应答机实际状态
        if not hasattr(self, "_diag_xpdr_done"):
            self._diag_xpdr_done = 0
        self._diag_xpdr_done += 1
        if self._diag_xpdr_done <= 3 or self._diag_xpdr_done % 60 == 0:
            _diag(f"pos_report #{self._diag_xpdr_done}: xpdr={xpdr_code} "
                  f"mode={self.transponder.virtual_mode.value} squawk={self.transponder.squawk} "
                  f"is_reporting={self.transponder.is_reporting} reporting_enabled={self.fsd_client._reporting_enabled} "
                  f"hdg_true={t.hdg_true:.1f}° lat={t.lat:.4f} lon={t.lon:.4f} alt={t.alt_m:.0f}m gs={t.gs_kts:.0f}kt")
        if xpdr_code == "0000":
            _diag(f"pos_report: xpdr_code=0000 (STBY), skipping "
                  f"(virtual_mode={self.transponder.virtual_mode.value})")
            return

        # 诊断：首次成功发送时打印
        if not hasattr(self, "_diag_posrpt_done"):
            self._diag_posrpt_done = True
            _diag(f"pos_report FIRST: lat={t.lat:.4f} lon={t.lon:.4f} alt={t.alt_m:.0f}m gs={t.gs_kts:.0f}kt hdg={t.hdg_true:.1f} xpdr={xpdr_code}")

        AsyncWorker.run_async(
            self.fsd_client.send_position_report(
                lat=t.lat,
                lon=t.lon,
                alt_ft=t.alt_m * 3.28084,
                gs_kts=t.gs_kts,
                hdg=t.hdg_true,
                xpdr=xpdr_code,
                xpdr_mode=self.transponder.virtual_mode.value
            )
        )

    def _check_transponder_sync(self):
        AsyncWorker.run_async(self._do_sync_check())

    async def _do_sync_check(self):
        result = await self.transponder.sync_check()
        if not result.get("synced", True):
            for warning in result.get("warnings", []):
                self.transponder_panel.show_warning(warning)

    def _check_dll_health(self):
        conn = self.dll_bridge.is_telemetry_connected

        if conn:
            if self._mock_enabled:
                self.status_dll.setText("Mock: ● 已连接")
                self.status_dll.setStyleSheet("color: #4CAF50; padding: 0 10px;")
                self.status_dll.setToolTip(
                    "模拟 DLL 模式 — 遥测数据为模拟飞行\n"
                    "应答机: %s  %s" % (
                        self.transponder.mode.value if self.transponder else "SBY",
                        self.transponder.squawk if self.transponder else "7000",
                    )
                )
            elif self.dll_bridge.latest_telemetry is None:
                # TCP 连接已建立但无遥测数据 — DLL 加载了但 AF4 飞机数据不可用
                self.status_dll.setText("DLL: ⚠ 无数据")
                self.status_dll.setStyleSheet("color: orange; padding: 0 10px;")
                self.status_dll.setToolTip(
                    "DLL 端口 12345 已连接，但未收到遥测数据\n"
                    "可能原因：AF4 飞机未初始化 / 非正版 DLL 兼容问题\n"
                    "建议：1) 重启 AF4  2) 重新进入驾驶舱  3) 点击「模拟DLL」按钮"
                )
            else:
                self.status_dll.setText("DLL: ● 已连接")
                self.status_dll.setStyleSheet("color: green; padding: 0 10px;")
                self.status_dll.setToolTip(
                    "AeroflyBridge.dll 遥测端口 12345 正常工作\n"
                    "应答机: %s  %s" % (
                        self.transponder.mode.value if self.transponder else "SBY",
                        self.transponder.squawk if self.transponder else "7000",
                    )
                )
        elif self._mock_enabled and self._mock_server is not None:
            # Mock 已启动但还没连上（启动中）
            self.status_dll.setText("Mock: ○ 启动中...")
            self.status_dll.setStyleSheet("color: orange; padding: 0 10px;")
            self.status_dll.setToolTip("模拟 DLL 服务器正在启动...")
        else:
            # 主动探测端口状态
            status_msg, tooltip = self._probe_dll_port()
            self.status_dll.setText(status_msg)
            self.status_dll.setStyleSheet("color: gray; padding: 0 10px;")
            self.status_dll.setToolTip(tooltip)

    def _toggle_mock_server(self, checked: bool):
        """开启/关闭内置模拟 DLL 服务器。"""
        if checked:
            self._start_mock_server()
        else:
            self._stop_mock_server()

    def _start_mock_server(self):
        """在 AsyncWorker 线程中启动 Mock 服务器。
        若真实 DLL 已连接，先断开它（端口冲突）。"""
        self._mock_enabled = True

        # 读取用户配置的 Mock 位置，默认希思罗
        config = self.connect_page.get_config()
        try:
            mock_lat = float(config.get("mock_lat") or "51.4775")
        except ValueError:
            mock_lat = 51.4775
        try:
            mock_lon = float(config.get("mock_lon") or "-0.4614")
        except ValueError:
            mock_lon = -0.4614
        try:
            mock_alt = float(config.get("mock_alt") or "3500")
        except ValueError:
            mock_alt = 3500.0

        async def _start():
            try:
                # 先断开真实 DLL 桥接（端口 12345 冲突）
                if self.dll_bridge.is_connected:
                    _diag("mock: disconnecting real DLL bridge first")
                    await self.dll_bridge.disconnect()
                    await asyncio.sleep(0.5)  # 等端口释放

                self._mock_server = MockServer(
                    center_lat=mock_lat,
                    center_lon=mock_lon,
                    alt_m=mock_alt,
                )
                ok = await self._mock_server.start()
                if ok:
                    _diag(f"mock: server started at ({mock_lat:.4f}, {mock_lon:.4f}, {mock_alt:.0f}m)")
                    print(f"[AeroBridge] Mock DLL 服务器已启动（{mock_lat:.4f}, {mock_lon:.4f}, {mock_alt:.0f}m）")
                    # Mock 服务器启动后，连接 DLL bridge 到它
                    await self.dll_bridge.connect()
                else:
                    _diag("mock: server start failed")
                    print("[AeroBridge] Mock DLL 启动失败")
                    self._mock_enabled = False
                    self.btn_mock.setChecked(False)
                    # 重连真实 DLL
                    if not self.dll_bridge.is_connected:
                        AsyncWorker.run_async(self.dll_bridge.connect())
            except Exception as e:
                _diag(f"mock: start exception: {e}")
                import traceback; _diag(traceback.format_exc())
                print(f"[AeroBridge] Mock DLL 启动异常: {e}")
                self._mock_enabled = False
                self.btn_mock.setChecked(False)
                # 重连真实 DLL
                if not self.dll_bridge.is_connected:
                    AsyncWorker.run_async(self.dll_bridge.connect())

        AsyncWorker.run_async(_start())

    def _stop_mock_server(self):
        """停止 Mock 服务器并重连真实 DLL。"""
        self._mock_enabled = False

        async def _stop():
            if self._mock_server:
                # 先断开 DLL bridge（连到 Mock 的）
                if self.dll_bridge.is_connected:
                    await self.dll_bridge.disconnect()
                await self._mock_server.stop()
                self._mock_server = None
                _diag("mock: server stopped")
                print("[AeroBridge] Mock DLL 服务器已停止")
                # 重连真实 DLL
                AsyncWorker.run_async(self.dll_bridge.connect())

        AsyncWorker.run_async(_stop())

    def _probe_dll_port(self):
        """主动探测 DLL 端口 12345 是否可达。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", 12345))
            sock.close()
            # 端口开放但遥测还没建立 → 正在握手
            return (
                "DLL: ○ 握手中...",
                "端口 12345 已开放，正在建立遥测连接...\n"
                "如果持续此状态，请重启 Aerofly FS 4"
            )
        except (ConnectionRefusedError, OSError):
            return (
                "DLL: ● 未连接",
                "端口 12345 未开放 — 请确认：\n"
                "1. 启动 Aerofly FS 4 并进入驾驶舱\n"
                "2. AeroflyBridge.dll 已放入 external_dll 文件夹\n"
                "3. 非正版游戏可能不支持 external DLL API\n"
                "提示: 先启动游戏再启动 AeroBridge"
            )

    # ──────────────────────────────────────────────
    # 信号槽处理
    # ──────────────────────────────────────────────

    def _on_telemetry_update(self, data: dict):
        alt_ft = data.get("alt_m", 0) * 3.28084
        gs = data.get("gs_kts", 0)
        self.status_flight.setText(f"高度: {alt_ft:.0f}ft  地速: {gs:.0f}kts")
        # 缓存位置数据，由 map_update_timer (5Hz) 统一刷新地图
        self._last_map_data = data
        # 诊断：首次收到时打印
        if not hasattr(self, "_diag_map_first"):
            self._diag_map_first = True
            _diag(f"_on_telemetry_update FIRST: lat={data.get('lat'):.4f} lon={data.get('lon'):.4f}")

    def _throttled_map_update(self):
        """节流地图刷新：200ms 调用一次，避免 runJavaScript 过频导致卡顿。"""
        if self._last_map_data:
            if not hasattr(self, "_diag_map_cnt"):
                self._diag_map_cnt = 0
            self._diag_map_cnt += 1
            if self._diag_map_cnt % 25 == 0:  # ~5秒打印一次
                d = self._last_map_data
                _diag(f"map_update #{self._diag_map_cnt}: lat={d.get('lat'):.4f} lon={d.get('lon'):.4f}")
            self._update_map_ownship(self._last_map_data)

    def _on_connection_status_change_if_current(self, status: str, message: str, generation: int):
        """代际过滤：只有当前 client 的 signal 才被处理，旧 client 的延迟事件直接丢弃。

        解决问题：断开后立即重连时，旧 client 的 disconnected 信号延迟到达，
        会把新 client 的 self.fsd_client 也错误置为 None。"""
        if generation != self._fsd_generation:
            return  # 旧 client 的延迟信号，忽略
        self._on_connection_status_change(status, message)

    def _on_connection_status_change(self, status: str, message: str):
        if status == "connected":
            self.status_connection.setText("● 已连接")
            self.status_connection.setStyleSheet("color: green; padding: 0 10px;")
            self.conn_status_icon.setStyleSheet("color: #4CAF50; font-size: 14px;")
            self.conn_status_label.setText("已连接")
            self.conn_status_label.setStyleSheet(
                "color: #e0e0e0; font-size: 13px; font-weight: bold;"
            )
            # 更新连接页状态（隐藏输入框，显示已连接）
            self.connect_page.set_connected(True)
            config = self.connect_page.get_config()
            self.conn_server_label.setText(config.get("server", ""))
            self.status_callsign.setText(f"呼号: {config.get('callsign', '---')}")
            # 切换到工作区（Page 1）
            self.left_stack.setCurrentIndex(1)
            self._set_workspace_split()
            self.log_panel.add_message("AeroBridge", "SYSTEM", message)

        elif status == "disconnected":
            self.status_connection.setText("● 未连接")
            self.status_connection.setStyleSheet("color: gray; padding: 0 10px;")
            self.status_flight.setText("高度: ---ft  地速: ---kts")
            self.status_callsign.setText("呼号: ---")
            # 清理连接状态
            self._cleanup_fsd_connection()
            # 退回连接页
            self.connect_page.set_connected(False)
            self.connect_page.set_connecting(False)
            self.left_stack.setCurrentIndex(0)
            self._set_connect_split()
            self.log_panel.add_message("AeroBridge", "SYSTEM", message)
            # 意外断连（非用户手动点击）→ 弹窗告知原因
            if not self._user_disconnect:
                QMessageBox.warning(self, "连接已断开", f"与服务器的连接意外断开：\n\n{message}")
            self._user_disconnect = False  # 重置标志

        elif status == "error":
            self.status_connection.setText(f"● 错误")
            self.status_connection.setStyleSheet("color: red; padding: 0 10px;")
            self.status_flight.setText("高度: ---ft  地速: ---kts")
            self.status_callsign.setText("呼号: ---")
            # 清理连接状态
            self._cleanup_fsd_connection()
            # 退回连接页
            self.connect_page.set_connected(False)
            self.connect_page.set_connecting(False)
            self.left_stack.setCurrentIndex(0)
            self._set_connect_split()
            self.log_panel.add_message("FSD", "ERROR", message)
            # 弹窗显示错误（模态，关闭后才能重试）
            QMessageBox.critical(self, "连接失败", f"无法连接到服务器：\n\n{message}")

        elif status == "connecting":
            self.status_connection.setText("● 连接中...")
            self.status_connection.setStyleSheet("color: orange; padding: 0 10px;")
            self.log_panel.add_message("AeroBridge", "SYSTEM", message)

    def _on_atc_message(self, source: str, dest: str, message: str):
        self.log_panel.add_message(source, dest, message)

    def _on_traffic_update(self, traffic: list):
        self._update_map_traffic(traffic)
        nearby = [t for t in traffic if t.get("distance_nm", 999) < 10]
        if nearby:
            self.statusBar().showMessage(f"附近 {len(nearby)} 架飞机", 3000)

    def _on_transponder_status_change(self, mode: str, code: str, ident: bool):
        ident_str = " IDENT" if ident else ""
        self.status_xpdr.setText(f"应答机: {mode} {code}{ident_str}")
        self.transponder_panel.update_display(mode, code, ident)

    def _on_xpdr_mode_change(self, mode: str):
        AsyncWorker.run_async(self.transponder.set_mode(mode))

    def _on_xpdr_code_change(self, code: str):
        AsyncWorker.run_async(self.transponder.set_squawk(code))

    def _on_ident(self):
        AsyncWorker.run_async(self.transponder.trigger_ident())

    def _on_transponder_state_change(self, state: dict):
        """应答机状态变化 → 更新底部状态栏"""
        mode = state.get("mode", "STBY")
        code = state.get("squawk", "7000")
        ident = state.get("ident_active", False)
        self.transponder_status_changed.emit(mode, code, ident)

    def _on_flight_plan_submit(self, plan: dict):
        if not self.fsd_client:
            self.flightplan_panel.on_submit_result(False, "未连接到服务器")
            return

        if not plan.get("callsign"):
            plan["callsign"] = self.connect_page.get_config().get("callsign", "")
        if not plan.get("pilot"):
            plan["pilot"] = self.connect_page.get_config().get("realname", "")

        AsyncWorker.run_async(self._do_submit_flight_plan(plan))

    async def _do_submit_flight_plan(self, plan: dict):
        try:
            success = await self.fsd_client.send_flight_plan(plan)
            self.flightplan_panel.on_submit_result(
                success, "" if success else "发送失败"
            )
        except Exception as e:
            self.flightplan_panel.on_submit_result(False, str(e))

    # ──────────────────────────────────────────────
    # 地图交互
    # ──────────────────────────────────────────────

    def _update_map_ownship(self, data: dict):
        if not getattr(self, '_map_loaded', False):
            self._map_pending_ownship = data  # 排队，页面加载完成后回放
            return
        callsign = self.connect_page.get_config().get("callsign", "OWN")

        js = f"""
if (window.updateOwnship) {{
    window.updateOwnship({{
        lat: {data['lat']},
        lon: {data['lon']},
        alt_ft: {data['alt_m'] * 3.28084},
        hdg: {data['hdg_true']},
        gs: {data['gs_kts']},
        callsign: "{callsign}"
    }});
}}
"""
        self.map_view.page().runJavaScript(js)

    def _update_map_traffic(self, traffic: list):
        if not getattr(self, '_map_loaded', False):
            self._map_pending_traffic = traffic  # 排队，页面加载完成后回放
            return
        traffic_json = json.dumps(traffic)
        js = f"""
if (window.updateTraffic) {{
    window.updateTraffic({traffic_json});
}}
"""
        self.map_view.page().runJavaScript(js)

    # ──────────────────────────────────────────────
    # 配置持久化
    # ──────────────────────────────────────────────

    def _load_settings(self):
        """加载用户保存的配置"""
        config_path = get_config_path()
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    settings = json.load(f)
                self.connect_page.load_settings(settings)
                self.flightplan_panel.load_settings(settings)
                self.flightplan_panel.set_callsign(settings.get("callsign", ""))
                self.flightplan_panel.set_pilot_name(settings.get("realname", ""))
            except (json.JSONDecodeError, IOError):
                pass

    def _save_settings(self):
        """保存当前配置"""
        settings = self.connect_page.get_config()
        # 合并飞行计划字段
        fp = self.flightplan_panel.get_flight_plan()
        settings.update({
            "aircraft": fp.get("aircraft", ""),
            "wake_category": fp.get("wake_category", "Medium"),
            "tas": fp.get("tas", ""),
            "dep_airport": fp.get("dep_airport", ""),
            "dest_airport": fp.get("dest_airport", ""),
            "alt_airport": fp.get("alt_airport", ""),
            "cruise_alt": fp.get("cruise_alt", ""),
            "route": fp.get("route", ""),
            "remarks": fp.get("remarks", ""),
            "eet": fp.get("eet", ""),
            "endurance": fp.get("endurance", ""),
        })
        config_path = get_config_path()
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)

    def closeEvent(self, event):
        """关闭窗口时保存配置并清理资源"""
        self._save_settings()
        if self._mock_server:
            self._stop_mock_server()
        if self.fsd_client:
            AsyncWorker.run_async(self.fsd_client.disconnect())
        self._async_worker.stop()
        event.accept()
