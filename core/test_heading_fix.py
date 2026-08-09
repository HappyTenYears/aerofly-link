"""
验证航向修复：AeroflyBridge 输出(弧度, 数学约定 East=0 逆时针)
→ 罗盘度(North=0 顺时针) → FSD PBH 往返一致。
"""
import asyncio
import math
import sys

from core.dll_bridge import aerofly_heading_to_compass, Telemetry

# 复制 fsd_client 中的 pack_pbh / unpack_pbh（纯函数，避免导入 PyQt6）
RAD = 180.0 / math.pi
PBH_PITCH_MULT = 256.0 / 90.0
PBH_BANK_MULT = 512.0 / 180.0
PBH_HDG_MULT = 1024.0 / 360.0


def pack_pbh(pitch_deg, bank_deg, heading_deg, on_ground):
    p = int(math.floor(pitch_deg * -PBH_PITCH_MULT))
    b = int(math.floor(bank_deg * -PBH_BANK_MULT))
    h = int(heading_deg * PBH_HDG_MULT)
    p &= 0x3FF; b &= 0x3FF; h &= 0x3FF
    og = 1 if on_ground else 0
    return ((p & 0x3FF) << 22) | ((b & 0x3FF) << 12) | ((h & 0x3FF) << 2) | (og << 1)


def unpack_pbh(pbh):
    p_raw = (pbh >> 22) & 0x3FF
    b_raw = (pbh >> 12) & 0x3FF
    h_raw = (pbh >> 2) & 0x3FF
    on_ground = ((pbh >> 1) & 1) == 1
    def s10(v):
        return v - 0x400 if v & 0x200 else v
    pitch_deg = math.floor(s10(p_raw) / -PBH_PITCH_MULT)
    bank_deg = math.floor(s10(b_raw) / -PBH_BANK_MULT)
    heading_deg = h_raw / PBH_HDG_MULT
    return pitch_deg, bank_deg, heading_deg, on_ground


def math_rad_from_compass(compass_deg):
    """真实 DLL 的航向输出：罗盘度 → 数学度(90-H) → 弧度。"""
    return math.radians((90 - compass_deg) % 360)


def main():
    print("compass | DLL(math_rad) | hdg_true(转换后) | PBH | 解码航向 | 误差")
    print("-" * 78)
    all_ok = True
    for compass in [0, 45, 90, 135, 180, 225, 270, 315]:
        frame = {
            "Aircraft.TrueHeading": math_rad_from_compass(compass),
            "Aircraft.MagneticHeading": math_rad_from_compass(compass),
            "Aircraft.Latitude": math.radians(31.14),
            "Aircraft.Longitude": math.radians(121.80),
            "Aircraft.Altitude": 3500.0,
            "Aircraft.GroundSpeed": 70.0,
            "Communication.TransponderCode": 1200,
        }
        t = Telemetry.from_aerofly_bridge(frame)
        hdg_true = t.hdg_true
        pbh = pack_pbh(0.0, 0.0, hdg_true, False)
        _, _, decoded, _ = unpack_pbh(pbh)
        err = abs((decoded - compass + 180) % 360 - 180)
        ok = err < 1.0
        all_ok &= ok
        print(f"{compass:6d}° | {math_rad_from_compass(compass):.4f}     | "
              f"{hdg_true:6.1f}°        | {pbh} | {decoded:6.1f}°   | {err:.2f}{'' if ok else '  <-- FAIL'}")

    # 额外：验证与 Swift 一致的打包（朝向 90° → 期望 PBH 1024）
    pbh90 = pack_pbh(0, 0, 90, False)
    print("-" * 78)
    print(f"朝向90° 期望 PBH=1024, 实际={pbh90}, {'OK' if pbh90 == 1024 else 'FAIL'}")
    all_ok &= (pbh90 == 1024)

    print("-" * 78)
    print("RESULT:", "ALL OK" if all_ok else "FAILURES PRESENT")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
