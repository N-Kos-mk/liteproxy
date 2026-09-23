"""liteproxy 自身が返す小さなページ（ホーム・エラー・ツールバー）。"""

from __future__ import annotations

import json
from html import escape
from urllib.parse import quote, urlsplit

# スマホの画面情報を Cookie に保存し、次の描画から同じ条件で PC 側に描画させる。
# 表示中のページの viewport 設定に左右されないよう、screen の値を縦向き基準で使う。
_ENV_SCRIPT = (
    "document.cookie='lp_env='+Math.min(screen.width,screen.height)+'_'"
    "+Math.max(screen.width,screen.height)+'_'+devicePixelRatio+'_'"
    "+(matchMedia('(prefers-color-scheme: dark)').matches?1:0)"
    "+';path=/;max-age=31536000;samesite=lax'"
)

_BASE_CSS = (
    ":root{color-scheme:light dark}"
    "body{margin:0;padding:16px;font:16px/1.6 system-ui,sans-serif;max-width:640px}"
    "h1{font-size:18px;margin:0 0 12px}"
    "form{display:flex;gap:8px}"
    "input{flex:1;min-width:0;font:inherit;padding:8px}"
    "button{font:inherit;padding:8px 14px}"
    "p{margin:12px 0}"
)

BAR_CSS = (
    # スクロールしても上端に残す。fixed ではなく sticky なので、ページの内容の位置はずれない
    "lp-bar{all:initial;position:sticky;top:0;z-index:2147483646;display:flex;gap:10px;align-items:center;"
    "padding:3px 8px;"
    "background:#1f2328;color:#d0d7de;font:12px/1.7 system-ui,sans-serif;white-space:nowrap;overflow:hidden}"
    "lp-bar *{all:unset}"
    "lp-bar a,lp-bar button{color:#79c0ff;cursor:pointer}"
    "lp-bar b{font-weight:700;color:#fff}"
    "lp-bar .t{flex:1;overflow:hidden;text-overflow:ellipsis}"
    # hidden 属性だけでは authorstyle の display:flex に負けて消えないため明示する（/app が iframe に埋め込むとき用）
    "lp-bar[hidden]{display:none}"
)


def env_script(nonce: str, *, own: bool = False) -> str:
    return f'<script nonce="{nonce}"{" data-lp-x" if own else ""}>{_ENV_SCRIPT}</script>'


def _page(title: str, body: str, nonce: str, *, head_extra: str = "", body_extra: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_BASE_CSS}</style>{head_extra}</head>"
        f"<body>{body}{env_script(nonce)}{body_extra}</body></html>"
    )


def _address_form(value: str = "") -> str:
    return (
        '<form action="/p"><input name="u" type="search" placeholder="URL または検索語" '
        f'value="{escape(value)}" autocapitalize="off" autocomplete="off" enterkeyhint="go">'
        "<button>開く</button></form>"
    )


def _pwa_head_tags(icon_src: str) -> str:
    return (
        '<link rel="manifest" href="/manifest.json" crossorigin="use-credentials">'
        '<meta name="theme-color" content="#1f2328">'
        f'<link rel="icon" href="{escape(icon_src)}" type="image/png">'
        f'<link rel="apple-touch-icon" href="{escape(icon_src)}">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="liteproxy">'
    )


def _sw_register_script(nonce: str) -> str:
    return (
        f'<script nonce="{nonce}">if("serviceWorker" in navigator)'
        'navigator.serviceWorker.register("/service-worker.js")</script>'
    )


def home(nonce: str, *, icon_src: str) -> str:
    return _page(
        "liteproxy",
        "<h1>liteproxy</h1>" + _address_form(),
        nonce,
        head_extra=_pwa_head_tags(icon_src),
        body_extra=_sw_register_script(nonce),
    )


