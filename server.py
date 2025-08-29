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
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

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
