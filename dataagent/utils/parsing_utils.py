# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import ast
import json
import re
import uuid
from typing import Any


def strip_code_fences(s: str) -> str:
    """去掉字符串首尾的 Markdown 代码块围栏（```lang ... ```），返回纯内容。"""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def remove_think_block(text: str, separator_token: str) -> str:
    """
    移除字符串中 separator_token 之前的内容（如 LLM 的 think 块）。

    Args:
        text: 原始文本
        separator_token: 分隔符（如 </think>）

    Returns:
        分隔符之后的文本
    """
    index = text.find(separator_token)
    if index != -1:
        text = text[index + len(separator_token) :]
    return text


def loads(
    s: str,
    *,
    strict: bool = False,
    fallback_literal: bool = False,
) -> Any:
    """
    统一解析字符串为 JSON 或 Python 字面量。

    - strict: 仅 json.loads，失败返回 None
    - fallback_literal: json 失败后尝试 ast.literal_eval（如 action payload）
    - 默认: strip_fences → json → json_repair，失败 raise ValueError
    """
    s = strip_code_fences(s) if not strict else (s or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        if strict:
            return None
        if fallback_literal:
            try:
                return ast.literal_eval(s)
            except Exception as e:
                raise ValueError(f"Failed to parse: {e}") from e
        raise ValueError("Failed to parse as JSON") from None


def extract_json_block(text: str) -> Any:
    """
    从文本中提取并解析 JSON 块。

    策略：
    1) 优先解析 fenced code block（```json ... ``` 或 ``` ... ```）
    2) 若存在多个 fenced block，逐个尝试解析
    3) 兜底：从全文中截取第一个 JSON 对象/数组
    """
    # 1) 优先解析 fenced code block
    fence_pattern = r"```(?:json)?\s*([\s\S]*?)```"
    fenced = re.findall(fence_pattern, text, flags=re.IGNORECASE)
    if len(fenced) == 1:
        return loads(fenced[0].strip())

    # 2) 若存在多个 fenced block，优先挑能解析成 JSON 的那个
    if len(fenced) > 1:
        last_err: Exception | None = None
        for block in fenced:
            try:
                return loads(block.strip())
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err

    # 3) 最后兜底：从全文中截取第一个 JSON 对象/数组
    text = text.strip()
    if not text:
        raise ValueError("Empty text")
    first_obj = text.find("{")
    first_arr = text.find("[")
    if first_obj == -1 and first_arr == -1:
        return loads(text)
    start = min(x for x in (first_obj, first_arr) if x != -1)
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)
    if end == -1 or end <= start:
        return loads(text)
    return loads(text[start : end + 1])


def normalize_newlines(data: Any) -> Any:
    """
    将字符串中的换行符或实际换行规范成真实换行。
    递归处理 dict/list 中的字符串。
    """
    if isinstance(data, str):
        return data.replace("\\n", "\n").replace("\r\n", "\n")
    if isinstance(data, list):
        return [normalize_newlines(item) for item in data]
    if isinstance(data, dict):
        return {k: normalize_newlines(v) for k, v in data.items()}
    return data


def parse_destination(dest_field: str) -> tuple[str, str | None]:
    """
    解析 Markdown 引用目标字段（可能包含可选 title）。
    用于解析 `[id]: path "optional title"` 或 `<url>` 形式。

    Returns:
        (url, title) 元组，title 可能为 None
    """
    dest = dest_field.strip()
    title: str | None = None
    if dest.startswith("<"):
        gt_index = dest.find(">")
        if gt_index != -1:
            url = dest[1:gt_index]
            remainder = dest[gt_index + 1 :].strip()
        else:
            url = dest
            remainder = ""
    else:
        m = re.match(r"^(\S+)(?:\s+(\".*\"|'.*'))?\s*$", dest)
        if m:
            url = m.group(1)
            if m.lastindex and m.lastindex >= 2:
                title = m.group(2)
            remainder = ""
        else:
            parts = dest.split()
            url = parts[0]
            remainder = " ".join(parts[1:])

    if title is None and remainder:
        m2 = re.match(r"^(\".*\"|'.*')\s*$", remainder)
        if m2:
            title = m2.group(1)
    return url, title


