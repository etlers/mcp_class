# mcp_servers/customers/mcp_cust_03/main.py
from fastapi import FastAPI
from mcp_servers.tools.azure import AzureSettings, build_azure_router

app = FastAPI(title="MCP Customer 03 Server")

# 고객3 환경(컨테이너/VM)에 AZ_* 환경변수만 설정해 두면 됩니다.
az_settings = AzureSettings()  # env 로드
app.include_router(build_azure_router(az_settings))  # /tools/az_list_resource_groups 등록
