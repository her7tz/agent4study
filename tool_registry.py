"""Provider-neutral tool contracts; the Agent exposes these through an adapter."""

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    executor: Callable
    effect: str = "read"

    def metadata(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError("工具已注册：" + tool.name)
        self._tools[tool.name] = tool

    def metadata(self):
        return [tool.metadata() for tool in self._tools.values()]

    def call(self, name, arguments, context):
        from capabilities import _validate_schema
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError("未注册的工具：" + str(name))
        normalized = _validate_schema(arguments, tool.parameters, tool.name)
        return tool.executor(normalized, context)
