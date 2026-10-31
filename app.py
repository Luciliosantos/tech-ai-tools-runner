import json
import os
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(title="TECH AI TOOLS Runner", version="0.3.0")

RUNNER_API_TOKEN = os.getenv("RUNNER_API_TOKEN", "")
R2_GATEWAY_URL = os.getenv("R2_GATEWAY_URL", "").rstrip("/")
R2_GATEWAY_TOKEN = os.getenv("R2_GATEWAY_TOKEN", "")
APKTOOL = os.getenv("APKTOOL_PATH", "/usr/local/bin/apktool")
MAX_APK_SIZE = 100 * 1024 * 1024


class ToolRequest(BaseModel):
    tool: str
    input: dict = {}


def authorize(token):
    if not RUNNER_API_TOKEN:
        raise HTTPException(503, "RUNNER_API_TOKEN não configurado")
    if token != f"Bearer {RUNNER_API_TOKEN}":
        raise HTTPException(401, "Não autorizado")


def check_storage():
    if not R2_GATEWAY_URL or not R2_GATEWAY_TOKEN:
        raise HTTPException(503, "Cloudflare R2 não configurado")


def validate_key(key):
    if not key or len(key) > 512 or ".." in key.split("/"):
        raise HTTPException(400, "Chave inválida")


def object_url(key):
    safe = "/".join(quote(p, safe="") for p in key.split("/"))
    return f"{R2_GATEWAY_URL}/objects/{safe}"


def storage_headers(content_type=None):
    headers = {"Authorization": f"Bearer {R2_GATEWAY_TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def r2_put(key, data, content_type):
    check_storage()
    validate_key(key)
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.put(
            object_url(key), content=data,
            headers=storage_headers(content_type)
        )
    if response.status_code >= 400:
        raise HTTPException(502, f"Erro no armazenamento R2: {response.status_code}")
    return response.json()


async def r2_get(key):
    check_storage()
    validate_key(key)
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.get(
            object_url(key), headers=storage_headers()
        )
    if response.status_code == 404:
        raise HTTPException(404, "Arquivo não encontrado")
    if response.status_code >= 400:
        raise HTTPException(502, "Erro ao baixar arquivo do R2")
    return response.content


def decode_apk(data):
    if not data or len(data) > MAX_APK_SIZE:
        raise HTTPException(413, "APK deve ter no máximo 100 MB")

    if not data.startswith(b"PK"):
        raise HTTPException(400, "Arquivo não parece ser um APK válido")

    with tempfile.TemporaryDirectory(prefix="tech-ai-") as temp:
        root = Path(temp)
        apk = root / "entrada.apk"
        output = root / "decodificado"
        archive = root / "decodificado.zip"
        apk.write_bytes(data)

        try:
            result = subprocess.run(
                [APKTOOL, "d", str(apk), "-o", str(output), "-f"],
                capture_output=True,
                text=True,
                timeout=300
            )
        except FileNotFoundError:
            raise HTTPException(503, "Apktool não instalado")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Tempo limite da decodificação excedido")

        if result.returncode != 0:
            raise HTTPException(
                422,
                (result.stderr or result.stdout or "Falha no Apktool")[-3000:]
            )

        files = [p for p in output.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)

        if total > MAX_APK_SIZE:
            raise HTTPException(413, "Resultado decodificado excede 100 MB")

        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as z:
            for path in files:
                z.write(path, path.relative_to(output).as_posix())

        summary = {
            "file_count": len(files),
            "decoded_size": total,
            "has_manifest": (output / "AndroidManifest.xml").exists(),
            "top_level": sorted(p.name for p in output.iterdir())[:80]
        }

        return archive.read_bytes(), summary


@app.get("/")
def root():
    return {
        "service": "TECH AI TOOLS Runner",
        "version": app.version,
        "health": "/health",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": app.version,
        "storage_configured": bool(R2_GATEWAY_URL and R2_GATEWAY_TOKEN),
        "runner_auth_configured": bool(RUNNER_API_TOKEN)
    }


@app.get("/tools")
def tools(authorization: str = Header(default="")):
    authorize(authorization)
    return {
        "tools": [
            {"name": "echo"},
            {"name": "text_stats"},
            {"name": "json_validate"},
            {"name": "apk_decode"}
        ]
    }


@app.put("/storage/{key:path}")
async def upload(key: str, request: Request,
                 authorization: str = Header(default="")):
    authorize(authorization)
    validate_key(key)
    data = await request.body()
    if len(data) > MAX_APK_SIZE:
        raise HTTPException(413, "Upload máximo de 100 MB")
    return await r2_put(
        key, data,
        request.headers.get("content-type", "application/octet-stream")
    )


@app.get("/storage/{key:path}")
async def download(key: str,
                   authorization: str = Header(default="")):
    authorize(authorization)
    data = await r2_get(key)
    return Response(data, media_type="application/octet-stream")


@app.delete("/storage/{key:path}")
async def delete(key: str,
                 authorization: str = Header(default="")):
    authorize(authorization)
    check_storage()
    validate_key(key)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.delete(
            object_url(key), headers=storage_headers()
        )
    if response.status_code >= 400:
        raise HTTPException(502, "Erro ao excluir arquivo")
    return response.json()


@app.post("/apk/decode")
async def apk_decode(request: Request,
                     authorization: str = Header(default="")):
    authorize(authorization)
    data = await request.body()
    decoded, summary = decode_apk(data)
    key = f"decoded/{uuid.uuid4().hex}.zip"
    stored = await r2_put(key, decoded, "application/zip")
    return {
        "ok": True,
        "output_key": key,
        "download_endpoint": f"/storage/{key}",
        "summary": summary,
        "storage": stored
    }


@app.post("/run")
async def run_tool(body: ToolRequest,
                   authorization: str = Header(default="")):
    authorize(authorization)

    if body.tool == "echo":
        return {"ok": True, "result": body.input}

    if body.tool == "text_stats":
        value = str(body.input.get("text", ""))
        return {
            "ok": True,
            "result": {
                "characters": len(value),
                "words": len(value.split()),
                "lines": len(value.splitlines())
            }
        }

    if body.tool == "json_validate":
        try:
            parsed = json.loads(body.input.get("json", ""))
            return {"ok": True, "valid": True, "result": parsed}
        except (TypeError, json.JSONDecodeError) as exc:
            return {"ok": True, "valid": False, "error": str(exc)}

    if body.tool == "apk_decode":
        source = str(body.input.get("storage_key", ""))
        validate_key(source)
        data = await r2_get(source)
        decoded, summary = decode_apk(data)
        key = str(body.input.get("output_key") or
                  f"decoded/{uuid.uuid4().hex}.zip")
        stored = await r2_put(key, decoded, "application/zip")
        return {
            "ok": True,
            "output_key": key,
            "download_endpoint": f"/storage/{key}",
            "summary": summary,
            "storage": stored
        }

    raise HTTPException(404, "Ferramenta não encontrada")
