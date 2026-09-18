# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.33.0",
#     "vgi-rpc>=0.46.0",
#     "httpx2>=2.9.1",
# ]
# ///
"""HTTP entry point for the Hacker News VGI worker (``uv run serve.py --port 8000``)."""

from __future__ import annotations

from vgi_hackernews.worker import main_http

if __name__ == "__main__":
    main_http()
