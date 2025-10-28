# mcp_servers/customers/mcp_cust_01.py
from mcp_servers.core.base import BaseMCPServer
from mcp_servers.core.settings import Settings
from mcp_servers.core.toolkit import ToolRegistry
from mcp_servers.tools.k8s import K8sTool
from mcp_servers.tools.prefect import PrefectTool

class Cust01Settings(Settings):
    customer_id: str = "cust01"
    debug: bool = True
    prefect_api_url: str = "https://prefect.cust01/api"
    prefect_api_key: str = "****"
    default_namespace: str = "cust01"

class MCPCust01(BaseMCPServer):
    def build_settings(self) -> Settings:
        return Cust01Settings()

    def register_tools(self, reg: ToolRegistry) -> None:
        reg.add(K8sTool(default_ns=self.settings.default_namespace))
        reg.add(PrefectTool(
            api_url=self.settings.prefect_api_url,
            api_key=self.settings.prefect_api_key,
        ))

    async def before_request(self, request):
        # 예: 채널/고객 헤더 → 내부 호출에 주입, 감사로그 등
        # x_channel_id = request.headers.get("x-channel-id")
        return None
