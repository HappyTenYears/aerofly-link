"""
应答机模块别名 —— 兼容 v1.1 规格书命名。
直接重导出 TransponderController 及所有相关类型。
"""

from .transponder_controller import (
    TransponderController,
    FSDClientProtocol,
    XpdrMode,
    XpdrResult,
    SyncResult,
    SQUAWK_VFR_US,
    SQUAWK_VFR_EU,
    SQUAWK_EMERGENCY,
    SQUAWK_RADIO_FAIL,
    SQUAWK_HIJACK,
)
