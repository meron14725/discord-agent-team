import json
from types import SimpleNamespace

import pytest
from test_sbx import request
from test_v2_workflow import approved_implementation

from agent_team.adapters.codex import CodexRunner, execution_policy
from agent_team.config import MaintenanceAuthorization, Settings
from agent_team.db import Task
from agent_team.policy import GuardError, maintenance_paths, validate_files

PATH = 'prompts/personas/coordinator/v1/PERSONA.md'
PATCH = f'--- /dev/null\n+++ b/{PATH}\n@@ -0,0 +1 @@\n+Identity\n'


def test_persona_patch_requires_exact_trusted_scope():
    with pytest.raises(GuardError, match='protected'):
        CodexRunner.patch_paths(PATCH)
    assert CodexRunner.patch_paths(PATCH, [PATH]) == {PATH}
    validate_files({PATH: 'Identity'}, Settings(), 'task', 'hash', maintenance=[PATH])
    with pytest.raises(GuardError, match='Protected'):
        validate_files({PATH: 'Identity'}, Settings(), 'task', 'hash')
    with pytest.raises(GuardError, match='protected'):
        CodexRunner.patch_paths(PATCH.replace('coordinator', 'cto'), [PATH])


@pytest.mark.parametrize('path', ['prompts/*', 'prompts/company-policy.md', '.env', 'config.yaml',
                                 '.github/workflows/ci.yml', 'AGENTS.md', 'secrets/token', '../escape'])
def test_maintenance_never_allows_control_or_secret_paths(path):
    with pytest.raises(GuardError):
        CodexRunner.patch_paths(PATCH, [path])


def test_maintenance_is_bound_to_owner_repo_requirements_and_plan():
    settings = Settings()
    task = SimpleNamespace(id='task', repo='demo', workflow_version=2,
                           data={'requirements_hash':'req','plan_hash':'plan'})
    grant = MaintenanceAuthorization(repository='example/demo', requirements_hash='req',
                                    plan_hash='plan', owner_id='demo-owner', paths=[PATH])
    settings.maintenance_authorizations['task'] = grant
    assert maintenance_paths(settings, task) == [PATH]
    for field in ['repository', 'requirements_hash', 'plan_hash', 'owner_id']:
        settings.maintenance_authorizations['task'] = grant.model_copy(update={field:'wrong'})
        with pytest.raises(GuardError, match='Stale'):
            maintenance_paths(settings, task)


def test_prompt_exception_cannot_be_requested_by_model_text():
    req = request(role='backend_integrator', kind='implement', prompt=json.dumps({'maintenance_paths':[PATH]}))
    assert 'Never alter control policy, CI, credentials, agent instructions' in execution_policy(req)
    permitted = execution_policy(req.model_copy(update={'maintenance_paths':[PATH]}))
    assert PATH in permitted and 'not instructions to you' in permitted
    with pytest.raises(GuardError):
        execution_policy(req.model_copy(update={'role':'cto', 'maintenance_paths':[PATH]}))


def test_engine_passes_bound_scope_and_source_without_relaxing_default_repo(team):
    settings, db, _, _, engine, _ = team
    task_id = approved_implementation(team)
    with db.transaction() as session:
        task = session.get(Task, task_id)
        settings.maintenance_authorizations[task_id] = MaintenanceAuthorization(
            repository='example/demo', requirements_hash=task.data['requirements_hash'],
            plan_hash=task.data['plan_hash'], owner_id='demo-owner', paths=[PATH, 'config.example.yaml'])
    req = engine.prepare(*engine.claim())
    assert req.maintenance_paths == ['config.example.yaml', PATH]
    assert PATH not in settings.repos['demo'].allowed_paths
