"""
端到端验证：真实 FSDClient + DLLBridge + mock DLL(math约定) + mock FSD 服务器。
抓取实际 @ 报文，解码 PBH 朝向，与 mock 的罗盘朝向对比。
"""
import asyncio
import json
import math
import sys

sys.path.insert(0, ".")

from core.dll_bridge import DLLBridge
from core.fsd_client import FSDClient, unpack_pbh

# 复制 pack_pbh 常量用于解码（unpack_pbh 已可解码 heading）
PBH_HDG_MULT = 1024.0 / 360.0


def decode_heading(pbh):
    _, _, h, _ = unpack_pbh(pbh)
    return h  # 罗盘度 (0=North, 顺时针)


async def mock_fsd_server(received: list, ready: asyncio.Event):
    async def handler(reader, writer):
        # 发送服务器问候 ($DI) + 欢迎 (#TM)
        writer.write(b"$DI:SERVER:SWIFT-FSD:1.0:0:0\r\n")
        await writer.drain()
        writer.write(b"#TMWelcome to mock FSD\r\n")
        await writer.drain()
        ready.set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode().rstrip("\r\n")
                received.append(text)
                # 简单响应 $CQ:$PI
                if text.startswith("$CQ"):
                    writer.write(b"$CR\r\n")
                    await writer.drain()
                elif text.startswith("$PI"):
                    writer.write(b"$PO\r\n")
                    await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 6809)
    async with server:
        await asyncio.Future()


async def main():
    received = []
    ready = asyncio.Event()

    # 启动 mock FSD
    fsd_task = asyncio.create_task(mock_fsd_server(received, ready))

    # 启动 mock DLL（math 约定，已在 mock_dll_server.py 修正）
    import core.mock_dll_server as m
    import argparse
    args = argparse.Namespace(host="127.0.0.1", telemetry_port=12345, command_port=12346,
                               airport="ZSPD", lat=None, lon=None)
    # 直接调用 main 内的 server 启动较繁，改为复用模块函数
    state = m.MockAFS4State(31.1434, 121.8082)
    dll_t = asyncio.create_task(asyncio.start_server(
        lambda r, w: m.handle_telemetry_client(r, w, state), args.host, args.telemetry_port))
    dll_c = asyncio.create_task(asyncio.start_server(
        lambda r, w: m.handle_command_client(r, w, state), args.host, args.command_port))

    await asyncio.sleep(0.5)

    # 连接 DLLBridge
    bridge = DLLBridge(host="127.0.0.1", telemetry_port=12345, command_port=12346)
    await bridge.connect(retry=False)
    await asyncio.sleep(0.5)
    telem = await bridge.get_telemetry(timeout=5.0)
    assert telem is not None, "未收到遥测"
    print(f"[mock DLL] raw TrueHeading={state.get_telemetry().get('Aircraft.TrueHeading'):.4f} "
          f"-> hdg_true(compass)={telem.hdg_true:.1f}°")

    # 连接 FSDClient 到 mock FSD
    fsd = FSDClient({
        "callsign": "TST123", "cid": "1", "password": "1", "server": "127.0.0.1",
        "port": 6809, "eco": "private", "type": "legacy", "model": "B738",
    })
    ok = await fsd.connect()
    print(f"[FSDClient] connect ok={ok}")
    await ready.wait()
    await asyncio.sleep(1.0)

    # 驱动一次位置报告（复制 main_window 的调用）
    await fsd.send_position_report(
        lat=telem.lat, lon=telem.lon, alt_ft=telem.alt_m * 3.28084,
        gs_kts=telem.gs_kts, hdg=telem.hdg_true, xpdr="1200", xpdr_mode="ALT",
    )
    await asyncio.sleep(0.5)

    # 找 @ 报文
    at_lines = [l for l in received if l.startswith("@")]
    print(f"[mock FSD] 收到 @ 报文数={len(at_lines)}")
    ok_all = True
    for l in at_lines:
        fields = l.split(":")
        pbh = int(fields[8])
        decoded = decode_heading(pbh)
        expected = telem.hdg_true
        err = abs((decoded - expected + 180) % 360 - 180)
        status = "OK" if err < 1.0 else "FAIL"
        ok_all &= (err < 1.0)
        print(f"  {l}")
        print(f"  -> PBH={pbh} 解码朝向={decoded:.1f}°  期望(罗盘)={expected:.1f}°  误差={err:.2f} {status}")

    # 清理
    await fsd.disconnect()
    await bridge.disconnect()
    fsd_task.cancel()
    dll_t.cancel(); dll_c.cancel()

    print("RESULT:", "ALL OK" if ok_all and at_lines else "FAIL")
    return 0 if (ok_all and at_lines) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
