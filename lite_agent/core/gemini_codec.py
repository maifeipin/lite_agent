"""Gemini 函数式编解码层（轻量，不建立 Adapter 类体系）。

只在边界处做统一内部格式（OpenAI 风格消息/工具事件）与 Google genai
Content/Part 之间的转换。Google SDK 本身仍通过 models.generate_content 调用，
这里绝不把 OpenAI 请求直接交给 Gemini。

Gemini 2.5+ 工具调用的原生元数据归属（google-genai 1.47.0 实测）：
- thought_signature 属于 function_call 的 Part 层，不属于 FunctionCall /
  FunctionResponse；且只保留在 model 的 function_call Part 上，function_response
  不回传签名（官方并行示例中 functionResponse 不携带签名）。
- FunctionCall 与 FunctionResponse 均有 id 字段，用于并行同名调用关联。
统一结构通过 provider_metadata 承载 thought_signature（仅 function_call 方向），
通过 tool_call_id 承载 id，避免归一化时丢失。
"""

import json
import uuid


def openai_tools_to_gemini_declarations(tools):
    """将 OpenAI Tool Schema 列表转换为 Gemini function_declarations 格式。

    纯格式转换，不访问 registry，不做权限过滤。
    tools 为 OpenAI 格式 list[dict]，每项形如:
        {"type": "function", "function": {"name", "description", "parameters": {...}}}
    返回 list[dict] 符合 Gemini function_declarations 格式。
    """
    decls = []
    for tool in tools:
        fn = tool["function"]
        params = fn.get("parameters", {})
        gemini_params = {
            "type": params.get("type", "OBJECT").upper(),
            "properties": {},
            "required": params.get("required", []),
        }
        for pname, pdef in params.get("properties", {}).items():
            gemini_params["properties"][pname] = {
                "type": pdef.get("type", "STRING").upper(),
                "description": pdef.get("description", ""),
            }
            if "enum" in pdef:
                gemini_params["properties"][pname]["enum"] = pdef["enum"]
        decls.append({
            "name": fn["name"],
            "description": fn["description"],
            "parameters": gemini_params,
        })
    return decls


def _function_call_part(name, args_obj, thought_signature, call_id):
    from google.genai import types
    fc_kwargs = {"name": name, "args": args_obj}
    if call_id:
        fc_kwargs["id"] = call_id
    part_kwargs = {"function_call": types.FunctionCall(**fc_kwargs)}
    if thought_signature:
        part_kwargs["thought_signature"] = thought_signature
    return types.Part(**part_kwargs)


def _function_response_part(name, result, call_id):
    from google.genai import types
    fr_kwargs = {"name": name, "response": {"result": result}}
    if call_id:
        fr_kwargs["id"] = call_id
    return types.Part(function_response=types.FunctionResponse(**fr_kwargs))


def openai_messages_to_gemini(messages):
    """OpenAI 风格 messages -> (system_instruction, contents)。

    messages 为 OpenAI Chat Completions 格式 list[dict]，role 含
    system / user / assistant / tool。返回 (system_instruction: str|None,
    contents: list[google.genai.types.Content])。

    同一轮连续的 role=tool 消息会被合并为一个 user Content（多个
    function_response parts），符合 Gemini 对并行工具结果的约定。
    """
    from google.genai import types

    system_instruction = None
    contents = []
    i = 0
    n = len(messages)

    while i < n:
        m = messages[i]
        role = m.get("role")

        if role == "system":
            system_instruction = m.get("content") or ""
            i += 1
            continue

        if role == "user":
            content = m.get("content")
            if isinstance(content, list):
                # OpenAI 多模态格式：text + image_url
                parts = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        parts.append(types.Part(text=item.get("text") or ""))
                    elif item.get("type") == "image_url":
                        url = (item.get("image_url") or {}).get("url", "")
                        if url:
                            parts.append(types.Part.from_uri(
                                file_uri=url, mime_type="image/jpeg"
                            ))
                contents.append(types.Content(role="user", parts=parts))
            else:
                contents.append(types.Content(
                    role="user", parts=[types.Part(text=content or "")]
                ))
            i += 1
            continue

        if role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(types.Part(text=m["content"]))
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments") or "{}"
                if isinstance(args, str):
                    try:
                        args_obj = json.loads(args)
                    except json.JSONDecodeError:
                        args_obj = {}
                else:
                    args_obj = args or {}
                sig = (tc.get("provider_metadata") or {}).get("thought_signature")
                call_id = tc.get("id")
                parts.append(_function_call_part(fn.get("name", ""), args_obj, sig, call_id))
            contents.append(types.Content(role="model", parts=parts))
            i += 1
            continue

        if role == "tool":
            # 合并同一轮连续的 tool 消息为一个 user Content
            # function_response 不携带 thought_signature（签名只保留在 function_call Part）
            response_parts = []
            while i < n and messages[i].get("role") == "tool":
                tm = messages[i]
                call_id = tm.get("tool_call_id")
                response_parts.append(_function_response_part(
                    tm.get("name", ""), tm.get("content") or "", call_id
                ))
                i += 1
            contents.append(types.Content(role="user", parts=response_parts))
            continue

        # 未知角色跳过，避免误构造成非法 Content
        i += 1

    return system_instruction, contents


def gemini_response_to_unified(response):
    """Gemini generate_content 响应 -> 统一结构 dict。

    返回::
      {
        "content": str,
        "tool_calls": [{"id", "name", "arguments", "provider_metadata"}],
        "finish_reason": str,
        "usage_total": int,
        "empty": bool,   # True 表示无候选/无 parts（如安全过滤）
      }
    """
    usage_total = 0
    prompt_tokens = 0
    completion_tokens = 0
    if getattr(response, "usage_metadata", None):
        usage_total = response.usage_metadata.total_token_count or 0
        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        completion_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

    candidates = getattr(response, "candidates", None)
    if not candidates:
        return {"content": "", "tool_calls": [], "finish_reason": "no_candidates",
                "usage_total": usage_total, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "empty": True}

    candidate = candidates[0]
    finish_reason = "stop"
    if hasattr(candidate, "finish_reason") and candidate.finish_reason is not None:
        finish_reason = getattr(candidate.finish_reason, "name", str(candidate.finish_reason))
    if str(finish_reason).upper() == "MAX_TOKENS":
        finish_reason = "length"
    else:
        finish_reason = str(finish_reason).lower()

    if not getattr(candidate, "content", None) or not candidate.content.parts:
        return {"content": "", "tool_calls": [], "finish_reason": finish_reason,
                "usage_total": usage_total, "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens, "empty": True}

    text_parts = []
    tool_calls = []
    for part in candidate.content.parts:
        if getattr(part, "text", None):
            text_parts.append(part.text)
        if hasattr(part, "function_call") and part.function_call:
            fn = part.function_call
            args_obj = dict(fn.args) if fn.args else {}
            # thought_signature 属于外层 Part，从 part 读取，非 FunctionCall
            sig = getattr(part, "thought_signature", None)
            # 保留 Gemini 原生 function_call.id，用于并行同名调用的关联
            call_id = getattr(fn, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
            meta = {"thought_signature": sig} if sig else None
            tool_calls.append({
                "id": call_id,
                "name": fn.name,
                "arguments": json.dumps(args_obj, ensure_ascii=False),
                "provider_metadata": meta,
            })

    return {
        "content": "\n".join(text_parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage_total": usage_total,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "empty": False,
    }
