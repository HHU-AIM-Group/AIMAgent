#!/usr/bin/env python3
"""Convenient local launcher with an available loopback port and browser opening."""
import argparse
import errno
import sys
import threading
import webbrowser

from app import RadarApp, RadarServer


def main():
    parser = argparse.ArgumentParser(description='启动 AIM Radar 本地工作台')
    parser.add_argument('--no-browser', action='store_true', help='只启动服务，不自动打开浏览器')
    args = parser.parse_args()
    radar = RadarApp()
    server = None
    for port in range(8765, 8786):
        try:
            server = RadarServer(('127.0.0.1', port), radar)
            break
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, 10048):
                raise
    if server is None:
        print('8765–8785 端口均被占用。请关闭旧的 AIM Radar 窗口后重试。', file=sys.stderr)
        return 1
    url = 'http://127.0.0.1:%d' % server.server_port
    print('AIM Radar 已启动：' + url, flush=True)
    print('保留本窗口；按 Ctrl+C 停止服务。', flush=True)
    if not args.no_browser:
        timer = threading.Timer(0.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nAIM Radar 已停止。', flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
