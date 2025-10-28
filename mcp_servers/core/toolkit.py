# mcp_servers/core/toolkit.py
from typing import Protocol, Iterable
from fastapi import APIRouter

class Tool(Protocol):
    name: str
    def get_router(self) -> APIRouter: ...

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: list[Tool] = []
    def add(self, tool: Tool): self._tools.append(tool)
    def routers(self) -> Iterable[APIRouter]:
        for t in self._tools:
            yield t.get_router()
