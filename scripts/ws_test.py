"""Quick end-to-end test of the Jarvis WebSocket.

Connects, prints every event, sends a test "say" command, and exits when
speaking_end is received (or after a timeout).
"""
import asyncio
import json
import sys

import websockets


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765"
    print(f"connecting to {url} ...")
    async with websockets.connect(url) as ws:
        text = sys.argv[2] if len(sys.argv) > 2 else "Hello sir, Jarvis online."
        await ws.send(json.dumps({"cmd": "say", "text": text}))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                evt = json.loads(raw)
                print("<", evt)
                if evt.get("event") == "speaking_end":
                    break
        except asyncio.TimeoutError:
            print("timeout waiting for events")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
