"""本地小程序：python server.py → 浏览器打开 http://127.0.0.1:8765
粘贴抖音/小红书分享文字 → 解析 → 下载无水印版到 D:\\vibe\\xiazai
"""
import hashlib
import hmac
import io
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

import core

HERE = os.path.dirname(os.path.abspath(__file__))
# 云端模式（Dockerfile 里设 NOWM_CLOUD=1）：文件只临时存放，下完让手机保存，定时清理
CLOUD = bool(os.environ.get("NOWM_CLOUD"))
OUT = os.environ.get("NOWM_OUT") or ("/tmp/nowm" if CLOUD else r"D:\vibe\xiazai")
PORT = int(os.environ.get("PORT") or 8765)
KEY = os.environ.get("NOWM_KEY", "")  # 设了就要输密码才能用（防止别人蹭你的服务器）
KEEP_SECONDS = 2 * 3600
BUSY = threading.Semaphore(2)  # 免费云主机内存小，最多同时跑 2 个任务


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not KEY:
            return True
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "nowm_k" in c and hmac.compare_digest(c["nowm_k"].value, key_token())

    def do_HEAD(self):  # 云平台健康检查
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path in ("/favicon.png", "/icon.png"):
            with open(os.path.join(HERE, self.path[1:]), "rb") as f:
                return self._send(200, f.read(), "image/png")
        if self.path == "/api/info":
            return self._send(200, {"lan": "" if CLOUD else lan_url(), "local": self._is_local() and not CLOUD,
                                    "cloud": CLOUD, "needKey": bool(KEY), "authed": self._authed()})
        if not self._authed():
            return self._send(401, {"error": "请先输入密码"})
        if self.path.startswith("/files/"):
            return self._file()
        if self.path == "/qr.svg" and not CLOUD:
            import segno
            buf = io.BytesIO()
            segno.make(lan_url(), error="m").save(buf, kind="svg", scale=6, border=2,
                                                 dark="#000", light="#fff")
            return self._send(200, buf.getvalue(), "image/svg+xml")
        if self.path.startswith("/api/job/"):
            job = JOBS.get(self.path[9:])
            if not job:
                return self._send(404, {"error": "任务不存在"})
            with LOCK:
                return self._send(200, dict(job))
        if self.path == "/open":
            if CLOUD or not self._is_local():
                return self._send(403, {"error": "只能在电脑上打开"})
            os.startfile(OUT)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _file(self):
        """流式发送文件，支持 Range（iPhone Safari 播放视频必须）；?dl=1 时作为附件下载。"""
        path, _, qs = self.path[7:].partition("?")
        name = os.path.basename(unquote(path))
        p = os.path.join(OUT, name)
        if not name or not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        ext = p.rsplit(".", 1)[-1].lower()
        ctype = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime",
                 "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                 "gif": "image/gif"}.get(ext, "application/octet-stream")
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # bytes=-N 末尾 N 字节
                start = max(size - int(m.group(2)), 0)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if "dl=1" in qs:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, BrokenPipeError):
            pass  # 手机拖动进度条会主动断开

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 100_000:
            return self._send(413, {"error": "内容太长"})
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/login":
            if KEY and hmac.compare_digest(str(req.get("key", "")), KEY):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"nowm_k={key_token()}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(body)
                return
            time.sleep(1)  # 减慢猜密码
            return self._send(403, {"error": "密码不对"})
        if not self._authed():
            return self._send(401, {"error": "请先输入密码"})
        try:
            if self.path == "/api/parse":
                return self._send(200, core.parse(req.get("text", "")))
            if self.path == "/api/start":
                jid = uuid.uuid4().hex[:12]
                JOBS[jid] = {"stage": "parse", "pct": 0, "file": 0, "files": 0,
                             "done": 0, "total": 0, "speed": 0, "t": time.time()}
                threading.Thread(target=run_job, args=(jid, req.get("text", "")), daemon=True).start()
                return self._send(200, {"id": jid})
            if self.path == "/api/download":
                info = core.parse(req.get("text", ""))
                saved = core.download(info, OUT)
                return self._send(200, {**info, "saved": saved,
                                        "files": ["/files/" + quote(os.path.basename(p)) for p in saved]})
        except core.ParseError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # 网络等
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})


JOBS = {}
LOCK = threading.Lock()


def run_job(jid, text):
    """后台解析+下载，进度写进 JOBS[jid]，前端轮询 /api/job/<id>。"""
    job = JOBS[jid]
    t0 = [time.time(), 0]  # 当前文件开始时间、上次汇总字节

    def progress(i, n, done, total, frac=None, note=""):
        now = time.time()
        if done == 0 or done < job.get("done", 0) or i + 1 != job.get("file"):
            t0[0] = now  # 新文件 / 新分段：速度重新计时
        if frac is None:
            frac = done / total if total else 0
        with LOCK:
            job.update(stage="download", file=i + 1, files=n, done=done, total=total, note=note,
                       speed=done / max(now - t0[0], 1e-3) if done else 0,
                       known=bool(total) or frac > 0,
                       pct=round(100 * (i + frac) / n, 1))

    try:
        with BUSY:
            info = core.parse(text)
            with LOCK:
                job.update(stage="download", files=len(info["urls"]))
            saved = core.download(info, OUT, progress)
        with LOCK:
            job.update(stage="done", pct=100, result={
                **info, "saved": saved,
                "files": ["/files/" + quote(os.path.basename(p)) for p in saved]})
    except core.ParseError as e:
        with LOCK:
            job.update(stage="error", error=str(e))
    except Exception as e:
        with LOCK:
            job.update(stage="error", error=f"{type(e).__name__}: {e}")


def lan_ip():
    """本机局域网 IP（不真正发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_url():
    return f"http://{lan_ip()}:{PORT}"


def key_token():
    return hashlib.sha256(("nowm:" + KEY).encode()).hexdigest()


def janitor():
    """云端：删掉 2 小时前的文件和任务，免得磁盘塞满。"""
    while True:
        time.sleep(600)
        cut = time.time() - KEEP_SECONDS
        for name in os.listdir(OUT):
            p = os.path.join(OUT, name)
            try:
                if os.path.getmtime(p) < cut:
                    os.remove(p)
            except OSError:
                pass
        with LOCK:
            for jid in [j for j, v in JOBS.items() if v.get("t", 0) < cut]:
                JOBS.pop(jid, None)


def main():
    os.makedirs(OUT, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    if CLOUD:
        threading.Thread(target=janitor, daemon=True).start()
        print(f"云端模式：端口 {PORT}，临时目录 {OUT}，密码{'已设置' if KEY else '未设置（任何人都能用）'}", flush=True)
        return srv.serve_forever()
    url = f"http://127.0.0.1:{PORT}"
    print(f"已启动：电脑 {url}")
    print(f"        手机（连同一个 Wi-Fi）浏览器打开 {lan_url()}  或扫网页右上角二维码")
    print(f"        下载目录：{OUT}   （关掉这个窗口即退出）")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
