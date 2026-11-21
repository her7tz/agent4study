"""Model adapter contract used by the Agent and domain workflows.

Adapters normalize provider responses to message dictionaries. Transport,
authentication and provider-specific fields remain inside the adapter.
"""
from typing import Any, Protocol


class ModelJSONError(RuntimeError):
    """A received model reply is not the requested JSON object."""


class ModelClient(Protocol):
    settings: Any
    timeout: float

    def chat(self, messages, tools=None, **kwargs) -> dict: ...

    def stream_chat(self, messages, tools=None, on_delta=None, **kwargs) -> dict: ...

    def json_chat(self, system, user, **kwargs) -> dict: ...
