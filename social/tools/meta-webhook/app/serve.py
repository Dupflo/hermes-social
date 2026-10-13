"""s09 — run the public app (:8791) and the internal app (:8792) in one process.

Console ADR 012: Caddy publishes all of :8791 on meta.dupuisweb.com, so /internal/* lives on a
second FastAPI app served on a port that is never published. Usage: `python -m app.serve`.

Signals are handled here, not by uvicorn (0.51 installs its own handlers per server, which
would only stop one of the two): SIGTERM/SIGINT stop both. If either listener stops or fails
(e.g. SystemExit when its port is taken), the other is stopped and the process exits non-zero.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys

import uvicorn


logger = logging.getLogger("app.serve")


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _run(name: str, server: uvicorn.Server, other: uvicorn.Server, state: dict[str, bool]) -> None:
    try:
        await server.serve()
    except BaseException:
        state["failed"] = True
        logger.error("serve_failed %s", name)
    else:
        if not state["stopping"]:
            state["failed"] = True
            logger.error("serve_failed %s", name)
    finally:
        other.should_exit = True


async def main() -> int:
    host = "0.0.0.0"
    public = _Server(uvicorn.Config("app.main:app", host=host, port=int(os.environ.get("META_PUBLIC_PORT", "8791"))))
    internal = _Server(
        uvicorn.Config("app.internal:app", host=host, port=int(os.environ.get("META_INTERNAL_PORT", "8792")))
    )
    state = {"stopping": False, "failed": False}

    def shutdown() -> None:
        state["stopping"] = True
        public.should_exit = True
        internal.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    await asyncio.gather(
        _run("public", public, internal, state),
        _run("internal", internal, public, state),
    )
    return 1 if state["failed"] else 0


if __name__ == "__main__":
    # WARNING: same verbosity as plain `uvicorn` (the app loggers had no handler before s09).
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:     %(name)s %(message)s")
    sys.exit(asyncio.run(main()))
