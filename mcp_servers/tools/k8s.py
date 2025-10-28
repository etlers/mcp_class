# mcp_servers/tools/k8s.py
from fastapi import APIRouter
from .common import kube_exec  # 선택: 공통 유틸

class K8sTool:
    name = "k8s"
    def __init__(self, default_ns: str = "default"):
        self.default_ns = default_ns

    def get_router(self) -> APIRouter:
        r = APIRouter(prefix="/k8s", tags=["k8s"])
        @r.get("/pods")
        async def list_pods(ns: str | None = None):
            ns = ns or self.default_ns
            return await kube_exec(["kubectl", "get", "pods", "-n", ns, "-o", "json"])
        return r
