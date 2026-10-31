import json
import os
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(title="TECH AI TOOLS Runner", version="0.2.0")

RUNNER_API_TOKEN = os.getenv("RUNNER_API_TOKEN", "")
R2_GATEWAY_URL = os.getenv("R2_GATEWAY_URL", "").rstrip("/")
R2_GATEWAY_TOKEN = os.getenv("R2_GATEWAY_TOKEN", "")


class ToolRequest(BaseModel):
    tool: str
    input: dict = {}


def authorize(value: str) -> None:
    if not RUNNER_API_TOKEN:
        raise HTTPException(status_code=503, detail="RUNNER_API_TOKEN is not configured")
    if value != f"Bearer {RUNNER_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def storage_configured() -> None:
    if not R2_GATEWAY_URL or not R2_GATEWAY_TOKEN:
        raise HTTPException(status_code=503, detail="R2 storage is not configured")


def storage_headers(content_type: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {R2_GATEWAY_TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def object_url(key: str) -> str:
    safe_key = "/".join(quote(part, safe="") for part in key.split("/"))
    return f"{R2_GATEWAY_URL}/objects/{safe_key}"


def validate_key(key: str) -> None:
    if not key or len(key) > 512 or ".." in key.split("/"):
        raise HTTPException(status_code=400, detail="Invalid object key")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "TECH AI TOOLS Runner",
        "storage_configured": bool(R2_GATEWAY_URL and R2_GATEWAY_TOKEN),
    }


@app.get("/storage")
async def list_storage(authorization: str = Header(default="")):
    authorize(authorization)
    storage_configured()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{R2_GATEWAY_URL}/objects",
            headers=storage_headers(),
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Storage gateway error")
    return response.json()


@app.put("/storage/{key:path}")
async def put_storage(
    key: str,
    request: Request,
    authorization: str = Header(default=""),
):
    authorize(authorization)
    storage_configured()
    validate_key(key)
    data = await request.body()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Maximum object size is 10 MB")

    content_type = request.headers.get("content-type", "application/octet-stream")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(
            object_url(key),
            content=data,
            headers=storage_headers(content_type),
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Storage gateway error")
    return response.json()


@app.get("/storage/{key:path}")
async def get_storage(key: str, authorization: str = Header(default="")):
    authorize(authorization)
    storage_configured()
    validate_key(key)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            object_url(key),
            headers=storage_headers(),
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Object not found")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Storage gateway error")

    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "application/octet-stream"),
    )


@app.delete("/storage/{key:path}")
async def delete_storage(key: str, authorization: str = Header(default="")):
    authorize(authorization)
    storage_configured()
    validate_key(key)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            object_url(key),
            headers=storage_headers(),
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Storage gateway error")
    return response.json()


@app.post("/run")
def run_tool(body: ToolRequest, authorization: str = Header(default="")):
    authorize(authorization)

    if body.tool == "echo":
        return {"ok": True, "result": body.input}

    if body.tool == "text_stats":
        text_value = str(body.input.get("text", ""))
        return {
            "ok": True,
            "result": {
                "characters": len(text_value),
                "words": len(text_value.split()),
                "lines": len(text_value.splitlines()),
            },
        }

    if body.tool == "json_validate":
        raw = body.input.get("json", "")
        try:
            parsed = json.loads(raw)
            return {"ok": True, "valid": True, "result": parsed}
        except (TypeError, json.JSONDecodeError) as exc:
            return {"ok": True, "valid": False, "error": str(exc)}

    raise HTTPException(status_code=404, detail="Tool not allowed")
