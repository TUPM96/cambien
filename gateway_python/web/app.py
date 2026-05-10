"""
Dashboard truc tiep cho gateway LoRa.
Chay qua entry: python run_all.py
Lich su: thu muc web/data/lora_history_YYYY-MM-DD.csv (UTF-8 BOM, mo duoc Excel).
"""

import os
import socket
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from data_store import get_state

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

app = FastAPI(title="LoRa Gateway Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"data_source": "Gateway noi bo"},
    )


@app.get("/api/state")
def api_state():
    return get_state()


def _pick_listen_port(preferred: int, span: int = 40) -> int:
    last_err: OSError | None = None
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError as e:
                last_err = e
                continue
            return port
    msg = f"Khong co cong trong trong [{preferred}, {preferred + span - 1}]"
    if last_err:
        msg += f": {last_err}"
    raise OSError(msg)


if __name__ == "__main__":
    import uvicorn

    want = int(os.getenv("WEB_PORT", "8000"))
    port = _pick_listen_port(want)
    if port != want:
        print(f"Cong {want} dang ban -> dung cong {port}")
    print(f"Mo trinh duyet: http://127.0.0.1:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
