"""
验证真实 send_position_report 代码路径：传入罗盘度 hdg，确认 @ 报文的 PBH 朝向正确。
直接调用 fsd_client.send_position_report（monkeypatch _send_line 抓取报文），不走网络握手。
"""
import asyncio
import sys

sys.path.insert(0, ".")

from core.fsd_client import FSDClient, unpack_pbh


async def main():
    captured = []

    fsd = FSDClient({"callsign": "TST123", "cid": "1", "password": "1",
                     "server": "x", "port": 6809, "eco": "private", "type": "legacy"})

    # 模拟已连接/已认证/上报开启
    fsd._connected = True
    fsd._auth_ok = True
    fsd._reporting_enabled = True

    async def fake_send(line):
        captured.append(line)

    fsd._send_line = fake_send  # monkeypatch

    # 直接调用真实方法（与 main_window._send_position_report 传参一致）
    await fsd.send_position_report(
        lat=31.1434, lon=121.8082, alt_ft=11480, gs_kts=136,
        hdg=90.0, xpdr="1200", xpdr_mode="ALT",
    )

    assert captured, "未生成 @ 报文"
    line = captured[0]
    print("captured @:", line)
    fields = line.split(":")
    pbh = int(fields[8])
    _, _, hdg_decoded, _ = unpack_pbh(pbh)
    print(f"PBH={pbh} 解码朝向={hdg_decoded:.1f}° 期望=90.0°")
    ok = abs((hdg_decoded - 90.0 + 180) % 360 - 180) < 1.0
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
