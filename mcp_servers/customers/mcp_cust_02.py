# mcp_servers/customers/mcp_cust_02.py
from mcp_servers.core.base import BaseMCPServer
from mcp_servers.core.toolkit import ToolRegistry
from mcp_servers.tools.k8s import K8sTool
from mcp_servers.tools.kis import KISTool

class MCPCust02(BaseMCPServer):
    def register_tools(self, reg: ToolRegistry) -> None:
        # 같은 k8s라도 기본 네임스페이스/권한 다르게
        reg.add(K8sTool(default_ns="cust02"))
        # cust02는 KIS 트레이딩 툴 활성화
        reg.add(KISTool(
            base_url="https://openapi.koreainvestment.com:9443",
            app_key="****", app_secret="****"
        ))
