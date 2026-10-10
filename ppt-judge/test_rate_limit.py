"""rate_limit 单元测试。兼容 pytest（pytest test_rate_limit.py），也可直接 python3 test_rate_limit.py 运行。"""
import asyncio
import time

from rate_limit import WeightedRateLimiter, RateLimitRejected


def test_global_page_budget():
    """全局预算：4+4+4 不得超过 10，第三个必须等前一个释放。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=10, tenant_budget=10,
                                  max_waiting_per_tenant=4, wait_timeout=5)
        peak = {"g": 0, "cur": 0}

        async def worker(w, hold):
            async with lim.slot("t1", weight=w):
                peak["cur"] += w
                peak["g"] = max(peak["g"], peak["cur"])
                await asyncio.sleep(hold)
                peak["cur"] -= w

        await asyncio.gather(worker(4, 0.05), worker(4, 0.05), worker(4, 0.05))
        assert peak["g"] == 8, peak
        assert lim._global_active == 0

    asyncio.run(run())


def test_tenant_budget_isolates_tenants():
    """租户预算：同租户 15+15>20 需串行；不同租户互不影响。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=50, tenant_budget=20,
                                  max_waiting_per_tenant=4, wait_timeout=5)
        peak = {"a": 0, "b": 0, "cur_a": 0, "cur_b": 0}

        async def worker(tenant, w, hold):
            key = "a" if tenant == "A" else "b"
            async with lim.slot(tenant, weight=w):
                peak[f"cur_{key}"] += w
                peak[key] = max(peak[key], peak[f"cur_{key}"])
                await asyncio.sleep(hold)
                peak[f"cur_{key}"] -= w

        await asyncio.gather(
            worker("A", 15, 0.05), worker("A", 15, 0.05),
            worker("B", 15, 0.05), worker("B", 15, 0.05),
        )
        assert peak["a"] == 15, peak
        assert peak["b"] == 15, peak

    asyncio.run(run())


def test_reject_when_queue_full():
    """排队满：占用全部额度后，第 1 个排队，第 2 个立即 busy 拒绝。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=20, tenant_budget=20,
                                  max_waiting_per_tenant=1, wait_timeout=5)
        results = {"inside": 0}

        async def holder():
            async with lim.slot("A", weight=20):
                await asyncio.sleep(0.3)

        async def waiter(i, res):
            try:
                async with lim.slot("A", weight=10):
                    results["inside"] += 1
            except RateLimitRejected as e:
                res.append(e.reason)

        res = []
        await asyncio.gather(holder(), waiter(1, res), waiter(2, res))
        assert "busy" in res, res
        assert len(res) == 1, res

    asyncio.run(run())


def test_timeout_reject():
    """等待超时：拿不到额度且超过 wait_timeout 则 timeout 拒绝。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=10, tenant_budget=10,
                                  max_waiting_per_tenant=4, wait_timeout=0.2)

        async def holder():
            async with lim.slot("A", weight=10):
                await asyncio.sleep(0.5)

        async def waiter():
            t0 = time.monotonic()
            try:
                async with lim.slot("B", weight=5):
                    raise AssertionError("should not enter")
            except RateLimitRejected as e:
                return e.reason, time.monotonic() - t0
            return None, time.monotonic() - t0

        _, (reason, w_elapsed) = await asyncio.gather(holder(), waiter())
        assert reason == "timeout", reason
        assert 0.15 < w_elapsed < 0.45, w_elapsed

    asyncio.run(run())


def test_oversize_rejects_immediately():
    """超过全局预算的单请求（51 页 > 50）立即拒绝，不等待。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=50, tenant_budget=20,
                                  max_waiting_per_tenant=4, wait_timeout=30)
        t0 = time.monotonic()
        try:
            async with lim.slot("A", weight=51):
                raise AssertionError("should not enter")
        except RateLimitRejected as e:
            assert e.reason == "oversize", e.reason
        assert time.monotonic() - t0 < 0.1

    asyncio.run(run())


def test_tenant_idle_allowance_for_big_request():
    """大请求兜底：30 页 PPT 超租户预算(20)但租户空闲时放行；第二个 30 页须等它结束。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=50, tenant_budget=20,
                                  max_waiting_per_tenant=4, wait_timeout=5)
        peak = {"g": 0, "cur": 0}

        async def worker(w, hold):
            async with lim.slot("A", weight=w):
                peak["cur"] += w
                peak["g"] = max(peak["g"], peak["cur"])
                await asyncio.sleep(hold)
                peak["cur"] -= w

        await asyncio.gather(worker(30, 0.05), worker(30, 0.05))
        assert peak["g"] == 30, peak

    asyncio.run(run())


def test_state_cleanup_after_release():
    """全部释放后，空闲租户状态出清。"""

    async def run():
        lim = WeightedRateLimiter(global_budget=50, tenant_budget=20,
                                  max_waiting_per_tenant=4, wait_timeout=5)
        async with lim.slot("A", weight=5):
            pass
        await asyncio.sleep(0.05)
        assert lim._tenants == {}, lim._tenants
        assert lim._global_active == 0

    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL GREEN")
