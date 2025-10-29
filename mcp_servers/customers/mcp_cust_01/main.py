# main.py (cust_01)
from fastapi import FastAPI
from mcp_servers.core.base import BaseMCPServer
from mcp_servers.core.settings import Settings
from mcp_servers.core.toolkit import ToolRegistry
from mcp_servers.tools.k8s import K8sTool
from mcp_servers.tools.prefect_tools import PrefectTool  # 이름 다르면 맞게 수정

class Cust01Settings(Settings):
    customer_id: str = "cust01"
    debug: bool = True
    default_namespace: str = "cust01"
    prefect_api_url: str = "http://prefect.local/api"
    prefect_api_key: str = "dummy"

class MCPCust01(BaseMCPServer):
    def build_settings(self) -> Settings:
        return Cust01Settings()

    def register_tools(self, reg: ToolRegistry) -> None:
        reg.add(K8sTool(default_ns=self.settings.default_namespace))
        reg.add(PrefectTool(api_url=self.settings.prefect_api_url,
                            api_key=self.settings.prefect_api_key))

# 🔴 여기서 실제 FastAPI 인스턴스를 만들어 export 해야 합니다.
app: FastAPI = MCPCust01().fastapi()
