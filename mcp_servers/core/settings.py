# before
# from pydantic import BaseSettings, Field

# after
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    customer_id: str = Field(default="base")
    debug: bool = False
    http_timeout: int = 15

    # 옵션: .env 자동 로드까지 원하면 아래 설정 추가
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
