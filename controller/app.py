# mattermost_proxy/app.py
import os, json, asyncio, logging, math
from typing import Any, Dict, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------- 환경 -----------------------------
MATTERMOST_VERIFY_TOKEN = os.getenv("MATTERMOST_WEBHOOK_TOKEN", "")
RESPONSE_TYPE = os.getenv("RESPONSE_TYPE", "ephemeral")   # "ephemeral" | "in_channel"
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "2"))
RETRY_SLEEP_SEC = float(os.getenv("RETRY_SLEEP_SEC", "0.5"))
VERIFY_TLS = os.getenv("VERIFY_TLS", "1") == "1"
FOLLOWUP_THRESHOLD = int(os.getenv("FOLLOWUP_THRESHOLD", "1800"))  # 본문 길이가 이 값 초과면 webhook로 후속전송

# 채널→고객
CHANNEL_TO_CUSTOMER: Dict[str, str] = {}
if os.getenv("CHANNEL_MAP_JSON"):
    CHANNEL_TO_CUSTOMER.update(json.loads(os.getenv("CHANNEL_MAP_JSON")))
else:
    CHANNEL_TO_CUSTOMER.update({
        "xyb58qpifff3df9pytodz3hfra": "cust01",
        "4xd3frqsx3b79x46hwuqid594w": "cust02",
    })

# 고객→MCP 서버 URL
CUSTOMER_TO_MCP: Dict[str, str] = {}
if os.getenv("CUSTOMER_MAP_JSON"):
    CUSTOMER_TO_MCP.update(json.loads(os.getenv("CUSTOMER_MAP_JSON")))
else:
    CUSTOMER_TO_MCP.update({
        "cust01": "http://localhost:8001",
        "cust02": "http://localhost:8002",
    })

# (선택) 채널→Incoming Webhook URL
# 예: CHANNEL_WEBHOOK_JSON='{"xyb...fra":"https://mm.example/hooks/abc123"}'
CHANNEL_TO_WEBHOOK: Dict[str, str] = {}
if os.getenv("CHANNEL_WEBHOOK_JSON"):
    CHANNEL_TO_WEBHOOK.update(json.loads(os.getenv("CHANNEL_WEBHOOK_JSON")))

# ----------------------------- 앱/로그 -----------------------------
app = FastAPI(title="Mattermost → MCP Gateway (Pattern A, Extended)")
logger = logging.getLogger("mcp_gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ----------------------------- 유틸 -----------------------------
async def parse_mm_body(req: Request) -> Dict[str, Any]:
    ctype = (req.headers.get("content-type") or "").lower()
    if ctype.startswith("application/x-www-form-urlencoded"):
        form = await req.form()
        return {k: (v if isinstance(v, str) else v.decode() if hasattr(v, "decode") else str(v)) for k, v in form.items()}
    else:
        try:
            return await req.json()
        except Exception:
            return {}

def mm_ok_text(text: str, response_type: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"response_type": response_type or RESPONSE_TYPE, "text": text})

def mm_error_text(text: str, response_type: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"response_type": response_type or "ephemeral", "text": f":warning: {text}"}, status_code=200)

async def post_with_retry(url: str, *, headers: Dict[str, str], json_body: Dict[str, Any],
                          timeout: float, verify: bool, retries: int, sleep_sec: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        for attempt in range(retries + 1):
            try:
                return await client.post(url, headers=headers, json=json_body)
            except Exception:
                if attempt < retries:
                    await asyncio.sleep(sleep_sec)
                else:
                    raise

def chunk_text(text: str, chunk_size: int = 3500) -> List[str]:
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

async def send_mm_webhook(channel_id: str, text: str, *, username: Optional[str]=None, icon_emoji: Optional[str]=None) -> None:
    """채널별 Incoming Webhook으로 후속 메시지 전송"""
    url = CHANNEL_TO_WEBHOOK.get(channel_id)
    if not url:
        logger.warning("No webhook mapped for channel_id=%s; skipping follow-up", channel_id)
        return
    payload: Dict[str, Any] = {"text": text}
    if username: payload["username"] = username
    if icon_emoji: payload["icon_emoji"] = icon_emoji
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=VERIFY_TLS) as c:
        r = await c.post(url, json=payload)
        if r.status_code >= 300:
            logger.error("Webhook post failed: %s %s", r.status_code, r.text[:500])

def to_markdown_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "_(no rows)_"
    cols = list(rows[0].keys())
    head = "| " + " | ".join(cols) + " |\n"
    sep  = "| " + " | ".join(["---"]*len(cols)) + " |\n"
    body = "".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n" for r in rows)
    return head + sep + body

def resolve_customer_and_mcp(channel_id: str) -> tuple[str, str]:
    customer_id = CHANNEL_TO_CUSTOMER.get(channel_id)
    if not customer_id:
        raise HTTPException(403, f"Unknown channel_id: `{channel_id}` (route not configured)")
    mcp = CUSTOMER_TO_MCP.get(customer_id)
    if not mcp:
        raise HTTPException(502, f"No MCP server configured for customer `{customer_id}`")
    return customer_id, mcp.rstrip("/")

