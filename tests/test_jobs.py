import asyncio

from liteproxy.browser import RenderError, Viewport
from liteproxy.jobs import RenderJobs, RenderKey

KEY = RenderKey("https://8.8.8.8/", Viewport(390, 844, 3.0, False), "UA")


class SlowRenderer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.release = asyncio.Event()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def render(self, url, viewport, user_agent, on_stage=None, image_quality=None, mode="layout"):
        self.calls += 1
        on_stage("fetching")
        await self.release.wait()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_same_request_while_rendering_reuses_job():
    async def run():
        renderer = SlowRenderer(["html"])
        jobs = RenderJobs(renderer)
        first = jobs.start(KEY)
        await asyncio.sleep(0)
        second = jobs.start(KEY)  # 描画中の再読み込み
        assert first is second
        renderer.release.set()
        await first.task
        assert jobs.cached(KEY) == "html"
        assert jobs.start(KEY) is first  # 有効な結果があれば描画し直さない
        assert renderer.calls == 1

    asyncio.run(run())


def test_failed_job_is_not_reused():
    async def run():
        renderer = SlowRenderer([RenderError("失敗"), "html"])
        renderer.release.set()
        jobs = RenderJobs(renderer)
        failed = jobs.start(KEY)
        await failed.task
        assert failed.error == "失敗" and jobs.cached(KEY) is None
        retry = jobs.start(KEY)
        assert retry is not failed
        await retry.task
        assert jobs.cached(KEY) == "html"

    asyncio.run(run())


def test_wait_change_wakes_on_stage_and_times_out():
    async def run():
        renderer = SlowRenderer(["html"])
        jobs = RenderJobs(renderer, on_complete=lambda job, result: 1234)
        job = jobs.start(KEY)
        seen = await job.wait_change(-1, 1.0)  # 開始時点の版はすぐ返る
        seen = await job.wait_change(seen, 1.0)  # "fetching" への変化
        assert job.stage == "fetching"
        assert await job.wait_change(seen, 0.05) == seen  # 変化がなければ時間切れで同じ版
        renderer.release.set()
        await job.wait_change(seen, 1.0)
        assert job.result == "html" and job.sent_bytes == 1234

    asyncio.run(run())


def test_old_results_expire():
    async def run():
        renderer = SlowRenderer(["html"])
        renderer.release.set()
        jobs = RenderJobs(renderer, ttl=0.0)
        job = jobs.start(KEY)
        await job.task
        assert jobs.cached(KEY) is None and jobs.get(job.id) is None

    asyncio.run(run())


def test_kept_result_outlives_reuse_period():
    async def run():
        renderer = SlowRenderer(["html", "html2"])
        renderer.release.set()
        jobs = RenderJobs(renderer, ttl=0.0, keep=60.0)
        job = jobs.start(KEY)
        await job.task
        # 使い回す期間は過ぎているが、ジョブの指定や kept では保持している結果を返す
        assert jobs.cached(KEY) is None
        assert jobs.cached(KEY, kept=True) == "html"
        assert jobs.get(job.id) is job
        # 新たな要求では描画し直す
        again = jobs.start(KEY)
        assert again is not job
        await again.task
        assert renderer.calls == 2 and jobs.get(job.id) is job

    asyncio.run(run())


def test_max_finished_drops_oldest():
    async def run():
        renderer = SlowRenderer(["a", "b", "c"])
        renderer.release.set()
        jobs = RenderJobs(renderer, keep=60.0, max_finished=2)
        done = []
        for path in ("a", "b", "c"):
            job = jobs.start(RenderKey(f"https://8.8.8.8/{path}", KEY.viewport, "UA"))
            await job.task
            done.append(job)
        assert jobs.get(done[0].id) is None
        assert jobs.get(done[1].id) is done[1] and jobs.get(done[2].id) is done[2]

    asyncio.run(run())
