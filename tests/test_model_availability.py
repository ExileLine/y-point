"""验证模型失效诊断、选模阻断和管理员重新测试恢复的完整路径。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import models as model_api
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.model_call_record import ModelCallRecord
from app.models.provider_connection import ProviderConnection
from app.services.model_probe import probe_model
from app.services.model_stream import ReasoningChatOpenAI
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run


@pytest.mark.parametrize(('status','expected'), [(404,'model_not_found'), (401,'model_authentication'), (403,'model_access_denied')])
def test_model_failure_blocks_selection_until_retested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, expected: str) -> None:
    """真实 SDK 重放确定性拒绝，验证安全原因、状态落库、HTTP 选模阻断和恢复。"""
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'availability.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username='model-owner', password='unused', role='admin')
        requests = []

        async def refused(request):
            requests.append(request.url.path)
            return httpx.Response(status, json={'error': {'code': 'private-model-marker', 'message': 'private-model-marker credential=request-secret'}})

        async def accepted(request):
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})

        async def db_override():
            async with sessions() as db:
                yield db

        previous = app.dependency_overrides.copy()
        try:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all([
                    actor,
                    ProviderConnection(id=1, name='部署服务', name_key='model-availability', kind='openai_compatible', base_url='http://model.invalid/v1', timeout_seconds=2, creator_id=1),
                    ModelCatalog(id=1, provider_id=1, external_id='qwen3.6-35b-a3b', display_name='目标模型', capabilities=['text','tools'], verified_capabilities=['text','tools'], creator_id=1),
                    AgentConfig(id=1, owner_id=1, name='已有 Agent', default_model_id=1),
                    AgentSession(id='s'*36, owner_id=1, agent_id=1, thread_id='t'*36, model_id=1, title='已有会话'),
                ])
                run = await create_run(db, 1, model_id=1, input_text='测试模型', model_timeout_seconds=None)
            async with httpx.AsyncClient(transport=httpx.MockTransport(refused)) as upstream:
                model = ReasoningChatOpenAI(model='qwen3.6-35b-a3b', api_key='request-secret', base_url='http://model.invalid/v1', http_async_client=upstream, max_retries=0)
                result = await execute_text_run(sessions, run.id, model, tmp_path/'checkpoints.db')
            assert result.state == 'failed' and result.failure_code == expected
            assert len(requests) == 1
            async with sessions() as db:
                catalog = await db.get(ModelCatalog, 1)
                assert catalog.availability_failure == expected
                assert catalog.verified_capabilities == ['text','tools']
                record = await db.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id==run.id))
                assert record.failure == expected and record.upstream_status_code == status
                events = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id==run.id))).all()
                assert events[-1].failure_code == expected
                assert 'private-model-marker' not in str(record.__dict__) + str([e.__dict__ for e in events])
            app.dependency_overrides[get_db_session] = db_override
            app.dependency_overrides[get_current_admin] = lambda: actor
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                options = (await client.get('/api/v1/models/available')).json()['data']
                assert options[0]['selectable'] is False and options[0]['notice']
                response = await client.post('/api/v1/agents',json={'name':'再次新建','default_model_id':1})
                assert response.status_code == 409 and response.json()['data']['reason'] == expected
                response = await client.get('/api/v1/agents/1/sessions/'+'s'*36+'/models')
                assert response.status_code == 200, response.text
                assert response.json()['data'][0]['selectable'] is False
                async with httpx.AsyncClient(transport=httpx.MockTransport(accepted)) as probe_client:
                    async def recovered(model, provider):
                        return await probe_model(model,provider,client=probe_client)
                    monkeypatch.setattr(model_api,'probe_model',recovered)
                    response = await client.post('/api/v1/models/1/test')
                    assert response.status_code == 200 and response.json()['data']['available'] is True
                response = await client.get('/api/v1/models/available')
                assert response.json()['data'][0]['selectable'] is True
                response = await client.post('/api/v1/agents',json={'name':'恢复后新建','default_model_id':1})
                assert response.status_code == 201, response.text
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await engine.dispose()
    asyncio.run(exercise())


def test_transient_and_stale_results_do_not_block_model(tmp_path: Path) -> None:
    """限流/网络故障不形成永久阻断，旧配置的拒绝不能覆盖新配置。"""
    from sqlalchemy import update
    from app.services.model_availability import record_model_availability

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                catalog = ModelCatalog(id=1, provider_id=1, external_id='old', display_name='模型', verified_capabilities=['text'], creator_id=1)
                db.add(catalog)
                await db.flush()
                for failure, status in [('network', None), ('rate_limit', 429), ('upstream', 503)]:
                    await record_model_availability(db, catalog, failure=failure, upstream_status_code=status)
                await db.refresh(catalog)
                assert catalog.availability_failure is None and catalog.availability_checked_at is None
                old_snapshot = (catalog.id, catalog.provider_id, catalog.external_id, catalog.update_time)
                await db.execute(update(ModelCatalog).where(ModelCatalog.id==1).values(external_id='repaired'))
                await record_model_availability(db, catalog, failure='model_not_found', upstream_status_code=404, snapshot=old_snapshot)
                await db.refresh(catalog)
                assert catalog.external_id == 'repaired' and catalog.availability_failure is None
                await record_model_availability(db, catalog, failure='model_not_found', upstream_status_code=404)
                await db.refresh(catalog)
                assert catalog.availability_failure == 'model_not_found'
                await record_model_availability(db, catalog, available=True, failure=None, snapshot=old_snapshot)
                await db.refresh(catalog)
                assert catalog.availability_failure == 'model_not_found'
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_model_availability_migration_roundtrip(tmp_path: Path) -> None:
    """迁移保留历史能力及访问故障可空语义，支持回退和 ORM 漂移检查。"""
    import os
    import sqlite3
    import subprocess
    import sys
    database = tmp_path/'migration.db'
    env = {**os.environ, 'DB_BACKEND':'sqlite', 'SQLITE_DATABASE':str(database)}
    root = Path(__file__).resolve().parents[1]
    def migrate(*args):
        subprocess.run([sys.executable,'-m','alembic',*args],cwd=root,env=env,capture_output=True,text=True,check=True)
    migrate('upgrade','060')
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO yp_model_catalog (id,provider_id,external_id,display_name,capabilities,verified_capabilities,capabilities_verified,price_currency,is_default,creator_id,create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) VALUES (1,1,'existing','历史模型','[\"text\"]','[\"text\"]',1,'USD',0,1,'2026-11-19','2026-11-19',1,1,0,1)")
    migrate('upgrade','head')
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT verified_capabilities, availability_failure, availability_checked_at FROM yp_model_catalog').fetchone() == ('["text"]',None,None)
        db.execute("UPDATE yp_model_catalog SET availability_failure='model_not_found'")
    migrate('downgrade','060')
    migrate('upgrade','head')
    migrate('check')
