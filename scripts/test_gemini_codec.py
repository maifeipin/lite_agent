"""Gemini codec 契约测试。

分两层：
1. 真实 SDK 对象契约：仅当 google-genai 可导入时运行，直接检查真实
   types.Part / FunctionCall / FunctionResponse 的字段层级。
2. 纯 fake 单测：不依赖真实 SDK，通过局部 mock 验证编解码逻辑
   （thought_signature 只保留在 function_call Part，function_response 不回传签名；
    function_call.id 保留并回填到 function_response.id；并行结果合并为单 Content）。

不发起任何外部 API 调用。
"""

import json
import os
import sys
import types as pytypes
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gemini_codec


# ---------------------------------------------------------------------------
#  fake google.genai.types（只模拟 gemini_codec 用到的字段）
# ---------------------------------------------------------------------------
class FakeFunctionCall:
    def __init__(self, name=None, args=None, id=None):
        self.name = name
        self.args = args
        self.id = id


class FakeFunctionResponse:
    def __init__(self, name=None, response=None, id=None):
        self.name = name
        self.response = response
        self.id = id


class FakePart:
    def __init__(self, text=None, function_call=None, function_response=None,
                 thought_signature=None, **kwargs):
        self.text = text
        self.function_call = function_call
        self.function_response = function_response
        self.thought_signature = thought_signature
        self.kwargs = kwargs

    @classmethod
    def from_uri(cls, file_uri=None, mime_type=None):
        p = cls()
        p.file_uri = file_uri
        p.mime_type = mime_type
        return p

    @classmethod
    def from_function_response(cls, name=None, response=None, **kwargs):
        return cls(function_response=FakeFunctionResponse(name=name, response=response))


class FakeContent:
    def __init__(self, role=None, parts=None):
        self.role = role
        self.parts = parts or []


def _make_fake_modules():
    fake_types = pytypes.ModuleType("google.genai.types")
    fake_types.Part = FakePart
    fake_types.FunctionCall = FakeFunctionCall
    fake_types.FunctionResponse = FakeFunctionResponse
    fake_types.Content = FakeContent

    fake_genai = pytypes.ModuleType("google.genai")
    fake_genai.types = fake_types

    fake_google = pytypes.ModuleType("google")
    fake_google.genai = fake_genai

    return {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_types,
    }


def _fake_response(parts):
    candidate = pytypes.SimpleNamespace()
    candidate.finish_reason = pytypes.SimpleNamespace(name="STOP")
    candidate.content = FakeContent(role="model", parts=parts)
    resp = pytypes.SimpleNamespace()
    resp.usage_metadata = pytypes.SimpleNamespace(total_token_count=42)
    resp.candidates = [candidate]
    return resp


def _check(name, cond):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
#  1. 真实 SDK 对象契约（仅当可导入时运行）
# ---------------------------------------------------------------------------
def test_real_sdk_contract():
    print("[1] 真实 SDK 对象契约")
    try:
        from google.genai import types
    except ImportError:
        print("  [SKIP] google-genai 未安装，跳过真实 SDK 契约测试")
        return

    sig = b"sig_x"
    part = types.Part(thought_signature=sig)
    _check("Part 支持 thought_signature 字段", part.thought_signature == sig)

    fc = types.FunctionCall(name="f", args={}, id="call_1")
    _check("FunctionCall 支持 id 字段", fc.id == "call_1")

    fr = types.FunctionResponse(name="f", response={}, id="call_1")
    _check("FunctionResponse 支持 id 字段", fr.id == "call_1")

    # FunctionCall / FunctionResponse 不应接受 thought_signature
    try:
        types.FunctionCall(name="f", args={}, thought_signature="x")
        _check("FunctionCall 不接受 thought_signature", False)
    except Exception:
        _check("FunctionCall 不接受 thought_signature", True)

    try:
        types.FunctionResponse(name="f", response={}, thought_signature="x")
        _check("FunctionResponse 不接受 thought_signature", False)
    except Exception:
        _check("FunctionResponse 不接受 thought_signature", True)

    # Part 同时承载 function_call 与 thought_signature（bytes）
    sig_y = b"sig_y"
    p = types.Part(function_call=fc, thought_signature=sig_y)
    _check("Part 支持 function_call + thought_signature", p.thought_signature == sig_y)


# ---------------------------------------------------------------------------
#  2. 纯 fake 单测
# ---------------------------------------------------------------------------
def test_decode_preserves_id_and_signature():
    print("[2] 解码保留 function_call.id 与 Part.thought_signature")
    part = FakePart(
        function_call=FakeFunctionCall(name="search", args={"q": "x"}, id="call_9"),
        thought_signature=b"sig_abc",
    )
    unified = gemini_codec.gemini_response_to_unified(_fake_response([part]))

    tc = unified["tool_calls"][0]
    _check("id 保留原值", tc["id"] == "call_9")
    _check("thought_signature 保留", tc["provider_metadata"]["thought_signature"] == b"sig_abc")
    _check("arguments 正确", json.loads(tc["arguments"]) == {"q": "x"})
    _check("usage_total", unified["usage_total"] == 42)


def test_decode_falls_back_when_no_id():
    print("[3] 无 id 时生成 fallback call_xxx")
    part = FakePart(function_call=FakeFunctionCall(name="f", args={}))
    unified = gemini_codec.gemini_response_to_unified(_fake_response([part]))
    _check("fallback id", unified["tool_calls"][0]["id"].startswith("call_"))


def test_encode_roundtrip_parallel_tools():
    print("[4] 编码回填 id；签名只在 function_call；并行结果合并为单 Content")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"a"}'},
                    "provider_metadata": {"thought_signature": b"sig_1"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"b"}'},
                    "provider_metadata": {"thought_signature": b"sig_2"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "r1"},
        {"role": "tool", "tool_call_id": "call_2", "name": "search", "content": "r2"},
    ]

    system_instruction, contents = gemini_codec.openai_messages_to_gemini(messages)

    _check("system_instruction 正确", system_instruction == "sys")

    model_contents = [c for c in contents if c.role == "model"]
    _check("存在 model content", len(model_contents) == 1)
    fc_parts = [p for p in model_contents[0].parts if p.function_call]
    _check("两个 function_call", len(fc_parts) == 2)
    _check("fc id 回填", fc_parts[0].function_call.id == "call_1" and fc_parts[1].function_call.id == "call_2")
    _check("fc 签名挂 function_call Part", fc_parts[0].thought_signature == b"sig_1" and fc_parts[1].thought_signature == b"sig_2")

    user_contents = [c for c in contents if c.role == "user" and any(p.function_response for p in c.parts)]
    _check("并行结果合并为单 Content", len(user_contents) == 1)
    fr_parts = [p for p in user_contents[0].parts if p.function_response]
    _check("两个 function_response", len(fr_parts) == 2)
    _check("fr id 回填", fr_parts[0].function_response.id == "call_1" and fr_parts[1].function_response.id == "call_2")
    _check("fr 不携带签名", fr_parts[0].thought_signature is None and fr_parts[1].thought_signature is None)


def main():
    test_real_sdk_contract()

    with mock.patch.dict(sys.modules, _make_fake_modules()):
        test_decode_preserves_id_and_signature()
        test_decode_falls_back_when_no_id()
        test_encode_roundtrip_parallel_tools()

    print("\n[OK] Gemini codec 契约测试完成")


if __name__ == "__main__":
    main()
