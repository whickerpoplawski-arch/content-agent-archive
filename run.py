import os
import sys
import asyncio

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    reload = os.environ.get("XHS_RELOAD", "1") != "0"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=reload)