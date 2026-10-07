#!/usr/bin/env python3
"""手机当键盘：手机网页输入 -> 局域网 WebSocket -> 电脑光标处注入按键。

用法:
    python server.py                 # 默认 0.0.0.0:8765
    python server.py --port 9000
    python server.py --token mypass  # 固定 token, 不用每次重开
"""

import argparse
import ctypes
import json
import re
import secrets
import socket
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from aiohttp import web

if sys.platform != "win32":
    print("本脚本目前只支持 Windows。")
    sys.exit(1)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# --------------------------------------------------------------------------
# Win32 SendInput
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def _ki(vk=0, scan=0, flags=0):
    """构造一个键盘 INPUT 事件。"""
    kb = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=kb))


def _send(*items):
    if not items:
        return
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), ctypes.byref(arr), ctypes.sizeof(INPUT))


# 虚拟键码表
VK = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "space": 0x20, "spacebar": 0x20,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "capslock": 0x14,
}
# 这些键在 Windows 上属于扩展键, 必须带 EXTENDED 标志
EXTENDED = {"delete", "left", "up", "right", "down", "home", "end",
            "pageup", "pagedown", "insert"}
MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
        "win": 0x5B, "meta": 0x5B}


def _resolve_key(name):
    """把名字解析成 (vk, flags)。支持 VK 名 / 单字符 / VkKeyScan 兜底。"""
    n = str(name).lower()
    if n in VK:
        return VK[n], (KEYEVENTF_EXTENDEDKEY if n in EXTENDED else 0)
    if len(n) == 1 and n.isalnum():
        return ord(n.upper()), 0
    if len(n) == 1:
        res = user32.VkKeyScanW(ctypes.c_wchar(n))
        if res != -1:
            return res & 0xFF, 0
    raise KeyError(name)


def press_key(name, count=1):
    """按下并松开某个键，count 次。"""
    vk, ext = _resolve_key(name)
    count = max(1, min(int(count), 200))
    for _ in range(count):
        _send(_ki(vk=vk, flags=ext),
              _ki(vk=vk, flags=ext | KEYEVENTF_KEYUP))


def send_combo(mods, key):
    """组合键，例如 Ctrl+S。"""
    vk, ext = _resolve_key(key)
    down, up = [], []
    for m in mods:
        mvk = MODS.get(str(m).lower())
        if mvk is None:
            continue
        down.append(_ki(vk=mvk))
        up.append(_ki(vk=mvk, flags=KEYEVENTF_KEYUP))
    down.append(_ki(vk=vk, flags=ext))
    up.append(_ki(vk=vk, flags=ext | KEYEVENTF_KEYUP))
    _send(*(down + up))


def _type_unicode_char(ch):
    """用 KEYEVENTF_UNICODE 注入任意字符（含 emoji 的代理对拆解）。"""
    code = ord(ch)
    if code > 0xFFFF:
        code -= 0x10000
        units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    else:
        units = [code]
    for u in units:
        _send(_ki(scan=u, flags=KEYEVENTF_UNICODE),
              _ki(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))