_SHELL_CSS = (
    ":root{color-scheme:light dark}"
    "html,body{height:100%;margin:0}"
    "body{display:flex;flex-direction:column;font:14px/1.4 system-ui,sans-serif}"
    "#lp-top{flex:0 0 auto;display:flex;gap:8px;align-items:center;padding:6px 8px;"
    "padding-top:calc(6px + env(safe-area-inset-top));background:#1f2328;color:#d0d7de;flex-wrap:wrap}"
    "#lp-top button{font:inherit;padding:6px 10px;border:0;border-radius:6px;background:#30363d;color:#e6edf3}"
    "#lp-top button:disabled{opacity:.4}"
    "#lp-addr{flex:1;display:flex;gap:6px;min-width:120px}"
    "#lp-addr input{flex:1;min-width:0;font:inherit;padding:6px 8px;border:0;border-radius:6px}"
    "#lp-view{flex:1 1 auto;width:100%;border:0}"
)

# 戻る/進むは常に有効にして呼ぶだけにする（iframe の履歴が実際に戻れるかを事前に知る手段がないため）。
# 「画像」「本文/全体」「元」は、iframe 内の（埋め込み時は隠れている）ツールバーの対応する要素を
# プログラム的にクリックすることで実現し、client.js 側は無変更のままにする。
# ツールバーの無いページ（ホームなど）では対象の要素が無いので、その場合はボタンを無効にする。
# 同期はすべて iframe の load イベントで行い、元ページへ移動後など同一オリジンでなくなった場合に
# 参照が例外を投げても、直前の表示のまま保持できるよう必ず try/catch で守る。
_SHELL_JS = """
(function(){
  var frame=document.getElementById('lp-view');
  var back=document.getElementById('lp-back'), fwd=document.getElementById('lp-fwd');
  var addr=document.getElementById('lp-addr-input');
  var imgBtn=document.getElementById('lp-img-btn'), modeBtn=document.getElementById('lp-mode-btn');
  var origBtn=document.getElementById('lp-orig-btn');
  function tap(id){
    try{
      var el=frame.contentDocument.getElementById(id);
      if(el) el.click();
    }catch(e){}
  }
  function sync(){
    try{
      addr.value=frame.contentWindow.location.href;
      document.title=frame.contentDocument.title||'liteproxy';
      var mode=frame.contentDocument.getElementById('lp-mode');
      modeBtn.textContent=mode?mode.textContent:'本文';
      modeBtn.disabled=!mode;
      imgBtn.disabled=!frame.contentDocument.getElementById('lp-img');
      origBtn.disabled=!frame.contentDocument.getElementById('lp-orig');
    }catch(e){
      modeBtn.disabled=imgBtn.disabled=origBtn.disabled=true;
    }
  }
  frame.addEventListener('load', sync);
  back.addEventListener('click', function(){try{frame.contentWindow.history.back()}catch(e){}});
  fwd.addEventListener('click', function(){try{frame.contentWindow.history.forward()}catch(e){}});
  imgBtn.addEventListener('click', function(){tap('lp-img')});
  modeBtn.addEventListener('click', function(){tap('lp-mode')});
  origBtn.addEventListener('click', function(){tap('lp-orig')});
})();
"""


def shell(nonce: str, *, icon_src: str) -> str:
    """/app: 外枠（アドレス欄・戻る/進む・画像/本文切替/元ページ）と、中身を表示する iframe。"""
    body = (
        '<div id="lp-top">'
        '<button type="button" id="lp-back">戻る</button>'
        '<button type="button" id="lp-fwd">進む</button>'
        '<form id="lp-addr" action="/p" target="lp-view">'
        '<input id="lp-addr-input" name="u" type="search" placeholder="URL または検索語" '
        'autocapitalize="off" autocomplete="off" enterkeyhint="go">'
        "<button>開く</button></form>"
        '<button type="button" id="lp-img-btn" title="画像の読み込みと画質の設定">画像</button>'
        '<button type="button" id="lp-mode-btn" title="表示モードを切り替える">本文</button>'
        '<button type="button" id="lp-orig-btn" title="元のページを直接開く（通信量に注意）">元</button>'
        "</div>"
        '<iframe name="lp-view" id="lp-view" src="/" title="liteproxy"></iframe>'
    )
    head_extra = f"<style>{_SHELL_CSS}</style>" + _pwa_head_tags(icon_src)
    body_extra = env_script(nonce) + _sw_register_script(nonce) + f'<script nonce="{nonce}">{_SHELL_JS}</script>'
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>liteproxy</title>{head_extra}</head><body>{body}{body_extra}</body></html>"
    )


