"""agent_backend 容器内回归用例。

运行方式（鲲鹏服务器）：
  docker exec -i docker-agent_backend-1 python3 - < cases/test_agent_backend.py

覆盖补丁：
  - dify-agent/.../adapters/llm/model.py（思考复读熔断：streak 8 / total 24 / periodic）
  - dify-agent/.../runtime/event_sink.py（emit_run_failed 终态幂等，run_scheduler 补丁依赖的兜底）
"""
import asyncio
import inspect
import sys

RESULTS = []


def record(name, fn):
    try:
        if inspect.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        RESULTS.append((name, "PASS", ""))
    except Exception as e:
        RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))


# ---------- 思考复读熔断 ----------
from dify_agent.adapters.llm import model as llm_model_mod  # noqa: E402

Parser = llm_model_mod._EmbeddedThinkingParser
SENT = "这是一句会被复读的思考内容，用于回归验证。"
OTHER = "另一句正常的思考内容，与复读无关。"


class _StubPartsManager:
    """duck-typed ModelResponsePartsManager：仅记录 thinking delta，避免依赖 pydantic_ai 内部构造。"""

    def __init__(self):
        self.contents: list[str] = []

    def handle_thinking_delta(self, vendor_part_id=None, content="", provider_name=None, **kwargs):
        self.contents.append(content)
        return []


def new_parser():
    try:
        return Parser()
    except TypeError:
        p = object.__new__(Parser)
        for name, field_obj in getattr(Parser, "__dataclass_fields__", {}).items():
            if field_obj.default is not getattr(field_obj, "default_factory", None) or field_obj.default is not None:
                continue
            setattr(p, name, field_obj.default)
        return p


def feed(parser, content):
    return parser._emit_thinking(_StubPartsManager(), "regress", content)


def t1_streak_break():
    p = new_parser()
    for _ in range(llm_model_mod._THINK_REPEAT_STREAK_LIMIT):
        feed(p, SENT)
    assert p._think_circuit_broken, "连续相同思考句达到 streak 上限未熔断"
    assert feed(p, SENT) == [], "熔断后仍产出思考事件"


def t2_total_break():
    p = new_parser()
    # 交替打断 streak，仅累计口径达到 total 上限
    n = llm_model_mod._THINK_REPEAT_TOTAL_LIMIT
    for i in range(n + n // 7 + 8):
        feed(p, SENT if i % 8 != 7 else OTHER)
    assert p._think_circuit_broken, "累计重复达到 total 上限未熔断"


def t3_normal_no_trip():
    p = new_parser()
    for i in range(40):
        feed(p, f"正常思考第 {i} 句，内容各不相同。")
    assert not p._think_circuit_broken, "正常多样思考被误熔断"


def t4_periodic_break():
    p = new_parser()
    feed(p, SENT * (llm_model_mod._THINK_REPEAT_STREAK_LIMIT + 1))
    assert p._think_circuit_broken, "单 delta 内周期性复读未熔断"


# ---------- emit_run_failed 幂等 ----------
from dify_agent.runtime.event_sink import InMemoryRunEventSink, emit_run_failed  # noqa: E402


def _events_len(sink):
    for attr in ("events", "_events", "terminal_events", "_terminal_events"):
        v = getattr(sink, attr, None)
        if isinstance(v, list):
            return len(v)
    return None


async def t5_run_failed_idempotent():
    sink = InMemoryRunEventSink()
    before = _events_len(sink)
    r1 = await emit_run_failed(sink, run_id="regress-run-1", error="injected failure")
    mid = _events_len(sink)
    r2 = await emit_run_failed(sink, run_id="regress-run-1", error="injected failure")
    after = _events_len(sink)
    assert r1 is not None and r2 is not None, "emit_run_failed 返回空"
    if r2 is r1:
        raise AssertionError("两次 finalize 返回同一结果对象，疑似未区分")
    if hasattr(r2, "applied"):
        assert r2.applied is False, "重复 finalize 未标记 applied=False"
    if mid is not None and after is not None:
        assert mid == after, "重复 finalize 追加了重复终态事件"
    if before is not None:
        assert mid is None or mid > before, "首次 finalize 未追加终态事件"


record("T1 streak=8 熔断且熔断后静默", t1_streak_break)
record("T2 total=24 熔断（streak 被打断场景）", t2_total_break)
record("T3 正常多样思考不误伤", t3_normal_no_trip)
record("T4 单 delta 周期性复读熔断", t4_periodic_break)
record("T5 emit_run_failed 终态幂等", t5_run_failed_idempotent)

print("\n=== agent_backend regress ===")
for name, status, err in RESULTS:
    print(f"{status}  {name}" + (f"  -- {err}" if err else ""))
failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
print(f"total={len(RESULTS)} failed={failed}")
sys.exit(1 if failed else 0)
