"""Read visible conversation text from public ChatGPT share snapshots.

Only known message structures or explicit author-labelled HTML are accepted.
Scripts are parsed as data; no JavaScript, cookies, logins or attachments run.
"""

import hashlib
import json
import re
import socket
import time
import urllib.parse
from html.parser import HTMLParser

from runtime_control import CancellationToken
from web_research import PublicFetchError, fetch_public, public_url


MAX_SHARE_BYTES = 8 * 1024 * 1024
MAX_TEXT = 300000
MAX_MESSAGES = 2000
_SHARE_PATH = re.compile(r"^/share/([A-Za-z0-9][A-Za-z0-9_-]{7,127})/?$")
_HOSTS = {"chatgpt.com", "chat.openai.com"}


class ShareLinkError(ValueError):
    """A public shared snapshot could not be read without guessing."""


class _UnresolvedPromise:
    pass


def normalize_share_url(value):
    if not isinstance(value, str):
        raise ShareLinkError("请输入 ChatGPT 的公开聊天分享链接。")
    try:
        parsed = urllib.parse.urlsplit(public_url(value.strip()))
        match = _SHARE_PATH.fullmatch(parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.port not in {None, 443} or not match:
            raise ValueError()
    except (ValueError, TypeError):
        raise ShareLinkError("仅支持 https://chatgpt.com/share/… 或 https://chat.openai.com/share/… 公开聊天分享链接。") from None
    return "https://chatgpt.com/share/" + match.group(1)


def _source_urls(messages, source_url):
    urls = {source_url}
    for message in messages:
        for value in re.findall(r"https?://[^\s<>\"'\]\)]+", message["content"]):
            try:
                urls.add(public_url(value.rstrip(".,;，。；")))
            except ValueError:
                continue
            if len(urls) > 1000:
                raise ShareLinkError("分享中的引用链接过多，请缩小分享内容后重试。")
    return sorted(urls)


class _ShareHTML(HTMLParser):
    OMIT = {"script", "style", "noscript", "svg", "nav", "footer", "header", "button", "form"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "tr", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.scripts, self.title, self.messages = [], [], [], []
        self.active, self.script = None, None
        self.has_images = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        depth = len(self.stack)
        if tag == "script":
            self.script = {"id": attrs.get("id", ""), "type": attrs.get("type", ""), "parts": []}
        if tag not in self.VOID:
            self.stack.append(tag)
        role = attrs.get("data-message-author-role")
        if role in {"user", "assistant"} and self.active is None:
            self.active = {"role": role, "parts": [], "depth": depth, "has_text": False,
                "source_message_id": attrs.get("data-message-id", "")[:200]}
        if self.active and not any(item in self.OMIT for item in self.stack):
            if tag in self.BLOCK or tag in {"br", "hr"}:
                self.active["parts"].append("\n\n" if tag != "br" else "\n")
            if tag == "li":
                self.active["parts"].append("- ")
            if tag == "pre":
                self.active["parts"].append("```\n")
            if tag == "td" or tag == "th":
                self.active["parts"].append(" | ")
            if tag == "a":
                try:
                    self.active["parts"].append(" [" + public_url(attrs.get("href", "")) + "] ")
                except ValueError:
                    pass
            if tag == "img":
                self.has_images = True
                self.active["parts"].append("\n[分享中有图像，未读取图像内容]\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append({**self.script, "content": "".join(self.script["parts"])})
            self.script = None
        if self.active and tag == "pre":
            self.active["parts"].append("\n```\n")
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.stack = self.stack[:index]
        if self.active and len(self.stack) <= self.active["depth"]:
            content = re.sub(r"\n{3,}", "\n\n", "".join(self.active["parts"])).strip()
            if content:
                self.messages.append({"role": self.active["role"], "content": content,
                    "source_message_id": self.active["source_message_id"], "_has_text": self.active["has_text"]})
            self.active = None
        elif self.active and tag in self.BLOCK:
            self.active["parts"].append("\n\n")

    def handle_data(self, data):
        if self.script is not None:
            self.script["parts"].append(data)
            return
        if "title" in self.stack:
            self.title.append(data)
        if self.active and not any(tag in self.OMIT for tag in self.stack):
            self.active["parts"].append(data)
            self.active["has_text"] = self.active["has_text"] or bool(data.strip())


def _unflatten(pool, deferred=None, root_index=0):
    """Decode the JSON reference table used by React Router's hydration stream."""
    if len(pool) > 250000:
        raise ShareLinkError("分享页面的数据结构过大。")
    cache, visiting = {}, set()

    def reference(index, depth):
        if type(index) is not int or index < 0:
            return None if type(index) is int else index
        if index >= len(pool):
            raise ShareLinkError("分享页面的数据引用不完整，请稍后重试。")
        if index in cache:
            return cache[index]
        if index in visiting:
            return _UnresolvedPromise()
        if depth > 160:
            raise ShareLinkError("分享页面的数据层级过深。")
        visiting.add(index)
        value = pool[index]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                match = re.fullmatch(r"_(\d+)", key)
                name = reference(int(match.group(1)), depth + 1) if match else key
                if isinstance(name, str):
                    result[name] = reference(item, depth + 1)
        elif isinstance(value, list):
            # Turbo-stream marks deferred values and non-JSON types with strings.
            if value and value[0] == "P":
                promise_id = value[1] if len(value) > 1 else None
                result = deferred(promise_id) if callable(deferred) else _UnresolvedPromise()
            elif value and value[0] == "Z":
                result = reference(value[1], depth + 1) if len(value) == 2 else _UnresolvedPromise()
            else:
                result = None if value and isinstance(value[0], str) else [reference(item, depth + 1) for item in value]
        else:
            result = value
        visiting.remove(index)
        cache[index] = result
        return result

    return reference(root_index, 0)


def _json_payloads(page):
    decoder = json.JSONDecoder()
    stream_chunks = []
    for script in page.scripts:
        text = script["content"].strip()
        if script["id"] == "__NEXT_DATA__" or script["type"].split(";", 1)[0].strip() in {"application/json", "application/ld+json"}:
            try:
                value = json.loads(text)
            except (ValueError, RecursionError):
                continue
            yield value
        # Read only the JSON string argument of this known hydration call.
        # Never evaluate JavaScript or scrape arbitrary strings out of scripts.
        for match in re.finditer(r"(?:window\.)?__(?:reactRouterContext|remixContext)\.streamController\.enqueue\s*\(\s*", text):
            try:
                argument, _ = decoder.raw_decode(text, match.end())
            except (ValueError, RecursionError):
                continue
            if not isinstance(argument, str):
                continue
            stream_chunks.append(argument)
    # A JSON line can span enqueue calls. Turbo-stream appends each P/E array
    # to ONE global reference table; integer payloads reuse an existing index.
    # Decode after collecting the table, preserving append offsets and Z refs.
    pool, root_indices, deferred_roots = [], [], {}
    for line in "".join(stream_chunks).splitlines():
        line = line.strip()
        if not line:
            continue
        promise = re.match(r"^([PE])(\d+):", line)
        body = line[promise.end():] if promise else line
        if not promise and not body.startswith(("[", "{")):
            raise ShareLinkError("分享的流式数据包含尚不支持的记录，未使用部分正文。")
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            raise ShareLinkError("分享的流式数据不完整，未猜测消息引用。") from None
        if promise:
            if not root_indices:
                raise ShareLinkError("分享的流式数据缺少初始引用表。")
            if isinstance(value, list):
                if not value:
                    raise ShareLinkError("分享的流式数据包含空引用表，未猜测消息引用。")
                start_index = len(pool)
                pool.extend(value)
            elif type(value) is int:
                start_index = value
            else:
                raise ShareLinkError("分享的流式数据格式尚不支持，未猜测消息引用。")
            promise_id = int(promise.group(2))
            if promise_id in deferred_roots:
                raise ShareLinkError("分享的流式数据重复定义同一消息，无法确认正文。")
            deferred_roots[promise_id] = (promise.group(1), start_index)
        elif isinstance(value, list):
            if root_indices:
                raise ShareLinkError("分享页面包含多组流式引用表，无法确认聊天范围。")
            if not value:
                raise ShareLinkError("分享的流式数据缺少初始引用表。")
            root_indices.append(0)
            pool.extend(value)
        else:
            yield value
        if len(pool) > 250000:
            raise ShareLinkError("分享页面的数据结构过大。")
    declared_promises = {value[1] for value in pool if isinstance(value, list) and len(value) == 2
        and value[0] == "P" and type(value[1]) is int}
    if set(deferred_roots) - declared_promises:
        raise ShareLinkError("分享的流式数据引用了不存在的消息，未尝试恢复正文。")
    deferred_cache, resolving = {}, set()

    def resolve_promise(promise_id):
        if type(promise_id) is not int or promise_id not in deferred_roots or promise_id in resolving:
            return _UnresolvedPromise()
        if promise_id not in deferred_cache:
            resolving.add(promise_id)
            kind, root_index = deferred_roots[promise_id]
            deferred_cache[promise_id] = _unflatten(pool, resolve_promise, root_index) if kind == "P" else _UnresolvedPromise()
            resolving.remove(promise_id)
        return deferred_cache[promise_id]

    for root_index in root_indices:
        yield _unflatten(pool, resolve_promise, root_index)


def _message_text(message):
    author = message.get("author") or {}
    role = author.get("role") if isinstance(author, dict) else message.get("role")
    role = role or message.get("role")
    if role not in {"user", "assistant"}:
        return None, False
    metadata = message.get("metadata") or {}
    if isinstance(metadata, dict) and (metadata.get("is_visually_hidden_from_conversation") or metadata.get("is_hidden")):
        return None, False
    channel = message.get("channel") or (metadata.get("channel") if isinstance(metadata, dict) else None)
    if channel not in {None, "final"} or message.get("recipient") not in {None, "all"}:
        return None, False
    content = message.get("content")
    if isinstance(content, dict) and content.get("content_type") in {"model_editable_context", "user_editable_context"}:
        return None, False
    parts = content.get("parts") if isinstance(content, dict) else [content] if isinstance(content, str) else None
    if parts is None and isinstance(content, dict) and isinstance(content.get("text"), str):
        parts = [content["text"]]
    if not isinstance(parts, list):
        return None, bool(content)
    text, attachments = [], False
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("content_type", part.get("type", "text")) in {"text", "input_text", "output_text"}:
            text.append(part["text"])
        else:
            attachments = True
    if isinstance(metadata, dict) and (metadata.get("attachments") or metadata.get("citations")):
        # Citations are not fetched; only URLs actually present in visible text count.
        attachments = attachments or bool(metadata.get("attachments"))
    body = "\n\n".join(text).strip()
    has_text = bool(body)
    if attachments:
        body += "\n\n[此消息含非文字内容或附件，未读取其内容]"
    if not body.strip():
        return None, attachments
    return {"role": role, "content": body.strip(), "source_message_id": str(message.get("id") or "")[:200],
        "_has_text": has_text}, attachments


def _conversation_messages(value):
    mapping = value.get("mapping")
    for key in ("linear_conversation", "messages"):
        raw = value.get(key)
        if isinstance(raw, list) and raw:
            messages = []
            for item in raw:
                if isinstance(mapping, dict):
                    node_id = item.get("id") if isinstance(item, dict) else item if isinstance(item, str) else None
                    if node_id in mapping:
                        item = mapping[node_id]
                    elif isinstance(item, str) or (isinstance(item, dict) and "message" not in item and "content" not in item):
                        raise ShareLinkError("分享的消息顺序引用不完整，未省略缺失消息。")
                messages.append(item.get("message", item) if isinstance(item, dict) else None)
            if all(item is None or isinstance(item, dict) for item in messages) and any(isinstance(item, dict) and ("author" in item or "role" in item) and "content" in item for item in messages):
                return messages
    if isinstance(mapping, dict) and mapping:
        if len(mapping) > MAX_MESSAGES * 4:
            raise ShareLinkError("分享中的消息数量过多，请缩小分享范围。")
        current = value.get("current_node")
        if current is not None and current not in mapping:
            raise ShareLinkError("分享的当前对话分支不存在，未尝试拼接其他分支。")
        if current not in mapping:
            leaves = [key for key, node in mapping.items() if isinstance(node, dict) and not node.get("children")]
            if len(leaves) != 1:
                return None
            current = leaves[0]
        chain, seen = [], set()
        while current is not None:
            if current in seen or current not in mapping:
                raise ShareLinkError("分享的消息关系不完整，未尝试拼接不同对话分支。")
            seen.add(current)
            node = mapping[current]
            if not isinstance(node, dict):
                return None
            chain.append(node.get("message"))
            current = node.get("parent")
        return list(reversed(chain))
    return None


def _structured_conversations(payload):
    stack, visited, count = [payload], set(), 0
    while stack:
        value = stack.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        count += 1
        if count > 300000:
            raise ShareLinkError("分享页面的数据结构过大。")
        if isinstance(value, dict):
            if any(key in value for key in ("linear_conversation", "mapping", "messages")):
                pending, checked = [value], set()
                while pending:
                    item = pending.pop()
                    if isinstance(item, _UnresolvedPromise):
                        raise ShareLinkError("分享的聊天正文尚有未完成的数据，未使用部分消息生成知识。")
                    if id(item) in checked:
                        continue
                    checked.add(id(item))
                    if isinstance(item, dict):
                        pending.extend(item.values())
                    elif isinstance(item, list):
                        pending.extend(item)
            messages = _conversation_messages(value)
            if messages:
                yield value, messages
                continue
            stack.extend(item for item in value.values() if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            stack.extend(item for item in value if isinstance(item, (dict, list)))


def read_shared_conversation(url, cancellation=None):
    source_url = normalize_share_url(url)
    token = cancellation or CancellationToken()

    def redirect_validator(target):
        if normalize_share_url(target) != source_url:
            raise ShareLinkError("分享链接跳转到了其他页面，未读取登录页或其他网址。")
        # Preserve the original supported host for legitimate legacy-host redirects.
        parsed = urllib.parse.urlsplit(target)
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))

    try:
        final_url, html, mime = fetch_public(source_url, cancellation=token, timeout=25,
            max_bytes=MAX_SHARE_BYTES, redirect_validator=redirect_validator,
            proxy_hosts=_HOSTS)
    except PublicFetchError as exc:
        raise ShareLinkError("未取得分享正文（HTTP {}）。分享可能已删除、需要登录或限制自动读取；可以改为粘贴分享的文字。".format(exc.http_status)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ShareLinkError("连接 ChatGPT 分享页面超时，尚未取得聊天正文；请检查本机网络或代理后重试。") from exc
    except OSError as exc:
        raise ShareLinkError("无法连接 ChatGPT 分享页面，尚未取得聊天正文；请检查本机网络、代理或 TLS 连接后重试。") from exc
    if "html" not in mime:
        raise ShareLinkError("分享链接未返回可识别的聊天网页。")
    token.check()
    page = _ShareHTML()
    try:
        page.feed(html)
        page.close()
    except (ValueError, RecursionError):
        raise ShareLinkError("分享页面格式无法解析，未提取网页脚本或不完整正文。") from None
    candidates = []
    for payload in _json_payloads(page):
        token.check()
        for conversation, raw_messages in _structured_conversations(payload):
            messages, has_attachments = [], False
            for raw in raw_messages:
                if not isinstance(raw, dict):
                    continue
                message, attachments = _message_text(raw)
                has_attachments = has_attachments or attachments
                if message:
                    messages.append(message)
            if messages:
                title = conversation.get("title")
                candidates.append((messages, title if isinstance(title, str) else "", has_attachments, "structured"))
    if candidates:
        unique = {}
        for candidate in candidates:
            fingerprint = hashlib.sha256(json.dumps(candidate[0], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            unique.setdefault(fingerprint, candidate)
        if len(unique) != 1:
            raise ShareLinkError("分享页面存在多组不一致的聊天正文，无法确认分享范围；未选择较长内容或合并其他分支。")
        messages, title, has_attachments, method = next(iter(unique.values()))
    elif page.messages:
        messages, title, has_attachments, method = page.messages, "", page.has_images, "author_html"
    else:
        raise ShareLinkError("未取得可识别的分享聊天正文。页面可能需要登录、验证，或分享已失效；不会用网页导航和脚本代替聊天内容。可改为粘贴聊天文字。")
    if len(messages) > MAX_MESSAGES or sum(len(message["content"]) for message in messages) > MAX_TEXT:
        raise ShareLinkError("分享内容超过 2000 条消息或 30 万字上限，请缩小分享范围后重试；尚未写入知识库。")
    if not any(message["role"] == "assistant" and message["_has_text"] for message in messages):
        raise ShareLinkError("分享中没有可读取的模型文字回复，请检查分享范围。")
    messages = [{"id": index, **{key: value for key, value in message.items() if key != "_has_text"}}
        for index, message in enumerate(messages, 1)]
    title = (title or "".join(page.title)).strip()
    title = re.sub(r"^(?:ChatGPT\s*[-–—·]\s*)|(?:\s*[-–—·]\s*ChatGPT)$", "", title).strip()[:200] or "ChatGPT 分享知识整理"
    limitations = ["只整理该链接公开分享范围内可读取的用户与模型文字，不代表完整原始聊天；未独立核验模型说法。", "图像、音频及附件未下载或识别。"]
    token.check()
    return {"title": title, "messages": messages, "source_type": "chatgpt_share", "source_url": source_url,
        "source_title": title, "source_urls": _source_urls(messages, source_url),
        "source_details": {"reader": method, "message_count": len(messages), "has_attachments": has_attachments,
            "limitations": limitations, "retrieved_at": time.time()}}
