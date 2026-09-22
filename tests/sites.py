"""テストで使うローカルのサイト。"""

from __future__ import annotations

import functools
import html
import http.server
import threading
import urllib.parse
from pathlib import Path

# JS で動く UI を並べたページ。PC 側での操作の再現（Phase 3）を確かめる
INTERACTIVE_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>操作</title>
<style>
  .panel { display: none; }
  .panel.open { display: block; color: green; }
  .item { margin: 0; }
  .item.new { font-weight: bold; }
  #cnt { cursor: pointer; }
</style></head><body>
<button id="acc" aria-expanded="false">開閉</button>
<div id="panel" class="panel">パネルの中身</div>
<div id="cnt">0</div>
<ul id="list"><li class="item">既存</li></ul>
<button id="add" type="button">追加</button>
<span id="go" role="button">移動</span>
<span id="spa" role="button">SPA</span>
<a id="blank" href="#">新しいタブ</a>
<a id="real" href="/next.html">通常リンク</a>
<p id="plain">ただの文章</p>
<p id="dyn"></p>
<script>
  const $ = (id) => document.getElementById(id);
  $('acc').addEventListener('click', () => {
    const open = $('panel').classList.toggle('open');
    $('acc').setAttribute('aria-expanded', String(open));
  });
  $('cnt').addEventListener('click', () => { $('cnt').textContent = String(Number($('cnt').textContent) + 1); });
  $('add').onclick = () => {
    const li = document.createElement('li');
    li.className = 'item new';
    li.textContent = '追加した項目';
    $('list').append(li);
  };
  $('go').addEventListener('click', () => { location.href = '/next.html'; });
  $('spa').addEventListener('click', () => { history.pushState({}, '', '/spa-page'); $('dyn').textContent = 'SPA後'; });
  $('blank').addEventListener('click', (e) => { e.preventDefault(); window.open('/next.html', '_blank'); });
</script>
</body></html>
"""

# POST 送信のページ。hidden の値はサイト側（PC 側のページ）が持つトークンに相当する
FORM_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>フォーム</title></head><body>
<form id="f" method="post" action="/echo">
  <input id="q" name="q" value="">
  <input type="hidden" name="token" value="t0ken">
  <textarea id="memo" name="memo"></textarea>
  <label><input id="opt" type="checkbox" name="opt" value="1">選択</label>
  <select id="sel" name="sel"><option value="a">A</option><option value="b">B</option></select>
  <button id="send" name="btn" value="go">送信</button>
</form>
</body></html>
"""

NEXT_PAGE = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>次</title></head><body><p id="next">次のページ</p></body></html>'


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        """受け取った内容をそのまま表示して返す。"""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        pairs = sorted(urllib.parse.parse_qsl(body, keep_blank_values=True))
        text = ";".join(f"{k}={v}" for k, v in pairs)
        page = (
            '<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>受信</title></head>'
            f'<body><p id="got">{html.escape(text)}</p></body></html>'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    # Windows ではレジストリ次第で MIME タイプがずれるため明示する
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html",
        ".css": "text/css",
        ".svg": "image/svg+xml",
    }

    def log_message(self, format, *args):  # noqa: A002
        pass


def serve(directory: Path) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Handler, directory=str(directory))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def write_interactive_site(root: Path) -> None:
    (root / "page.html").write_text(INTERACTIVE_PAGE, encoding="utf-8")
    (root / "next.html").write_text(NEXT_PAGE, encoding="utf-8")
    (root / "form.html").write_text(FORM_PAGE, encoding="utf-8")