# ----------------------------- 헬스/운영 -----------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/admin/route")
async def admin_route():
    return {"channels": CHANNEL_TO_CUSTOMER, "customers": CUSTOMER_TO_MCP, "webhooks": {k: "***" for k in CHANNEL_TO_WEBHOOK}}

# ----------------------------- ① Slash Command 기본 라우팅 -----------------------------
@app.post("/mattermost/cmd")
async def mattermost_cmd(req: Request, bg: BackgroundTasks):
    body = await parse_mm_body(req)

    # (선택) 토큰검증
    incoming_token = body.get("token") or body.get("verification_token") or req.headers.get("X-MM-Token")
    if MATTERMOST_VERIFY_TOKEN:
        if not incoming_token:
            return mm_error_text("Missing verification token.")
        if incoming_token != MATTERMOST_VERIFY_TOKEN:
            return mm_error_text("Invalid verification token.")

    channel_id = body.get("channel_id") or req.headers.get("X-Channel-Id")
    if not channel_id:
        return mm_error_text("channel_id is missing in request.")
    team_id = body.get("team_id") or req.headers.get("X-Team-Id")
    user_id = body.get("user_id") or req.headers.get("X-User-Id")
    text = (body.get("text") or "").strip()

    customer_id, mcp_base = resolve_customer_and_mcp(channel_id)
    target_url = f"{mcp_base}/router"

    headers = {"content-type": "application/json", "x-customer-id": customer_id, "x-channel-id": channel_id}
    if team_id: headers["x-team-id"] = team_id
    if user_id: headers["x-user-id"] = user_id

    payload = dict(body)
    payload["_proxy_ctx"] = {"source": "mattermost", "route_by": "channel_id", "customer_id": customer_id}

    try:
        resp = await post_with_retry(target_url, headers=headers, json_body=payload,
                                     timeout=HTTP_TIMEOUT, verify=VERIFY_TLS, retries=RETRY_COUNT, sleep_sec=RETRY_SLEEP_SEC)
    except Exception as e:
        logger.exception("Forward error")
        return mm_error_text(f"Forwarding failed to MCP `{customer_id}`: {e}")

    ct = resp.headers.get("content-type", "")
    if resp.status_code >= 400:
        detail = resp.text[:2000]
        return mm_error_text(f"MCP `{customer_id}` error ({resp.status_code}):\n```\n{detail}\n```")

    # JSON이면 그대로 혹은 pretty
    if "application/json" in ct:
        try:
            data = resp.json()
            if isinstance(data, dict) and "text" in data:
                # Mattermost 형식 그대로
                txt = data.get("text", "")
                if len(txt) > FOLLOWUP_THRESHOLD and channel_id in CHANNEL_TO_WEBHOOK:
                    # 즉답은 짧게, 본문은 후속웹훅
                    head = f":hourglass_flowing_sand: 결과가 길어서 웹훅으로 후속 전달합니다. (customer={customer_id})"
                    bg.add_task(send_mm_webhook, channel_id, txt, username="MCP-Gateway", icon_emoji=":robot_face:")
                    return mm_ok_text(head, response_type="ephemeral")
                return JSONResponse(data)
            # pretty 출력
            pretty = json.dumps(data, ensure_ascii=False, indent=2)
            if len(pretty) > FOLLOWUP_THRESHOLD and channel_id in CHANNEL_TO_WEBHOOK:
                head = f":hourglass_flowing_sand: JSON 응답이 커서 웹훅으로 후속 전달합니다. (len={len(pretty)})"
                for chunk in chunk_text(f"```json\n{pretty}\n```"):
                    bg.add_task(send_mm_webhook, channel_id, chunk, username="MCP-Gateway")
                return mm_ok_text(head, response_type="ephemeral")
            return mm_ok_text(f"MCP `{customer_id}` 응답:\n```json\n{pretty}\n```", response_type="ephemeral")
        except Exception:
            pass

    # 평문
    txt = resp.text
    if len(txt) > FOLLOWUP_THRESHOLD and channel_id in CHANNEL_TO_WEBHOOK:
        head = f":hourglass_flowing_sand: 응답이 길어서 웹훅으로 후속 전달합니다. (len={len(txt)})"
        for chunk in chunk_text(f"```\n{txt}\n```"):
            bg.add_task(send_mm_webhook, channel_id, chunk, username="MCP-Gateway")
        return mm_ok_text(head, response_type="ephemeral")
    if len(txt) > 3800:
        txt = txt[:3800] + "\n...(truncated)..."
    return mm_ok_text(f"MCP `{customer_id}` 응답:\n```\n{txt}\n```", response_type="ephemeral")

