"""PC 側で保持したページへのタップの再現（Phase 3）を、実際の Chrome で確かめる。

ブラウザ（既定はインストール済みの Chrome）を起動できない環境ではスキップする。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from liteproxy.browser.pool import BrowserPool, Snapshot, Viewport
from liteproxy.config import BrowserConfig, RenderConfig, SessionConfig
from liteproxy.security import HostGuard
from tests.sites import serve, write_interactive_site


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("interactive")
    write_interactive_site(root)
    server = serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(site: str, scenario, *, sessions: SessionConfig | None = None):
    """ページを描画してセッションを作り、scenario(pool, snap, path_of) を実行する。"""

    async def main():
        pool = BrowserPool(
            BrowserConfig(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome"), settle_timeout_ms=2000),
            RenderConfig(),
            HostGuard(allow_private=True),
            None,
            sessions or SessionConfig(),
        )
        try:
            await pool.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"ブラウザを起動できません: {e}")
        try:
            snap = await pool.render(site + "/page.html", Viewport(390, 844, 2, False), None)
            assert isinstance(snap, Snapshot) and snap.session_id

            async def path_of(selector: str) -> list[int]:
                s = pool._sessions[snap.session_id]
                path = await s.page.evaluate(f"(q) => window[{json.dumps(s.ns)}].pathForSelector(q)", selector)
                assert path is not None, selector
                return path

            return await scenario(pool, snap, path_of)
        finally:
            await pool.stop()

    return asyncio.run(main())


def tag(html: str, id_: str) -> str:
    m = re.search(rf'<[a-z]+\b[^>]*\bid="{id_}"[^>]*>', html)
    assert m, id_
    return m.group(0)


def test_tappable_elements_are_marked(site):
    async def scenario(pool, snap, path_of):
        return snap

    snap = run(site, scenario)
    for id_ in ("acc", "cnt", "add", "go", "blank"):
        assert "data-lp-t" in tag(snap.html, id_), id_
    for id_ in ("real", "plain", "panel"):
        assert "data-lp-t" not in tag(snap.html, id_), id_


def test_toggle_sends_attribute_changes_and_new_css(site):
    async def scenario(pool, snap, path_of):
        return snap, await pool.act(snap.session_id, snap.rev, await path_of("#acc"))

    snap, result = run(site, scenario)
    assert result.status == "ok" and result.rev == snap.rev + 1
    attrs = {json.dumps(op["a"], ensure_ascii=False) for op in result.ops if op["t"] == "a"}
    assert any('"class": "panel open"' in a for a in attrs)
    assert any('"aria-expanded": "true"' in a for a in attrs)
    # 開いた状態の CSS は描画時点では使われていなかったため、差分で追加される
    assert any(".panel.open" in c for c in result.css)


def test_text_change_replaces_element(site):
    async def scenario(pool, snap, path_of):
        path = await path_of("#cnt")
        first = await pool.act(snap.session_id, snap.rev, path)
        second = await pool.act(snap.session_id, first.rev, path)
        return first, second

    first, second = run(site, scenario)
    assert [op["t"] for op in first.ops] == ["h"] and ">1<" in first.ops[0]["h"]
    assert ">2<" in second.ops[0]["h"]


def test_added_element_and_its_css(site):
    async def scenario(pool, snap, path_of):
        return await pool.act(snap.session_id, snap.rev, await path_of("#add"))

    result = run(site, scenario)
    html = "".join(op.get("h", "") for op in result.ops)
    assert 'class="item new"' in html and "追加した項目" in html
    assert any(".item.new" in c for c in result.css)


def test_stale_version_requests_reload(site):
    async def scenario(pool, snap, path_of):
        return await pool.act(snap.session_id, snap.rev - 1, await path_of("#acc"))

    assert run(site, scenario).status == "reload"


def test_js_navigation_returns_new_page(site):
    async def scenario(pool, snap, path_of):
        return snap, await pool.act(snap.session_id, snap.rev, await path_of("#go"))

    snap, result = run(site, scenario)
    assert result.status == "navigated"
    assert result.snapshot.url.endswith("/next.html") and "次のページ" in result.snapshot.html
    assert result.snapshot.session_id == snap.session_id and result.snapshot.rev > snap.rev


def test_push_state_returns_new_page(site):
    async def scenario(pool, snap, path_of):
        return await pool.act(snap.session_id, snap.rev, await path_of("#spa"))

    result = run(site, scenario)
    assert result.status == "navigated"
    assert result.snapshot.url.endswith("/spa-page") and "SPA後" in result.snapshot.html


def test_new_tab_is_opened_through_proxy(site):
    async def scenario(pool, snap, path_of):
        return await pool.act(snap.session_id, snap.rev, await path_of("#blank"))

    result = run(site, scenario)
    assert result.status == "popup" and result.url.endswith("/next.html")


def test_unknown_or_evicted_session_is_expired(site):
    async def scenario(pool, snap, path_of):
        unknown = await pool.act("unknown", 0, [])
        # 保持数の上限（1）を超えると古いページから破棄する
        await pool.render(site + "/next.html", Viewport(390, 844, 2, False), None)
        evicted = await pool.act(snap.session_id, snap.rev, [])
        return unknown, evicted

    unknown, evicted = run(site, scenario, sessions=SessionConfig(max_sessions=1))
    assert unknown.status == "expired" and evicted.status == "expired"
