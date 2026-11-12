"""Local review page for the AOI candidates: a small HTTP server on 127.0.0.1 that serves the page and
the evidence images and writes the decisions file, so nothing depends on browser storage.

    .venv/bin/python -m aoi.review_server --result <folder>/aoi_result.json [--port N] [--no-open]

`protocol_run` starts it at the end of a run (flag `--no-open` to suppress). Localhost only, the Host header
is checked against DNS rebinding, writes need a per-run token that is embedded in the page. Decisions go to
`aoi_review_decisions.json` beside the result; Export writes `aoi_result.reviewed.json` (see review_merge).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from polybench.aoi import board_view as BV
from polybench.aoi import review_merge as M

PAGE = Path(__file__).with_name('review_page.html')
INFO_NAME = 'aoi_review_server.json'
IDLE_SECONDS = 4 * 3600


class ReviewApp:
    def __init__(self, result_path, port=0, token=None):
        self.result_path = Path(result_path).resolve()
        if not self.result_path.is_file():
            raise M.ReviewError(f'{self.result_path} does not exist')
        self.folder = self.result_path.parent
        self.decisions_path = self.folder / M.DECISIONS_NAME
        self.token = token or secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.last_request = time.time()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', port), self._handler())
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/?t={self.token}'

    # ---- state
    def state(self):
        document = json.loads(self.result_path.read_text())
        items = M.review_items(document)
        data = M.load_decisions(self.decisions_path, self.result_path)
        bv = BV.load(self.folder)
        M.attach_peers(items)
        order = [b['board'] for b in document['boards']]
        for item in items:
            item['tiles'] = [item['board']] + [b for b in order if b != item['board']]   # tile order of the evidence sheet
            marker = BV.marker_for_id(item['id'], bv) if bv else None
            item['marker'] = marker if marker and item['board'] in bv['boards'] else None
            d = data['decisions'].get(item['key'], {})
            item['decision'], item['operator_note'] = d.get('decision'), d.get('note', '')
            item['image'] = self._image_url(item['evidence'])
            del item['evidence']
        same_run = data.get('result', {}).get('sha256') == M.file_sha(self.result_path)
        return {'tool': document['tool'], 'generated': document['generated'], 'report_ref': document['report_ref'],
                'items': items, 'cursor': data.get('cursor', 0), 'progress': M.progress(items, data),
                'decisions_file': str(self.decisions_path), 'same_run': same_run,
                'boards': [b['board'] for b in document['boards']], 'boardview': bool(bv)}

    def _image_url(self, evidence):
        for e in evidence:
            try:
                rel = Path(e).resolve().relative_to(self.folder)
            except ValueError:
                continue
            return '/file/' + str(rel).replace(os.sep, '/')
        return None

    def _handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'AOIReview/0'

            def log_message(self, *args):
                pass

            def _host_ok(self):
                return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

            def _send(self, code, body, ctype='application/json'):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                app.last_request = time.time()
                if not self._host_ok():
                    return self._send(403, {'error': 'bad host'})
                url = urlparse(self.path)
                if url.path == '/':
                    html = PAGE.read_text().replace("__TOKEN__", app.token).replace("__BASE__", "")
                    return self._send(200, html, 'text/html; charset=utf-8')
                if url.path == '/api/ping':
                    return self._send(200, {'ok': True, 'result': str(app.result_path)})
                if url.path == '/api/state':
                    try:
                        with app.lock:
                            return self._send(200, app.state())
                    except M.ReviewError as error:
                        return self._send(409, {'error': str(error)})
                if url.path == '/api/boardview':
                    bv = BV.load(app.folder)
                    if bv is None:
                        return self._send(404, {'error': 'no board views for this run; run: python -m aoi.board_view --result ' + str(app.result_path)})
                    return self._send(200, bv)
                if url.path.startswith('/file/'):
                    target = (app.folder / url.path[len('/file/'):]).resolve()
                    try:
                        target.relative_to(app.folder)
                    except ValueError:
                        return self._send(403, {'error': 'outside the run folder'})
                    if not target.is_file() or target.suffix.lower() not in ('.jpg', '.jpeg', '.png'):
                        return self._send(404, {'error': 'not found'})
                    return self._send(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
                return self._send(404, {'error': 'not found'})

            def do_POST(self):
                app.last_request = time.time()
                if not self._host_ok():
                    return self._send(403, {'error': 'bad host'})
                if self.headers.get('X-Token') != app.token:
                    return self._send(403, {'error': 'bad token'})
                origin = self.headers.get('Origin')
                if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost'):
                    return self._send(403, {'error': 'bad origin'})
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    body = json.loads(self.rfile.read(length) or b'{}')
                except (ValueError, json.JSONDecodeError):
                    return self._send(400, {'error': 'bad json'})
                path = urlparse(self.path).path
                try:
                    with app.lock:
                        if path == '/api/decision':
                            M.set_decision(app.decisions_path, app.result_path, body.get('key'), body.get('decision'), body.get('note', ''))
                            return self._send(200, {'ok': True, 'progress': app.state()['progress']})
                        if path == '/api/cursor':
                            M.set_cursor(app.decisions_path, app.result_path, body.get('index', 0))
                            return self._send(200, {'ok': True})
                        if path == '/api/export':
                            paths, summary = M.export(app.result_path, app.decisions_path)
                            return self._send(200, {'ok': True, 'paths': paths, 'summary': summary})
                except M.ReviewError as error:
                    return self._send(409, {'error': str(error)})
                return self._send(404, {'error': 'not found'})

        return Handler

    def serve(self, idle_seconds=None, open_browser=False):
        info = self.folder / INFO_NAME
        info.write_text(json.dumps({'pid': os.getpid(), 'port': self.port, 'url': self.url, 'result': str(self.result_path)}))
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(self.url)).start()
        if idle_seconds:
            def watchdog():
                while True:
                    time.sleep(30)
                    if time.time() - self.last_request > idle_seconds:
                        self.httpd.shutdown()
                        return
            threading.Thread(target=watchdog, daemon=True).start()
        try:
            self.httpd.serve_forever()
        finally:
            try:
                info.unlink()
            except OSError:
                pass


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _alive(url_info):
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{url_info["port"]}/api/ping', timeout=1) as r:
            return json.loads(r.read()).get('result') == url_info.get('result')
    except Exception:
        return False


def launch_detached(result_path, open_browser=True, log=print):
    """Start the review server as a separate process (or reuse the running one for this result) and
    return its URL. Honours AOI_NO_OPEN=1 (tests): then nothing is started and None is returned."""
    if os.environ.get('AOI_NO_OPEN') == '1':
        return None
    result_path = Path(result_path).resolve()
    info_path = result_path.parent / INFO_NAME
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text())
            if _alive(info):
                if open_browser:
                    webbrowser.open(info['url'])
                return info['url']
        except (OSError, json.JSONDecodeError):
            pass
    port = _free_port()
    token = secrets.token_urlsafe(16)
    cmd = [sys.executable, '-m', 'aoi.review_server', '--result', str(result_path), '--port', str(port), '--token', token,
           '--idle-seconds', str(IDLE_SECONDS)]
    if not open_browser:
        cmd.append('--no-open')
    root = Path(__file__).resolve().parents[1]
    subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    url = f'http://127.0.0.1:{port}/?t={token}'
    for _ in range(40):
        time.sleep(0.15)
        if _alive({'port': port, 'result': str(result_path)}):
            return url
    log('Review server did not answer within 6 s; start it by hand: python -m aoi.review_server --result ' + str(result_path))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--result', required=True)
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--token')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--idle-seconds', type=int, default=0, help='exit after this long without a request (0: never)')
    args = ap.parse_args(argv)
    try:
        app = ReviewApp(args.result, args.port, args.token)
    except (M.ReviewError, OSError) as error:
        print(f'REFUSED: {error}', file=sys.stderr)
        return 2
    print(f'AOI review page: {app.url}\nDecisions: {app.decisions_path}\nCtrl-C to stop.', flush=True)
    try:
        app.serve(args.idle_seconds or None, open_browser=not args.no_open)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
