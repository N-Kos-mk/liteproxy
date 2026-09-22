// liteproxy が返したページでスマホ側に読み込む唯一の JS。
// 画像の枠（img[data-lp-src]）をタップされたときだけ、/i から選んだ画質の画像を読み込む。
(() => {
  'use strict';
  const bar = document.querySelector('lp-bar');
  if (!bar) return;

  const Q = { low: '低', mid: '中', high: '高', orig: '原本' };
  const page = bar.dataset.page || ''; // 元のページの URL（画像取得時の Referer）
  const sized = bar.dataset.q || ''; // 枠に表示しているサイズの画質
  const cookie = (name) => {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  };
  let quality = Q[cookie('lp_q')] ? cookie('lp_q') : Q[bar.dataset.dq] ? bar.dataset.dq : 'mid';
  const kb = (n) => (n < 1024 ? n + 'B' : (n / 1024).toFixed(n < 10240 ? 1 : 0) + 'KB');

  const style = document.createElement('style');
  style.textContent =
    'img[data-lp-src]{-webkit-touch-callout:none;-webkit-user-select:none;user-select:none;cursor:pointer}' +
    'img[data-lp-state=loading]{animation:lp-blink 1s ease-in-out infinite}' +
    'img[data-lp-state=error]{outline:2px solid #dc2626;outline-offset:-2px}' +
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
    'lp-sheet button.x{display:block;width:100%;margin-top:12px;background:none;color:#9da7b3}';
  document.head.append(style);

  const pending = () =>
    [...document.querySelectorAll('img[data-lp-src]')].filter((i) => i.dataset.lpState !== 'done');

  function load(img, q) {
    if (img.dataset.lpState === 'loading') return;
    // 枠と同じ寸法を保つ。低画質の画像は固有サイズが小さいため、属性がないとレイアウトが変わる
    if (img.dataset.lpState !== 'done' && !img.hasAttribute('width') && img.naturalWidth) {
      img.setAttribute('width', img.naturalWidth);
      img.setAttribute('height', img.naturalHeight);
    }
    const src =
      '/i?u=' + encodeURIComponent(img.dataset.lpSrc) + '&q=' + q + (page ? '&r=' + encodeURIComponent(page) : '');
    img.dataset.lpState = 'loading';
    // 読み込み中に壊れた画像の表示にならないよう、別の Image で読み込んでから差し替える
    const probe = new Image();
    probe.onload = () => {
      img.src = src;
      img.dataset.lpState = 'done';
      img.dataset.lpQ = q;
    };
    probe.onerror = () => {
      img.dataset.lpState = 'error';
    };
    probe.src = src;
  }

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

  // 長押しの直後に発生するクリックで、既定の画質の読み込みが始まらないようにする
  let suppressUntil = 0;
  let timer = 0;
  let pressed = false;

  document.addEventListener(
    'click',
    (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (!target || target.closest('lp-sheet')) return;
      if (Date.now() < suppressUntil) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      const img = target.closest('img[data-lp-src]');
      if (!img || img.dataset.lpState === 'done') return; // 読み込み済みならリンクなどは通常どおり動かす
      e.preventDefault();
      e.stopPropagation();
      load(img, quality);
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
