# -*- coding: utf-8 -*-
"""
打包启动器（供 PyInstaller 打成单文件 exe 用）。
双击 exe → 启动服务并自动打开浏览器。
"""
import os
import socket
import threading
import time
from pathlib import Path

PORT = int(os.environ.get("PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_browser():
    try:
        time.sleep(1.5)
        import webbrowser
        webbrowser.open(URL)
    except Exception:
        pass


def main():
    print("=" * 58)
    print("  视频 Cut 关键帧截取工具（本地版，视频不会上传到任何服务器）")
    print("=" * 58)
    if port_in_use(PORT):
        print(f"  [提示] 端口 {PORT} 已被占用——工具可能已经在运行。")
        print(f"  请直接打开浏览器访问：{URL}")
        input("  按回车键退出…")
        return
    print(f"  服务已准备就绪：{URL}")
    print("  浏览器将自动打开；使用完毕后关闭本窗口即可退出。\n")

    import server  # noqa: WPS433  路径解析基于打包环境，故延迟导入
    import uvicorn

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
