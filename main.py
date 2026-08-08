# -*- coding: utf-8 -*-
"""
AeroBridge - Aerofly FS 4 第三方联机客户端
主入口文件，初始化 Qt 应用和主窗口
"""
import sys
import os
from pathlib import Path

# PyInstaller 打包后资源路径
if getattr(sys, 'frozen', False):
    os.chdir(sys._MEIPASS)

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Windows 下 asyncio 兼容性：使用 SelectorEventLoop
import asyncio
import platform
if platform.system() == 'Windows':
    try:
        from asyncio import WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
    except ImportError:
        pass

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from main_window import MainWindow

# ── 全局异常捕获 ──────────────────────────────────
CRASH_LOG = Path(__file__).parent.parent / "crash.log" if not getattr(sys, 'frozen', False) else Path(os.environ.get("APPDATA", Path.home())) / "AeroBridge" / "crash.log"

def _excepthook(exc_type, exc_value, exc_tb):
    """捕获所有未处理的异常，写入 crash.log"""
    import traceback
    import datetime
    crash_log = CRASH_LOG
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    with open(crash_log, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"CRASH: {datetime.datetime.now().isoformat()}\n")
        f.write(f"Type: {exc_type.__name__}\n")
        f.write(f"Message: {exc_value}\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        f.write(f"{'='*60}\n")
    # 调用默认处理（打印到 stderr）
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook


def main():
    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("AeroBridge")
    app.setApplicationVersion("1.0.0")

    # 全局深色主题样式
    app.setStyleSheet("""
        QMainWindow {
            background-color: #1a1a1a;
        }
        QWidget {
            background-color: #1e1e1e;
            color: #e0e0e0;
            font-size: 13px;
        }
        QGroupBox {
            border: 1px solid #333;
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 14px;
            font-size: 13px;
            font-weight: bold;
            color: #ccc;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #aaa;
        }
        QLabel {
            background-color: transparent;
            color: #ccc;
        }
        QSplitter::handle {
            background-color: #333;
            width: 2px;
        }
        QSplitter::handle:hover {
            background-color: #4CAF50;
        }
        QStatusBar {
            background-color: #1a1a1a;
            color: #e0e0e0;
            border-top: 1px solid #333;
        }
        QScrollBar:vertical {
            background: #1e1e1e;
            width: 10px;
            border-radius: 5px;
        }
        QScrollBar::handle:vertical {
            background: #555;
            border-radius: 5px;
            min-height: 30px;
        }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0;
        }
    """)

    # 创建主窗口
    window = MainWindow()
    window.show()

    # 启动事件循环
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