def parse_action_payloads_to_tool_calls(
    *,
    payloads: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    解析所有 action payloads，生成 tool_calls / invalid_tool_calls / tool_call_meta（保持 parse_actions_to_ai_message 的旧行为）。
    """
    tool_calls: list[dict[str, Any]] = []
    invalid_tool_calls: list[dict[str, Any]] = []
    tool_call_meta: dict[str, dict[str, Any]] = {}

    for _idx, payload_raw in enumerate(payloads, start=1):
        call_id = f"{uuid.uuid4()}"
        try:
            parsed = _parse_action_payload_best_effort(payload_raw)
            if parsed is None:
                raise ValueError("unsupported <action> payload format")

            tool_call, meta, invalid = _action_parsed_to_tool_call_or_invalid(parsed, call_id=call_id)
            if invalid is not None:
                invalid_tool_calls.append(invalid)
                continue
            if tool_call is not None:
                tool_calls.append(tool_call)
            if meta is not None:
                tool_call_meta[call_id] = meta
        except Exception as e:
            invalid_tool_calls.append(
                {"id": call_id, "name": "unknown", "error": f"failed to parse <action> block: {e}"}
            )

    return tool_calls, invalid_tool_calls, tool_call_meta


def parse_kv_style_action(s: str) -> dict[str, Any] | None:
    """
    兼容 log 里常见的 “多行 key = value” 形式（不是 JSON）。
    需要至少解析出 action_name；action_parameters 若缺失则为空 dict。
    """
    s = (s or "").strip()
    if not s or "=" not in s:
        return None

    action_name = _extract_kv_string_value(s, key="action_name")
    if not action_name:
        return None

    action_id = _extract_kv_string_value(s, key="action_id")
    desc = _extract_kv_string_value(s, key="description") or ""

    params_obj: Any = _extract_kv_action_parameters(s)
    params_obj = _ensure_action_parameters_dict(params_obj)

    # 兼容“混合输出”：kv + 后缀 JSON（仅当 kv 没显式给 action_parameters 时才补齐，JSON 优先）
    if params_obj == {}:
        action_id, desc, params_obj = _merge_action_json_suffix_if_any(
            raw=s,
            action_id=action_id,
            desc=desc,
            params=params_obj,
        )

    return {
        "action_id": action_id,
        "description": desc,
        "action_name": action_name,
        "action_parameters": params_obj,
    }


def _extract_kv_string_value(raw: str, key: str) -> str:
    """从 `key = value` 风格文本中抽取一行字符串值（去掉引号与首尾空白）；未命中返回空串。"""
    if not raw or not key:
        return ""
    m = re.search(rf"{re.escape(key)}\s*=\s*([^\n\r#]+)", raw)
    if not m:
        return ""
    return (m.group(1) or "").strip().strip('"').strip("'")


def _extract_kv_action_parameters(raw: str) -> Any:
    """
    提取 action_parameters（可选）。

    - 支持跨多行（取 action_parameters=... 到文本末尾）
    - 若其中包含 `{...}` 块，则优先截取该块作为参数载荷
    - 解析失败时返回 `{"_raw_action_parameters": <raw>}`（保持旧行为）
    """
    if not raw:
        return {}
    m_params = re.search(r"action_parameters\s*=\s*([\s\S]+)$", raw)
    if not m_params:
        return {}

    params_raw = (m_params.group(1) or "").strip()
    m_brace = re.search(r"(\{[\s\S]*\})", params_raw)
    if m_brace:
        params_raw = (m_brace.group(1) or "").strip()

    try:
        return loads(params_raw, fallback_literal=True)
    except Exception:
        return {"_raw_action_parameters": params_raw}


def _ensure_action_parameters_dict(params_obj: Any) -> dict[str, Any]:
    """确保 action_parameters 一定为 dict；否则包一层 `_raw_action_parameters`。"""
    if isinstance(params_obj, dict):
        return params_obj
    return {"_raw_action_parameters": params_obj}


def _looks_like_action_json(obj: dict[str, Any]) -> bool:
    """仅在看起来像 action JSON 时才合并，避免误把其它花括号块当 action。"""
    return "action_parameters" in obj or "action_id" in obj or "description" in obj


def _extract_action_parameters_from_action_json(obj: dict[str, Any]) -> dict[str, Any]:
    """
    从 action JSON 中抽取 action_parameters 并保证为 dict：
    - dict: 直接返回
    - str: best-effort 解析为 dict；失败/非 dict 则返回 {}
    """
    ap = obj.get("action_parameters")
    if isinstance(ap, dict):
        return ap
    if isinstance(ap, str) and ap.strip():
        try:
            ap2 = loads(ap, fallback_literal=True)
            if isinstance(ap2, dict):
                return ap2
        except Exception:
            return {}
    return {}


def _merge_action_json_suffix_if_any(
    *,
    raw: str,
    action_id: Any,
    desc: str,
    params: dict[str, Any],
) -> tuple[Any, str, dict[str, Any]]:
    """
    当 kv 未给 action_parameters 时，尝试从文本中的“后缀 JSON 对象”补齐 params / action_id / description。
    只合并第一个满足 `_looks_like_action_json` 的 JSON 对象（保持旧行为：找到即 break）。
    """
    if params != {}:
        return action_id, desc, params

    for m_json in re.finditer(r"(\{[\s\S]*\})", raw or ""):
        cand = (m_json.group(1) or "").strip()
        obj = loads(cand, fallback_literal=True)
        if not isinstance(obj, dict):
            continue
        if not _looks_like_action_json(obj):
            continue

        parsed_params = _extract_action_parameters_from_action_json(obj)
        if parsed_params:
            params = parsed_params

        if (action_id == "" or action_id is None) and obj.get("action_id", None) not in (None, ""):
            action_id = obj.get("action_id")
        if not desc and obj.get("description"):
            desc = str(obj.get("description") or "")
        break

    return action_id, desc, params


def extract_action_payloads(text: str) -> list[str]:
    """提取所有 `<action>...</action>` 的 payload（宽松：大小写不敏感）。"""
    raw = str(text or "")
    return [m.group(1) for m in re.finditer(r"<action>\s*([\s\S]*?)\s*</action>", raw, flags=re.IGNORECASE)]


def _parse_action_payload_best_effort(payload_raw: str) -> dict[str, Any] | None:
    """
    将单个 `<action>` payload 解析为 dict（best-effort），失败返回 None 或抛出 ValueError：
    - 若检测到 `action_name =`：优先按 kv 解析，避免 json_repair 误解析
    - 否则：先 loads，再降级 kv
    """
    payload_raw = str(payload_raw or "")
    looks_like_kv = bool(re.search(r"action_name\s*=", payload_raw))
    if looks_like_kv:
        parsed = parse_kv_style_action(payload_raw)
    else:
        try:
            parsed = loads(payload_raw, fallback_literal=True)
        except Exception:
            parsed = parse_kv_style_action(payload_raw)

    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("action payload must be a JSON object")
    return parsed


def _normalize_action_parameters(args: Any) -> dict[str, Any]:
    """将 action_parameters 规整为 dict；失败时回退到 `_raw_action_parameters`。"""
    if isinstance(args, str) and args.strip():
        try:
            args = loads(args, fallback_literal=True)
        except Exception:
            return {"_raw_action_parameters": args}
    if isinstance(args, dict):
        return args
    return {"_raw_action_parameters": args}


def _action_parsed_to_tool_call_or_invalid(
    parsed: dict[str, Any],
    *,
    call_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """
    将已解析的 action dict 转换为 tool_call + meta。

    返回：(tool_call, meta, invalid_item)，三者最多一个为 None（invalid_item 非 None 表示该 action 无法形成 tool_call）。
    """
    tool_name = str(parsed.get("action_name") or "").strip()
    action_id = parsed.get("action_id", "")
    if not tool_name:
        return (
            None,
            None,
            {"id": call_id, "name": "unknown", "error": f"missing action_name for action_id={action_id}"},
        )

    args = _normalize_action_parameters(parsed.get("action_parameters", {}))
    tool_call = {"id": call_id, "name": tool_name, "args": args}
    meta = {"action_id": str(action_id), "description": str(parsed.get("description") or "")}
    return tool_call, meta, None
