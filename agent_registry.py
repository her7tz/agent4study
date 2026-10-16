"""Subagents own their operation toolkits; workflows never create agents."""
from dataclasses import dataclass, field
import threading
from types import MappingProxyType
from typing import Mapping

@dataclass(frozen=True)
class Subagent:
    id: str
    title: str
    description: str
    operations: Mapping = field(default_factory=dict)
    skills: tuple = ()
    legacy_roles: tuple = ()
    page: str = ''
    delegatable: bool = True

    def __post_init__(self):
        object.__setattr__(self,"operations",MappingProxyType(dict(self.operations)))
        object.__setattr__(self,"skills",tuple(self.skills))
        object.__setattr__(self,"legacy_roles",tuple(self.legacy_roles))

    def metadata(self):
        from capabilities import get_capability
        operations=[]
        for name,factory in self.operations.items():
            if factory is not None:
                operations.append(get_capability(name).metadata())
            else:
                operations.append({'id':name,'title':'对话知识整理','description':'从用户选择归档的本地对话或 ChatGPT 公开分享链接提取知识；每次保存一份包含全部知识点的文档并纳入个人知识库。'})
        return {'id':self.id,'title':self.title,'description':self.description,'operations':operations,
                'tools':sorted({name for factory in self.operations.values() if factory for name in getattr(factory,'tool_names',())}),
                'workflow_steps':[] if self.delegatable else ['读取对话来源','知识提取','来源校验','保存归档'],
                'default_skills':list(self.skills),'page':self.page or 'agent_'+self.id,'delegatable':self.delegatable}

class SubagentRegistry:
    def __init__(self):self._agents={};self._lock=threading.RLock()
    def register(self,agent):
        import re
        with self._lock:
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',agent.id) or agent.id in self._agents:raise ValueError('Subagent 编号无效或已注册。')
            names={name for item in self._agents.values() for name in (item.id,*item.legacy_roles)}
            if len(set((agent.id,*agent.legacy_roles)))!=1+len(agent.legacy_roles) or names.intersection((agent.id,*agent.legacy_roles)):raise ValueError('Subagent 编号或兼容别名重复。')
            if agent.page and (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',agent.page) or agent.page in {'chat','knowledge','library','schedules','history','settings'}):raise ValueError('Subagent 页面编号无效。')
            if any((item.page or 'agent_'+item.id)==(agent.page or 'agent_'+agent.id) for item in self._agents.values()):raise ValueError('Subagent 页面已注册。')
            existing={name for item in self._agents.values() for name in item.operations}
            if existing.intersection(agent.operations):raise ValueError('能力已归属于其他 Subagent。')
            self._agents[agent.id]=agent
    def add_operation(self,agent_id,operation,factory):
        from dataclasses import replace
        from capabilities import get_capability
        get_capability(operation)
        if not callable(factory):raise ValueError('能力必须提供可调用的 Harness 工厂。')
        with self._lock:
            agent=self.get(agent_id)
            if any(operation in a.operations for a in self._agents.values()):raise ValueError('能力已属于某个 Subagent。')
            self._agents[agent.id]=replace(agent,operations={**agent.operations,operation:factory})
    def get(self,agent_id):
        with self._lock:
            for agent in self._agents.values():
                if agent_id==agent.id or agent_id in agent.legacy_roles:return agent
        raise ValueError('没有注册该 Subagent：'+str(agent_id))
    def owner(self,operation):
        with self._lock:
            for agent in self._agents.values():
                if operation in agent.operations:return agent
        raise ValueError('能力尚未归属于 Subagent：'+str(operation))
    def all(self):
        with self._lock:return list(self._agents.values())

registry=SubagentRegistry()
from business_harnesses import VideoHarness, PaperHarness
registry.register(Subagent('video_learning','视频学习 Agent','理解视频内容，组织知识笔记；拥有字幕、转录、画面和总结处理工具。',{'video_summary':VideoHarness},('video-summary',),('video_summary',),'video'))
registry.register(Subagent('paper_reading','论文阅读 Agent','阅读论文材料，分析问题、贡献、方法、证据与知识；拥有论文处理工具。',{'paper_summary':PaperHarness},('academic-paper-review','paper-summary'),('paper_summary',),'paper'))
registry.register(Subagent('knowledge_archivist','知识整理 Agent','整理本地归档对话与 ChatGPT 公开分享对话，管理知识证据、个人知识库和归档文档。',{'conversation_knowledge':None},('knowledge-extraction',),('conversation_knowledge',),'archivist',False))

def canonical_role(role):
    if role in {'global','conversation'}:return role
    return registry.get(role).id
