"""Optional client for the shared Hobby Corner AI gateway.

Do not pass arbitrary private Discord messages to this adapter. The caller must
validate staff permissions and supply only authorized professional task context.
The gateway independently enforces authenticated service permissions.
"""
import asyncio
import json
import os
from urllib import request, error

SUPPORTED_TASK_CAPABILITIES = frozenset({
    "task.extract", "task.summarize", "task.suggest", "knowledge.answer"
})

class HobbyAIError(RuntimeError):
    pass

def is_configured() -> bool:
    return bool(os.getenv("HOBBY_AI_GATEWAY_URL") and os.getenv("HOBBY_AI_SERVICE_TOKEN"))

def _execute_sync(capability: str, task_id: str, text: str, request_id: str):
    if capability not in SUPPORTED_TASK_CAPABILITIES:
        raise HobbyAIError("Unsupported AI capability")
    if not task_id or not request_id or not text.strip():
        raise HobbyAIError("Task, request ID and text are required")
    if len(text) > 6000:
        raise HobbyAIError("Input exceeds configured maximum")
    url = os.getenv("HOBBY_AI_GATEWAY_URL", "").rstrip("/")
    token = os.getenv("HOBBY_AI_SERVICE_TOKEN", "")
    if not url.startswith("https://") or not token:
        raise HobbyAIError("Shared AI gateway is not configured")
    payload = json.dumps({
        "capability": capability,
        "scope": "professional",
        "task_id": task_id,
        "request_id": request_id,
        "input": {"text": text}
    }).encode("utf-8")
    req = request.Request(
        url + "/v1/ai/execute",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with request.urlopen(req, timeout=20) as res:
            if res.status != 200:
                raise HobbyAIError(f"AI gateway returned HTTP {res.status}")
            data = json.loads(res.read(131072).decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, ValueError) as exc:
        raise HobbyAIError("AI gateway request failed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("output"), dict):
        raise HobbyAIError("Invalid AI gateway response")
    return data["output"]

async def execute_task_ai(capability: str, task_id: str, text: str, request_id: str):
    """Run blocking network IO off Discord's event loop."""
    return await asyncio.to_thread(_execute_sync, capability, task_id, text, request_id)
