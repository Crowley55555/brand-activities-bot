from __future__ import annotations

import asyncio
import logging
import sys

from tg_worker.bot import run_polling


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
