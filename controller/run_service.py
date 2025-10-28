import os
import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, Any, Optional
import httpx
import subprocess
import asyncio
import re
import shlex
from base64 import b64encode

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config_simple import config

# ------------------------------------------------------------------
# (선택) .env 자동 로드
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------------------------------------------------
# 채널→서버 라우팅 설정
# ------------------------------------------------------------------
CHANNEL_SERVER_MAP = {
    "4xd3frqsx3b79x46hwuqid594w": "https://k8s.bestpath.co.kr:16443",
    "#k8s-admin": "https://mcpsvr2.bestpath.co.kr",
    "devops": "https://k8s.bestpath.co.kr:16443"
}
K8S_DEFAULT_SERVER = "https://k8s.bestpath.co.kr:16443"

# 필요시 Basic 인증 (옵션)
K8S_SERVER_BASIC_USER = os.getenv("K8S_SERVER_BASIC_USER", "").strip()
K8S_SERVER_BASIC_PASS = os.getenv("K8S_SERVER_BASIC_PASS", "").strip()

# "테스트 모드": True면 실행하지 않고 에코만
K8S_TEST_MODE = os.getenv("K8S_TEST_MODE", "0") == "1"

# ------------------------------------------------------------------
# 환경 변수
# ------------------------------------------------------------------
# Mattermost 검증 토큰(슬래시/아웃고잉 웹훅 공통). 비워두면 검증 생략.
MATTERMOST_TOKEN = os.getenv("MATTERMOST_TOKEN", "")

# 응답 표시 방식: "ephemeral"(개인) 또는 "in_channel"(채널 전체 공개)
RESPONSE_TYPE = os.getenv("RESPONSE_TYPE", "ephemeral")

# ABCLab 스트리밍 API 설정
ABCLAB_API_URL = os.getenv("ABCLAB_API_URL", "")
ABCLAB_API_KEY = os.getenv("ABCLAB_API_KEY", "")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
MCP_SERVER_URL = os.getenv("MCP_K8S_SERVER_URL", "http://localhost:3000")

# ------------------------------------------------------------------
# 로깅 (콘솔 + 파일 회전)
# ------------------------------------------------------------------
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("mm_proxy")
logger.setLevel(logging.INFO)

def _ensure_handler(handler_type, creator):
    if not any(isinstance(h, handler_type) for h in logger.handlers):
        logger.addHandler(creator())

def _console_handler():
    h = logging.StreamHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    return h

