"""V2 review API compatibility and stage-projection contract.

Needs full runtime test dependencies, but no paid APIs or external network.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import engine as db_engine
from backend.db import get_repository
from backend.db.models import Base
from backend.domain.task import TaskStatus


@pytest.fixture
def api(tmp_path, monkeypatch):
    import backend.main as main_module
    from backend.runtime import queue as job_queue
    from backend.runtime import redis as rt_redis
    from backend.runtime import events

    db_path = tmp_path / 'v2-api.db'
    monkeypatch.setattr(db_engine, 'resolve_database_url', lambda: f'sqlite+pysqlite:///{db_path}')
    db_engine.reset_engine()
    Base.metadata.create_all(db_engine.get_engine())

    class DummyRedis:
        pass

    async def fake_publish(*_args, **_kwargs):
        return '1-1'

    async def fake_enqueue(*_args, **_kwargs):
        return '1-2'

    monkeypatch.setattr(rt_redis, 'get_redis', lambda: DummyRedis())
    monkeypatch.setattr(job_queue, 'enqueue', fake_enqueue)
    monkeypatch.setattr(events, 'publish', fake_publish)
    client = TestClient(main_module.app)
    yield client, get_repository()
    db_engine.reset_engine()


def waiting_task(client, repo, stage):
    response = client.post('/api/research/start', json={'query': 'compare databases'})
    assert response.status_code == 200, response.text
    tid = response.json()['thread_id']
    task = repo.get(tid)
    task.status = TaskStatus.WAITING_REVIEW.value
    task.stage = stage
    repo.save(task)
    return tid


def test_v0_v1_tasks_reject_v2_only_action_without_consuming_review(api):
    client, repo = api
    tid = waiting_task(client, repo, 'human_review')
    for action in ('revise_draft', 'more_research'):
        resp = client.post(f'/api/research/{tid}/resume', json={
            'action': action, 'feedback': 'missing pricing data',
        })
        assert resp.status_code == 409, resp.text
        assert resp.json()['error']['code'] == 'REVIEW_NOT_ALLOWED'
    assert repo.latest_review(tid) is None
    assert repo.get(tid).status == TaskStatus.WAITING_REVIEW.value


def test_v2_more_research_persists_action_and_targets_research_stage(api):
    client, repo = api
    tid = waiting_task(client, repo, 'human_review_v2')
    resp = client.post(f'/api/research/{tid}/resume', json={
        'action': 'more_research', 'feedback': 'Find price evidence for Europe',
    })
    assert resp.status_code == 200, resp.text
    saved = repo.latest_review(tid)
    assert saved.action == 'more_research'
    assert saved.feedback == 'Find price evidence for Europe'
    assert repo.get(tid).stage == 'targeted_research'


def test_v2_draft_edit_requires_feedback_without_requesting_research(api):
    client, repo = api
    tid = waiting_task(client, repo, 'human_review_v2')
    bad = client.post(f'/api/research/{tid}/resume', json={
        'action': 'revise_draft', 'feedback': ' ',
    })
    assert bad.status_code == 422
    assert repo.get(tid).status == TaskStatus.WAITING_REVIEW.value
    good = client.post(f'/api/research/{tid}/resume', json={
        'action': 'revise_draft', 'feedback': 'Put risks first',
    })
    assert good.status_code == 200, good.text
    assert repo.get(tid).stage == 'evaluate_research'


def test_cancel_remains_separate_from_resume(api):
    client, repo = api
    tid = waiting_task(client, repo, 'human_review_v2')
    bad = client.post(f'/api/research/{tid}/resume', json={'action': 'cancel'})
    assert bad.status_code == 422
    cancelled = client.post(f'/api/research/{tid}/cancel')
    assert cancelled.status_code == 200, cancelled.text
    assert repo.get(tid).status == TaskStatus.CANCELLED.value
