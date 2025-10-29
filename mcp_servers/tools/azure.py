# mcp_servers/tools/azure.py
from __future__ import annotations
import os
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, BaseSettings, Field
from fastapi import APIRouter, HTTPException
import httpx

# 필요 시: pip install azure-identity
from azure.identity import ClientSecretCredential  # , ManagedIdentityCredential

ARM_SCOPE_DEFAULT = "https://management.azure.com/.default"
ARM_BASE_DEFAULT  = "https://management.azure.com"
API_VER_DEFAULT   = "2024-03-01"

class AzureSettings(BaseSettings):
    # 표준 이름 유지: AZ_* 환경변수로 주입
    tenant_id: str = Field(..., env="AZ_TENANT_ID")
    client_id: str = Field(..., env="AZ_CLIENT_ID")
    client_secret: str = Field(..., env="AZ_CLIENT_SECRET")
    default_subscription_id: Optional[str] = Field(None, env="AZ_SUBSCRIPTION_ID")

    # 옵션
    arm_scope: str = Field(ARM_SCOPE_DEFAULT, env="AZ_ARM_SCOPE")
    arm_base: str  = Field(ARM_BASE_DEFAULT,  env="AZ_ARM_BASE")
    api_version: str = Field(API_VER_DEFAULT, env="AZ_API_VERSION")

class RGReq(BaseModel):
    subscription_id: Optional[str] = None

class RGItem(BaseModel):
    name: str
    location: Optional[str] = None
    id: Optional[str] = None
    tags: Optional[Dict[str, Any]] = None

class RGResp(BaseModel):
    result: Dict[str, Any]
    data: List[RGItem]

def _get_token(settings: AzureSettings) -> str:
    # 필요 시 ManagedIdentityCredential 사용 고려:
    # cred = ManagedIdentityCredential()  # MSI 환경일 때
    cred = ClientSecretCredential(
        tenant_id=settings.tenant_id,
        client_id=settings.client_id,
        client_secret=settings.client_secret
    )
    token = cred.get_token(settings.arm_scope)
    return token.token

async def _list_resource_groups(settings: AzureSettings, subscription_id: str) -> List[RGItem]:
    url = f"{settings.arm_base}/subscriptions/{subscription_id}/resourcegroups"
    params = {"api-version": settings.api_version}
    headers = {"Authorization": f"Bearer {_get_token(settings)}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=headers, params=params)
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        data = r.json()

    items: List[RGItem] = []
    for rg in data.get("value", []):
        items.append(RGItem(
            name=rg.get("name", ""),
            location=rg.get("location"),
            id=rg.get("id"),
            tags=rg.get("tags", {})
        ))
    return items

def build_azure_router(settings: AzureSettings, prefix: str = "/tools") -> APIRouter:
    """
    공용 Azure 도구 라우터 생성.
    각 고객 서버는 app.include_router(build_azure_router(settings)) 로 재사용.
    """
    router = APIRouter(prefix=prefix, tags=["azure"])

    @router.post("/az_list_resource_groups", response_model=RGResp)
    async def az_list_resource_groups(req: RGReq):
        sub_id = req.subscription_id or settings.default_subscription_id
        if not sub_id:
            raise HTTPException(
                status_code=400,
                detail="subscription_id is required (pass in request body or set AZ_SUBSCRIPTION_ID)."
            )
        items = await _list_resource_groups(settings, sub_id)
        return RGResp(
            result={"content": [{"type": "text", "text": "Azure Resource Groups fetched."}]},
            data=items
        )

    return router
