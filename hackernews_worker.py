# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "vgi-python[http]>=0.55.1,<0.56",
#     "vgi-rpc>=0.57.1",
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
