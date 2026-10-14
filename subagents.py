"""Isolated business Agent loops; ordinary conversations do not enter these."""

import json
import time

from business_harnesses import BUSINESS_HARNESSES
from runtime_control import RunCancelled


def run_business_subagent(capability_id, parameters, context):
    harness = BUSINESS_HARNESSES[capability_id](parameters, context)
    shared = harness.shared
    messages = [{"role": "system", "content": harness.instructions()},
                {"role": "user", "content": json.dumps({"goal": harness.title, "parameters": harness.task_description()}, ensure_ascii=False)}]
    deadline = time.monotonic() + harness.time_budget
    production_error = None
    try:
        for index in range(harness.max_rounds):
            shared.cancellation.check()
            if time.monotonic() >= deadline:
                raise RuntimeError(harness.title + "超过运行时间预算。")
            shared.trace("model_round", number=index + 1)
            reply = shared.model.chat(messages, tools=harness.tools.metadata())
            raw = {key: reply[key] for key in ("role", "content", "reasoning_content", "tool_calls") if key in reply}
            raw["role"] = "assistant"
            calls = raw.get("tool_calls") or []
            if not calls:
                if harness.finished:
                    break
                # A prose response is not evidence that a business task finished.
                messages.append(raw)
                messages.append({"role": "user", "content": "尚未通过完成检查。请依据工具返回状态继续 produce_result、inspect_result 或 finish_task；不要只给建议。"})
                continue
            if not isinstance(calls, list) or len(calls) > 6 or any(not isinstance(call, dict) or not call.get("id") or not isinstance(call.get("function"), dict) for call in calls):
                raise RuntimeError("业务 Agent 返回了无效的工具调用。")
            messages.append(raw)
            for call in calls:
                function = call["function"]
                try:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("业务运行时间预算已用完。")
                    arguments = json.loads(function.get("arguments") or "{}")
                    result = shared.execute_tool(harness.tools, function.get("name", ""), arguments)
                except RunCancelled:
                    raise
                except Exception as exc:
                    detail = str(exc).replace(shared.settings.api_key, "[已隐藏凭证]") if shared.settings.api_key else str(exc)
                    result = {"success": False, "error": detail[:1200]}
                    shared.trace("tool_failed", name=function.get("name", ""), error=detail[:1200])
                    if function.get("name") == "produce_result":
                        production_error = detail[:1200]
                    # Invalid/unavailable input cannot be repaired by repeating the same tool.
                    if not getattr(exc, "retryable", True):
                        raise
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            if harness.finished:
                break
        if not harness.finished:
            raise RuntimeError(harness.title + "未通过完成检查。" + ("处理原因：" + production_error if production_error else "请查看运行记录后重试。"))
        result = dict(harness.result)
        result["subagent"] = {"role": capability_id, "run_id": shared.run_id, "verified": True}
        return result
    except RunCancelled:
        shared.trace("cancelled")
        raise
    except Exception as exc:
        shared.trace("failed", error=str(exc)[:1200])
        raise
