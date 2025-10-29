from fastapi import FastAPI
from mcp_servers.core.base import BaseMCPServer
from mcp_servers.core.settings import Settings
from mcp_servers.core.toolkit import ToolRegistry
from mcp_servers.tools.k8s import K8sTool

class Cust02Settings(Settings):
    customer_id: str = "cust02"
    debug: bool = False
    default_namespace: str = "cust02"
    kis_app_key: str = "dummy_key"
    kis_app_secret: str = "dummy_secret"

class MCPCust02(BaseMCPServer):
    def build_settings(self) -> Settings:
        return Cust02Settings()

    def register_tools(self, reg: ToolRegistry) -> None:
        reg.add(K8sTool(default_ns=self.settings.default_namespace))

# 🔴 FastAPI 인스턴스 반드시 필요
app: FastAPI = MCPCust02().fastapi()
