# mcp_servers/tools/prefect_tools.py
from fastapi import APIRouter

class PrefectTool:
    name = "prefect"
    def __init__(self, api_url: str, api_key: str):
        self.api_url, self.api_key = api_url, api_key

    def get_router(self) -> APIRouter:
        r = APIRouter(prefix="/prefect", tags=["prefect"])
        @r.post("/trigger")
        async def trigger(flow_name: str, params: dict = {}):
            # Prefect 3.x API 호출 …
            return {"triggered": True, "flow": flow_name, "params": params}
        return r
