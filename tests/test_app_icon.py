"""The same artwork is shipped in each app and packaging format."""

import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_ICON = ROOT / "ticker" / "app" / "static" / "ticker.png"
LINUX_ICON = ROOT / "packaging" / "linux" / "ticker.png"
WINDOWS_ICON = ROOT / "packaging" / "windows" / "ticker.ico"
MACOS_ICON = ROOT / "packaging" / "macos" / "ticker.icns"


def png_size(data):
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_native_browser_and_linux_share_the_same_png():
    assert APP_ICON.read_bytes() == LINUX_ICON.read_bytes()
    assert png_size(APP_ICON.read_bytes()) == (256, 256)


def test_windows_icon_contains_usable_png_sizes():
    data = WINDOWS_ICON.read_bytes()
    reserved, icon_type, count = struct.unpack_from("<HHH", data)
    assert (reserved, icon_type) == (0, 1)
    sizes = set()
    for index in range(count):
        width, height, _, _, _, _, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16)
        width = width or 256
        height = height or 256
        assert png_size(data[offset:offset + length]) == (width, height)
        sizes.add(width)
    assert {16, 32, 48, 256} <= sizes


def test_macos_icon_contains_retina_png_sizes():
    data = MACOS_ICON.read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack_from(">I", data, 4)[0] == len(data)
    kinds = {b"icp4": 16, b"icp5": 32, b"icp6": 64, b"ic07": 128,
             b"ic08": 256, b"ic09": 512, b"ic10": 1024}
    found = set()
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4]
        length = struct.unpack_from(">I", data, offset + 4)[0]
        size = kinds[kind]
        assert png_size(data[offset + 8:offset + length]) == (size, size)
        found.add(size)
        offset += length
    assert offset == len(data)
    assert found == set(kinds.values())


def test_packaging_uses_the_generated_icons():
    from ticker.app.desktop import APP_USER_MODEL_ID

    spec = (ROOT / "packaging" / "ticker.spec").read_text(encoding="utf-8")
    installer = (ROOT / "packaging" / "windows" / "ticker.iss").read_text(encoding="utf-8")
    assert '"windows", "ticker.ico"' in spec
    assert '"macos", "ticker.icns"' in spec
    assert "SetupIconFile=ticker.ico" in installer
    assert installer.count(f'AppUserModelID: "{APP_USER_MODEL_ID}"') == 2
