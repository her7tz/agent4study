"""Offline acceptance evidence for FR-02/03/04/05/17.

Uses disposable storage and an injected model adapter, never personal data or
live model credentials. Assertions cover execution, not model answer quality.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_profiles import AgentProfiles, PROVIDERS, ProfileError, create_client, resolve_agent
from capabilities import Capability, CapabilityContext, CapabilityRegistry
from conversation import ChatService
from knowledge import KnowledgeService
from runtime_control import CancellationToken
from runtime_pool import ParallelDaemonExecutor
from tool_registry import Tool, ToolRegistry


class AgentContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = SimpleNamespace(data_home=root, output_dir=root / 'summaries',
            database_path=root / 'harness.sqlite3', api_key='fixture-key', model='default-model',
            base_url='https://api.deepseek.com', max_parallel_tasks=4)

    def service(self):
        service = ChatService(self.settings.database_path, lambda: self.settings, Mock())
        self.addCleanup(service.close)
        return service

    def test_main_loop_uses_registered_adapter_and_feeds_tool_result_back(self):
        service = self.service()
        registry = ToolRegistry()
        executor = Mock(return_value={'value': 42})
        registry.register(Tool('lookup', 'lookup', {'type': 'object', 'properties': {}}, executor))
        client = SimpleNamespace(settings=self.settings, timeout=55)
        rounds = []

        def reply(messages, **kwargs):
            rounds.append(json.loads(json.dumps(messages)))
            if len(rounds) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call-1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}
            self.assertEqual(messages[-1]['role'], 'tool')
            self.assertEqual(json.loads(messages[-1]['content']), {'value': 42})
            return {'role': 'assistant', 'content': '查询结果为42。'}

        client.stream_chat = reply
        factory = Mock(return_value=client)
        with patch.dict(PROVIDERS, {'fixture': factory}), patch.object(service, '_registry', return_value=registry):
            AgentProfiles(self.settings).path.write_text(json.dumps({'global': {'provider': 'fixture'}}))
            result = service.send('查询结果', web_enabled=False)
        executor.assert_called_once()
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0].provider, 'fixture')
        self.assertEqual(result['messages'][-1]['content'], '查询结果为42。')
        self.assertEqual(len(rounds), 2)
        with service._connect() as db:
            raw = [json.loads(row[0]) for row in db.execute('SELECT message FROM conversation_messages ORDER BY id')]
        self.assertEqual([item['role'] for item in raw], ['user', 'assistant', 'tool', 'assistant'])

    def test_web_disabled_removes_research_and_study_log_tools(self):
        service = self.service()
        web = Mock()
        tools = service._registry(web, False)
        names = {item['function']['name'] for item in tools.metadata()}
        for name in ('search_web', 'read_webpage', 'read_study_log', 'create_study_report',
                     'schedule_study_report', 'import_shared_conversation'):
            self.assertNotIn(name, names)
            with self.assertRaises(ValueError):
                tools.call(name, {}, [])
        web.assert_not_called()

    def test_independent_profiles_inheritance_and_running_skill_snapshot(self):
        profiles = AgentProfiles(self.settings)
        profiles.update('paper_reading', {'api_key': 'paper-key', 'model': 'paper-model', 'skills': []})
        paper = resolve_agent(self.settings, 'paper_reading')
        video = resolve_agent(self.settings, 'video_learning')
        self.assertEqual((paper.api_key, paper.model, paper.skill_names), ('paper-key', 'paper-model', ()))
        self.assertEqual((video.api_key, video.model), ('fixture-key', 'default-model'))
        self.assertNotIn('paper-key', json.dumps(profiles.list()))
        profiles.update('paper_reading', {'inherit': ['api_key', 'model', 'skills']})
        next_run = resolve_agent(self.settings, 'paper_reading')
        self.assertEqual((next_run.api_key, next_run.model), ('fixture-key', 'default-model'))
        self.assertEqual((paper.api_key, paper.model, paper.skill_names), ('paper-key', 'paper-model', ()))
        profiles.write_skill('summary-core', '下一次任务的新规则', role='video_learning')
        self.assertNotIn('下一次任务的新规则', video.skill_instructions)
        self.assertIn('下一次任务的新规则', resolve_agent(self.settings, 'video_learning').skill_instructions)

    def test_unknown_provider_is_rejected_before_execution(self):
        self.settings.provider = 'not_registered'
        with self.assertRaisesRegex(ProfileError, '尚未适配'):
            create_client(self.settings)
        AgentProfiles(self.settings).path.write_text(json.dumps({'global': {'provider': 'not_registered'}}))
        with self.assertRaisesRegex(ProfileError, '尚未适配'):
            resolve_agent(self.settings, 'conversation')

    def test_main_profile_can_override_defaults_without_changing_subagents(self):
        profiles = AgentProfiles(self.settings)
        profiles.update('global', {'skills': ['summary-core']})
        profiles.write_skill('summary-core', '共享默认规则', role='global')
        profiles.update('conversation', {'model': 'conversation-model', 'api_key': 'conversation-key',
                                         'skills': ['summary-core']})
        profiles.write_skill('summary-core', '仅供主 Agent 的规则', role='conversation')
        main = resolve_agent(self.settings, 'conversation')
        self.assertEqual((main.model, main.api_key), ('conversation-model', 'conversation-key'))
        self.assertIn('仅供主 Agent 的规则', main.skill_instructions)
        self.assertNotIn('共享默认规则', main.skill_instructions)

        video = resolve_agent(main, 'video_learning')
        self.assertEqual((video.model, video.api_key), ('default-model', 'fixture-key'))
        self.assertIn('共享默认规则', video.skill_instructions)
        self.assertNotIn('仅供主 Agent 的规则', video.skill_instructions)
        self.assertIn('fixture-key', AgentProfiles(main).secrets())
        self.assertEqual(profiles.public('conversation')['sources']['model'], 'independent')
        self.assertNotIn('conversation-key', json.dumps(profiles.list()))

        profiles.update('conversation', {'inherit': ['model', 'api_key', 'skills']})
        inherited = resolve_agent(self.settings, 'conversation')
        self.assertEqual((inherited.model, inherited.api_key), ('default-model', 'fixture-key'))
        self.assertIn('仅供主 Agent 的规则', inherited.skill_instructions)
        self.assertEqual(main.model, 'conversation-model')

    def test_knowledge_uses_its_role_adapter(self):
        service = self.service()
        profiles = AgentProfiles(self.settings)
        factory = Mock(return_value=SimpleNamespace(settings=self.settings, timeout=120))
        with patch.dict(PROVIDERS, {'fixture': factory}):
            profiles.update('knowledge_archivist', {'provider': 'fixture', 'model': 'archive-model'})
            service.knowledge._extract({'title': '空对话', 'messages': []}, CancellationToken(), lambda _: None)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0].model, 'archive-model')

    def test_new_capability_preserves_injected_adapter_and_saves_result(self):
        registry = CapabilityRegistry()
        client = object()
        observed = []
        def execute(parameters, context):
            observed.append(context)
            return {'markdown': '# 示例\n\n生成成果。'}
        registry.register(Capability('fixture', '示例', '示例',
            {'type': 'object', 'properties': {}}, execute, settings_role='conversation'))
        # A newly registered capability can choose an existing output category.
        with patch('capabilities.capability_settings_role', return_value='conversation'), \
             patch('storage_layout.feature_directory', return_value=self.settings.output_dir / 'notes'):
            result = registry.execute('fixture', {}, CapabilityContext(self.settings, client=client))
        self.assertIs(observed[0].client, client)
        self.assertEqual(Path(result['path']).read_text(), '# 示例\n\n生成成果。')
        self.assertEqual(result['capability'], 'fixture')

    def test_independent_workers_overlap_and_failure_does_not_stop_other_task(self):
        pool = ParallelDaemonExecutor(2)
        barrier = threading.Barrier(2)
        def work(fail):
            barrier.wait(timeout=3)
            if fail:
                raise ValueError('独立任务失败')
            return '另一任务成功'
        try:
            first, second = pool.submit(work, True), pool.submit(work, False)
            with self.assertRaisesRegex(ValueError, '独立任务失败'):
                first.result(timeout=4)
            self.assertEqual(second.result(timeout=4), '另一任务成功')
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    unittest.main()
