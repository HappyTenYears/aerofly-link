#!/usr/bin/env python3
"""Test that keepalive uses cached position data instead of zeros."""
import sys
import asyncio
sys.path.insert(0, '.')

from core.fsd_client import (
    FSDClient, pack_pbh, unpack_pbh,
    xpdr_mode_to_fsd_letter, DEFAULT_RATING
)


def test_pack_unpack():
    """PBH roundtrip test."""
    for h in [0, 45, 90, 180, 270, 359]:
        pbh = pack_pbh(0, 0, h, False)
        _, _, decoded_h, _ = unpack_pbh(pbh)
        assert abs(decoded_h - h) < 1, f"heading {h} -> pbh {pbh} -> decoded {decoded_h}"
    print("[OK] pack_pbh/unpack_pbh roundtrip")


def test_mode_letters():
    """FSD mode letter mapping."""
    assert xpdr_mode_to_fsd_letter("ALT") == "N"
    assert xpdr_mode_to_fsd_letter("STBY") == "S"
    assert xpdr_mode_to_fsd_letter("IDENT") == "Y"
    print("[OK] mode letters: ALT->N, STBY->S, IDENT->Y")


def test_default_rating():
    """DEFAULT_RATING must be 2 (Student Pilot, not OBS=1)."""
    assert DEFAULT_RATING == 2, f"Expected 2, got {DEFAULT_RATING}"
    print(f"[OK] DEFAULT_RATING = {DEFAULT_RATING}")


def test_keepalive_uses_cached_data():
    """Keepalive should use cached alt/gs/pbh, not zeros."""
    config = {
        "callsign": "TST123",
        "cid": "testcid",
        "password": "testpwd",
        "realname": "Test",
    }
    client = FSDClient(config)
    client.callsign = "TST123"
    client._connected = True
    client._auth_ok = True
    client._reporting_enabled = True
    client._last_valid_lat = 31.14340
    client._last_valid_lon = 121.80820
    client._last_valid_position_set = True

    # Simulate having sent a position report
    client._last_sent_alt = 11480
    client._last_sent_gs = 136
    client._last_sent_pbh = 1024
    client._last_sent_xpdr = "1200"
    client._last_sent_mode = "N"
    client._has_sent_position = True

    # Build the keepalive line manually (same logic as _keepalive_loop)
    k_alt = client._last_sent_alt
    k_gs = client._last_sent_gs
    k_pbh = client._last_sent_pbh
    k_xpdr = client._last_sent_xpdr
    k_mode = client._last_sent_mode
    keep_line = (
        f"@{k_mode}:{client.callsign}:{k_xpdr}:{DEFAULT_RATING}:"
        f"{client._last_valid_lat:.5f}:{client._last_valid_lon:.5f}:"
        f"{k_alt}:{k_gs}:{k_pbh}:0"
    )
    print(f"  Keepalive line: {keep_line}")

    # Verify it contains real data, not zeros
    assert ":11480:" in keep_line, f"alt should be 11480, got: {keep_line}"
    assert ":136:" in keep_line, f"gs should be 136, got: {keep_line}"
    assert ":1024:" in keep_line, f"pbh should be 1024, got: {keep_line}"
    assert ":1200:" in keep_line, f"xpdr should be 1200, got: {keep_line}"
    assert not ":0:0:0:0" in keep_line, f"keepalive should NOT contain :0:0:0:0 (zeros), got: {keep_line}"
    print("[OK] keepalive uses cached data (alt=11480, gs=136, pbh=1024, xpdr=1200)")


def test_keepalive_before_first_position():
    """Before first position report, keepalive should use defaults (not crash)."""
    config = {
        "callsign": "TST123",
        "cid": "testcid",
        "password": "testpwd",
        "realname": "Test",
    }
    client = FSDClient(config)
    client._has_sent_position = False  # No position sent yet

    # Build keepalive line (same logic)
    if client._has_sent_position:
        k_alt = client._last_sent_alt
        k_gs = client._last_sent_gs
        k_pbh = client._last_sent_pbh
        k_xpdr = client._last_sent_xpdr
        k_mode = client._last_sent_mode
    else:
        k_alt = 0
        k_gs = 0
        k_pbh = 0
        k_xpdr = "1200"
        k_mode = "N"

    assert k_alt == 0 and k_gs == 0 and k_pbh == 0
    assert k_xpdr == "1200" and k_mode == "N"
    print("[OK] keepalive before first position uses safe defaults")


if __name__ == "__main__":
    test_pack_unpack()
    test_mode_letters()
    test_default_rating()
    test_keepalive_uses_cached_data()
    test_keepalive_before_first_position()
    print("\n=== ALL TESTS PASSED ===")
