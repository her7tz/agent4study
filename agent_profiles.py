"""Private per-agent overrides and immutable execution snapshots."""
import copy
import json
import re
import threading
from pathlib import Path
from harness import SKILLS_DIR, DeepSeekClient
from storage_layout import atomic_text

LOCK = threading.RLock()
PROVIDERS = {'deepseek': DeepSeekClient}

class ProfileError(ValueError):
    status = 400


def roles():
    from capabilities import list_capabilities
    return {'global': '主 Agent / 默认配置', **{c['id']: c['title'] for c in list_capabilities()}, 'conversation_knowledge': '知识归档'}


def available_skills():
    return sorted(p.parent.name for p in SKILLS_DIR.glob('*/SKILL.md'))


def defaults(role):
    return json.loads((SKILLS_DIR/'config.json').read_text(encoding='utf-8'))['capabilities'].get(role, [])


def read_skill(settings, name, role="global"):
    if name not in available_skills():
        raise ProfileError('没有找到该 Skill。')
    role = "global" if role == "conversation" else role
    if role not in roles(): raise ProfileError("没有注册该 Agent。")
    override = settings.data_home/"skill-overrides"/role/(name+".md")
    global_override = settings.data_home/"skill-overrides"/"global"/(name+".md")
    path = override if override.exists() else global_override if global_override.exists() else SKILLS_DIR/name/"SKILL.md"
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ProfileError('Skill 文件不使用符号链接。')
    return path.read_text(encoding='utf-8')


class AgentProfiles:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_home/'agent-profiles.json'

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ProfileError('配置文件不使用符号链接。')
        return json.loads(self.path.read_text(encoding='utf-8'))

    def secrets(self):
        with LOCK:
            return [self.settings.api_key] + [v['api_key'] for v in self._read().values() if v.get('api_key')]

    def resolve(self, role):
        if getattr(self.settings, '_profile_role', None) == role:
            return self.settings
        if role not in roles() and role != 'conversation':
            raise ProfileError('没有注册这个 Agent。')
        with LOCK:
            profiles = self._read()
            profile = profiles.get(role, {}) if role != 'conversation' else {}
            global_profile = profiles.get('global', {})
            result = copy.copy(self.settings)
            result.provider = profile.get('provider', 'deepseek')
            result.api_key = profile.get('api_key', self.settings.api_key)
            result.model = profile.get('model', self.settings.model)
            names = list(profile.get('skills', global_profile.get('skills', defaults(role))))
            result.skill_names = tuple(names)
            result.skill_instructions = '\n\n'.join('<skill name="{}">\n{}\n</skill>'.format(name, read_skill(self.settings, name, role)) for name in names)
            result._profile_role = role
            result._profile_secrets = tuple(self.secrets()) + (result.api_key,)
            result._profile_public = self.public(role, profiles)
            return result

    def public(self, role, profiles=None):
        profiles = self._read() if profiles is None else profiles
        profile = profiles.get(role, {}) if role != 'global' else profiles.get('global', {})
        names = profile.get('skills', profiles.get('global', {}).get('skills', defaults(role)))
        return {'role': role, 'title': roles().get(role, role),
            'provider': profile.get('provider', 'deepseek'), 'model': profile.get('model', self.settings.model),
            'api_key_configured': bool(profile.get('api_key', self.settings.api_key)), 'skills': names,
            'sources': {key: ('independent' if key in profile and role != 'global' else 'default') for key in ('provider','model','api_key','skills')},
            'skill_source': 'independent' if 'skills' in profile else 'global' if 'skills' in profiles.get('global', {}) else 'business_default'}

    def list(self):
        with LOCK:
            data = self._read()
            return {'agents': [self.public(role, data) for role in roles()], 'available_skills': available_skills(), 'providers': list(PROVIDERS)}

    def update(self, role, body):
        if role not in roles() or not isinstance(body, dict) or set(body)-{'provider','model','api_key','skills','inherit'}:
            raise ProfileError('Agent 配置字段无效。')
        inherit = body.get('inherit', [])
        if not isinstance(inherit, list) or any(key not in {'provider','model','api_key','skills'} for key in inherit):
            raise ProfileError('继承字段无效。')
        with LOCK:
            data = self._read(); profile = dict(data.get(role, {}))
            for key in inherit: profile.pop(key, None)
            for key in ('provider','model','api_key','skills'):
                if key not in body: continue
                value = body[key]
                if key == 'api_key' and value == '': continue
                if key == 'provider' and value not in PROVIDERS: raise ProfileError('尚未适配该服务商。')
                if key == 'model' and (not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,99}', value)): raise ProfileError('模型名称格式无效。')
                if key == 'api_key' and (not isinstance(value,str) or len(value)>4096 or any(ord(c)<33 or c in '\"\'' for c in value)): raise ProfileError('API 凭证格式无效。')
                if key == 'skills' and (not isinstance(value,list) or len(value)>20 or any(name not in available_skills() for name in value)): raise ProfileError('Skills 列表无效。')
                if role == 'global' and key in {'model','api_key','provider'}: continue
                profile[key] = list(dict.fromkeys(value)) if key == 'skills' else value
            data[role] = profile
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(self.path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            return self.public(role,data)

    def write_skill(self, name, content, role="global"):
        if role not in roles(): raise ProfileError("没有注册该 Agent。")
        if name not in available_skills() or not isinstance(content,str) or not content.strip() or len(content)>30000:
            raise ProfileError('Skill 需要有效名称和 1 至 30000 字的规则。')
        if any(secret and secret in content for secret in self.secrets()):
            raise ProfileError('请勿将 API 凭证写入 Skill。')
        directory = self.settings.data_home/'skill-overrides'/role
        if directory.is_symlink() or directory.parent.is_symlink():raise ProfileError('规则目录不使用符号链接。')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic_text(directory/(name+'.md'),content.strip()+'\n')
        return {'saved':True,'name':name}


def resolve_agent(settings, role):
    return AgentProfiles(settings).resolve(role)


def create_client(settings, timeout=180):
    return PROVIDERS[getattr(settings, 'provider', 'deepseek')](settings, timeout=timeout)


def redact(settings, value):
    text = str(value)
    for secret in getattr(settings,'_profile_secrets',(settings.api_key,)):
        if secret: text = text.replace(secret,'[已隐藏凭证]')
    return text
