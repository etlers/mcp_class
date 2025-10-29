# mcp_servers/tools/common.py
import asyncio
import json
from typing import List, Union, Dict, Any

class KubeExecError(Exception):
    pass

async def _run(cmd: List[str]) -> str:
    # kubectl이 없거나 실패해도 에러 메시지를 보기 좋게 던져줍니다.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()
    if proc.returncode != 0:
        raise KubeExecError(f"kubectl error({proc.returncode}): {err_b.decode().strip()}")
    return out_b.decode()

async def kube_exec(cmd: List[str]) -> Union[Dict[str, Any], str]:
    """
    예: ["kubectl", "get", "pods", "-n", "default", "-o", "json"]
    JSON이면 dict로, 아니면 str로 반환
    """
    text = await _run(cmd)
    # -o json 이면 JSON으로 변환
    try:
        return json.loads(text)
    except Exception:
        return text