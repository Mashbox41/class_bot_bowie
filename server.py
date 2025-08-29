# server.py
import os, json, asyncio
from typing import AsyncGenerator, Dict, Any, List
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

APP_ORIGIN = os.getenv("APP_ORIGIN", "*")
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN] if APP_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Minimal in-memory store (prod: swap for SQLite/Postgres) ----
SERVER_MEMORY: Dict[str, Any] = {
    "facts": {},          # { key: value }
    "snippets": [],       # [ {text, ts} ]
}

async def call_llm(messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """
    Stream assistant text from Groq's OpenAI-compatible /chat/completions API.
    Yields text deltas suitable for SSE relaying.
    """
    if not GROQ_API_KEY:
        # Fail fast with a helpful message
        yield "[Server error: Missing GROQ_API_KEY]"
        return

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "stream": True,
        # Optional tunables:
        # "temperature": 0.5,
        # "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", GROQ_API_URL, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        # OpenAI-style chunk:
                        delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                        if delta:
                            yield delta
                    except Exception:
                        # If Groq ever emits plain text or a non-JSON line
                        continue


from fastapi.responses import StreamingResponse
import urllib.parse, base64, json

def _json_b64_decode(s: str):
    try:
        raw = base64.b64decode(s.encode("utf-8")).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return {}

@app.get("/chat_sse")
async def chat_sse(q: str, ctx: str = ""):
    """
    Streaming chat via GET to avoid CORS preflight.
    q   = prompt (URL-encoded)
    ctx = base64(JSON of {lastTurns, factHits, snippets})
    """
    prompt = urllib.parse.unquote_plus(q)
    context = _json_b64_decode(ctx)

    sys = "You are Class Robot—concise, friendly, practical. Use context sparingly."
    messages = [{"role": "system", "content": sys}]
    if context:
        messages.append({"role":"user","content":f"[Context]\n{json.dumps(context)[:4000]}"} )
    messages.append({"role": "user", "content": prompt})

    async def event_stream():
        yield "data: " + json.dumps({"delta": ""}) + "\n\n"
        async for chunk in call_llm(messages):
            yield "data: " + json.dumps({"delta": chunk}) + "\n\n"
        yield "data: " + json.dumps({"done": True}) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/mem/pull")
async def mem_pull():
    return JSONResponse(SERVER_MEMORY)

@app.post("/mem/push")
async def mem_push(req: Request):
    body = await req.json()
    # Merge-append simple policy:
    facts = body.get("facts", {})
    for k, v in facts.items():
        SERVER_MEMORY["facts"][k] = v
    for snip in body.get("snippets", []):
        if snip and isinstance(snip, dict) and snip.get("text"):
            SERVER_MEMORY["snippets"].append(snip)
    return JSONResponse({"ok": True})

@app.post("/chat_stream")
async def chat_stream(req: Request):
    body = await req.json()
    user = body.get("user", "anonymous")
    prompt = body.get("prompt", "")
    context = body.get("context", [])  # optional: top local memory snippets/facts
    sys = "You are Class Robot—concise, friendly, and practical. Use given context sparingly."
    messages = [{"role": "system", "content": sys}]
    if context:
        messages.append({"role": "user", "content": f"[Context]\n{json.dumps(context)[:4000]}"} )
    messages.append({"role": "user", "content": prompt})

    async def event_stream():
        yield "data: " + json.dumps({"delta": ""}) + "\n\n"  # kick off
        async for chunk in call_llm(messages):
            yield "data: " + json.dumps({"delta": chunk}) + "\n\n"
        yield "data: " + json.dumps({"done": True}) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
