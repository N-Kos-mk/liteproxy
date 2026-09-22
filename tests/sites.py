"""テストで使うローカルのサイト。"""

from __future__ import annotations

import functools
import html
import http.server
import threading
import urllib.parse
from pathlib import Path

from PIL import Image

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

# 記事のページ。reader モード（本文の抽出）を確かめる
ARTICLE_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本文抽出のテスト記事 | テストサイト</title>
<style>body{font-family:sans-serif}.ad{background:#eee}</style>
</head><body>
<header id="site-header"><nav><a href="/">ホーム</a> <a href="/list.html">記事一覧</a> <a href="/about.html">運営情報</a></nav></header>
<div class="ad" id="ad-top">広告枠です。ここは本文ではありません。クリックで別サイトへ移動します。</div>
<main>
<article>
<h1>本文抽出のテスト記事</h1>
<p class="lead">この記事は、本文だけを抽出する機能を確かめるために用意したものです。導入の段落として、十分な長さの日本語の文章を書いておきます。通信量を抑えるために、本文以外の部分は取り除かれます。</p>
<h2>最初の見出し</h2>
<p>最初の節の本文です。ここでは、抽出した結果に見出しと段落が残ることを確かめます。日本語の文章として自然な長さになるよう、もう少し文章を続けて書いておきます。記事らしい体裁を保つことが目的です。</p>
<p>二つ目の段落では、<a href="/next.html">別のページへのリンク</a>と、<strong>強調した語</strong>を含めます。リンクは中継経由に書き換えられ、強調は残ります。この段落も十分な長さにしておきます。</p>
<figure><img src="/photo.png" alt="写真の説明文" width="400" height="200"><figcaption>図の説明です</figcaption></figure>
<h2>二つ目の見出し</h2>
<ul><li>箇条書きの一つ目です。</li><li>箇条書きの二つ目です。</li><li>箇条書きの三つ目です。</li></ul>
<p>最後の段落です。抽出の対象として十分な文字数になるよう、本文の分量を確保しています。日本語の記事として読める内容になっていれば、判定の条件を満たします。もう少し文章を足しておきます。</p>
</article>
</main>
<aside id="related"><h2>関連記事</h2><ul><li><a href="/other1.html">関連記事その一</a></li><li><a href="/other2.html">関連記事その二</a></li></ul></aside>
<footer id="site-footer"><p>フッターの表記です。著作権表示など。</p></footer>
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
    (root / "article.html").write_text(ARTICLE_PAGE, encoding="utf-8")
    Image.new("RGB", (400, 200), (40, 90, 160)).save(root / "photo.png", "PNG")
