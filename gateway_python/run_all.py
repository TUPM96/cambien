import os
import socket
from pathlib import Path

from dotenv import load_dotenv

from gateway_lora_mqtt import LoRaMQTTGateway


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
    msg = f"No free port in [{preferred}, {preferred + span - 1}]"
    if last_err:
        msg += f": {last_err}"
    raise OSError(msg)


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")

    gw = LoRaMQTTGateway()
    gw.start()

    import uvicorn

    want = int(os.getenv("WEB_PORT", "8000"))
    port = _pick_listen_port(want)
    if port != want:
        print(f"Port {want} busy -> use {port}")

    print(f"Open: http://0.0.0.0:{port}")
    uvicorn.run("web.app:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
