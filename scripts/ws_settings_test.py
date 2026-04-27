"""Quick probe: ask the backend for settings and print them."""
import asyncio
import json
import sys

import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        hello = await ws.recv()
        hello_j = json.loads(hello)
        print("hello.settings:", json.dumps(hello_j.get("settings"), indent=2))

        await ws.send(json.dumps({"cmd": "list_settings"}))
        seen_settings = False
        while not seen_settings:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            j = json.loads(msg)
            if j.get("event") == "settings":
                print("settings event:", json.dumps(j, indent=2))
                seen_settings = True
            else:
                print("other event:", j)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
