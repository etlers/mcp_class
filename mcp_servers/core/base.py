# mcp_servers/core/base.py
from fastapi import FastAPI, Request
from .toolkit import ToolRegistry, Tool
from .settings import Settings

class BaseMCPServer:
    """
    템플릿 훅:
      - build_settings()
      - register_tools(registry)
      - before_request(request)
      - after_response(response)
    """
    def __init__(self) -> None:
        self.settings = self.build_settings()
        self.registry = ToolRegistry()
        self.app = FastAPI(title=f"MCP [{self.settings.customer_id}]")
        self._wire_up()

    # --- Template hooks ---
    def build_settings(self) -> Settings:
        return Settings()  # 기본값(환경변수로 override)

    def register_tools(self, reg: ToolRegistry) -> None:
        """하위 클래스/고객 서버에서 툴 추가"""
        pass

    async def before_request(self, request: Request) -> None:
        """공통 프리프로세싱(채널/고객 헤더 주입, 감사로그 등)"""
        return None

    # --- internal wiring ---
    def _wire_up(self):
        # 툴 등록(하위에서 확장)
        self.register_tools(self.registry)

        # 미들웨어(요청 공통 처리)
        @self.app.middleware("http")
        async def _preprocess(request, call_next):
            await self.before_request(request)
            resp = await call_next(request)
            return resp

        # 툴 라우터 부착
        for router in self.registry.routers():
            self.app.include_router(router)

    def fastapi(self) -> FastAPI:
        return self.app
