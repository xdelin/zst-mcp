"""主 api 容器内回归用例。

运行方式（鲲鹏服务器）：
  docker exec -i docker-api-1 python3 - < cases/test_api_patches.py

覆盖补丁：
  - api/core/memory/token_buffer_memory.py（多模态历史降级：视觉能力检测 + 实例级缓存 + 异常回退）
  - api/core/app/apps/agent_app/runtime_request_builder.py（usermem 身份注入在位，防升级丢失）
"""
import sys

RESULTS = []


def record(name, fn):
    try:
        fn()
        RESULTS.append((name, "PASS", ""))
    except Exception as e:
        RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))


# ---------- 多模态历史降级 ----------
from core.memory.token_buffer_memory import TokenBufferMemory  # noqa: E402
from graphon.model_runtime.entities.model_entities import ModelFeature  # noqa: E402


class _Schema:
    def __init__(self, features):
        self.features = features


class _FakeLLM:
    def __init__(self, features=None, boom=False):
        self.calls = 0
        self._features = features or []
        self._boom = boom

    def get_model_schema(self, model_name, credentials):
        self.calls += 1
        if self._boom:
            raise RuntimeError("schema lookup failed")
        return _Schema(self._features)


def _mem(llm):
    mi = type("MI", (), {"model_type_instance": llm, "model_name": "regress-model", "credentials": {}})()
    return TokenBufferMemory(conversation=None, model_instance=mi), llm


def t6_non_vision_detected():
    mem, llm = _mem(_FakeLLM(features=[]))
    assert mem._model_supports_vision() is False, "无 VISION 特性的模型被判为支持视觉"
    assert llm.calls == 1


def t7_vision_cache():
    mem, llm = _mem(_FakeLLM(features=[ModelFeature.VISION]))
    assert mem._model_supports_vision() is True
    mem._model_supports_vision()
    mem._model_supports_vision()
    assert llm.calls == 1, "视觉能力查询未做实例级缓存（逐条历史消息会重复查询）"


def t8_schema_error_fallback_vision():
    mem, _ = _mem(_FakeLLM(boom=True))
    assert mem._model_supports_vision() is True, "schema 查询异常时应回退为附带文件（防误伤视觉模型）"


# ---------- usermem 身份注入在位 ----------
INJECT_FILE = "/app/api/core/app/apps/agent_app/runtime_request_builder.py"


def t9_identity_injection_inplace():
    src = open(INJECT_FILE, encoding="utf-8").read()
    assert "用户上下文" in src, "runtime_request_builder 缺少 usermem 身份注入（升级时被官方代码覆盖？）"
    assert "UserFrom.END_USER" in src, "注入缺少 end-user 判定"
    assert "conversation_id" in src, "注入缺少会话 id"


record("T6 非视觉模型正确识别", t6_non_vision_detected)
record("T7 视觉能力判定实例级缓存", t7_vision_cache)
record("T8 schema 查询异常回退不误伤", t8_schema_error_fallback_vision)
record("T9 usermem 身份注入在位", t9_identity_injection_inplace)

print("\n=== api regress ===")
for name, status, err in RESULTS:
    print(f"{status}  {name}" + (f"  -- {err}" if err else ""))
failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
print(f"total={len(RESULTS)} failed={failed}")
sys.exit(1 if failed else 0)
