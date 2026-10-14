"""Registered local application adapters, independent of Agent and UI logic.

Applications receive only documents and directories resolved by ArtifactStore.
Adapters declare identities and supported actions; platform implementations own
application discovery and launching. A submitted open request never confirms
that the application has displayed, read, modified, or exported a document.
"""

import plistlib
import re
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


class LocalAppError(Exception):
    def __init__(self, status: int, message: str, code: str = "local_app_error",
                 may_have_submitted: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.may_have_submitted = may_have_submitted


@dataclass(frozen=True)
class AppAdapter:
    """Trusted registration data; no executable command or user path fields."""

    id: str
    title: str
    description: str
    bundle_ids: Tuple[str, ...]
    bundle_names: Tuple[str, ...]
    document_extensions: Tuple[str, ...]
    actions: Tuple[str, ...] = ("open_document",)
    platforms: Tuple[str, ...] = ("darwin",)

    def __post_init__(self):
        for name in ("bundle_ids", "bundle_names", "document_extensions", "actions", "platforms"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", self.id):
            raise ValueError("本地应用编号无效。")
        if not self.title or not self.bundle_ids or not self.bundle_names:
            raise ValueError("应用适配器必须声明名称及安装包身份。")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value)
               for value in self.bundle_ids):
            raise ValueError("应用 bundle ID 无效。")
        if any(not isinstance(value, str) or not value.endswith(".app") or
               Path(value).name != value or "/" in value or "\\" in value or value.startswith(".")
               for value in self.bundle_names):
            raise ValueError("安装包名称必须是普通 .app 文件名。")
        if any(not isinstance(value, str) or not re.fullmatch(r"\.[a-z0-9]{1,20}", value)
               for value in self.document_extensions):
            raise ValueError("应用文档后缀必须是小写且以点开头。")
        if not self.actions or not set(self.actions).issubset({"open_document", "open_directory"}):
            raise ValueError("应用包含未实现的本地操作。")
        if "open_document" in self.actions and not self.document_extensions:
            raise ValueError("文档打开操作必须声明支持的文件后缀。")

    def metadata(self) -> Dict[str, Any]:
        """Safe registration metadata for callers and future tool catalogs."""
        return {"id": self.id, "title": self.title, "description": self.description,
                "supported_actions": list(self.actions),
                "supported_extensions": list(self.document_extensions),
                "platforms": list(self.platforms)}


