# server.py
import os, json, base64, urllib.parse
from typing import AsyncGenerator, Dict, Any, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------- Env ----------------
APP_ORIGIN   = os.getenv("APP_ORIGIN", "*")  # e.g., "https://class-bot-bowie.onrender.com"
GROQ_API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Pick any currently supported Groq model in your Render env; this is a safe default:
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# ------------- FastAPI ---------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN] if APP_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Minimal in-memory store (swap for DB in prod) ----
SERVER_MEMORY: Dict[str, Any] = {
    "facts": {},          # { key: value }
    "snippets": [],       # [ {id, text, ts} ]
}

# ------------- Helpers ---------------
def _json_b64_decode(s: str) -> Dict[str, Any]:
    try:
        raw = base64.b64decode(s.encode("utf-8")).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return {}

async def call_llm(messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """
    Stream assistant text from Groq's OpenAI-compatible /chat/completions API.
    Yields plain text deltas suitable for SSE relaying.
    Surfaces errors inline so the frontend can display them.
    """
    if not GROQ_API_KEY:
        yield "[Server error] Missing GROQ_API_KEY"
        return

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "stream": True,
        # Optional:
        # "temperature": 0.5,
        # "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", GROQ_API_URL, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")
                    try:
                        err = json.loads(body).get("error")
                        msg = err.get("message") if isinstance(err, dict) else body
                    except Exception:
                        msg = body or "Unknown error"
                    yield f"[Groq {resp.status_code}] {msg}"
                    return

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        # Streaming frames are JSON objects
                        try:
                            obj = json.loads(data)
                        except Exception:
                            # In case Groq sends a plain-text diagnostic
                            if data:
                                yield str(data)
                            continue

                        if "error" in obj:
                            err = obj["error"]
                            msg = err.get("message", str(err))
                            yield f"[Groq error] {msg}"
                            continue

                        choices = obj.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            # Providers may send role first—ignore it until content arrives
                            content = delta.get("content")
                            if content:
                                yield content
        except httpx.HTTPError as e:
            yield f"[HTTP error] {str(e)}"
        except Exception as e:
            yield f"[Server exception] {str(e)}"

# ------------- Routes ----------------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/chat_sse")
async def chat_sse(q: str, ctx: str = ""):
    """
    Streaming chat via GET (SSE) to avoid CORS preflight.
    q   = prompt (URL-encoded)
    ctx = base64(JSON of {lastTurns, factHits, snippets})
    """
    prompt = urllib.parse.unquote_plus(q)
    context = _json_b64_decode(ctx)

    sys = "You are Class Robot—concise, friendly, practical. Use context sparingly."
    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]
    if context:
        messages.append({"role": "user", "content": f"[Context]\n{json.dumps(context)[:4000]}"} )
    messages.append({"role": "user", "content": prompt})

    async def event_stream():
        # Optional kickoff (blank); safe to remove if you prefer no empty token
        # yield "data: " + json.dumps({"delta": ""}) + "\n\n"
        async for chunk in call_llm(messages):
            yield "data: " + json.dumps({"delta": chunk}) + "\n\n"
        yield "data: " + json.dumps({"done": True}) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable proxy buffering
            "Transfer-Encoding": "chunked",
        },
    )

@app.post("/chat_stream")
async def chat_stream(req: Request):
    """
    Streaming chat via POST (if you prefer POST semantics).
    Body: { user?, prompt, context? }
    """
    body = await req.json()
    prompt: str = body.get("prompt", "") or ""
    context: Optional[Any] = body.get("context", [])

    sys = "You are Class Robot—concise, friendly, and practical. Use given context sparingly."
    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]
    if context:
        messages.append({"role": "user", "content": f"[Context]\n{json.dumps(context)[:4000]}"} )
    messages.append({"role": "user", "content": prompt})

    async def event_stream():
        async for chunk in call_llm(messages):
            yield "data: " + json.dumps({"delta": chunk}) + "\n\n"
        yield "data: " + json.dumps({"done": True}) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )

# ---- Memory sync (toy) ----
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
        # Ensure a minimal shape and avoid dupes
        if snip and isinstance(snip, dict) and snip.get("text"):
            SERVER_MEMORY["snippets"].append({
                "id": snip.get("id") or f"srv_{len(SERVER_MEMORY['snippets'])+1}",
                "text": snip["text"],
                "ts": snip.get("ts") or 0
            })
    return JSONResponse({"ok": True})

# ---- Simple non-streaming test (debug) ----
@app.get("/chat_test")
async def chat_test(q: str = "Say hello"):
    if not GROQ_API_KEY:
        return JSONResponse({"error": "Missing GROQ_API_KEY"}, status_code=500)
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "You are Class Robot—concise, friendly, practical."},
            {"role": "user", "content": q},
        ],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(GROQ_API_URL, headers=headers, json=payload)
        try:
            j = r.json()
        except Exception:
            j = {"raw": (await r.aread()).decode("utf-8", "ignore")}
        if r.status_code != 200:
            return JSONResponse({"status": r.status_code, "error": j}, status_code=500)
        try:
            text = j["choices"][0]["message"]["content"]
        except Exception:
            text = str(j)
        return {"ok": True, "text": text}

# ---- Optional: run with uvicorn locally ----
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
