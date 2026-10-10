"""全局限流 + 租户限流（页数加权并发额度）。

额度模型：一次评审请求占用的额度 = PPT 页数，而不是 1 请求 1 份——
后端 /ai/chat 是整份 PPT 截图全页并发视觉分析，负载随页数线性增长。

- 全局预算：整个服务同时在评的页数上限（默认 50）
- 租户预算：单个租户同时在评的页数上限（默认 20）
- 排队上限：单租户最多 max_waiting 个请求排队，超限立即拒绝（busy）
- 等待超时：排队超过 wait_timeout 秒仍未拿到额度则拒绝（timeout）
- 大请求兜底：单请求页数超过租户预算但不超过全局预算时，若该租户当前
  空闲则放行——否则 30 页答辩 PPT 在 20 页租户预算下永远无法执行

实现说明：不用 asyncio.Condition——wait_for 取消 cond.wait() 需要
重新抢锁才能送达超时，而锁被分钟级的评审占着，超时会被无限推迟。
改为 waiters 列表 + future 唤醒：所有状态变更不含 await（事件循环内
天然原子），wait_for 直接挂在 future 上，超时精确送达。
纯 asyncio 实现，不依赖 mcp 包，可独立单测。
"""
import asyncio
import os
from contextlib import asynccontextmanager


class RateLimitRejected(Exception):
    """限流拒绝。reason: busy（排队满）/ timeout（等待超时）/ oversize（超过全局预算）"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class WeightedRateLimiter:
    """全局/租户两级加权并发额度。

    权重为 1 时退化为普通并发数限流（用作请求闸门）。
    """

    def __init__(self, global_budget: int, tenant_budget: int,
                 max_waiting_per_tenant: int, wait_timeout: float):
        self.global_budget = global_budget
        self.tenant_budget = tenant_budget
        self.max_waiting = max_waiting_per_tenant
        self.wait_timeout = wait_timeout
        self._global_active = 0
        # tenant -> [active_weight, waiting_count]
        self._tenants = {}
        # [(future, tenant, weight)]，FIFO 唤醒；fut 已完成/取消的惰性清理
        self._waiters = []

    def _state(self, tenant: str):
        if tenant not in self._tenants:
            self._tenants[tenant] = [0, 0]
        return self._tenants[tenant]

    def _can_admit(self, weight: int, state) -> bool:
        global_ok = self._global_active + weight <= self.global_budget
        tenant_ok = (
            state[0] + weight <= self.tenant_budget
            or (state[0] == 0 and weight <= self.global_budget)
        )
        return global_ok and tenant_ok

    def _wake_eligible(self):
        for entry in list(self._waiters):
            fut, tenant, weight = entry
            if fut.done():
                self._waiters.remove(entry)
                continue
            state = self._tenants.get(tenant)
            if state and self._can_admit(weight, state):
                self._waiters.remove(entry)
                fut.set_result(None)

    @asynccontextmanager
    async def slot(self, tenant: str, weight: int = 1):
        if weight > self.global_budget:
            raise RateLimitRejected(
                "oversize",
                f"单次评审页数（{weight}）超过服务上限（{self.global_budget} 页），请拆分 PPT 后重试",
            )
        queued = self._tenants.get(tenant)
        if queued and queued[1] >= self.max_waiting:
            raise RateLimitRejected(
                "busy",
                f"评审排队已满（该用户最多 {self.max_waiting} 个等待请求），请稍后重试",
            )
        state = self._state(tenant)
        state[1] += 1
        acquired = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_timeout
        try:
            while True:
                if self._can_admit(weight, state):
                    self._global_active += weight
                    state[0] += weight
                    # 拿到额度的瞬间退出排队计数，否则占用者会把排队位占满
                    state[1] -= 1
                    acquired = True
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RateLimitRejected(
                        "timeout",
                        f"评审繁忙：等待评审额度超时（>{self.wait_timeout:.0f}s），请稍后重试",
                    )
                fut = loop.create_future()
                entry = (fut, tenant, weight)
                self._waiters.append(entry)
                try:
                    await asyncio.wait_for(fut, remaining)
                except asyncio.TimeoutError:
                    if entry in self._waiters:
                        self._waiters.remove(entry)
                    raise RateLimitRejected(
                        "timeout",
                        f"评审繁忙：等待评审额度超时（>{self.wait_timeout:.0f}s），请稍后重试",
                    )
            try:
                yield
            finally:
                self._global_active -= weight
                state[0] -= weight
                self._wake_eligible()
        finally:
            if not acquired:
                state[1] -= 1
            # 空闲租户状态出清，避免长会话下字典无限增长
            if state[0] == 0 and state[1] == 0:
                self._tenants.pop(tenant, None)