def _file_handler():
    h = RotatingFileHandler(
        os.path.join(LOG_DIR, "mattermost_proxy.log"),
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    return h

_ensure_handler(logging.StreamHandler, _console_handler)
_ensure_handler(RotatingFileHandler, _file_handler)

# ------------------------------------------------------------------
# K8S 명령어 관련 패턴 및 함수
# ------------------------------------------------------------------
K8S_CMD_PATTERN = re.compile(r"^\s*(kubectl|k)\b", re.IGNORECASE)
HELM_CMD_PATTERN = re.compile(r"^\s*helm\b", re.IGNORECASE)

# 보안: 허용 서브커맨드 화이트리스트
ALLOWED_KUBECTL = {"get", "describe", "logs", "top", "version", "api-resources", "api-versions"}
ALLOWED_HELM = {"list", "status", "version", "history"}

# 위험 연산자 차단
DANGEROUS_PATTERN = re.compile(r"[;&|]{1,2}|`|>\>|\<\<|>\s|<\s")

def is_k8s_command(text: str) -> bool:
    """사용자 입력이 k8s/helm 계열 명령인지 판별"""
    if not text:
        return False
    t = text.strip()
    return bool(K8S_CMD_PATTERN.match(t) or HELM_CMD_PATTERN.match(t))

def pick_target_server(channel_id: Optional[str], channel_name: Optional[str]) -> Optional[str]:
    # 1순위: 채널 ID 매핑
    if channel_id and channel_id in CHANNEL_SERVER_MAP:
        return CHANNEL_SERVER_MAP[channel_id]
    # 2순위: 채널 이름 매핑
    if channel_name:
        if channel_name in CHANNEL_SERVER_MAP:
            return CHANNEL_SERVER_MAP[channel_name]
        key = f"#{channel_name}"
        if key in CHANNEL_SERVER_MAP:
            return CHANNEL_SERVER_MAP[key]
    # 3순위: 기본 서버
    if K8S_DEFAULT_SERVER:
        return K8S_DEFAULT_SERVER
    return None

def _build_basic_auth_header() -> Dict[str, str]:
    if not (K8S_SERVER_BASIC_USER and K8S_SERVER_BASIC_PASS):
        return {}
    token = b64encode(f"{K8S_SERVER_BASIC_USER}:{K8S_SERVER_BASIC_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def _sanitize_command_text(cmd_text: str) -> str:
    t = cmd_text.strip()
    t = t.replace("\r", " ").replace("\n", " ").replace("`", "'")
    return t

async def run_k8s_command_local(cmd_text: str, timeout_sec: int = 20) -> Dict[str, Any]:
    safe_cmd = _sanitize_command_text(cmd_text)

    def _runner():
        env = os.environ.copy()
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"]:
            env.pop(k, None)

        return subprocess.run(
            safe_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )

    try:
        proc = await asyncio.to_thread(_runner)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"Timeout after {timeout_sec}s"}

def format_cmd_result(cmd: str, server: Optional[str], result: Dict[str, Any], team: Optional[str], 
                      channel: Optional[str], channel_id: Optional[str], user: Optional[str], 
                      cmd_ms: Optional[int] = None) -> str:
    meta = [
        f"- **Team**: `{team or '-'}`",
        f"- **Channel**: `{channel or '-'}` (`{channel_id or '-'}`)",
        f"- **User**: `{user or '-'}`",
    ]
    if server:
        meta.append(f"- **Server**: `{server}`")
    if cmd_ms is not None:
        meta.append(f"- **Elapsed**: `{cmd_ms} ms`")

    head = "### ✅ 실행 결과" if result.get("ok") else "### ❌ 실행 실패"
    parts = [
        head,
        "",
        "```bash",
        cmd,
        "```",
        "",
        "---",
        "#### 메타",
        "\n".join(meta),
    ]

    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stdout.strip():
        parts += ["", "#### STDOUT", "```", stdout[:3800], "```"]
    if stderr.strip():
        parts += ["", "#### STDERR", "```", stderr[:3800], "```"]

    rc = result.get("returncode")
    parts += ["", f"- **Return Code**: `{rc}`"]

    return "\n".join(parts)

def _check_whitelist(cmd_text: str) -> None:
    """kubectl/helm인지 확인하고, 첫 서브커맨드가 화이트리스트에 있는지 검사"""
    if not is_k8s_command(cmd_text):
        raise HTTPException(status_code=400, detail="Only kubectl/helm commands are allowed.")

    parts = shlex.split(cmd_text)
    if not parts:
        raise HTTPException(status_code=400, detail="Empty command.")

    tool = parts[0].lower()
    if tool.startswith("k"):  # kubectl / k
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid kubectl command.")
        sub = parts[1]
        if sub not in ALLOWED_KUBECTL:
            raise HTTPException(status_code=403, detail=f"kubectl subcommand '{sub}' is not allowed.")
    elif tool == "helm":
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid helm command.")
        sub = parts[1]
        if sub not in ALLOWED_HELM:
            raise HTTPException(status_code=403, detail=f"helm subcommand '{sub}' is not allowed.")

def _check_dangerous(cmd_text: str) -> None:
    """shell=True를 쓰므로 파이프/리다이렉션 등 위험 연산자를 간단히 차단"""
    if DANGEROUS_PATTERN.search(cmd_text):
        raise HTTPException(status_code=400, detail="Pipes, redirections, and command chaining are not allowed.")

# ------------------------------------------------------------------
# 유틸
# ------------------------------------------------------------------
def _mask(v: Optional[str]) -> Optional[str]:
    """민감정보 마스킹"""
    if not v:
        return v
    if len(v) <= 6:
        return "*" * len(v)
    return v[:3] + "*" * (len(v) - 6) + v[-3:]

def build_mm_response(text: str, in_channel: bool = False) -> Dict[str, Any]:
    """Mattermost 호환 응답 JSON"""
    resp: Dict[str, Any] = {"text": text}
    if in_channel or RESPONSE_TYPE == "in_channel":
        resp["response_type"] = "in_channel"
    return resp

async def send_delayed_response(response_url: Optional[str], text: str) -> None:
    """Mattermost Slash Command의 response_url로 지연 응답 전송"""
    if not response_url:
        return
    payload = {"response_type": RESPONSE_TYPE, "text": text}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
        try:
            r = await client.post(response_url, json=payload)
            r.raise_for_status()
            logger.info("[MM DELAYED] response_url 전송 완료 (%s bytes)", len(text.encode("utf-8")))
        except Exception:
            logger.exception("response_url 지연응답 실패")

# ------------------------------------------------------------------
# ABCLab 스트리밍 호출
# ------------------------------------------------------------------
async def call_abclab_streaming(query: str, customer_id: str, channel_id: str) -> str:
    # 로그로 입력 받은 channel_id, customer_id 값을 활용하여 스트리밍 호출
    logger.info(f"[ABCLab DEBUG] channel_id='{channel_id}' customer_id='{customer_id}'")
    """ABCLab SSE 스트리밍에서 오직 agent_thought 의 'thought'만 수집해 반환"""
    if not ABCLAB_API_URL:
        return "ABCLab 미설정: ABCLAB_API_URL 환경변수를 확인하세요."

    headers = {
        "Authorization": f"Bearer {ABCLAB_API_KEY}" if ABCLAB_API_KEY else "",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Channel-Id": channel_id or "",
        "X-Customer-Id": customer_id or "",
    }
    payload = {
        "query": query,
        "user": customer_id or "unknown",
        "response_mode": "streaming",
        "x_channel_id": channel_id or "",
        "x_customer_id": customer_id or "",
        "inputs": {
            "channel_id": channel_id or "",
            "customer_id": customer_id or "",
            "x_channel_id": channel_id or "",
            "x_customer_id": customer_id or "",
            "locale": "ko-KR"
        }
    }
    
    logger.info(f"[ABCLab DEBUG] 보내는 payload: {json.dumps(payload, ensure_ascii=False)}")
    logger.info(f"[ABCLab DEBUG] channel_id='{channel_id}' customer_id='{customer_id}'")

    timeout = httpx.Timeout(HTTP_TIMEOUT)
    thought_chunks = []
    fallback_answer_chunks = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            logger.info("[ABCLab->SSE] url=%s headers=%s payload=%s",
                        ABCLAB_API_URL,
                        json.dumps({k: (v if k != "Authorization" else "Bearer ****") for k, v in headers.items()}, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False))
            async with client.stream("POST", ABCLAB_API_URL, headers=headers, json=payload) as resp:
                ct = (resp.headers.get("content-type") or "").lower()
                if resp.status_code >= 400:
                    body = await resp.aread()
                    body_txt = body.decode(errors="ignore")
                    logger.warning("[ABCLab<-ERR] %s %s | ct=%s | body=%s",
                                   resp.status_code, resp.reason_phrase, ct, body_txt[:1000])
                    resp.raise_for_status()

                if "text/event-stream" not in ct:
                    body = await resp.aread()
                    txt = body.decode(errors="ignore")
                    logger.warning("[ABCLab<-NON-SSE] ct=%s len=%s", ct, len(body))
                    return txt.strip()[:4000] if txt else f"예상 외 Content-Type: {ct}"

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if not data:
                        continue
                    if data.upper() == "[DONE]":
                        break

                    try:
                        js = json.loads(data)
                    except Exception:
                        continue

                    ev = js.get("event", "")
                    if ev == "agent_thought":
                        t = js.get("thought") or ""
                        if t:
                            thought_chunks.append(str(t))
                    elif ev == "agent_message":
                        a = js.get("answer") or js.get("text") or js.get("delta") or ""
                        if a:
                            fallback_answer_chunks.append(str(a))

        except Exception as e:
            logger.exception("ABCLab streaming 호출 중 예외")
            return f"ABCLab 호출 실패(스트리밍): {e}"

    if thought_chunks:
        return "".join(thought_chunks)[:4000]

    if fallback_answer_chunks:
        return "".join(fallback_answer_chunks)[:4000]

    return "ABCLab 스트리밍에서 thought/answer가 수신되지 않았습니다."

# ------------------------------------------------------------------
# FastAPI 앱
# ------------------------------------------------------------------
app = FastAPI(title="MCP Controller", version="1.0.0")

# 전역 Mattermost 데이터 저장소 (ABCLab 변수 치환용)
_latest_mattermost_data = {}

# ------------------------------------------------------------------
# Adapter: ABCLab -> MCP Server 프록시 (/adapter/tools/{tool})
# ------------------------------------------------------------------
def _extract_ids_from_request(req: Request, body: Dict[str, Any]) -> Dict[str, Optional[str]]:
    logger.info(f"[DEBUG] _extract_ids_from_request: req={req} body={body}")
    """헤더/바디/inputs에서 channel_id, customer_id를 최대한 찾아낸다."""
    
    # ABCLab 변수 치환을 위한 실제 값들 추출 (Mattermost에서 온 원본 데이터)
    actual_channel_id = None
    actual_customer_id = None
    
    # Mattermost 원본 데이터에서 실제 ID 추출 (전역 저장소에서)
    logger.info(f"[VAR REPLACE] Global MM data: {_latest_mattermost_data}")
    if _latest_mattermost_data:
        actual_channel_id = _latest_mattermost_data.get("channel_id")
        actual_customer_id = _latest_mattermost_data.get("user_name")  # 또는 user_id
        logger.info(f"[VAR REPLACE] Using global MM data: channel_id={actual_channel_id}, customer_id={actual_customer_id}")
    else:
        logger.warning("[VAR REPLACE] No global MM data available - webhook may not have been called")
        
        # 대안: ABCLab 요청에서 직접 추출 시도
        # ABCLab이 보낸 inputs에서 추출
        if isinstance(body, dict):
            inputs = body.get("inputs", {})
            if isinstance(inputs, dict):
                # inputs에서 실제 값이 있는지 확인 ({{inputs.channel_id}}가 아닌)
                actual_channel_id = inputs.get("channel_id")
                actual_customer_id = inputs.get("customer_id")
                
                # {{inputs.xxx}} 패턴이 아닌 실제 값인지 확인
                if actual_channel_id and not actual_channel_id.startswith("{{"):
                    logger.info(f"[VAR REPLACE] Found actual channel_id in inputs: {actual_channel_id}")
                else:
                    actual_channel_id = None
                    
                if actual_customer_id and not actual_customer_id.startswith("{{"):
                    logger.info(f"[VAR REPLACE] Found actual customer_id in inputs: {actual_customer_id}")
                else:
                    actual_customer_id = None
    
    # 헤더(언더스코어 표기)
    h_channel = req.headers.get("x_channel_id") or req.headers.get("x-channel-id")
    h_customer = req.headers.get("x_customer_id") or req.headers.get("x-customer-id")

    # 바디 최상위
    b_channel = None
    b_customer = None
    if isinstance(body, dict):
        b_channel = body.get("channel_id") or body.get("x_channel_id")
        b_customer = body.get("customer_id") or body.get("x_customer_id")
        inputs = body.get("inputs")
        if isinstance(inputs, dict):
            b_channel = b_channel or inputs.get("channel_id") or inputs.get("x_channel_id")
            b_customer = b_customer or inputs.get("customer_id") or inputs.get("x_customer_id")

    # 변수 치환 로직: {{inputs.channel_id}} 같은 패턴을 실제 값으로 치환
    def replace_variables(value: str) -> str:
        if not isinstance(value, str):
            return value
        
        # {{inputs.channel_id}} -> 실제 channel_id
        if "{{inputs.channel_id}}" in value:
            # 우선순위: Mattermost 원본 데이터 > 헤더 > 바디
            if actual_channel_id:
                result = value.replace("{{inputs.channel_id}}", actual_channel_id)
                logger.info(f"[VAR REPLACE] {{inputs.channel_id}} -> {actual_channel_id}")
                return result
            elif h_channel and h_channel != "{{inputs.channel_id}}":
                result = value.replace("{{inputs.channel_id}}", h_channel)
                logger.info(f"[VAR REPLACE] {{inputs.channel_id}} -> {h_channel} (from header)")
                return result
            elif b_channel and b_channel != "{{inputs.channel_id}}":
                result = value.replace("{{inputs.channel_id}}", b_channel)
                logger.info(f"[VAR REPLACE] {{inputs.channel_id}} -> {b_channel} (from body)")
                return result
            else:
                logger.warning(f"[VAR REPLACE] {{inputs.channel_id}} not replaced - no actual value found")
        
        # {{inputs.customer_id}} -> 실제 customer_id
        if "{{inputs.customer_id}}" in value:
            # 우선순위: Mattermost 원본 데이터 > 헤더 > 바디
            if actual_customer_id:
                result = value.replace("{{inputs.customer_id}}", actual_customer_id)
                logger.info(f"[VAR REPLACE] {{inputs.customer_id}} -> {actual_customer_id}")
                return result
            elif h_customer and h_customer != "{{inputs.customer_id}}":
                result = value.replace("{{inputs.customer_id}}", h_customer)
                logger.info(f"[VAR REPLACE] {{inputs.customer_id}} -> {h_customer} (from header)")
                return result
            elif b_customer and b_customer != "{{inputs.customer_id}}":
                result = value.replace("{{inputs.customer_id}}", b_customer)
                logger.info(f"[VAR REPLACE] {{inputs.customer_id}} -> {b_customer} (from body)")
                return result
            else:
                logger.warning(f"[VAR REPLACE] {{inputs.customer_id}} not replaced - no actual value found")
        
        return value

    # 변수 치환 적용
    h_channel = replace_variables(h_channel)
    h_customer = replace_variables(h_customer)
    b_channel = replace_variables(b_channel)
    b_customer = replace_variables(b_customer)

    # ✅ 실제 값 우선 반환 (inputs.xxx 패턴이 아닌 실제 ID)
    final_channel_id = h_channel or b_channel or ""
    final_customer_id = h_customer or b_customer or ""
    
    # inputs.xxx나 {{xxx}} 패턴이면 actual 값으로 대체
    if final_channel_id and (final_channel_id.startswith("inputs.") or "{{" in final_channel_id):
        if actual_channel_id:
            logger.info(f"[VAR REPLACE] Replacing template '{final_channel_id}' with actual '{actual_channel_id}'")
            final_channel_id = actual_channel_id
        else:
            logger.warning(f"[VAR REPLACE] Template '{final_channel_id}' found but no actual value available")
            final_channel_id = None
    
    if final_customer_id and (final_customer_id.startswith("inputs.") or "{{" in final_customer_id):
        if actual_customer_id:
            logger.info(f"[VAR REPLACE] Replacing template '{final_customer_id}' with actual '{actual_customer_id}'")
            final_customer_id = actual_customer_id
        else:
            logger.warning(f"[VAR REPLACE] Template '{final_customer_id}' found but no actual value available")
            final_customer_id = None

    return {
        "channel_id": (final_channel_id or "").strip() or None,
        "customer_id": (final_customer_id or "").strip() or None,
    }

@app.post("/tools/{tool}")
async def tools_redirect(tool: str, req: Request):
    """
    ABCLab이 /tools/{tool}로 호출할 때 /adapter/tools/{tool}로 리다이렉트
    """
    # /adapter/tools/{tool}로 리다이렉트
    return await adapter_tools(tool, req)

@app.post("/adapter/tools/{tool}")
async def adapter_tools(tool: str, req: Request):
    """
    ABCLab 요청을 수신하여 MCP 서버의 /tools/{tool}로 전달하는 어댑터.
    - channel_id/customer_id를 헤더/바디에서 추출하여 보강
    - MCP 서버에는 헤더(x_channel_id/x_customer_id)와 바디(channel_id/customer_id)에 함께 세팅
    """
    # 원본 요청 바디 파싱
    if req.headers.get("content-type", "").startswith("application/json"):
        body: Dict[str, Any] = await req.json()
    else:
        form = await req.form()
        body = dict(form)
    
    # 디버깅: ABCLab 요청의 모든 정보 로깅
    logger.info(f"[ADAPTER DEBUG] tool={tool}")
    logger.info(f"[ADAPTER DEBUG] headers={dict(req.headers)}")
    logger.info(f"[ADAPTER DEBUG] body={body}")
    logger.info(f"[ADAPTER DEBUG] query_params={dict(req.query_params)}")
    
    # ABCLab 요청에서 실제 ID 값 추출 시도
    # ABCLab이 보낸 실제 값이 있는지 확인 ({{inputs.xxx}}가 아닌)
    actual_channel_id = None
    actual_customer_id = None
    
    # 헤더에서 실제 값 확인
    for header_name, header_value in req.headers.items():
        if header_name.lower() in ['x_channel_id', 'x-customer-id'] and header_value and not header_value.startswith("{{") and not header_value.startswith("inputs."):
            if header_name.lower() == 'x_channel_id':
                actual_channel_id = header_value
                logger.info(f"[ADAPTER DEBUG] Found actual channel_id in header: {actual_channel_id}")
            elif header_name.lower() == 'x-customer-id':
                actual_customer_id = header_value
                logger.info(f"[ADAPTER DEBUG] Found actual customer_id in header: {actual_customer_id}")
    
    # 바디에서 실제 값 확인
    if isinstance(body, dict):
        for key, value in body.items():
            if key in ['channel_id', 'customer_id'] and value and not value.startswith("{{") and not value.startswith("inputs."):
                if key == 'channel_id':
                    actual_channel_id = value
                    logger.info(f"[ADAPTER DEBUG] Found actual channel_id in body: {actual_channel_id}")
                elif key == 'customer_id':
                    actual_customer_id = value
                    logger.info(f"[ADAPTER DEBUG] Found actual customer_id in body: {actual_customer_id}")
    
    # 실제 값이 있다면 전역 변수에 저장
    if actual_channel_id or actual_customer_id:
        global _latest_mattermost_data
        _latest_mattermost_data = {
            "channel_id": actual_channel_id,
            "customer_id": actual_customer_id,
            "user_name": actual_customer_id,
            "source": "abclab_request"
        }
        logger.info(f"[ADAPTER DEBUG] Stored actual IDs in global data: {_latest_mattermost_data}")

    ids = _extract_ids_from_request(req, body)
    channel_id = ids.get("channel_id")
    customer_id = ids.get("customer_id")

    # 바디 보강: 최상위에 channel_id/customer_id 주입 (기존 값 보존)
    body = dict(body or {})
    if channel_id:
        body.setdefault("channel_id", channel_id)
    if customer_id:
        body.setdefault("customer_id", customer_id)

    # MCP 서버 호출 준비
    target_url = f"{MCP_SERVER_URL.rstrip('/')}/tools/{tool}"
    
    # ✅ 실제 값으로 강제 덮어쓰기 (inputs.xxx 패턴 제거)
    actual_channel_id = channel_id if channel_id and not channel_id.startswith("inputs.") and not channel_id.startswith("{{") else None
    actual_customer_id = customer_id if customer_id and not customer_id.startswith("inputs.") and not customer_id.startswith("{{") else None
    
    # 전역 변수에서 실제 값 가져오기 (템플릿 문자열인 경우)
    if not actual_channel_id or not actual_customer_id:
        if _latest_mattermost_data:
            if not actual_channel_id:
                actual_channel_id = _latest_mattermost_data.get("channel_id")
            if not actual_customer_id:
                actual_customer_id = _latest_mattermost_data.get("user_name")
    
    fwd_headers = {
        "content-type": "application/json",
        "x_channel_id": actual_channel_id or "",
        "x_customer_id": actual_customer_id or "",
    }
    
    # 바디도 실제 값으로 강제 덮어쓰기
    if actual_channel_id:
        body["channel_id"] = actual_channel_id
    if actual_customer_id:
        body["customer_id"] = actual_customer_id

    # 로깅
    logger.info("[ADAPTER] Forward -> %s | headers=%s | body=%s", target_url, {k: v for k, v in fwd_headers.items()}, json.dumps(body, ensure_ascii=False)[:1500])

    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
        resp = await client.post(target_url, headers=fwd_headers, json=body)
        # 에러 시 그대로 바디를 반환하여 원인 파악
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": await resp.aread()}
        return JSONResponse(data, status_code=resp.status_code)

class ExecPayload(BaseModel):
    cmd: Optional[str] = None
    command: Optional[str] = None
    timeout_sec: Optional[int] = 20
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    user: Optional[str] = None

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "mcp-controller"}

