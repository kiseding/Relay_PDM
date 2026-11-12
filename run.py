"""Relay PDM · 启动脚本

用法：
    python run.py                 # 默认 127.0.0.1:8000，只本机能访问
    python run.py --lan           # 0.0.0.0:8000，同局域网同事也能打开
    python run.py --port 9000     # 换端口
    python run.py --no-reload     # 关掉热重载（改代码不自动重启）
"""
import argparse
import os
import socket
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parent


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    ap = argparse.ArgumentParser(description="Relay PDM")
    ap.add_argument("--lan", action="store_true", help="对局域网开放")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default=None)
    ap.add_argument("--no-reload", action="store_true")
    args = ap.parse_args()

    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    if not (Path(__file__).parent / "app" / "main.py").exists():
        sys.exit("请在项目根目录运行：python run.py")

    print("\n  Relay PDM")
    print("  " + "-" * 46)
    print(f"  本机打开      http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"  同事访问      http://{lan_ip()}:{args.port}")
    print(f"  账号          admin / admin123     （其余演示账号 123456）")
    print("  " + "-" * 46)
    print("  Ctrl+C 停止\n")

    uvicorn.run("app.main:app", host=host, port=args.port,
                reload=not args.no_reload, reload_dirs=[str(BASE / "app")])


if __name__ == "__main__":
    main()
