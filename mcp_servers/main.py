# mcp_servers/main.py
import os
from fastapi import FastAPI
from mcp_servers.customers.mcp_cust_01 import MCPCust01
from mcp_servers.customers.mcp_cust_02 import MCPCust02

def build() -> FastAPI:
    customer = os.getenv("CUSTOMER_ID", "cust01")
    server_map = {
        "cust01": MCPCust01,
        "cust02": MCPCust02,
    }
    srv_cls = server_map.get(customer)
    if not srv_cls:
        raise RuntimeError(f"Unknown CUSTOMER_ID: {customer}")
    return srv_cls().fastapi()

app = build()
# uvicorn mcp_servers.main:app --reload