def error_page(message: str, nonce: str, *, target: str | None = None) -> str:
    body = f"<h1>開けませんでした</h1><p>{escape(message)}</p>"
    if target:
        body += (
            f'<p><a href="/p?u={quote(target, safe="")}">再試行</a> / '
            f'<a href="{escape(target)}" rel="noreferrer">元のページを直接開く（通信量に注意）</a></p>'
        )
    body += _address_form(target or "")
    return _page("エラー - liteproxy", body, nonce)


def non_html_page(url: str, content_type: str, size_text: str, nonce: str) -> str:
    body = (
        "<h1>HTML ではないファイルです</h1>"
        f"<p>種類: {escape(content_type)}<br>サイズ: {escape(size_text)}</p>"
        f'<p><a href="{escape(url)}" rel="noreferrer">直接開く（通信量に注意）</a></p>'
    ) + _address_form(url)
    return _page("ファイル - liteproxy", body, nonce)


# 読み込み中画面。最初にこの HTML を送り、以後は loader_update() の <script> を追記していく。
# 段階の変化と 2 秒ごとの生存通知が届くため、画面が出ない・通知が途切れる場合は通信側の遅延、
# 段階が進んでいる場合は PC 側の処理中と見分けられる。
_LOADER_CSS = (
    ":root{color-scheme:light dark}"
    "body{margin:0;font:15px/1.6 system-ui,sans-serif}"
    "main{padding:32px 20px;max-width:420px;margin:0 auto}"
    ".sp{width:26px;height:26px;border:3px solid #8884;border-top-color:#3b82f6;border-radius:50%;"
    "animation:r 1s linear infinite}"
    ".sp.x{animation:none;border-color:#dc2626}"
    "@keyframes r{to{transform:rotate(1turn)}}"
    "h1{font-size:17px;margin:14px 0 2px}"
    ".h{color:#888;font-size:13px;overflow-wrap:anywhere}"
    "ol{list-style:none;padding:0;margin:16px 0}"
    "li{position:relative;padding-left:22px;color:#8889}"
    "li::before{content:'';position:absolute;left:3px;top:.5em;width:10px;height:10px;border-radius:50%;"
    "border:2px solid currentColor;box-sizing:border-box}"
    "li.ok{color:#16a34a}li.ok::before{background:currentColor}"
    "li.on{color:inherit;font-weight:600}li.on::before{border-color:#3b82f6;background:#3b82f6}"
    "#w{color:#c2410c;font-size:13px;min-height:1.6em}"
    "a{color:#3b82f6}"
)

_LOADER_JS = """
var S=['fetching','running','scrolling','transforming','sent'],srv=0,base=Date.now(),last=base,mode='run';
function $(i){return document.getElementById(i)}
function kb(n){return n<1024?n+'B':(n/1024).toFixed(1)+'KB'}
function lp(s,ms,x){
 srv=ms;base=last=Date.now();x=x||{};
 var i=S.indexOf(s==='done'?'sent':s),li=$('st').children;
 if(i>=0)for(var k=0;k<li.length;k++)li[k].className=k<i?'ok':k===i?'on':'';
 if(s==='queued')$('t').textContent='順番待ちです（別のページを処理中）';
 else if(s==='done'){mode='send';$('t').textContent='スマホへ転送しています';
  $('w').textContent=x.size?'約'+kb(x.size)+'。回線が遅いと時間がかかります':'';location.replace(x.next)}
 else if(s==='error'){mode='end';$('sp').className='sp x';$('t').textContent='開けませんでした';
  $('w').textContent=x.message;$('rt').hidden=false}
 else $('t').textContent='PCで処理しています';
}
setInterval(function(){
 if(mode==='end')return;
 var now=Date.now();$('m').textContent='経過 '+((srv+now-base)/1000).toFixed(1)+' 秒';
 if(mode==='run'){var q=Math.floor((now-last)/1000);
  $('w').textContent=q>=6?'PCからの応答が '+q+' 秒途切れています。通信が遅延している可能性があります':''}
},200);
"""


