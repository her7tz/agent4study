"""Non-secret account session metadata and recoverable material handoffs."""
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from web_research import public_url, _addresses
from storage_layout import atomic_text

class SessionError(ValueError):
    status=400

# Automated acquisition is opt-in per adapter; unknown publishers use manual import.
ADAPTERS = {
    'ieeexplore.ieee.org': {'id':'ieee','title':'IEEE Xplore','automatic':False},
    'openreview.net': {'id':'openreview','title':'OpenReview','automatic':True},
    'arxiv.org': {'id':'arxiv','title':'arXiv','automatic':True},
}


def safe_source(url):
    value=public_url(url)
    p=urlsplit(value)
    query=urlencode([(k,v) for k,v in parse_qsl(p.query) if k.lower() in {'id','document','article','doi','p','page','lang'}])
    return urlunsplit((p.scheme,p.netloc,p.path,query,''))


def adapter_for(url):
    host=(urlsplit(public_url(url)).hostname or '').lower()
    return ADAPTERS.get(host,{'id':'manual','title':host,'automatic':False})


class SiteSessions:
    def __init__(self,settings):
        self.settings=settings
        self.path=settings.data_home/'site-sessions.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():raise SessionError('会话数据库不使用符号链接。')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS site_sessions(id TEXT PRIMARY KEY,url TEXT NOT NULL,origin TEXT NOT NULL,
                adapter TEXT NOT NULL,automatic INTEGER NOT NULL,status TEXT NOT NULL,task_id TEXT,conversation_id TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,note TEXT NOT NULL DEFAULT '')''')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row;return db

    @staticmethod
    def public(row):
        value=dict(row);value['automatic']=bool(value['automatic']);return value

    def create(self,url,task_id=None,conversation_id=None):
        url=public_url(url.strip());p=urlsplit(url)
        _addresses(p.hostname,p.port or (443 if p.scheme=='https' else 80))
        # Store a clean initial page, never redirect OAuth tokens or browser cookies.
        clean=safe_source(url);adapter=adapter_for(clean)
        with self.connect() as db:
            previous=db.execute("SELECT * FROM site_sessions WHERE task_id=? AND status NOT IN ('cleared','clear_pending') ORDER BY created_at DESC LIMIT 1",(task_id,)).fetchone() if task_id else None
            if previous:return self.public(previous)
            session_id=uuid.uuid4().hex;now=time.time()
            db.execute('INSERT INTO site_sessions(id,url,origin,adapter,automatic,status,task_id,conversation_id,created_at,updated_at) VALUES(?,?,?,?,?,\'needs_login\',?,?,?,?)',
                (session_id,clean,p.scheme+'://'+p.netloc,adapter['id'],int(adapter['automatic']),task_id,conversation_id,now,now))
        return self.get(session_id)

    def get(self,session_id):
        if not isinstance(session_id,str) or not re.fullmatch(r'[a-f0-9]{32}',session_id):raise SessionError('会话编号无效。')
        with self.connect() as db:row=db.execute('SELECT * FROM site_sessions WHERE id=?',(session_id,)).fetchone()
        if not row:raise SessionError('没有找到该登录会话。')
        return self.public(row)

    def list(self):
        with self.connect() as db:return [self.public(row) for row in db.execute('SELECT * FROM site_sessions ORDER BY updated_at DESC LIMIT 100')]

    def update(self,session_id,status,note=''):
        self.get(session_id)
        if status not in {'session_saved','needs_login','material_imported','clear_pending','cleared','access_failed'}:raise SessionError('会话状态无效。')
        # The client reports state, not proof of subscription or successful authentication.
        notes={'session_saved':'浏览器会话已保存，访问权限由网站判断。','needs_login':'请在专用窗口完成登录或验证。',
            'material_imported':'论文 PDF 已导入，可继续总结。','clear_pending':'等待桌面客户端清除浏览器会话。',
            'cleared':'浏览器会话已清除。','access_failed':'未取得有效论文 PDF，请完成验证或手动上传。'}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT status FROM site_sessions WHERE id=?',(session_id,)).fetchone()[0]
            if current=='cleared':return self.public(db.execute('SELECT * FROM site_sessions WHERE id=?',(session_id,)).fetchone())
            if current in {'cleared','clear_pending'} and status not in {'clear_pending','cleared'}: raise SessionError('会话已退出，不能继续使用。')
            db.execute('UPDATE site_sessions SET status=?,note=?,updated_at=? WHERE id=?',(status,notes[status],time.time(),session_id))
        return self.get(session_id)
