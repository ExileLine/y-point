# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:20:41
# @Author  : yangyuexiong
# @File    : mcp_stdio_server.py

"""使用标准库实现受控 MCP Server，验证真实管道及容器生命周期。"""

import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
if mode == "malformed":
    print("not-json-private-secret", flush=True)
    time.sleep(20)
if mode == "large_output":
    print("x" * 8192, flush=True)
    time.sleep(20)
if mode == "large_logs":
    sys.stderr.write("private-secret" * 1024)
    sys.stderr.flush()
    time.sleep(20)
if mode == "nonzero":
    sys.exit(23)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if mode == "stall":
        time.sleep(20)
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "controlled-stdio",
                "version": "1.0",
            },
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": "受控读取",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "string",
                            },
                        },
                        "required": ["value"],
                    },
                    "annotations": {
                        "readOnlyHint": True,
                    },
                }
                for name in ("echo", "lookup")
            ],
        }
    elif method == "tools/call":
        args = request["params"]["arguments"]
        result = {
            "content": [
                {
                    "type": "text",
                    "text": args["value"],
                }
            ],
            "isError": args["value"] == "error",
        }
    else:
        result = {}
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": result,
            }
        ),
        flush=True,
    )
