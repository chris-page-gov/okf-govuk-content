#!/usr/bin/env python3
"""Run the read-only GOV.UK new-child MCP adapter."""

from govuk_okf.demo_mcp import serve_main


if __name__ == "__main__":
    raise SystemExit(serve_main())
