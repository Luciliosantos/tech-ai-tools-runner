import json
import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="TECH AI TOOLS Runner", version="0.1.0")
TOKEN = os.getenv("RUNNER_API_TOKEN", "")

class ToolRequest(BaseModel):
    tool: str
    input: dict = {}

def authorize(value):
    if not TOKEN:
        raise HTTPException(status_code=503, detail="RUNNER_API_TOKEN is not configured")
    if value != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/health")
def health():
    return {"status": "ok", "service": "TECH AI TOOLS Runner"}

@app.post("/run")
def run_tool(body: ToolRequest, authorization: str = Header(default="")):
    authorize(authorization)
    if body.tool == "echo":
        return {"ok": True, "result": body.input}
    if body.tool == "text_stats":
        text = str(body.input.get("text", ""))
        return {"ok": True, "result": {"characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())}}
    if body.tool == "json_validate":
        raw = body.input.get("json", "")
        try:
            parsed = json.loads(raw)
            return {"ok": True, "valid": True, "result": parsed}
        except (TypeError, json.JSONDecodeError) as exc:
            return {"ok": True, "valid": False, "error": str(exc)}
    raise HTTPException(status_code=404, detail="Tool not allowed")
