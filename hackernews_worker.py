# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.34.1",
#     "vgi-rpc>=0.46.0",
#     "httpx2>=2.9.1",
# ]
# ///
"""Stdio entry point for the Hacker News VGI worker (``uv run``).

ATTACH 'hackernews' (TYPE vgi, LOCATION 'uv run hackernews_worker.py');
"""

from __future__ import annotations

from vgi_hackernews.worker import main

if __name__ == "__main__":
    main()