def type_text(text):
    """注入一段文本。回车/制表符走真实按键，其余走 Unicode。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in text:
        if ch == "\n":
            press_key("enter")
        elif ch == "\t":
            press_key("tab")
        else:
            _type_unicode_char(ch)


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------
clients = set()
TOKEN = ""


async def handle_index(request):
    if request.query.get("t") != TOKEN:
        return web.Response(status=403, text="token 无效，请用启动时打印的完整链接打开。")
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_ws(request):
    if request.query.get("t") != TOKEN:
        return web.Response(status=403, text="token invalid")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    await ws.prepare(request)
    clients.add(ws)
    peer = request.remote
    print(f"[+] 已连接: {peer}  (在线 {len(clients)})")

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            try:
                handle_message(data)
            except Exception as exc:  # 单个消息出错不应断开连接
                print(f"[!] 处理失败 {data!r}: {exc}")
    finally:
        clients.discard(ws)
        print(f"[-] 断开: {peer}  (在线 {len(clients)})")

    return ws


def handle_message(data):
    kind = data.get("type")

    if kind == "text":
        payload = data.get("data") or ""
        if isinstance(payload, str) and payload:
            type_text(payload)

    elif kind == "key":
        key = data.get("key")
        if key:
            press_key(key, data.get("count", 1))

    elif kind == "combo":
        key = data.get("key")
        mods = data.get("mods") or []
        if key:
            send_combo(mods, key)


def _usable(ip):
    return not ip.startswith("127.") and not ip.startswith("169.254.")


def _default_route_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _ipconfig_ips():
    """解析 ipconfig，能拿到 getaddrinfo 漏掉的网卡地址（如移动热点的 192.168.137.1）。"""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = out.decode("gbk", errors="ignore")
    ips = []
    for m in re.finditer(r"IPv4[^:：]*[:：]\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text):
        ip = m.group(1)
        if _usable(ip) and ip not in ips:
            ips.append(ip)
    return ips


# 这些网段是虚拟机/本机虚拟网卡，手机访问不到
VIRTUAL_HINTS = ("192.168.144.", "192.168.56.", "192.168.122.",
                 "192.168.230.", "172.17.", "172.18.", "172.19.")


def _rank(ip):
    """越小越可能是手机能访问到的地址。"""
    if ip == "192.168.137.1":      # Windows 移动热点 / ICS 默认网关
        return 0
    if ip.startswith(VIRTUAL_HINTS):
        return 5
    if ip.startswith("192.168."):
        return 1
    if ip.startswith("172."):
        return 2
    if ip.startswith("10."):
        return 3
    return 4


def local_ips():
    """返回按「手机可达概率」排序的局域网 IPv4 列表。"""
    primary = _default_route_ip()

    found = []
    for ip in ([primary] if primary else []) + _ipconfig_ips():
        if ip and _usable(ip) and ip not in found:
            found.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _usable(ip) and ip not in found:
                found.append(ip)
    except OSError:
        pass

    # 排序：移动热点 > 当前联网网卡 > 其它（虚拟网卡垫底）
    found.sort(key=lambda ip: (0 if ip == primary else 1, _rank(ip)))
    if "192.168.137.1" in found:
        found.remove("192.168.137.1")
        found.insert(0, "192.168.137.1")
    return found


def print_qr(url):
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main():
    global TOKEN

    parser = argparse.ArgumentParser(description="手机当键盘（手机打字，电脑出字）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--token", default=None, help="固定访问 token（默认随机生成）")
    parser.add_argument("--ip", default=None, help="强制指定要显示的电脑 IP")
    args = parser.parse_args()

    TOKEN = args.token or secrets.token_urlsafe(6)

    ips = [args.ip] if args.ip else local_ips()
    if not ips:
        ips = ["127.0.0.1"]
    primary = ips[0]
    url = f"http://{primary}:{args.port}/?t={TOKEN}"

    print("=" * 60)
    print("  手机当键盘 - 服务已启动")
    print("=" * 60)
    print("  电脑自检（先在电脑浏览器里打开，能出页面说明服务正常）:")
    print(f"     http://127.0.0.1:{args.port}/?t={TOKEN}")
    print()
    print("  手机浏览器打开下面这个地址:")
    print(f"     {url}")
    print()
    if len(ips) > 1:
        print("  本机其它网卡地址（上面那个不通就挨个试）:")
        for ip in ips[1:]:
            print(f"     http://{ip}:{args.port}/?t={TOKEN}")
        print()
    print("  手机连不上？按顺序检查:")
    print("    1) 手机和电脑是否在同一网段（校园网/公共 Wi-Fi 常有客户端隔离，")
    print("       同 SSID 也互相不通，此时改用 Windows 移动热点最稳）")
    print("    2) 网络配置文件是「公用」时入站会被拦（见下方命令）")
    print("    3) 防火墙放行端口（见下方命令）")
    print()
    print("  管理员 PowerShell 执行一次即可（端口不同请自行修改）:")
    print(f'    netsh advfirewall firewall add rule name="PhoneKeyboard" '
          f"dir=in action=allow protocol=TCP localport={args.port}")
    print("    Set-NetConnectionProfile -NetworkCategory Private")
    print("=" * 60)
    print()
    try:
        print_qr(url)
    except Exception as exc:
        # 终端编码不支持二维码字符时不要让整个服务起不来
        print(f"  (二维码显示失败: {exc}，直接用上面的链接即可)")
    print()

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    # 静态资源（style.css / script.js）。不含敏感信息，因此不校验 token
    app.router.add_static("/static/", STATIC_DIR, name="static")

    try:
        web.run_app(app, host=args.host, port=args.port, print=None)
    except OSError as exc:
        print(f"启动失败: {exc}\n端口可能被占用，试试 --port 8766")
        sys.exit(1)


if __name__ == "__main__":
    main()
