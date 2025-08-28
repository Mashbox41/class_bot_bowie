# Class Robot (Web + FastAPI)

Low-latency streaming chat with local (IndexedDB) memory + simple server memory sync.

## Deploy

### 1) Render via `render.yaml`
- Fork this repo, then in Render: **Blueprint → New from YAML** (point at your repo).
- Set env vars after create:
  - `APP_ORIGIN` = `https://<your-frontend-domain>` (or `*` for testing)
  - `ANTHROPIC_PROXY_URL` = your proxy endpoint (e.g., `https://droidrun.onrender.com/anthropic`)

### 2) Frontend hosting
- Any static host works (GitHub Pages, Netlify, your existing domain).
- In `web/index.html`, set `API_BASE` to your Render URL (e.g., `https://class-robot-server.onrender.com`).

### Local dev

```bash
cd server
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
