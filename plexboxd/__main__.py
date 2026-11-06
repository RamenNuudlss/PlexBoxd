"""Run with: python -m plexboxd"""

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run("plexboxd.web:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8977")),
                log_level=os.environ.get("LOG_LEVEL", "info").lower(), proxy_headers=True)


if __name__ == "__main__":
    main()
