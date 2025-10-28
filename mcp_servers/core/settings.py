# mcp_servers/core/settings.py
from pydantic import BaseSettings, Field

class Settings(BaseSettings):
    customer_id: str = Field(default="base")
    # 공통 옵션
    debug: bool = False
    http_timeout: int = 15
    # 고객별/툴별 설정도 여기 확장 가능