def loader_page(target: str, nonce: str) -> str:
    """読み込み中画面の先頭部分。末尾は閉じず、loader_update() を順に追記する。"""
    host = urlsplit(target).hostname or target
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>読み込み中 - {escape(host)}</title><style>{_LOADER_CSS}</style></head><body><main>"
        '<div class="sp" id="sp"></div><h1 id="t">PCで処理しています</h1>'
        f'<div class="h">{escape(target)}</div>'
        '<ol id="st"><li>ページを取得</li><li>スクリプトの実行を待機</li><li>ページ全体を読み込み</li>'
        "<li>軽量化</li><li>スマホへ転送</li></ol>"
        '<div id="m">経過 0.0 秒</div><div id="w"></div>'
        f'<p id="rt" hidden><a href="/p?u={quote(target, safe="")}">再試行</a></p>'
        f'<p><a href="{escape(target)}" rel="noreferrer">元のページを直接開く（通信量に注意）</a></p>'
        f'</main><script nonce="{nonce}">{_LOADER_JS}{_ENV_SCRIPT}</script>'
    )


def loader_update(stage: str, elapsed_ms: int, nonce: str, extra: dict | None = None) -> str:
    args = [stage, elapsed_ms] + ([extra] if extra else [])
    # </script> で要素が閉じられないよう、JSON 中の < をエスケープする
    payload = json.dumps(args, ensure_ascii=False).replace("<", "\\u003c")[1:-1]
    return f'<script nonce="{nonce}">lp({payload})</script>'


def toolbar(
    *,
    title: str,
    url: str,
    sent: str,
    fetched: str,
    nonce: str,
    sized_quality: str | None,
    default_quality: str,
    client_src: str,
    session_id: str | None = None,
    rev: int = 0,
    mode: str = "layout",
    embed: bool = False,
) -> str:
    """client.js へ渡す情報を属性に持たせる。

    sized_quality は画像の枠に表示したサイズの画質、session_id と rev は操作を中継するための
    PC 側のページと HTML の版。liteproxy が足す script には data-lp-x を付け、差分の位置の数え方から外す。
    embed が真のときは /app のシェルが iframe に埋め込んでいるため、バー自体は隠す
    （要素とデータ属性は残し、シェル側 JS がボタンをプログラム的に操作できるようにする）。
    """
    hidden = " hidden" if embed else ""
    return (
        f'<lp-bar{hidden} data-page="{escape(url)}" data-q="{escape(sized_quality or "")}" '
        f'data-dq="{escape(default_quality)}" data-s="{escape(session_id or "")}" data-r="{rev}">'
        f'<a href="/"><b>LP</b></a><span class="t">{escape(title)}</span>'
        f'<span title="送信量（圧縮後の目安） / PC 側の取得量">{escape(sent)} / {escape(fetched)}</span>'
        '<button type="button" id="lp-img" title="画像の読み込みと画質の設定">画像</button>'
        f'<a id="lp-mode" href="/p?u={quote(url, safe="")}&m={"layout" if mode == "reader" else "reader"}" '
        f'title="{"レイアウトを保った表示に切り替える" if mode == "reader" else "本文だけの表示に切り替える"}">'
        f'{"全体" if mode == "reader" else "本文"}</a>'
        f'<a id="lp-orig" href="{escape(url)}" rel="noreferrer" title="元のページを直接開く（通信量に注意）">元</a>'
        f'</lp-bar>{env_script(nonce, own=True)}<script src="{escape(client_src)}" defer data-lp-x></script>'
    )