class AppRegistry:
    def __init__(self):
        self._adapters: Dict[str, AppAdapter] = {}
        self._lock = threading.RLock()

    def register(self, adapter: AppAdapter) -> None:
        if not isinstance(adapter, AppAdapter):
            raise TypeError("必须注册 AppAdapter。")
        with self._lock:
            if adapter.id in self._adapters:
                raise ValueError("本地应用已注册：" + adapter.id)
            self._adapters[adapter.id] = adapter

    def get(self, app_id: str) -> AppAdapter:
        if not isinstance(app_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", app_id):
            raise LocalAppError(400, "请提供已注册的本地应用编号。", "invalid_app_id")
        with self._lock:
            adapter = self._adapters.get(app_id)
        if adapter is None:
            raise LocalAppError(404, "没有注册该本地应用。", "app_not_registered")
        return adapter

    def all(self) -> List[AppAdapter]:
        with self._lock:
            return list(self._adapters.values())


@dataclass(frozen=True)
class AppInstallation:
    """Platform-private paths are deliberately absent from public metadata."""

    path: Path
    bundle_id: str
    version: str
    document_extensions: Tuple[str, ...]
    opens_directories: bool


class AppPlatform(Protocol):
    name: str

    def discover(self, adapter: AppAdapter) -> Optional[AppInstallation]:
        ...

    def launch(self, installation: AppInstallation, target: Path) -> None:
        ...


class MacOSAppPlatform:
    """Discover fixed app bundles and invoke the system open command safely."""

    name = "darwin"

    def __init__(self, roots: Optional[Tuple[Path, ...]] = None,
                 runner: Optional[Callable] = None, timeout: float = 12):
        self._roots = tuple(Path(path) for path in roots) if roots is not None else (
            Path("/Applications"), Path.home() / "Applications")
        self._runner = runner or subprocess.run
        if timeout <= 0 or timeout > 60:
            raise ValueError("应用启动超时必须大于 0 且不超过 60 秒。")
        self._timeout = timeout

    @staticmethod
    def _regular_plist(bundle: Path) -> Optional[Dict[str, Any]]:
        info = bundle / "Contents" / "Info.plist"
        try:
            if any(path.is_symlink() for path in (bundle, bundle / "Contents", info)):
                return None
            if not bundle.is_dir():
                return None
            details = info.stat()
            if not stat.S_ISREG(details.st_mode) or details.st_size > 1024 * 1024:
                return None
            value = plistlib.loads(info.read_bytes())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            return None

    def discover(self, adapter: AppAdapter) -> Optional[AppInstallation]:
        for root in self._roots:
            if root.is_symlink():
                continue
            for name in adapter.bundle_names:
                bundle = root / name
                info = self._regular_plist(bundle)
                if info is None or info.get("CFBundleIdentifier") not in adapter.bundle_ids:
                    continue
                extensions = set()
                opens_directories = False
                document_types = info.get("CFBundleDocumentTypes", [])
                if not isinstance(document_types, list):
                    document_types = []
                for document in document_types:
                    if not isinstance(document, dict) or document.get("CFBundleTypeRole") not in {"Editor", "Viewer"}:
                        continue
                    declared = document.get("CFBundleTypeExtensions", [])
                    if isinstance(declared, list):
                        extensions.update("." + suffix.lower() for suffix in declared
                                          if isinstance(suffix, str) and re.fullmatch(r"[A-Za-z0-9]{1,20}", suffix))
                    content_types = document.get("LSItemContentTypes", [])
                    if isinstance(content_types, list) and "public.folder" in content_types:
                        opens_directories = True
                version = info.get("CFBundleShortVersionString", "")
                version = version.strip()[:80] if isinstance(version, str) else ""
                return AppInstallation(bundle.absolute(), info["CFBundleIdentifier"], version,
                                       tuple(sorted(extensions)), opens_directories)
        return None

    def launch(self, installation: AppInstallation, target: Path) -> None:
        # Both paths are absolute and originate from trusted discovery or the
        # public ArtifactStore resolver, never from a request parameter.
        current_info = self._regular_plist(installation.path)
        if current_info is None or current_info.get("CFBundleIdentifier") != installation.bundle_id:
            raise LocalAppError(503, "应用安装包已变更，请重新检查应用状态。", "app_installation_changed")
        command = ["/usr/bin/open", "-a", str(installation.path), str(target)]
        try:
            result = self._runner(command, shell=False, check=False, timeout=self._timeout,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise LocalAppError(504, "等待应用启动请求超时；请求可能已经提交，请先查看应用再决定是否重试。",
                                "launch_timeout", may_have_submitted=True) from exc
        except OSError as exc:
            raise LocalAppError(503, "无法调用系统应用启动服务，请检查本机运行环境。", "launch_unavailable") from exc
        if result.returncode != 0:
            raise LocalAppError(502, "系统没有接受应用打开请求，请检查应用是否可以正常启动。", "launch_rejected")


class UnsupportedAppPlatform:
    def __init__(self, name: str):
        self.name = name

    def discover(self, adapter: AppAdapter) -> Optional[AppInstallation]:
        return None

    def launch(self, installation: AppInstallation, target: Path) -> None:
        raise LocalAppError(501, "当前系统尚未接入本地应用启动适配。", "unsupported_platform")


registry = AppRegistry()
registry.register(AppAdapter(
    id="typora", title="Typora", description="使用本机 Typora 打开内容库文档或内容目录。",
    bundle_ids=("abnerworks.Typora",), bundle_names=("Typora.app",),
    document_extensions=(".md", ".markdown", ".mmd", ".mdown", ".mkd", ".mdwn", ".mdtxt",
                         ".mdtext", ".rmarkdown", ".rmd", ".apib", ".qmd", ".mdx", ".txt", ".text"),
    actions=("open_document", "open_directory"),
))
DEFAULT_REGISTRY = registry


class LocalAppService:
    def __init__(self, artifacts: Any, registry: Optional[AppRegistry] = None,
                 platform: Optional[AppPlatform] = None, logger: Any = None):
        self._artifacts = artifacts
        self._registry = registry if registry is not None else DEFAULT_REGISTRY
        self._platform = platform if platform is not None else (
            MacOSAppPlatform() if sys.platform == "darwin" else UnsupportedAppPlatform(sys.platform))
        self._logger = logger

    def _inspect(self, adapter: AppAdapter) -> Tuple[Dict[str, Any], Optional[AppInstallation]]:
        result = {**adapter.metadata(), "platform": self._platform.name, "installed": False,
                  "available": False, "version": "", "available_actions": [], "available_extensions": []}
        if self._platform.name not in adapter.platforms or isinstance(self._platform, UnsupportedAppPlatform):
            return ({**result, "state": "unsupported_platform", "message": "当前系统尚不支持该应用适配。"}, None)
        installation = self._platform.discover(adapter)
        if installation is None:
            return ({**result, "state": "not_installed",
                     "message": "未在受支持的应用目录找到有效安装包，请先安装该应用。"}, None)
        extensions = sorted(set(adapter.document_extensions).intersection(installation.document_extensions))
        actions = [action for action in adapter.actions if
                   (action == "open_document" and bool(extensions)) or
                   (action == "open_directory" and installation.opens_directories)]
        result.update(installed=True, version=installation.version,
                      available_actions=actions, available_extensions=extensions)
        if not actions:
            return ({**result, "state": "unsupported_actions", "message": "当前安装包未声明适配器所需的文档或目录类型。"}, installation)
        return ({**result, "available": True, "state": "ready", "message": "应用已安装，可提交打开请求。"}, installation)

    def list(self) -> List[Dict[str, Any]]:
        return [self._inspect(adapter)[0] for adapter in self._registry.all()]

    def status(self, app_id: str) -> Dict[str, Any]:
        return self._inspect(self._registry.get(app_id))[0]

    def _ready(self, app_id: str, action: str) -> Tuple[AppAdapter, AppInstallation, Dict[str, Any]]:
        adapter = self._registry.get(app_id)
        details, installation = self._inspect(adapter)
        if details["state"] == "unsupported_platform":
            raise LocalAppError(501, details["message"], "unsupported_platform")
        if installation is None:
            raise LocalAppError(503, details["message"], "app_not_installed")
        if action not in details["available_actions"]:
            raise LocalAppError(409, "该应用的当前适配不支持此操作。", "unsupported_action")
        return adapter, installation, details

    def _record(self, app_id: str, action: str, identifier: str, outcome: str) -> None:
        if self._logger is not None:
            self._logger.info("本地应用请求 app_id=%s action=%s reference=%s outcome=%s",
                              app_id, action, identifier, outcome)

    def _submit(self, app_id: str, action: str, installation: AppInstallation,
                target: Path) -> Dict[str, Any]:
        target = Path(target)
        try:
            details = target.lstat()
            # The resolver supplies a canonical root. A new link anywhere in
            # its child path must not turn an accepted ID into an outside path.
            canonical = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise LocalAppError(409, "内容位置已变更，请重新查询内容库后再打开。", "target_changed") from exc
        if (not target.is_absolute() or stat.S_ISLNK(details.st_mode) or canonical != target or
                (action == "open_document" and not stat.S_ISREG(details.st_mode)) or
                (action == "open_directory" and not stat.S_ISDIR(details.st_mode))):
            raise LocalAppError(403, "应用只能打开内容库解析的普通文档或目录。", "invalid_target")
        self._platform.launch(installation, target)
        return {"app_id": app_id, "action": action, "status": "submitted",
                "message": "打开请求已提交给系统；这不表示应用已经完成阅读、编辑或导出。"}

    def open_document(self, app_id: str, artifact_id: str) -> Dict[str, Any]:
        safe_app_id, safe_reference = "-", "-"
        try:
            safe_app_id = self._registry.get(app_id).id
            _, installation, details = self._ready(safe_app_id, "open_document")
            resolved = self._artifacts.resolve_document(artifact_id)
            safe_reference = resolved["artifact"]["id"]
            target = Path(resolved["path"])
            if target.suffix.lower() not in details["available_extensions"]:
                raise LocalAppError(415, "该应用不支持这份文档的文件类型。", "unsupported_document_type")
            if not target.is_file():
                raise LocalAppError(404, "文档已移除，请重新查询内容库。", "document_not_found")
            result = self._submit(safe_app_id, "open_document", installation, target)
        except LocalAppError as exc:
            self._record(safe_app_id, "open_document", safe_reference, exc.code)
            raise
        except Exception:
            self._record(safe_app_id, "open_document", safe_reference, "request_failed")
            raise
        self._record(safe_app_id, "open_document", safe_reference, "submitted")
        return {**result, "artifact_id": safe_reference}

    def open_directory(self, app_id: str, category: str = "") -> Dict[str, Any]:
        safe_app_id, safe_reference = "-", "-"
        try:
            safe_app_id = self._registry.get(app_id).id
            _, installation, _ = self._ready(safe_app_id, "open_directory")
            target = Path(self._artifacts.resolve_directory(category))
            safe_reference = category or "library"
            if not target.is_dir():
                raise LocalAppError(404, "没有找到该内容目录。", "directory_not_found")
            result = self._submit(safe_app_id, "open_directory", installation, target)
        except LocalAppError as exc:
            self._record(safe_app_id, "open_directory", safe_reference, exc.code)
            raise
        except Exception:
            self._record(safe_app_id, "open_directory", safe_reference, "request_failed")
            raise
        self._record(safe_app_id, "open_directory", safe_reference, "submitted")
        return {**result, "category": category}
