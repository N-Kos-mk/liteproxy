// PC 側の Chrome で描画済みのページに対して実行し、スマホへ送る軽量 HTML を組み立てる。
//
// - 生きている DOM は変更せず、JS の実行されない別ドキュメントへ複製したものを加工する
//   （後のフェーズで同じページを操作し続けるため）。
// - 画像は要素を残したまま、同じ寸法の極小 SVG に差し替える。サイトの CSS
//   （例: `.card img { width: 100% }`）がそのまま効くので、レイアウトが崩れにくい。
// - CSS は現在の DOM で使われているルールだけを残し、外部リソースへの url() を除く。
// - スマホ側で外部への通信が一切発生しない HTML を出力することを目標とする。
(args) => {
  const {
    proxyPath,
    formPath,
    formActionField,
    formCharsetField,
    cssTexts, // CSSOM から読めないクロスオリジン CSS の本文 { 絶対URL: テキスト }
    removeSelectors,
    maxInlineSvg,
    maxDataUri,
    pruneClasses,
  } = args;

  const pageURL = location.href.split('#')[0];
  const inert = document.implementation.createHTMLDocument('');
  const DESCEND = 0; // 要素を残し、子要素も処理する
  const KEEP = 1; // 要素を残すが、子要素は処理しない
  const GONE = 2; // 要素を削除または置換した

  const abs = (u, base = document.baseURI) => {
    try {
      return new URL(u, base).href;
    } catch {
      return null;
    }
  };
  const proxied = (u) => proxyPath + encodeURIComponent(u);
  const htmlEsc = (s) => s.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
  const xmlEsc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const isA = (obj, name) => typeof window[name] === 'function' && obj instanceof window[name];

  // ================================================================ CSS

  const usedClasses = new Set();
  const classPatterns = []; // [演算子, 値]  例: [class*="col-"] → ['*', 'col-']
  const usedAttrs = new Set();
  const keyframes = [];

  // querySelector で判定できない（状態に依存する）疑似クラス・疑似要素。取り除いてから判定する
  const PSEUDO_ELEMENT = /(?<!\\)::[\w-]+(?:\((?:[^()]|\([^()]*\))*\))?/g;
  const DYNAMIC_PSEUDO = new RegExp(
    String.raw`(?<!\\):(?:-[a-z]+-[\w-]+|before|after|first-line|first-letter|hover|focus|focus-within|focus-visible|active|visited|target|target-within|checked|indeterminate|placeholder-shown|autofill|invalid|valid|user-invalid|user-valid|in-range|out-of-range|open|closed|popover-open|modal|fullscreen|picture-in-picture|playing|paused|seeking|buffering|stalled|muted|volume-locked|current|past|future|defined)(?![\w-])(?:\((?:[^()]|\([^()]*\))*\))?`,
    'gi',
  );
  const CLASS_TOKEN = /\.((?:\\[0-9a-fA-F]{1,6}\s?|\\[^\n\r\f0-9a-fA-F]|[\w -￿-])+)/g;
  const ATTR_TOKEN = /\[\s*(?:[\w-]*\|)?([\w:-]+)\s*(?:([~|^$*]?)=\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?/g;
  const URL_FN = /url\(\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^)"']*)\s*\)/gi;

  const unescapeCss = (s) =>
    s.replace(/\\([0-9a-fA-F]{1,6})\s?|\\(.)/g, (m, hex, ch) =>
      hex ? String.fromCodePoint(parseInt(hex, 16)) : ch,
    );

  const tryMatch = (sel) => {
    try {
      return document.querySelector(sel) !== null;
    } catch {
      return null;
    }
  };

  function selectorUsed(selectorText) {
    let s = selectorText.replace(PSEUDO_ELEMENT, '').replace(DYNAMIC_PSEUDO, '');
    let r = tryMatch(s.trim() || '*');
    if (r === null) {
      // `a > :hover` → `a > ` のように壊れたセレクタを補修して再判定する
      s = s
        .replace(/:(?:not|is|where|has)\(\s*\)/g, '')
        .replace(/([>+~]\s*)(?=$|[,>+~)])/g, '$1*')
        .replace(/(^|,)\s*(?=,|$)/g, '$1*');
      r = tryMatch(s.trim() || '*');
    }
    return r !== false; // 判定できないものは安全側に倒して残す
  }

  function collect(selectorText) {
    for (const m of selectorText.matchAll(CLASS_TOKEN)) usedClasses.add(unescapeCss(m[1]));
    for (const m of selectorText.matchAll(ATTR_TOKEN)) {
      const name = m[1].toLowerCase();
      usedAttrs.add(name);
      if (name === 'class' && m[2] !== undefined) {
        classPatterns.push([m[2], m[3] ?? m[4] ?? m[5] ?? '']);
      }
    }
  }

  function cleanCss(text) {
    if (text.includes('url(')) {
      text = text.replace(URL_FN, (m) => {
        if (/^url\(\s*["']?#/.test(m)) return m; // 文書内の SVG フィルタ等への参照は通信しない
        if (/^url\(\s*["']?data:/i.test(m) && m.length <= maxDataUri) return m;
        return 'none';
      });
    }
    // スマホ側ではカスタム要素が定義されないため、:not(:defined) で隠されるのを防ぐ
    if (text.includes(':defined')) text = text.replace(/:defined/g, ':is(*)');
    return text;
  }

  function serializeRules(rules, prune) {
    let out = '';
    for (const r of rules) {
      try {
        if (isA(r, 'CSSStyleRule')) {
          if (!prune) {
            out += cleanCss(r.cssText);
          } else if (selectorUsed(r.selectorText)) {
            collect(r.selectorText);
            out += cleanCss(r.cssText);
          }
        } else if (isA(r, 'CSSImportRule')) {
          const mt = r.media && r.media.mediaText;
          if (mt && !matchMedia(mt).matches) continue;
          const href = abs(r.href, (r.parentStyleSheet && r.parentStyleSheet.href) || document.baseURI);
          out += serializeSheet(r.styleSheet, href, prune);
        } else if (isA(r, 'CSSFontFaceRule') || isA(r, 'CSSPageRule')) {
          // フォントは送らない。印刷用の指定も不要
        } else if (isA(r, 'CSSKeyframesRule')) {
          if (prune) keyframes.push(r);
          else out += cleanCss(r.cssText);
        } else if (isA(r, 'CSSScopeRule') || !r.cssRules) {
          out += cleanCss(r.cssText);
        } else {
          // @media / @supports / @layer / @container などのグループ化ルール
          if (isA(r, 'CSSMediaRule') && r.media.mediaText && !matchMedia(r.media.mediaText).matches) {
            continue;
          }
          const inner = serializeRules(r.cssRules, prune);
          if (inner) out += r.cssText.slice(0, r.cssText.indexOf('{')) + '{' + inner + '}';
        }
      } catch {
        // 1 つのルールの失敗で全体を止めない
      }
    }
    return out;
  }

  function serializeText(text, href, prune, depth = 0) {
    let out = '';
    // 構築済みスタイルシートは @import を無視するため、取得済みの本文から展開する
    if (depth < 3) {
      for (const m of text.matchAll(/@import\s+(?:url\(\s*)?["']?([^"')\s;]+)/g)) {
        const u = abs(m[1], href);
        if (u && cssTexts[u] != null) out += serializeText(cssTexts[u], u, prune, depth + 1);
      }
    }
    try {
      const sheet = new CSSStyleSheet({ baseURL: href });
      sheet.replaceSync(text);
      out += serializeRules(sheet.cssRules, prune);
    } catch {
      // 解析できない CSS は捨てる
    }
    return out;
  }

  function serializeSheet(sheet, href, prune) {
    let rules = null;
    if (sheet) {
      try {
        rules = sheet.cssRules;
      } catch {
        rules = null; // クロスオリジンで CSSOM から読めない
      }
    }
    if (rules) return serializeRules(rules, prune);
    const text = href ? cssTexts[href] : null;
    return text != null ? serializeText(text, href, prune) : '';
  }

  let css = '';
  for (const sheet of [...document.styleSheets, ...(document.adoptedStyleSheets || [])]) {
    if (sheet.disabled) continue;
    const mt = sheet.media && sheet.media.mediaText;
    if (mt && !matchMedia(mt).matches) continue;
    css += serializeSheet(sheet, sheet.href, true);
  }
  for (const k of keyframes) if (css.includes(k.name)) css += cleanCss(k.cssText);
  for (const m of css.matchAll(/attr\(\s*([\w-]+)/g)) usedAttrs.add(m[1].toLowerCase());
  css = css.replace(/<\/(style)/gi, '<\\/$1');

  // 属性セレクタが class 文字列全体の並びに依存する場合は、class を削ると結果が変わるため削らない
  const classPruning =
    pruneClasses && !classPatterns.some(([op]) => op !== '*' && op !== '~' && op !== '|');
  const classKept = (t) =>
    usedClasses.has(t) ||
    classPatterns.some(([op, v]) =>
      op === '*' ? t.includes(v) : op === '|' ? t === v || t.startsWith(v + '-') : t === v,
    );

  // ================================================================ DOM

  const removeSet = new Set();
  for (const sel of removeSelectors) {
    try {
      document.querySelectorAll(sel).forEach((el) => removeSet.add(el));
    } catch {
      // 不正なセレクタは無視
    }
  }

  // <use href="#id"> から参照されているスプライト内の symbol
  const usedRefs = new Set();
  for (const u of document.querySelectorAll('use')) {
    const h = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
    if (h.startsWith('#')) usedRefs.add(h.slice(1));
  }

  const styleCache = new WeakMap();
  const style = (el) => {
    let s = styleCache.get(el);
    if (!s) {
      s = getComputedStyle(el);
      styleCache.set(el, s);
    }
    return s;
  };

  function placeholder(w, h, label, fontPx) {
    let svg =
      `<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'>` +
      `<rect width='100%' height='100%' fill='#888' fill-opacity='.2'/>`;
    if (label) {
      svg +=
        `<text x='50%' y='50%' text-anchor='middle' dominant-baseline='central' ` +
        `font-family='sans-serif' font-size='${fontPx}' fill='#666'>${xmlEsc(label)}</text>`;
    }
    svg += '</svg>';
    return (
      'data:image/svg+xml,' +
      svg.replace(/%/g, '%25').replace(/#/g, '%23').replace(/</g, '%3C').replace(/>/g, '%3E')
    );
  }

  // 表示サイズに合わせて、スマホ上でおよそ 11px に見える文字サイズとラベルを決める
  function label(text, fallback, dispW, dispH, svgW) {
    if (dispW < 40 || dispH < 16) return [null, 0];
    const scale = svgW > 0 ? svgW / dispW : 1;
    const t0 = (text || '').replace(/\s+/g, ' ').trim() || fallback;
    const max = Math.max(2, Math.floor(dispW / 12) - 1);
    const t = t0.length > max ? t0.slice(0, max - 1) + '…' : t0;
    return [t, Math.max(1, Math.round(11 * scale))];
  }

  function img(l, c) {
    const rect = l.getBoundingClientRect();
    const current = l.currentSrc || l.src || '';
    const lazy =
      c.getAttribute('data-src') || c.getAttribute('data-lazy-src') || c.getAttribute('data-original');
    const src = current.startsWith('data:') && lazy ? abs(lazy) : current;
    if (src && src.startsWith('data:') && src.length <= maxDataUri) return; // 小さな埋め込み画像はそのまま

    // 置き換え後も元画像と同じ固有サイズ（縦横比）を持たせる
    let w = l.naturalWidth;
    let h = l.naturalHeight;
    if (!(w && h)) {
      const aw = parseFloat(c.getAttribute('width'));
      const ah = parseFloat(c.getAttribute('height'));
      [w, h] = aw > 0 && ah > 0 ? [aw, ah] : [rect.width, rect.height];
    }
    w = Math.round(w);
    h = Math.round(h);
    if (src && !src.startsWith('data:')) c.setAttribute('data-lp-src', src);
    const [t, font] = label(c.getAttribute('alt'), '画像', rect.width, rect.height, w);
    c.setAttribute('src', placeholder(w, h, t, font));
  }

  function video(l, c) {
    const rect = l.getBoundingClientRect();
    let src = l.currentSrc || abs(c.getAttribute('src') || '') || '';
    if (!src) {
      const s = l.querySelector('source[src]');
      if (s) src = abs(s.getAttribute('src')) || '';
    }
    if (src && !src.startsWith('blob:')) c.setAttribute('data-lp-src', src);
    const poster = c.getAttribute('poster');
    if (poster) c.setAttribute('data-lp-poster', abs(poster) || poster);
    // 動画の固有サイズはポスター画像から決まるため、表示サイズのポスターを与える
    const w = Math.round(rect.width) || 300;
    const h = Math.round(rect.height) || 150;
    const [t, font] = label('', '動画', rect.width, rect.height, w);
    c.setAttribute('poster', placeholder(w, h, t, font));
    for (const a of ['src', 'autoplay', 'controls', 'loop']) c.removeAttribute(a);
    c.setAttribute('preload', 'none');
    c.replaceChildren();
  }

  function iframe(l, c) {
    const rect = l.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      c.remove(); // 計測用などの見えない iframe
      return GONE;
    }
    const src = abs(c.getAttribute('src') || '') || '';
    let host = '';
    try {
      host = new URL(src).hostname;
    } catch {
      // srcdoc や about:blank
    }
    const inner = /^https?:/.test(src)
      ? `<a href="${htmlEsc(proxied(src))}" target="_top">埋め込み: ${htmlEsc(host)}</a>`
      : '埋め込み';
    for (const a of ['src', 'allow', 'allowfullscreen']) c.removeAttribute(a);
    if (src) c.setAttribute('data-lp-src', src);
    // iframe 要素自体は残し、サイトの CSS（レスポンシブ埋め込みの指定など）を効かせる
    c.setAttribute('sandbox', 'allow-top-navigation-by-user-activation');
    c.setAttribute(
      'srcdoc',
      '<style>html,body{margin:0;height:100%}body{display:flex;align-items:center;' +
        'justify-content:center;background:#8883;font:12px sans-serif}a{color:#555}</style>' +
        inner,
    );
    c.replaceChildren();
    return KEEP;
  }

  function replaceBox(l, c, text) {
    const rect = l.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      c.remove();
      return;
    }
    const d = style(l).display;
    const s = inert.createElement('span');
    for (const a of ['id', 'class']) if (c.hasAttribute(a)) s.setAttribute(a, c.getAttribute(a));
    s.setAttribute(
      'style',
      `display:${d === 'inline' ? 'inline-block' : d};width:${Math.round(rect.width)}px;` +
        `height:${Math.round(rect.height)}px;max-width:100%;box-sizing:border-box;overflow:hidden;` +
        'background:#8883;color:#666;font:12px/1.4 sans-serif;text-align:center',
    );
    s.textContent = text;
    c.replaceWith(s);
  }

  function svg(l, c) {
    if (l.querySelector('symbol')) return DESCEND; // アイコンスプライトは未使用の symbol だけ削る
    if (l.outerHTML.length <= maxInlineSvg) return DESCEND;
    const rect = l.getBoundingClientRect();
    if (!c.hasAttribute('viewBox') && !c.hasAttribute('width')) {
      c.setAttribute('width', String(Math.round(rect.width)));
      c.setAttribute('height', String(Math.round(rect.height)));
    }
    c.replaceChildren();
    const bg = inert.createElementNS('http://www.w3.org/2000/svg', 'rect');
    for (const [k, v] of [['width', '100%'], ['height', '100%'], ['fill', '#888'], ['fill-opacity', '.2']]) {
      bg.setAttribute(k, v);
    }
    c.append(bg);
    return KEEP;
  }

  function link(c) {
    const attr = c.hasAttribute('href') ? 'href' : c.hasAttribute('xlink:href') ? 'xlink:href' : null;
    if (!attr) return;
    const raw = c.getAttribute(attr).trim();
    if (!raw || raw.startsWith('#')) return;
    const u = abs(raw);
    if (!u || !/^https?:/i.test(u)) return; // mailto: tel: javascript: などはそのまま
    const hash = u.indexOf('#');
    if (hash >= 0 && u.slice(0, hash) === pageURL) {
      c.setAttribute(attr, u.slice(hash));
      return;
    }
    c.setAttribute(attr, proxied(u));
  }

  function form(c) {
    const method = (c.getAttribute('method') || 'get').toLowerCase();
    if (method === 'dialog') return;
    const action = abs(c.getAttribute('action') || pageURL) || pageURL;
    c.setAttribute('action', formPath);
    c.setAttribute('method', method === 'post' ? 'post' : 'get');
    // 子要素の走査位置がずれないよう、隠しフィールドは末尾に追加する
    for (const [name, value] of [[formActionField, action], [formCharsetField, document.characterSet]]) {
      const i = inert.createElement('input');
      i.setAttribute('type', 'hidden');
      i.setAttribute('name', name);
      i.setAttribute('value', value);
      c.append(i);
    }
  }

  function input(l, c) {
    const type = (c.getAttribute('type') || 'text').toLowerCase();
    if (type === 'password') {
      c.removeAttribute('value'); // 入力済みのパスワードは決して書き出さない
    } else if (type === 'checkbox' || type === 'radio') {
      if (l.checked) c.setAttribute('checked', '');
      else c.removeAttribute('checked');
    } else if (type === 'image') {
      const r = l.getBoundingClientRect();
      c.setAttribute('src', placeholder(Math.round(r.width), Math.round(r.height), null, 0));
    } else if (type !== 'file' && l.value !== (c.getAttribute('value') ?? '')) {
      c.setAttribute('value', l.value); // JS で書き換えられた現在の値を反映する
    }
  }

  const DROP_ATTRS = new Set([
    'srcset', 'imagesrcset', 'sizes', 'ping', 'nonce', 'integrity', 'crossorigin',
    'referrerpolicy', 'fetchpriority', 'loading', 'decoding', 'importance', 'itemprop',
    'itemscope', 'itemtype', 'itemid', 'background', 'autoplay', 'formaction', 'lowsrc',
    'dynsrc', 'longdesc', 'manifest',
  ]);
  // フレームワークが JS 用に付ける属性。CSS から参照されていなければ不要
  const FRAMEWORK_ATTR = /^(?:[@:#]|x-|v-|ng-|wire:|hx-|js)/;

  function attrs(l, c, inShadow) {
    for (const { name, value } of Array.from(c.attributes)) {
      const n = name.toLowerCase();
      if (n.startsWith('on') || DROP_ATTRS.has(n)) {
        c.removeAttribute(name);
      } else if (n.startsWith('data-')) {
        if (!n.startsWith('data-lp-') && !usedAttrs.has(n)) c.removeAttribute(name);
      } else if (FRAMEWORK_ATTR.test(n)) {
        if (!usedAttrs.has(n)) c.removeAttribute(name);
      } else if (n === 'style' && value.includes('url(')) {
        c.setAttribute('style', cleanCss(value));
      }
    }
    // シャドウ DOM 内と、:host(.x) で参照されうるホスト要素の class は削らない
    if (classPruning && !inShadow && !l.shadowRoot && c.hasAttribute('class')) {
      const kept = c.getAttribute('class').split(/\s+/).filter((t) => t && classKept(t));
      if (kept.length || usedAttrs.has('class')) c.setAttribute('class', kept.join(' '));
      else c.removeAttribute('class');
    }
  }

  function element(l, c, inShadow) {
    if (removeSet.has(l)) {
      c.remove();
      return GONE;
    }
    const tag = c.localName.toLowerCase();
    let r = DESCEND;
    switch (tag) {
      case 'script':
      case 'noscript':
      case 'link':
      case 'base':
      case 'template':
      case 'style': // 規則は上の CSS 抽出でまとめて出力する
      case 'source':
      case 'track':
      case 'portal':
        c.remove();
        return GONE;
      case 'meta':
        // charset は UTF-8 で付け直すため、viewport 以外は捨てる
        if (c.hasAttribute('charset') || (c.getAttribute('name') || '').toLowerCase() !== 'viewport') {
          c.remove();
          return GONE;
        }
        return KEEP;
      case 'img':
        img(l, c);
        r = KEEP;
        break;
      case 'iframe':
        if (iframe(l, c) === GONE) return GONE;
        r = KEEP;
        break;
      case 'frame': {
        const src = abs(c.getAttribute('src') || '');
        if (src && /^https?:/.test(src)) c.setAttribute('src', proxied(src));
        r = KEEP;
        break;
      }
      case 'video':
        video(l, c);
        r = KEEP;
        break;
      case 'audio':
        replaceBox(l, c, '音声');
        return GONE;
      case 'object':
      case 'embed':
      case 'applet':
        replaceBox(l, c, '埋め込み');
        return GONE;
      case 'canvas':
        c.replaceChildren();
        c.setAttribute('style', (c.getAttribute('style') || '') + ';background:#8883');
        r = KEEP;
        break;
      case 'svg':
        r = svg(l, c);
        break;
      case 'symbol':
        if (!inShadow && c.id && !usedRefs.has(c.id)) {
          c.remove();
          return GONE;
        }
        break;
      case 'image':
      case 'feimage':
        c.removeAttribute('href');
        c.removeAttribute('xlink:href');
        break;
      case 'use': {
        const h = c.getAttribute('href') || c.getAttribute('xlink:href') || '';
        if (h && !h.startsWith('#')) {
          c.removeAttribute('href');
          c.removeAttribute('xlink:href');
        }
        break;
      }
      case 'a':
      case 'area':
        link(c);
        break;
      case 'form':
        form(c);
        break;
      case 'input':
        input(l, c);
        r = KEEP;
        break;
      case 'textarea':
        c.textContent = l.value;
        r = KEEP;
        break;
      case 'option':
        if (l.selected) c.setAttribute('selected', '');
        else c.removeAttribute('selected');
        break;
    }
    attrs(l, c, inShadow);
    return r;
  }

  function text(l, c) {
    const v = c.data;
    if (!/[\t\n\r\f]| {2}/.test(v)) return;
    const p = l.parentElement;
    if (!p) return;
    const s = style(p);
    const ws = s.whiteSpaceCollapse || s.whiteSpace;
    if (ws === 'collapse' || ws === 'normal' || ws === 'nowrap') c.data = v.replace(/[ \t\n\r\f]+/g, ' ');
  }

  // 開いているシャドウ DOM は宣言的シャドウ DOM（<template shadowrootmode>）として書き出す
  function shadow(l, c) {
    const root = l.shadowRoot;
    const t = inert.createElement('template');
    t.setAttribute('shadowrootmode', root.mode);
    if (root.delegatesFocus) t.setAttribute('shadowrootdelegatesfocus', '');
    for (const n of root.childNodes) t.content.append(inert.importNode(n, true));
    walk(root, t.content, true);
    let shadowCss = '';
    for (const s of [...root.styleSheets, ...(root.adoptedStyleSheets || [])]) {
      shadowCss += serializeSheet(s, s.href, false);
    }
    if (shadowCss) {
      const st = inert.createElement('style');
      st.textContent = shadowCss.replace(/<\/(style)/gi, '<\\/$1');
      t.content.prepend(st);
    }
    c.prepend(t);
  }

  // 生きている DOM と複製を同じ位置で並行して辿る
  function walk(live, clone, inShadow) {
    const lc = live.childNodes;
    const cc = Array.from(clone.childNodes);
    for (let i = 0; i < cc.length && i < lc.length; i++) {
      const l = lc[i];
      const c = cc[i];
      if (c.nodeType === Node.COMMENT_NODE) {
        c.remove();
      } else if (c.nodeType === Node.TEXT_NODE) {
        text(l, c);
      } else if (c.nodeType === Node.ELEMENT_NODE) {
        const r = element(l, c, inShadow);
        if (r === GONE) continue;
        if (r === DESCEND) walk(l, c, inShadow);
        if (l.shadowRoot) shadow(l, c);
      }
    }
  }

  const root = inert.importNode(document.documentElement, true);
  attrs(document.documentElement, root, false);
  walk(document.documentElement, root, false);

  const head =
    root.querySelector(':scope > head') || root.insertBefore(inert.createElement('head'), root.firstChild);
  const meta = inert.createElement('meta');
  meta.setAttribute('charset', 'utf-8');
  head.prepend(meta);
  const st = inert.createElement('style');
  st.textContent = css;
  head.append(st);
  const body = root.querySelector(':scope > body');
  if (body) body.prepend(inert.createComment('lp-bar'));

  // 互換モードのページに標準モードの DOCTYPE を付けるとレイアウトが変わるため、そのまま引き継ぐ
  const doctype = document.compatMode === 'BackCompat' ? '' : '<!DOCTYPE html>';
  return {
    html: doctype + root.outerHTML,
    title: document.title,
    url: location.href,
    cssBytes: css.length,
  };
}