@app.get("/")
async def root():
    """Root endpoint."""
    return {"name": "MCP Controller", "version": "1.0.0"}

@app.post("/exec")
async def exec_handler(body: ExecPayload, req: Request):
    """로컬 서버에서 kubectl/helm 실행 후 JSON 반환"""
    cmd_text = body.cmd or body.command
    if not cmd_text:
        raise HTTPException(status_code=400, detail="Missing 'cmd' or 'command' field.")

    cmd_text = _sanitize_command_text(cmd_text)
    _check_whitelist(cmd_text)
    _check_dangerous(cmd_text)

    timeout = int(body.timeout_sec or 20)
    result = await run_k8s_command_local(cmd_text, timeout_sec=HTTP_TIMEOUT)

    return {
        "ok": bool(result.get("ok")),
        "returncode": int(result.get("returncode", -1)),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }

@app.post("/webhook")
async def webhook(req: Request, background: BackgroundTasks):
    """
    Mattermost Slash Command / Outgoing Webhook 수신 엔드포인트
    Content-Type: application/x-www-form-urlencoded
    """
    raw_body = (await req.body()).decode("utf-8", errors="ignore")

    headers_to_log = {
        "content-type": req.headers.get("content-type"),
        "user-agent": req.headers.get("user-agent"),
        "x-forwarded-for": req.headers.get("x-forwarded-for"),
        "authorization": _mask(req.headers.get("authorization") or ""),
    }

    form = await req.form()
    token = form.get("token")
    team_domain = form.get("team_domain")
    channel_id = form.get("channel_id")
    channel_name = form.get("channel_name")
    user_id = form.get("user_id")
    user_name = form.get("user_name")
    command = form.get("command")
    trigger_word = form.get("trigger_word")
    text = form.get("text") or ""
    response_url = form.get("response_url")

    form_logged = {
        "token": _mask(token or ""),
        "team_domain": team_domain,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "user_id": user_id,
        "user_name": user_name,
        "command": command,
        "trigger_word": trigger_word,
        "text": text,
        "response_url": _mask(response_url or ""),
    }

    logger.info(
        "[MM INBOUND] path=%s method=%s headers=%s form=%s raw=%s",
        req.url.path, req.method,
        json.dumps(headers_to_log, ensure_ascii=False),
        json.dumps(form_logged, ensure_ascii=False),
        raw_body[:1500],
    )

    # Mattermost 원본 데이터를 전역 저장소에 저장 (ABCLab 변수 치환용)
    global _latest_mattermost_data
    _latest_mattermost_data = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "user_id": user_id,
        "user_name": user_name,
        "team_domain": team_domain,
        "command": command,
        "text": text
    }
    logger.info(f"[MM DATA] Stored global MM data: channel_id={channel_id}, user_name={user_name}")

    # 토큰 검증
    if MATTERMOST_TOKEN and token != MATTERMOST_TOKEN:
        logger.warning("[SECURITY] 잘못된 토큰 수신: %s", token)
        raise HTTPException(status_code=403, detail="Invalid token")

    user_text = (text or trigger_word or "").strip()
    if not user_text:
        return JSONResponse(build_mm_response("형식: `/etlers <query>` 또는 트리거 키워드 + 본문"))
    
    # k8s/helm 명령어 처리
    if is_k8s_command(user_text):
        target_server = pick_target_server(channel_id, channel_name)

        # 테스트 모드
        if K8S_TEST_MODE:
            preview = (
                "🧪 *테스트 모드*: 아래 **k8s/helm 명령어**를 실행하지 않고 그대로 표시합니다.\n"
                "```bash\n"
                f"{user_text}\n"
                "```\n"
                "\n---\n"
                "#### 라우팅\n"
                f"- **Resolved Server**: `{target_server or '- (no mapping)'}`\n"
                f"- **Channel**: `{channel_name or '-'}` (`{channel_id or '-'}`)\n"
            )
            if response_url:
                await send_delayed_response(response_url, preview)
                return JSONResponse(build_mm_response("✅ 테스트 표시를 채널로 전송했습니다."))
            else:
                return JSONResponse(build_mm_response(preview, in_channel=True))

        # 실제 실행
        t0 = asyncio.get_event_loop().time()
        if target_server:
            result = await run_k8s_command_local(user_text, timeout_sec=HTTP_TIMEOUT)
        else:
            result = await run_k8s_command_local(user_text, timeout_sec=HTTP_TIMEOUT)
        t1 = asyncio.get_event_loop().time()
        took_ms = int((t1 - t0) * 1000)

        preview = format_cmd_result(
            cmd=user_text,
            server=target_server,
            result=result,
            team=team_domain,
            channel=channel_name,
            channel_id=channel_id,
            user=(user_name or user_id),
            cmd_ms=took_ms
        )

        if response_url:
            await send_delayed_response(response_url, preview)
            return JSONResponse(build_mm_response("✅ k8s/helm 명령 실행 결과를 채널에 전송했습니다."))
        else:
            return JSONResponse(build_mm_response(preview, in_channel=True))

    # 즉시 ACK (메타정보 포함)
    meta_preview = (
        f"**Team**: `{team_domain or '-'}` | "
        f"**Channel**: `{channel_name or '-'}` (`{channel_id or '-'}`) | "
        f"**User**: `{user_name or user_id or '-'}`"
    )
    ack = build_mm_response(
        f"요청 접수: `{user_text}` 처리 중입니다...\n{meta_preview}"
    )
    
    # 백그라운드 처리: ABCLab 스트리밍 호출
    async def work():
        try:
            logger.info("[BACKGROUND] 처리 시작: channel=%s user=%s text=%s", channel_name, user_name, user_text)
            logger.info(f"[BACKGROUND] ABCLab 호출 시작: user_text='{user_text}' channel_id='{channel_id}' user_name='{user_name}'")
            
            result_text = await call_abclab_streaming(user_text, user_name or user_id or "", channel_id or "")
            logger.info(f"[BACKGROUND] ABCLab 호출 완료: result_length={len(result_text) if result_text else 0}")
            if response_url:
                await send_delayed_response(response_url, result_text)
            else:
                logger.info("[BACKGROUND] 처리 완료(로그만): %s", (result_text[:500] + "…" if len(result_text) > 500 else result_text))
        except Exception as e:
            logger.exception("백그라운드 처리 실패: %s", e)

    background.add_task(work)
    return JSONResponse(ack)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "run_service:app",
        host="0.0.0.0",
        port=config.PORT,
        reload=True
    )
