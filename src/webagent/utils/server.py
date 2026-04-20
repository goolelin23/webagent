"""
简易的本地 HTTP 服务器工具
用于挂载和托管本地前端代码或 ZIP 压缩包，以便 WebPilot 自主探索
"""
import os
import zipfile
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler
import socketserver
import time
from webagent.utils.logger import get_logger

logger = get_logger("webagent.utils.server")

_current_server_thread = None
_current_httpd = None

def get_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

def host_local_directory(path: str) -> str:
    """挂载指定的本地目录或ZIP文件，并启动 HTTP 后台服务返回 URL"""
    global _current_server_thread, _current_httpd

    path = os.path.expanduser(path)

    # 如果是 ZIP 文件，先解压到临时目录
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        temp_dir = tempfile.mkdtemp(prefix="webpilot_hosted_")
        logger.info(f"正在解压 {path} 到 {temp_dir}")
        with zipfile.ZipFile(path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        serve_dir = temp_dir
    else:
        serve_dir = path
        if not os.path.isdir(serve_dir):
            raise ValueError(f"指定的路径不存在或不是目录/ZIP文件: {path}")

    # 停止之前的服务器
    if _current_httpd is not None:
        try:
            _current_httpd.shutdown()
            _current_httpd.server_close()
            if _current_server_thread:
                _current_server_thread.join(timeout=2)
        except Exception as e:
            logger.debug(f"停止前置服务出错: {e}")

    port = get_free_port()
    
    class WebPilotRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)

        def log_message(self, format, *args):
            # 禁用多余日志输出
            pass

    _current_httpd = socketserver.TCPServer(("127.0.0.1", port), WebPilotRequestHandler)
    
    def run_server():
        try:
            _current_httpd.serve_forever()
        except Exception:
            pass

    _current_server_thread = threading.Thread(target=run_server, daemon=True)
    _current_server_thread.start()
    
    # 稍微等一下确保启动
    time.sleep(0.2)

    return f"http://127.0.0.1:{port}"
