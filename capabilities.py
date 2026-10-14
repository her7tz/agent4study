"""Registered Agent capabilities shared by manual work and scheduled work.

Register a Capability with its input schema, optional domain validator and
executor. The scheduler stores only its ID and validated JSON parameters;
it does not need to import or understand an individual feature.
"""

import copy
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness import DeepSeekClient, Settings


@dataclass
class CapabilityContext:
    settings: Settings
    logger: Any = None
    client: Optional[DeepSeekClient] = None
    progress: Optional[Callable[[str], None]] = None
    output_path: Optional[Path] = None
    cancellation: Any = None


def _validate_schema(value: Any, schema: Dict[str, Any], field: str) -> Any:
    """Validate the schema types/constraints used by capability input forms.

    This deliberately small implementation has no provider or UI dependency.
    A capability can supply a validator for additional domain constraints.
    """
    kind = schema.get("type")
    matches = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": lambda: value is None,
    }
    if kind not in matches:
        raise ValueError("{} 的参数 schema 使用了不支持的类型。".format(field))
    if not matches[kind]():
        raise ValueError("{} 必须是 {} 类型。".format(field, kind))
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise ValueError("{} 的值不在可选范围内。".format(field))
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        unknown = set(value) - set(properties)
        # Inputs are closed by default, including schemas without this keyword.
        if unknown:
            raise ValueError("{} 包含未知字段：{}。".format(field, ", ".join(sorted(map(str, unknown)))))
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError("{} 缺少必填字段：{}。".format(field, ", ".join(missing)))
        result: Dict[str, Any] = {}
        for name, subschema in properties.items():
            if name in value:
                result[name] = _validate_schema(value[name], subschema, "{}.{}".format(field, name))
            elif "default" in subschema:
                result[name] = _validate_schema(copy.deepcopy(subschema["default"]), subschema, "{}.{}".format(field, name))
        return result
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError("{} 的元素数量不符合要求。".format(field))
        return [_validate_schema(item, schema["items"], "{}[{}]".format(field, index)) for index, item in enumerate(value)]
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError("{} 的长度不符合要求。".format(field))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError("{} 的格式不符合要求。".format(field))
    if kind in {"integer", "number"}:
        if not math.isfinite(value):
            raise ValueError("{} 必须是有限数值。".format(field))
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError("{} 超出允许范围。".format(field))
    return value


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    executor: Callable[[Dict[str, Any], CapabilityContext], Dict[str, Any]]
    validator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
        }

    def validate(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        normalized = _validate_schema(parameters, self.input_schema, self.title + "参数")
        if self.validator:
            normalized = self.validator(normalized)
            normalized = _validate_schema(normalized, self.input_schema, self.title + "参数")
        return normalized


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: Dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", capability.id):
            raise ValueError("功能 ID 只能包含小写字母、数字和下划线，并以字母开头。")
        if capability.id in self._capabilities:
            raise ValueError("功能已经注册：{}。".format(capability.id))
        if capability.input_schema.get("type") != "object":
            raise ValueError("功能输入 schema 必须是 object。")
        self._capabilities[capability.id] = capability

    def get(self, capability_id: str) -> Capability:
        if not isinstance(capability_id, str) or capability_id not in self._capabilities:
            raise ValueError("未注册的功能：{}。".format(capability_id))
        return self._capabilities[capability_id]

    def list(self) -> List[Dict[str, Any]]:
        return [capability.metadata() for capability in self._capabilities.values()]

    def validate(self, capability_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        return self.get(capability_id).validate(parameters)

    def execute(self, capability_id: str, parameters: Dict[str, Any], context: CapabilityContext) -> Dict[str, Any]:
        capability = self.get(capability_id)
        normalized = capability.validate(parameters)
        if context.cancellation:
            context.cancellation.check()
        result = capability.executor(normalized, context)
        if context.cancellation:
            context.cancellation.check()
        if not isinstance(result, dict):
            raise RuntimeError("{} 必须返回结果对象。".format(capability.title))
        result = dict(result)
        result["capability"] = capability_id
        result.setdefault("title", capability.title)
        if isinstance(result.get("path"), Path):
            result["path"] = str(result["path"])
        if not result.get("path"):
            key = "markdown" if isinstance(result.get("markdown"), str) else "text"
            text = result.get(key)
            if isinstance(text, str) and text.strip():
                directory = context.settings.output_dir
                directory.mkdir(parents=True, exist_ok=True)
                filename = "{}_{}_{}{}".format(capability_id, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8], ".md" if key == "markdown" else ".txt")
                target = context.output_path or directory / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                pending = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(target.parent), delete=False) as stream:
                        pending = Path(stream.name)
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(str(pending), str(target))
                finally:
                    if pending:
                        pending.unlink(missing_ok=True)
                result["path"] = str(target)
        return result


def _validate_video(parameters: Dict[str, Any]) -> Dict[str, Any]:
    from video_summary import validate_video_url

    parameters["url"] = validate_video_url(parameters["url"])
    return parameters


def _execute_video(parameters: Dict[str, Any], context: CapabilityContext) -> Dict[str, Any]:
    from video_summary import summarize_video, _CANCELLATION
    client = context.client or DeepSeekClient(context.settings)
    token = context.cancellation
    if token:
        class BoundClient:
            def chat(self, *args, **kwargs):
                token.check()
                return client.chat(*args, **dict(kwargs, cancellation=token))

            def json_chat(self, *args, **kwargs):
                token.check()
                return client.json_chat(*args, **dict(kwargs, cancellation=token))
        bound = BoundClient()
    else:
        bound = client
    marker = _CANCELLATION.set(token)
    try:
        return summarize_video(
            parameters["url"], bound, context.settings,
            include_frames=parameters["include_frames"], output_path=context.output_path,
            logger=context.logger, progress=context.progress)
    finally:
        _CANCELLATION.reset(marker)


registry = CapabilityRegistry()
registry.register(
    Capability(
        id="video_summary",
        title="视频总结",
        description="读取公开的 B 站或 YouTube 视频，整理为知识笔记。",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "title": "视频链接", "minLength": 1, "format": "uri", "description": "公开的 B 站或 YouTube 视频链接"},
                "include_frames": {"type": "boolean", "title": "提取知识相关原图", "default": True},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        executor=_execute_video,
        validator=_validate_video,
    )
)


def _execute_research(parameters, context):
    from web_research import research_report
    return research_report(parameters, context)


registry.register(Capability(
    id="web_research", title="联网研究",
    description="围绕问题搜索并阅读公开网页，比较资料，生成带来源引用的研究报告并自动保存。",
    input_schema={"type": "object", "properties": {
        "topic": {"type": "string", "title": "研究问题", "minLength": 1, "maxLength": 500},
        "max_sources": {"type": "integer", "title": "最多读取的来源数", "default": 5, "minimum": 2, "maximum": 8}},
        "required": ["topic"], "additionalProperties": False}, executor=_execute_research))


def list_capabilities() -> List[Dict[str, Any]]:
    return registry.list()


def get_capability(capability_id: str) -> Capability:
    return registry.get(capability_id)


def validate_parameters(capability_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return registry.validate(capability_id, parameters)


def execute_capability(
    capability_id: str,
    parameters: Dict[str, Any],
    settings: Settings,
    logger: Any = None,
    client: Optional[DeepSeekClient] = None,
    progress: Optional[Callable[[str], None]] = None,
    output_path: Optional[Path] = None,
    cancellation: Any = None,
) -> Dict[str, Any]:
    return registry.execute(
        capability_id, parameters,
        CapabilityContext(settings, logger, client, progress, output_path, cancellation),
    )
