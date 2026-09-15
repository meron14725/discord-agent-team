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


def test_implementation_answer_keeps_approvals_and_does_not_redraft(team):
    from sqlalchemy import select

    from agent_team.db import Job

    _, db, _, service, engine, command = team
    task_id = approved_implementation(team)
    with db.transaction() as session:
        task = session.get(Task, task_id)
        before = (task.data['requirements_approval_id'], task.data['plan_approval_id'])
        task.state = 'Blocked'
        job = session.scalar(select(Job).where(Job.task_id == task_id).order_by(Job.created.desc()))
        job.status = 'done'
        job.data = {**job.data, 'response': {'result': {'status':'needs_clarification'}}}
    result = command('answer', task_id=task_id, text='復旧して進めて')
    assert result['state'] == 'Queued'
    assert (result['data']['requirements_approval_id'], result['data']['plan_approval_id']) == before
    req = engine.prepare(*engine.claim())
    assert json.loads(req.prompt)['execution_clarifications'] == ['復旧して進めて']
    revised = command('revise', task_id=task_id, text='要件を変更する')
    assert revised['state'] == 'DraftingRequirements'
    assert revised['data']['requirements_approval_id'] == ''


def test_native_source_reads_only_apply_to_scoped_maintenance_jobs():
    from agent_team.adapters.codex import execution_role

    req = request(role='backend_integrator', kind='implement')
    assert 'do not invoke shell' in execution_role(req)
    instruction = execution_role(req.model_copy(update={'maintenance_paths':[PATH]}))
    assert 'shell read commands' in instruction
    assert 'never edit the source snapshot or the broker output directory' in instruction
    assert 'broker executes' in instruction


def test_patch_file_contract_only_allows_fixed_path_and_one_source():
    from pydantic import ValidationError

    from agent_team.contracts import PatchProposal

    assert PatchProposal(patch_file='/tmp/team-implementation.patch', rationale='large diff').patch == ''
    for fields in ({'patch_file':'/etc/passwd'}, {'patch':'diff','patch_file':'/tmp/team-implementation.patch'}, {}):
        with pytest.raises(ValidationError):
            PatchProposal(rationale='invalid', **fields)


def test_patch_file_is_validated_and_saved_before_application(tmp_path):
    import asyncio

    from agent_team.adapters.codex import MockRunner
    from agent_team.adapters.sbx import SbxRunner
    from agent_team.contracts import PatchProposal

    class PatchRunner(SbxRunner):
        async def command(self, job_id, args, *other, **kwargs):
            assert 'lstat()' in args[-1] and 'stat.S_ISREG' in args[-1]
            return PATCH

    req = request(role='backend_integrator', kind='implement', maintenance_paths=[PATH])
    result = asyncio.run(MockRunner().run(req)).result
    result.patches = [PatchProposal(patch_file='/tmp/team-implementation.patch', rationale='file output')]
    runner = PatchRunner(state_dir=tmp_path)
    asyncio.run(runner.resolve_patches('job','sandbox',req,result))
    assert result.patches[0].patch == PATCH and result.patches[0].patch_file == ''
    saved = json.loads(next((tmp_path / 'results').glob('*.json')).read_text())
    assert saved['result']['patches'][0]['patch'] == PATCH
    assert 'not evidence' in saved['note']


def test_known_source_patterns_do_not_hide_real_credentials():
    from agent_team.redaction import SecretScanner

    scanner = SecretScanner(b'test-salt-long-enough')
    assert not scanner.scan_text('SECRET = re.compile(r"pattern")').blocked
    assert not scanner.scan_text(json.dumps('SECRET = re.compile(r"pattern")')).blocked
    assert not scanner.scan_text('api_key="test-auth-value"').blocked
    assert scanner.scan_text('api_key="actual-secret-value"').blocked
    assert scanner.scan_text('secret="actual-secret-value"').blocked
    assert scanner.scan_text('SECRET = re.compile(r"' + 'ghp_' + 'A' * 36 + '")').blocked


def test_saved_patch_resume_requires_same_spec_plan_scope_and_identity(tmp_path):
    import asyncio
    import hashlib

    from agent_team.adapters.codex import MockRunner
    from agent_team.adapters.sbx import SbxRunner
    from agent_team.contracts import PatchProposal

    req = request(role='backend_integrator', kind='implement', maintenance_paths=[PATH], resume_patch_job_id='old-job')
    result = asyncio.run(MockRunner().run(req)).result
    result.patches = [PatchProposal(patch=PATCH, rationale='saved proposal')]
    root = tmp_path / 'results'
    root.mkdir()
    (root / (hashlib.sha256(b'old-job').hexdigest() + '.json')).write_text(json.dumps({
        'job_id':'old-job', 'result':result.model_dump(), 'maintenance_paths':[PATH]}))
    runner = SbxRunner(state_dir=tmp_path)
    assert runner.resume_patch(req).patches[0].patch == PATCH
    assert runner.resume_patch(req.model_copy(update={'kind':'fix'})).patches[0].patch == PATCH
    for change in [{'spec_hash':'other'}, {'head_sha':'other'}, {'maintenance_paths':['src/app.py']}, {'kind':'review'}]:
        with pytest.raises(GuardError):
            runner.resume_patch(req.model_copy(update=change))


def test_sbx_maintenance_input_limit_is_separate_from_published_change_limit():
    from agent_team.adapters.sbx import source_byte_limit, source_file_limit

    source = {f'src/input_{index}.py': 'safe = True\n' for index in range(101)}
    req = request(role='backend_integrator', kind='fix', files=source,
                  maintenance_paths=[PATH])
    assert source_file_limit(req.model_copy(update={'maintenance_paths': []})) == 100
    assert source_file_limit(req) == 190
    assert source_byte_limit(req.model_copy(update={'maintenance_paths': []})) == 2_000_000
    assert source_byte_limit(req) == 4_000_000
    review = req.model_copy(update={'kind': 'review', 'role': 'cto', 'maintenance_paths': []})
    assert source_file_limit(review) == 190
    assert source_byte_limit(review) == 4_000_000
