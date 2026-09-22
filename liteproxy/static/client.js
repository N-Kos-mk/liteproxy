// liteproxy が返したページでスマホ側に読み込む唯一の JS。
// - 画像の枠（img[data-lp-src]）をタップされたときだけ、/i から選んだ画質の画像を読み込む。
// - JS で動く UI（data-lp-t の付いた要素）のタップを PC 側へ送り、返ってきた差分を反映する。
(() => {
  'use strict';
  const bar = document.querySelector('lp-bar');
  if (!bar) return;

  const Q = { low: '低', mid: '中', high: '高', orig: '原本' };
  const page = bar.dataset.page || ''; // 元のページの URL（画像取得時の Referer）
  const sized = bar.dataset.q || ''; // 枠に表示しているサイズの画質
  const session = bar.dataset.s || ''; // PC 側で保持しているページ
  let rev = Number(bar.dataset.r || 0); // この HTML の版
  const cookie = (name) => {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  };
  let quality = Q[cookie('lp_q')] ? cookie('lp_q') : Q[bar.dataset.dq] ? bar.dataset.dq : 'mid';
  const kb = (n) => (n < 1024 ? n + 'B' : (n / 1024).toFixed(n < 10240 ? 1 : 0) + 'KB');

  const style = document.createElement('style');
  style.setAttribute('data-lp-x', '');
  style.textContent =
    'img[data-lp-src]{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none;cursor:pointer}' +
    'img[data-lp-state=loading],[data-lp-busy]{animation:lp-blink 1s ease-in-out infinite}' +
    'img[data-lp-state=error]{outline:2px solid #dc2626;outline-offset:-2px}' +
    '[data-lp-busy]{outline:2px solid #3b82f6;outline-offset:2px}' +
    '@keyframes lp-blink{50%{opacity:.35}}' +
    'lp-sheet{all:initial;position:fixed;left:0;right:0;bottom:0;z-index:2147483647;box-sizing:border-box;' +
    'padding:14px 14px calc(14px + env(safe-area-inset-bottom));background:#1f2328;color:#e6edf3;' +
    'font:14px/1.5 system-ui,sans-serif;border-radius:12px 12px 0 0;box-shadow:0 -4px 16px #0006}' +
    'lp-sheet *{all:unset;box-sizing:border-box}' +
    'lp-sheet b{display:block;font-weight:700;overflow-wrap:anywhere}' +
    'lp-sheet p{display:block;margin:10px 0 6px;color:#9da7b3;font-size:12px}' +
    'lp-sheet div{display:flex;gap:8px;flex-wrap:wrap}' +
    'lp-sheet button{flex:1 1 auto;min-width:56px;padding:10px 12px;border-radius:8px;background:#30363d;' +
    'color:#e6edf3;text-align:center;cursor:pointer}' +
    'lp-sheet button.on{background:#1f6feb;color:#fff}' +
    'lp-sheet button.x{display:block;width:100%;margin-top:12px;background:none;color:#9da7b3}' +
    'lp-toast{all:initial;position:fixed;left:12px;right:12px;bottom:16px;z-index:2147483647;padding:10px 14px;' +
    'border-radius:8px;background:#1f2328;color:#e6edf3;font:13px/1.5 system-ui,sans-serif;' +
    'box-shadow:0 2px 12px #0006}';
  document.head.append(style);

  // ================================================================ 画像

  const loaded = new Map(); // 読み込み済みの画像 data-lp-src → { src, q }。差分で置き換わった枠に戻す

  const pending = () =>
    [...document.querySelectorAll('img[data-lp-src]')].filter((i) => i.dataset.lpState !== 'done');

  // 枠と同じ寸法を保つ。低画質の画像は固有サイズが小さいため、属性がないとレイアウトが変わる
  function keepSize(img) {
    if (img.dataset.lpState !== 'done' && !img.hasAttribute('width') && img.naturalWidth) {
      img.setAttribute('width', img.naturalWidth);
      img.setAttribute('height', img.naturalHeight);
    }
  }

  function load(img, q) {
    if (img.dataset.lpState === 'loading') return;
    keepSize(img);
    const lpSrc = img.dataset.lpSrc;
    const src = '/i?u=' + encodeURIComponent(lpSrc) + '&q=' + q + (page ? '&r=' + encodeURIComponent(page) : '');
    img.dataset.lpState = 'loading';
    // 読み込み中に壊れた画像の表示にならないよう、別の Image で読み込んでから差し替える
    const probe = new Image();
    probe.onload = () => {
      img.src = src;
      img.dataset.lpState = 'done';
      img.dataset.lpQ = q;
      loaded.set(lpSrc, { src, q });
    };
    probe.onerror = () => {
      img.dataset.lpState = 'error';
    };
    probe.src = src;
  }

  // 差分で新しくなった枠のうち、読み込み済みの画像はキャッシュから表示し直す（通信は発生しない）
  function restoreImages(root) {
    const imgs = root.matches && root.matches('img[data-lp-src]') ? [root] : [];
    if (root.querySelectorAll) imgs.push(...root.querySelectorAll('img[data-lp-src]'));
    for (const img of imgs) {
      const hit = loaded.get(img.dataset.lpSrc);
      if (!hit || img.dataset.lpState === 'done') continue;
      keepSize(img);
      img.src = hit.src;
      img.dataset.lpState = 'done';
      img.dataset.lpQ = hit.q;
    }
  }

  // ================================================================ シート（下から出るメニュー）と通知

  function closeSheet() {
    document.querySelectorAll('lp-sheet').forEach((el) => el.remove());
  }

  // groups: [{ note, buttons: [[ラベル, 処理, 選択中か]] }]
  function sheet(title, groups) {
    closeSheet();
    const el = document.createElement('lp-sheet');
    const b = document.createElement('b');
    b.textContent = title;
    el.append(b);
    for (const g of groups) {
      if (g.note) {
        const p = document.createElement('p');
        p.textContent = g.note;
        el.append(p);
      }
      const row = document.createElement('div');
      for (const [text, fn, on] of g.buttons) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = text;
        if (on) btn.className = 'on';
        btn.addEventListener('click', () => {
          closeSheet();
          fn();
        });
        row.append(btn);
      }
      el.append(row);
    }
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'x';
    x.textContent = '閉じる';
    x.addEventListener('click', closeSheet);
    el.append(x);
    document.body.append(el);
  }

  let toastTimer = 0;
  function toast(text) {
    document.querySelectorAll('lp-toast').forEach((el) => el.remove());
    const el = document.createElement('lp-toast');
    el.textContent = text;
    document.body.append(el);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), 4000);
  }

  function imageMenu(img) {
    const size = img.dataset.lpSize && sized ? `${Q[sized]}画質で約${kb(+img.dataset.lpSize)}` : '';
    const current = img.dataset.lpQ || quality;
    sheet(img.alt || 'この画像', [
      {
        note: '画質を選んで読み込む' + (size ? `（${size}）` : ''),
        buttons: Object.keys(Q).map((q) => [Q[q], () => load(img, q), q === current]),
      },
    ]);
  }

  function pageMenu() {
    const rest = pending();
    const known = rest.filter((i) => i.dataset.lpSize);
    const total = known.reduce((s, i) => s + +i.dataset.lpSize, 0);
    const estimate =
      quality === sized && known.length ? `約${kb(total)}${known.length < rest.length ? '以上' : ''}` : 'サイズ不明';
    sheet(`画像（未読み込み ${rest.length} 枚）`, [
      {
        note: `${Q[quality]}画質・${estimate}`,
        buttons: [['すべて読み込む', () => rest.forEach((i) => load(i, quality)), false]],
      },
      {
        note: '既定の画質（タップで読み込むときの画質。枠のサイズ表示は次に開くページから変わる）',
        buttons: Object.keys(Q).map((q) => [
          Q[q],
          () => {
            quality = q;
            document.cookie = 'lp_q=' + q + ';path=/;max-age=31536000;samesite=lax';
          },
          q === quality,
        ]),
      },
    ]);
  }

  const button = document.getElementById('lp-img');
  if (button) button.addEventListener('click', pageMenu);

  // ================================================================ 操作の中継

  // PC 側のミラーと同じ数え方で要素の位置（パス）を求める。liteproxy が足した要素と、
  // 宣言的シャドウ DOM の template は数えない
  const skipped = (el) =>
    el.localName === 'template' || el.localName.startsWith('lp-') || el.hasAttribute('data-lp-x');

  function pathOf(el) {
    const path = [];
    const root = document.documentElement;
    while (el && el !== root) {
      const p = el.parentElement;
      if (!p || skipped(el)) return null;
      let i = 0;
      for (const s of p.children) {
        if (s === el) break;
        if (!skipped(s)) i++;
      }
      path.push(i);
      el = p;
    }
    return el === root ? path.reverse() : null;
  }

  function resolve(path) {
    let el = document.documentElement;
    for (const index of path) {
      let i = 0;
      let next = null;
      for (const c of el.children) {
        if (skipped(c)) continue;
        if (i++ === index) {
          next = c;
          break;
        }
      }
      if (!next) return null;
      el = next;
    }
    return el;
  }

  // スマホ側で付けた属性（画像の読み込み状態など）は、差分で上書きしない
  function setAttrs(el, attrs) {
    const keep = new Set(['data-lp-state', 'data-lp-q', 'data-lp-busy']);
    if (el.dataset.lpState === 'done') {
      if (attrs['data-lp-src'] === el.dataset.lpSrc) ['src', 'width', 'height'].forEach((a) => keep.add(a));
      else ['data-lp-state', 'data-lp-q'].forEach((a) => keep.delete(a)); // 別の画像に変わった
    }
    for (const a of [...el.attributes]) if (!(a.name in attrs) && !keep.has(a.name)) el.removeAttribute(a.name);
    for (const [k, v] of Object.entries(attrs)) if (!keep.has(k) && el.getAttribute(k) !== v) el.setAttribute(k, v);
  }

  function replaceBody(op) {
    const own = [...document.body.children].filter(skipped);
    own.forEach((c) => c.remove());
    document.body.innerHTML = op.h;
    setAttrs(document.body, op.a);
    document.body.prepend(...own);
    restoreImages(document.body);
  }

  // PC 側のミラーと同じ手順（template で解析して差し替え）で反映し、両者の DOM を一致させる
  function apply(data) {
    if (data.css && data.css.length) {
      const st = document.createElement('style');
      st.setAttribute('data-lp-x', '');
      st.textContent = data.css.join('');
      document.head.append(st);
    }
    for (const op of data.ops) {
      if (op.t === 'b') {
        replaceBody(op);
        continue;
      }
      const el = resolve(op.p);
      if (!el) return false;
      if (op.t === 'h') {
        const tpl = document.createElement('template');
        tpl.innerHTML = op.h;
        const next = tpl.content.firstElementChild;
        el.replaceWith(tpl.content);
        if (next) restoreImages(next);
      } else if (op.t === 'a') {
        setAttrs(el, op.a);
        restoreImages(el);
      }
    }
    rev = data.r;
    return true;
  }

  const title = bar.querySelector('.t');
  const titleText = title ? title.textContent : '';
  function status(text) {
    if (title) title.textContent = text || titleText;
  }

  function reloadFresh(message) {
    toast(message);
    location.href = '/p?u=' + encodeURIComponent(page) + '&f=1';
  }

  let queue = Promise.resolve();

  function forward(el) {
    const path = pathOf(el);
    if (!path) return;
    el.setAttribute('data-lp-busy', '');
    queue = queue.then(() => act(path, el)).finally(() => el.removeAttribute('data-lp-busy'));
  }

  async function act(path, el) {
    const started = Date.now();
    const tick = setInterval(() => status(`PCで操作中… ${((Date.now() - started) / 1000).toFixed(1)}秒`), 200);
    status('PCで操作中…');
    try {
      const res = await fetch('/a', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-LP': '1' },
        body: JSON.stringify({ s: session, r: rev, p: path }),
      });
      const data = await res.json();
      if (data.nav) {
        status('ページを移動しています…');
        location.href = data.nav;
        return;
      }
      if (data.expired) return reloadFresh('PC側のページの保持期限が切れたため、読み込み直します');
      if (data.reload) return reloadFresh('ページの状態が変わったため、読み込み直します');
      if (data.error) return toast(data.error);
      if (!apply(data)) return reloadFresh('画面の更新に失敗したため、読み込み直します');
      // ページ内リンクで何も変化しなかった場合は、スマホ側で移動する
      const href = el.getAttribute && el.getAttribute('href');
      if (!data.ops.length && href && href.length > 1 && href.startsWith('#')) location.hash = href;
    } catch {
      toast('PCとの通信に失敗しました');
    } finally {
      clearInterval(tick);
      status('');
    }
  }

  const TEXT_INPUT = /^(?:text|search|email|url|tel|number|password|date|time|datetime-local|month|week|color|range|file)$/;
  const isTextEntry = (el) =>
    el.isContentEditable ||
    el.localName === 'textarea' ||
    el.localName === 'select' ||
    el.localName === 'option' ||
    (el.localName === 'input' && TEXT_INPUT.test((el.getAttribute('type') || 'text').toLowerCase()));

  // 長押しの直後に発生するクリックで、既定の画質の読み込みが始まらないようにする
  let suppressUntil = 0;
  let timer = 0;
  let pressed = false;

  document.addEventListener(
    'click',
    (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (!target || target.closest('lp-sheet, lp-toast, lp-bar')) return;
      if (Date.now() < suppressUntil) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      const img = target.closest('img[data-lp-src]');
      if (img && img.dataset.lpState !== 'done') {
        e.preventDefault();
        e.stopPropagation();
        load(img, quality);
        return;
      }

      // JS で動く UI は PC 側へ送る。通常のリンク（中継経由で開く）と文字入力はスマホ側で扱う
      if (!session || !target.closest('[data-lp-t]') || isTextEntry(target)) return;
      const link = target.closest('a[href]');
      if (link && !link.hasAttribute('data-lp-t')) return;
      // 開閉やチェックなど、JS がなくても動くものはスマホ側でもそのまま反応させる
      const native = target.closest('summary, label, input[type=checkbox], input[type=radio]');
      if (!native) {
        e.preventDefault();
        e.stopPropagation();
      }
      forward(target);
    },
    true,
  );

  // iOS は長押しで contextmenu が発生しないため、タッチの継続時間で判定する
  document.addEventListener(
    'touchstart',
    (e) => {
      const img = e.target instanceof Element ? e.target.closest('img[data-lp-src]') : null;
      if (!img || e.touches.length > 1) return;
      pressed = false;
      timer = setTimeout(() => {
        pressed = true;
        suppressUntil = Infinity;
        imageMenu(img);
      }, 500);
    },
    { passive: true },
  );
  const release = () => {
    clearTimeout(timer);
    if (pressed) {
      pressed = false;
      suppressUntil = Date.now() + 500;
    }
  };
  document.addEventListener('touchend', release, { passive: true });
  document.addEventListener('touchcancel', release, { passive: true });
  document.addEventListener('touchmove', () => clearTimeout(timer), { passive: true });

  // Android の長押しと PC の右クリック
  document.addEventListener('contextmenu', (e) => {
    const img = e.target instanceof Element ? e.target.closest('img[data-lp-src]') : null;
    if (!img) return;
    e.preventDefault();
    if (!document.querySelector('lp-sheet')) imageMenu(img);
  });
})();
