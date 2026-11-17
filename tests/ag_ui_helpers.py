"""业务回归通过唯一 AG-UI 入口提交；流生命周期另由 test_ag_ui 验证。"""

import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from ag_ui.encoder import EventEncoder
from fastapi.responses import StreamingResponse

from app.api.v1 import ag_ui
from app.services.ag_ui_projection import EVENT_ADAPTER, load_projection


async def _submission_stream(run_id, request, actor, db, **kwargs):
    """只截短观察连接，不替换授权、输入校验、数据库事务或 Worker。"""
    run = await ag_ui._owned_run(db, run_id, actor.id)
    _, frames = await load_projection(db, run)
    encoder = EventEncoder()
    after = kwargs.get("after", 0)
    content = "".join(
        f"id: {run_id}:{frame['sequence']}\n" + encoder.encode(EVENT_ADAPTER.validate_python(frame["event"])) for frame in frames if frame["sequence"] > after
    )
    return StreamingResponse(iter([content]), media_type="text/event-stream")


async def submit_agent_run(client, session_run_path, *, json, **kwargs):
    """将业务场景参数构造成实际 RunAgentInput，返回便于核对的业务 Run。"""
    match = re.fullmatch(r"(.*/agents/\d+)/sessions/([^/]+)/runs", session_run_path)
    assert match, session_run_path
    base, session_id = match.groups()
    endpoint = f"{base}/ag-ui"
    history = await client.get(f"{endpoint}/threads/{session_id}", **kwargs)
    if history.status_code != 200:
        return history
    messages = history.json()["data"]["messages"]
    key = json.get("request_key", str(uuid4()))
    user_id = str(uuid5(NAMESPACE_URL, session_run_path + key))
    prior = next((i for i, message in enumerate(messages) if message["id"] == user_id), len(messages))
    props = {key: value for key, value in json.items() if key not in {"input_text", "request_key"}}
    body = {
        "threadId": session_id,
        "runId": key,
        "protocolVersion": "1.0",
        "messages": [
            *messages[:prior],
            {
                "id": user_id,
                "role": "user",
                "content": json.get("input_text", ""),
            },
        ],
        "forwardedProps": props,
    }
    with patch.object(ag_ui, "build_run_stream", _submission_stream):
        response = await client.post(endpoint, json=body, **kwargs)
    if response.status_code != 200:
        return response
    run = await client.get(f"/api/v1/runs/{response.headers['X-Y-Point-Run-ID']}", **kwargs)
    return httpx.Response(201, json=run.json(), request=response.request)


async def read_thread_messages(client, session_message_path, **kwargs):
    """从协议历史和交付元数据组装业务断言，不调用已移除的消息历史接口。"""
    url = urlsplit(session_message_path)
    match = re.fullmatch(r"(.*/agents/\d+)/sessions/([^/]+)/messages", url.path)
    assert match, session_message_path
    base, session_id = match.groups()
    response = await client.get(f"{base}/ag-ui/threads/{session_id}", **kwargs)
    if response.status_code != 200:
        return response
    thread = response.json()["data"]
    query = parse_qs(url.query)
    after = int(query.get("after", [0])[0])
    messages = [
        {
            **thread["messageDetails"][item["id"]],
            "content": item["content"],
            "role": item["role"],
        }
        for item in thread["messages"]
        if item["id"] in thread["messageDetails"] and thread["messageDetails"][item["id"]]["id"] > after
    ]
    return httpx.Response(
        200,
        json={
            "code": 200,
            "message": "success",
            "data": messages,
        },
        request=response.request,
    )


async def resume_agent_run(client, run_id, *, decision="approve", expected_approval_id=None, **kwargs):
    """通过相同 AG-UI POST 回答审批，保留原业务 Run。"""
    run_response = await client.get(f"/api/v1/runs/{run_id}", **kwargs)
    if run_response.status_code != 200:
        return run_response
    run = run_response.json()["data"]
    endpoint = f"/api/v1/agents/{run['agent_id']}/ag-ui"
    history = await client.get(f"{endpoint}/threads/{run['session_id']}", **kwargs)
    approval = await client.get(f"/api/v1/runs/{run_id}/approval", **kwargs)
    body = {
        "threadId": run["session_id"],
        "runId": str(uuid4()),
        "messages": history.json()["data"]["messages"],
        "resume": [
            {
                "interruptId": expected_approval_id or approval.json()["data"]["id"],
                "status": "resolved",
                "payload": {
                    "decision": decision,
                },
            }
        ],
    }
    with patch.object(ag_ui, "build_run_stream", _submission_stream):
        return await client.post(endpoint, json=body, **kwargs)
