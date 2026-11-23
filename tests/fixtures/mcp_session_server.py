# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : mcp_session_server.py

"""有状态 stdio 协议夹具，记录真实收包；慢调用仍能接受取消通知。"""

import json
from pathlib import Path
import sys

log = Path(sys.argv[1])
count = 0
for line in sys.stdin:
    request = json.loads(line)
    with log.open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    method = request["method"]
    if "id" not in request:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "session-state",
                "version": "1.0",
            },
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "inspect",
                    "description": "读取当前页面状态",
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
            ]
        }
    elif method == "tools/call":
        count += 1
        value = request["params"]["arguments"]["value"]
        if value == "slow":
            continue
        result = {
            "content": [
                {
                    "type": "text",
                    "text": f"stdio:{count}:{value}",
                }
            ],
            "isError": False,
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