# ----------------------------- ② LLM 전용 단축 엔드포인트 -----------------------------
# 프록시가 고객 식별 → 해당 MCP의 /llm/chat 으로 전달
@app.post("/mm/llm")
async def mm_llm(req: Request, bg: BackgroundTasks):
    body = await parse_mm_body(req)
    channel_id = body.get("channel_id") or req.headers.get("X-Channel-Id")
    if not channel_id:
        return mm_error_text("channel_id required.")
    prompt = body.get("prompt") or body.get("text") or ""
    model  = body.get("model") or "gpt-4o-mini"   # 기본값, MCP 쪽에서 무시/매핑 가능

    customer_id, mcp_base = resolve_customer_and_mcp(channel_id)
    url = f"{mcp_base}/llm/chat"
    headers = {"content-type":"application/json","x-customer-id":customer_id,"x-channel-id":channel_id}
    payload = {"prompt": prompt, "model": model, "_proxy_ctx":{"source":"mm/llm"}}

    try:
        r = await post_with_retry(url, headers=headers, json_body=payload,
                                  timeout=HTTP_TIMEOUT, verify=VERIFY_TLS, retries=RETRY_COUNT, sleep_sec=RETRY_SLEEP_SEC)
    except Exception as e:
        logger.exception("LLM forward error")
        return mm_error_text(f"LLM call failed for `{customer_id}`: {e}")

    if r.status_code >= 400:
        return mm_error_text(f"LLM error {r.status_code}: {r.text[:1500]}")
    data = r.json() if "application/json" in (r.headers.get("content-type","")) else {"text": r.text}
    txt = data.get("text") or json.dumps(data, ensure_ascii=False)
    if len(txt) > FOLLOWUP_THRESHOLD and channel_id in CHANNEL_TO_WEBHOOK:
        bg.add_task(send_mm_webhook, channel_id, txt, username="LLM", icon_emoji=":crystal_ball:")
        return mm_ok_text(":hourglass_flowing_sand: LLM 응답을 웹훅으로 후속 전달합니다.")
    return mm_ok_text(txt, response_type="ephemeral")

# ----------------------------- ③ Prefect 전용 단축 엔드포인트 -----------------------------
# 프록시가 고객 식별 → 해당 MCP의 /prefect/trigger 로 전달
@app.post("/mm/quick/prefect")
async def mm_quick_prefect(req: Request, bg: BackgroundTasks):
    body = await parse_mm_body(req)
    channel_id = body.get("channel_id") or req.headers.get("X-Channel-Id")
    if not channel_id:
        return mm_error_text("channel_id required.")
    flow = body.get("flow") or body.get("flow_name") or body.get("text")
    params = body.get("params") or {}

    if not flow:
        return mm_error_text("flow (or flow_name) is required.")

    customer_id, mcp_base = resolve_customer_and_mcp(channel_id)
    url = f"{mcp_base}/prefect/trigger"
    headers = {"content-type":"application/json","x-customer-id":customer_id,"x-channel-id":channel_id}
    payload = {"flow_name": flow, "params": params, "_proxy_ctx":{"source":"mm/quick/prefect"}}

    try:
        r = await post_with_retry(url, headers=headers, json_body=payload,
                                  timeout=HTTP_TIMEOUT, verify=VERIFY_TLS, retries=RETRY_COUNT, sleep_sec=RETRY_SLEEP_SEC)
    except Exception as e:
        logger.exception("Prefect forward error")
        return mm_error_text(f"Prefect trigger failed for `{customer_id}`: {e}")

    # 결과 요약 + 후속 상세는 웹훅으로
    try:
        data = r.json()
    except Exception:
        data = {"status": r.status_code, "text": r.text[:2000]}

    summary = f":white_check_mark: Prefect trigger requested.\n- customer: `{customer_id}`\n- flow: `{flow}`\n- params: ```json\n{json.dumps(params, ensure_ascii=False, indent=2)}\n```"
    if channel_id in CHANNEL_TO_WEBHOOK:
        # 상세 응답은 웹훅으로
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
        bg.add_task(send_mm_webhook, channel_id, f"*Prefect response (raw)*\n```json\n{pretty}\n```", username="Prefect", icon_emoji=":white_check_mark:")
    return mm_ok_text(summary, response_type="ephemeral")

# ----------------------------- ④ Webhook 수동 전송(도구화) -----------------------------
# 긴 로그/표를 직접 보낼 때 사용
@app.post("/mm/webhook/send")
async def mm_webhook_send(req: Request):
    body = await parse_mm_body(req)
    channel_id = body.get("channel_id")
    text = body.get("text") or ""
    if not channel_id or not text:
        return mm_error_text("channel_id and text are required.")
    await send_mm_webhook(channel_id, text, username=body.get("username"), icon_emoji=body.get("icon_emoji"))
    return mm_ok_text(":incoming_envelope: Webhook sent.", response_type="ephemeral")

# 표 렌더링 도우미: rows(list[dict]) → 마크다운 테이블로 변환 후 웹훅 전송
@app.post("/mm/webhook/table")
async def mm_webhook_table(req: Request):
    body = await parse_mm_body(req)
    channel_id = body.get("channel_id")
    rows = body.get("rows") or []
    title = body.get("title") or "Table"
    if not channel_id or not isinstance(rows, list):
        return mm_error_text("channel_id and rows(list) are required.")
    md = f"**{title}**\n{to_markdown_table(rows)}"
    await send_mm_webhook(channel_id, md, username=body.get("username"), icon_emoji=body.get("icon_emoji"))
    return mm_ok_text(":table_tennis_paddle_and_ball: Table sent.", response_type="ephemeral")
