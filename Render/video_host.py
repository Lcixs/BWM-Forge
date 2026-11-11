from __future__ import annotations

import atexit
from collections import deque
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests
from config import VIDEO_PUBLISH_TIMEOUT


class SingleVideoServer:
    def __init__(self, video):
        self.video = Path(video).resolve(strict=True)
        if not self.video.is_file() or self.video.suffix.lower() not in ('.mp4', '.mov'):
            raise ValueError('Select a local MP4 or MOV white-model video')
        self.route = '/' + secrets.token_urlsafe(24) + self.video.suffix.lower()
        video_path, route = self.video, self.route

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if urlsplit(self.path).path != route:
                    self.send_error(404)
                    return
                try:
                    with video_path.open('rb') as stream:
                        size = os.fstat(stream.fileno()).st_size
                        start, end = 0, size - 1
                        requested = self.headers.get('Range')
                        if requested:
                            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                            try:
                                if not match or not any(match.groups()):
                                    raise ValueError()
                                first, last = match.groups()
                                if first:
                                    start = int(first)
                                    end = min(int(last), size - 1) if last else size - 1
                                else:
                                    length = int(last)
                                    if length <= 0:
                                        raise ValueError()
                                    start = max(0, size - length)
                                if start > end or start >= size:
                                    raise ValueError()
                            except ValueError:
                                self.send_response(416)
                                self.send_header('Content-Range', f'bytes */{size}')
                                self.send_header('Content-Length', '0')
                                self.end_headers()
                                return
                        self.send_response(206 if requested else 200)
                        self.send_header('Content-Type', 'video/mp4' if video_path.suffix.lower() == '.mp4' else 'video/quicktime')
                        self.send_header('Content-Length', str(end - start + 1))
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('Cache-Control', 'no-store')
                        if requested:
                            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                        self.end_headers()
                        if body:
                            stream.seek(start)
                            remaining = end - start + 1
                            while remaining > 0:
                                chunk = stream.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except FileNotFoundError:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LocalVideoPublisher:
    def __init__(self):
        self.host = None
        self.process = None
        self.url = None
        self.reader = None
        self.signature = None
        atexit.register(self.close)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if self.reader:
                self.reader.join(timeout=2)
            if process.stdout:
                process.stdout.close()
        if self.host:
            self.host.close()
            self.host = None
        self.url = self.signature = None

    def publish(self, video, notify=lambda message: None, timeout=VIDEO_PUBLISH_TIMEOUT):
        video = Path(video).resolve(strict=True)
        signature = (str(video), video.stat().st_size, video.stat().st_mtime_ns)
        if self.signature == signature and self.process and self.process.poll() is None:
            return self.url
        self.close()
        project = Path(__file__).resolve().parent
        candidates = [project / 'tools/cloudflared.exe', project.parent / 'tools/cloudflared.exe']
        binary = next((str(p) for p in candidates if p.is_file()), None) or shutil.which('cloudflared')
        if not binary:
            raise FileNotFoundError('tools/cloudflared.exe is missing; no video generation task was submitted')
        try:
            notify('Starting the single-video file server and public tunnel...')
            self.host = SingleVideoServer(video)
            self.process = subprocess.Popen(
                [binary, 'tunnel', '--no-autoupdate', '--url', self.host.origin],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            process = self.process
            route = self.host.route
            ready = threading.Event()
            found = []
            diagnostics = deque(maxlen=6)
            def detail():
                return '; Cloudflare logs: ' + ' | '.join(diagnostics) if diagnostics else ''

            def read_output():
                for line in process.stdout:
                    is_error = re.search(r'\b(?:ERR|WRN|error|failed|failure|warn)\b', line, re.I)
                    if is_error:
                        diagnostics.append(line.strip()[:600])
                    # 排除日志中的 API 端点，避免误认成隧道地址。
                    match = re.search(r'https://(?!api\.)([a-z0-9-]+)\.trycloudflare\.com(?=[\s|]|$)', line)
                    if match and not is_error and not found:
                        found.append(match.group(0) + route)
                        ready.set()
            self.reader = threading.Thread(target=read_output, daemon=True)
            self.reader.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.reader.join(timeout=1)
                    raise RuntimeError('The Cloudflare tunnel exited; generation was not submitted' + detail())
                if ready.wait(.2):
                    notify('Temporary URL obtained; verifying public video access...')
                    break
            else:
                raise TimeoutError('Unable to obtain a public URL; generation was not submitted' + detail())
            last_probe = 'Accessibility check is not yet complete'
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.reader.join(timeout=1)
                    raise RuntimeError('The Cloudflare tunnel exited; generation was not submitted' + detail())
                try:
                    with requests.get(found[0], headers={'Range': 'bytes=0-0'}, stream=True,
                                      timeout=min(5, max(.1, deadline - time.monotonic())), allow_redirects=False) as response:
                        last_probe = f'HTTP {response.status_code}, Content-Range={response.headers.get("Content-Range", "missing")}'
                        if response.status_code == 206 and response.headers.get('Content-Range') == f'bytes 0-0/{signature[1]}':
                            with video.open('rb') as local:
                                if next(response.iter_content(1), b'') == local.read(1):
                                    self.url, self.signature = found[0], signature
                                    notify('The white-model video is publicly accessible. Only the selected file is exposed.')
                                    return self.url
                                last_probe = 'The initial bytes of the remote video do not match the local file'
                except requests.RequestException as exc:
                    last_probe = type(exc).__name__ + ': ' + str(exc).replace(found[0], '[video URL]').replace(route, '/[video]')[:500]
                time.sleep(.5)
            raise TimeoutError('The public URL is not accessible; generation was not submitted. Last check: ' + last_probe + detail())
        except Exception:
            self.close()
            raise
