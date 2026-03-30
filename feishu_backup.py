import argparse
import hashlib
import httpx
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from lark_oapi.api.wiki.v2 import ListSpaceRequest
from pydantic import BaseModel, Field, ValidationError

try:
    from feishu_docx import FeishuExporter
    from feishu_docx.auth import OAuth2Authenticator, TenantAuthenticator
except ModuleNotFoundError:
    FeishuExporter = None
    OAuth2Authenticator = None
    TenantAuthenticator = None

try:
    from pypinyin import lazy_pinyin
except ModuleNotFoundError:
    lazy_pinyin = None

DEFAULT_BASE_URL = "https://feishu.cn"
VERSION = "0.1.0"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 180.0
DEFAULT_FEISHU_AUTH_MODE = "oauth"
DEFAULT_OAUTH_REDIRECT_PORT = 9527
DEFAULT_LAUNCHD_LABEL = "com.longbiao.feishu-backup"
DEFAULT_SCHEDULE_HOUR = 2
DEFAULT_SCHEDULE_MINUTE = 15
DEFAULT_WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
SUPPORTED_NODE_TYPES = {"doc", "docx", "sheet", "bitable", "wiki"}
DEFAULT_CATEGORY_EXCLUDES = {
    "CamIn Files",
    "TencentMeeting",
    "_duplicates-2",
}
STRICT_FOLDER_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+$")
DEFAULT_SECOND_SLUG_TOKEN = "docs"
COMMON_CHINESE_PREFIXES = (
    "厦门大学",
    "厦门",
    "漳州",
    "龙文",
)
COMMON_CHINESE_SUFFIXES = (
    "的知识库",
    "知识库",
    "有限责任公司",
    "科技有限公司",
    "技术有限公司",
    "有限公司",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_segment(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value).strip(". ")
    return cleaned or "untitled"


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "untitled"


def is_strict_two_word_slug(value: str) -> bool:
    return bool(STRICT_FOLDER_RE.fullmatch(value))


def ascii_slug_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in re.split(r"[^a-z0-9]+", ascii_text.lower()) if token]
    if tokens:
        return tokens
    if lazy_pinyin is not None:
        simplified = value.strip()
        for prefix in COMMON_CHINESE_PREFIXES:
            if simplified.startswith(prefix):
                simplified = simplified[len(prefix):]
                break
        for suffix in COMMON_CHINESE_SUFFIXES:
            if simplified.endswith(suffix):
                simplified = simplified[: -len(suffix)]
                break
        cjk_chars = [char for char in simplified if "\u4e00" <= char <= "\u9fff"]
        if cjk_chars:
            pinyin_tokens = [token.lower() for token in lazy_pinyin("".join(cjk_chars[:2])) if token and token.strip()]
            if pinyin_tokens:
                return ["".join(pinyin_tokens)]
    return []


def strict_two_word_slug(value: str, secondary_fallback: str = DEFAULT_SECOND_SLUG_TOKEN) -> str:
    tokens = ascii_slug_tokens(value)
    if not tokens:
        tokens = ["backup"]
    if len(tokens) == 1:
        tokens.append(secondary_fallback)
    return f"{tokens[0]}-{tokens[1]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_unique_name(name: str, seen: set[str], token: str) -> str:
    candidate = name
    if candidate not in seen:
        seen.add(candidate)
        return candidate

    suffix = sanitize_segment(token)[:8] or "node"
    candidate = f"{name}__{suffix}"
    while candidate in seen:
        suffix = f"{suffix}x"
        candidate = f"{name}__{suffix}"
    seen.add(candidate)
    return candidate


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_setting(key: str, env_file_values: dict[str, str], default: str | None = None) -> str | None:
    return os.getenv(key) or env_file_values.get(key) or default


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def relative_asset_target(markdown_path: Path, asset_path: Path) -> str:
    return os.path.relpath(asset_path, start=markdown_path.parent).replace(os.sep, "/")


def rewrite_local_asset_links(markdown: str, original_folder: str, target_folder: str) -> str:
    patterns = [
        (f"]({original_folder}/", f"]({target_folder}/"),
        (f"](./{original_folder}/", f"]({target_folder}/"),
        (f"]({original_folder}%2F", f"]({target_folder}%2F"),
        (f"({original_folder}/", f"({target_folder}/"),
        (f"(./{original_folder}/", f"({target_folder}/"),
    ]
    result = markdown
    for old, new in patterns:
        result = result.replace(old, new)
    return result


def compact_markdown_excerpt(markdown_text: str, limit: int = 2500) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", markdown_text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"`{1,3}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def prune_empty_parents(path: Path, stop_at: Path) -> None:
    current = path
    while current != stop_at and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@dataclass
class BackupConfig:
    app_id: str
    app_secret: str
    documents_root: Path
    state_root: Path
    base_url: str = DEFAULT_BASE_URL
    feishu_auth_mode: str = DEFAULT_FEISHU_AUTH_MODE
    oauth_redirect_port: int = DEFAULT_OAUTH_REDIRECT_PORT
    max_depth: int = 20
    retries: int = 2
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS
    space_ids: tuple[str, ...] = ()
    space_name_allowlist: tuple[str, ...] = ()
    launchd_label: str = DEFAULT_LAUNCHD_LABEL
    launchd_hour: int = DEFAULT_SCHEDULE_HOUR
    launchd_minute: int = DEFAULT_SCHEDULE_MINUTE
    web_base_url: str | None = None
    web_session_cookie: str | None = None
    web_csrf_token: str | None = None

    @property
    def env_file_path(self) -> Path:
        return self.state_root / "env"

    @property
    def feishu_docx_cache_dir(self) -> Path:
        return self.state_root / "feishu-docx-auth"

    @property
    def launch_agent_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.launchd_label}.plist"

    @classmethod
    def default_state_root(cls) -> Path:
        return Path.home() / "Library" / "Application Support" / "feishu-backup"

    @classmethod
    def from_env(
        cls,
        *,
        require_app_credentials: bool = True,
    ) -> "BackupConfig":
        state_root = Path(
            get_setting("STATE_ROOT", {}, str(cls.default_state_root())) or ""
        ).expanduser()
        env_file_values = load_env_file(state_root / "env")

        app_id = get_setting("APP_ID", env_file_values)
        app_secret = get_setting("APP_SECRET", env_file_values)
        if require_app_credentials and (not app_id or not app_secret):
            raise SystemExit("Missing APP_ID or APP_SECRET. Set them in env or in the state env file.")

        documents_root = Path(
            get_setting("DOCUMENTS_ROOT", env_file_values, str(Path.home() / "Documents")) or ""
        ).expanduser()

        return cls(
            app_id=app_id or "",
            app_secret=app_secret or "",
            documents_root=documents_root,
            state_root=state_root,
            base_url=(get_setting("FEISHU_BASE_URL", env_file_values, DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
            feishu_auth_mode=(
                get_setting("FEISHU_AUTH_MODE", env_file_values, DEFAULT_FEISHU_AUTH_MODE)
                or DEFAULT_FEISHU_AUTH_MODE
            ).strip().lower(),
            oauth_redirect_port=int(
                get_setting("FEISHU_OAUTH_REDIRECT_PORT", env_file_values, str(DEFAULT_OAUTH_REDIRECT_PORT))
                or DEFAULT_OAUTH_REDIRECT_PORT
            ),
            max_depth=int(get_setting("MAX_DEPTH", env_file_values, "20") or "20"),
            retries=int(get_setting("RETRY_COUNT", env_file_values, "2") or "2"),
            ollama_base_url=(
                get_setting("OLLAMA_BASE_URL", env_file_values, DEFAULT_OLLAMA_BASE_URL) or DEFAULT_OLLAMA_BASE_URL
            ).rstrip("/"),
            ollama_model=get_setting("OLLAMA_MODEL", env_file_values, DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL,
            ollama_timeout_seconds=float(
                get_setting(
                    "OLLAMA_TIMEOUT_SECONDS",
                    env_file_values,
                    str(DEFAULT_OLLAMA_TIMEOUT_SECONDS),
                )
                or DEFAULT_OLLAMA_TIMEOUT_SECONDS
            ),
            space_ids=tuple(parse_csv(get_setting("SPACE_IDS", env_file_values))),
            space_name_allowlist=tuple(parse_csv(get_setting("SPACE_NAME_ALLOWLIST", env_file_values))),
            launchd_label=get_setting("LAUNCHD_LABEL", env_file_values, DEFAULT_LAUNCHD_LABEL) or DEFAULT_LAUNCHD_LABEL,
            launchd_hour=int(get_setting("LAUNCHD_HOUR", env_file_values, str(DEFAULT_SCHEDULE_HOUR)) or DEFAULT_SCHEDULE_HOUR),
            launchd_minute=int(get_setting("LAUNCHD_MINUTE", env_file_values, str(DEFAULT_SCHEDULE_MINUTE)) or DEFAULT_SCHEDULE_MINUTE),
            web_base_url=(get_setting("FEISHU_WEB_BASE_URL", env_file_values) or "").rstrip("/") or None,
            web_session_cookie=get_setting("FEISHU_WEB_SESSION_COOKIE", env_file_values),
            web_csrf_token=get_setting("FEISHU_WEB_CSRF_TOKEN", env_file_values),
        )

    def env_file_payload(self) -> str:
        values = {
            "APP_ID": self.app_id,
            "APP_SECRET": self.app_secret,
            "FEISHU_AUTH_MODE": self.feishu_auth_mode,
            "FEISHU_OAUTH_REDIRECT_PORT": str(self.oauth_redirect_port),
            "DOCUMENTS_ROOT": str(self.documents_root),
            "STATE_ROOT": str(self.state_root),
            "FEISHU_BASE_URL": self.base_url,
            "MAX_DEPTH": str(self.max_depth),
            "RETRY_COUNT": str(self.retries),
            "SPACE_IDS": ",".join(self.space_ids),
            "SPACE_NAME_ALLOWLIST": ",".join(self.space_name_allowlist),
            "LAUNCHD_LABEL": self.launchd_label,
            "LAUNCHD_HOUR": str(self.launchd_hour),
            "LAUNCHD_MINUTE": str(self.launchd_minute),
            "FEISHU_WEB_BASE_URL": self.web_base_url or "",
            "FEISHU_WEB_SESSION_COOKIE": self.web_session_cookie or "",
            "FEISHU_WEB_CSRF_TOKEN": self.web_csrf_token or "",
        }
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def render_backup_report(result: dict[str, Any]) -> str:
    failed_documents = result.get("failed_documents") or []
    created_folders = result.get("created_folders") or []
    lines = [
        "# Feishu Backup Report",
        "",
        f"- Mode: `{result.get('mode', 'unknown')}`",
        f"- Started at: `{result.get('started_at', '')}`",
        f"- Finished at: `{result.get('finished_at', '')}`",
        f"- Spaces: `{result.get('space_count', 0)}`",
        f"- Documents discovered: `{result.get('document_count', 0)}`",
        f"- New: `{result.get('new', 0)}`",
        f"- Updated: `{result.get('updated', 0)}`",
        f"- Skipped: `{result.get('skipped', 0)}`",
        f"- Deleted: `{result.get('deleted', 0)}`",
        f"- Failed: `{result.get('failed', 0)}`",
        f"- Documents root: `{result.get('documents_root', '')}`",
        f"- Progress log: `{result.get('progress_log_path', '')}`",
        f"- Resume state: `{result.get('resume_state_path', '')}`",
    ]
    if result.get("resumed_from_previous_run"):
        lines.append("- Resume mode: `true`")
    if created_folders:
        lines.extend(["", "## Created folders", ""])
        lines.extend(f"- `{folder}`" for folder in created_folders)
    if failed_documents:
        lines.extend(["", "## Failed documents", ""])
        for item in failed_documents:
            title = item.get("title") or item.get("document_key") or "unknown"
            error = item.get("error") or "unknown error"
            lines.append(f"- `{title}`: {error}")
    return "\n".join(lines) + "\n"


def render_shareable_summary(result: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Feishu Backup",
            f"spaces={result.get('space_count', 0)} docs={result.get('document_count', 0)}",
            f"new={result.get('new', 0)} updated={result.get('updated', 0)} skipped={result.get('skipped', 0)}",
            f"deleted={result.get('deleted', 0)} failed={result.get('failed', 0)}",
            f"resume={str(bool(result.get('resumed_from_previous_run'))).lower()}",
        ]
    )


class StatefulFeishuExporter(FeishuExporter):
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        auth_mode: str,
        cache_dir: Path,
        redirect_port: int,
        access_token: str | None = None,
    ):
        super().__init__(
            app_id=app_id,
            app_secret=app_secret,
            access_token=access_token,
            auth_mode=auth_mode,
        )
        self.cache_dir = cache_dir
        self.redirect_port = redirect_port

    def get_access_token(self) -> str:
        if self._access_token:
            return self._access_token

        if not self.app_id or not self.app_secret:
            raise ValueError("需要提供 access_token 或 (app_id + app_secret)")

        if self._authenticator is None:
            if self.auth_mode == "tenant":
                self._authenticator = TenantAuthenticator(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    cache_dir=self.cache_dir,
                    is_lark=self.is_lark,
                )
            elif self.auth_mode == "oauth":
                self._authenticator = OAuth2Authenticator(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    redirect_port=self.redirect_port,
                    cache_dir=self.cache_dir,
                    is_lark=self.is_lark,
                )
            else:
                raise ValueError(f"Unsupported FEISHU_AUTH_MODE: {self.auth_mode}")

        if isinstance(self._authenticator, TenantAuthenticator):
            return self._authenticator.get_token()
        return self._authenticator.authenticate()


@dataclass
class SpaceRecord:
    space_id: str
    name: str

    @property
    def slug(self) -> str:
        return sanitize_segment(self.name)


@dataclass
class NodeRecord:
    document_key: str
    space_id: str
    source_space_name: str
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    parent_node_token: str | None
    obj_edit_time: str | None
    source_relative_markdown_path: str
    source_relative_asset_dir: str
    source_path_hint: str


@dataclass
class ClassificationResult:
    folder_name: str
    matched_existing_folder: bool
    reason: str


@dataclass
class WebNode:
    node_token: str
    obj_token: str
    title: str
    obj_type: str
    parent_node_token: str | None
    has_child: bool
    obj_edit_time: str | None


class FeishuWebSessionClient:
    def __init__(
        self,
        base_url: str,
        session_cookie: str,
        csrf_token: str,
        http_client: httpx.Client | None = None,
        retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.session_cookie = session_cookie.strip()
        self.csrf_token = csrf_token.strip()
        self.retries = max(retries, 0)
        self.client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={
                "Accept": "application/json, text/plain, */*",
                "X-CsrfToken": self.csrf_token,
                "User-Agent": DEFAULT_WEB_USER_AGENT,
                "Referer": f"{self.base_url}/wiki/",
                "Cookie": self.session_cookie,
                "Doc-Platform": "web",
                "Doc-OS": "mac",
                "Doc-Biz": "Lark",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "DNT": "1",
            },
        )

    def ensure_available(self) -> None:
        response = self.client.get("/space/api/user/")
        if response.status_code != 200:
            raise RuntimeError(
                f"Feishu web session check failed at {self.base_url}: HTTP {response.status_code}."
            )
        payload = response.json()
        if payload.get("code") not in {0, None}:
            raise RuntimeError(
                f"Feishu web session is not usable at {self.base_url}: {payload.get('msg') or payload}."
            )

    def list_spaces(self) -> list[SpaceRecord]:
        spaces: list[SpaceRecord] = []
        seen_ids: set[str] = set()
        last_label: str | None = None
        while True:
            params = {"size": "200"}
            if last_label:
                params["last_label"] = last_label
            response = self.client.get("/space/api/wiki/v2/space/get/", params=params)
            if response.status_code != 200:
                raise RuntimeError(f"Failed to list wiki spaces from web session: HTTP {response.status_code}.")
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"Failed to list wiki spaces from web session: {payload.get('msg') or payload}.")
            data = payload.get("data") or {}
            for item in data.get("spaces") or []:
                space_id = str(item.get("space_id") or "")
                if not space_id or space_id in seen_ids:
                    continue
                seen_ids.add(space_id)
                spaces.append(SpaceRecord(space_id=space_id, name=item.get("space_name") or space_id))
            if not data.get("has_more"):
                break
            last_label = data.get("last_label")
            if not last_label:
                break
        return spaces

    def list_space_nodes(self, space: SpaceRecord) -> list[WebNode]:
        root_data = self._fetch_tree(space_id=space.space_id, wiki_token="")
        tree = root_data.get("tree") or {}
        root_token = str(tree.get("root_token") or "")
        queue: deque[str] = deque(
            token
            for token in tree.get("root_list") or []
            if token and token != root_token
        )
        visited_subtrees: set[str] = {""}
        nodes_map: dict[str, dict[str, Any]] = dict(tree.get("nodes") or {})
        child_map: dict[str, list[str]] = {
            str(key): [str(child) for child in value or []]
            for key, value in (tree.get("child_map") or {}).items()
        }

        while queue:
            wiki_token = queue.popleft()
            if wiki_token in visited_subtrees:
                continue
            visited_subtrees.add(wiki_token)
            branch_tree = (self._fetch_tree(space_id=space.space_id, wiki_token=wiki_token).get("tree") or {})
            for key, value in (branch_tree.get("nodes") or {}).items():
                nodes_map[str(key)] = value
            for key, value in (branch_tree.get("child_map") or {}).items():
                child_tokens = [str(child) for child in value or []]
                child_map[str(key)] = child_tokens
                for child_token in child_tokens:
                    queue.append(child_token)

        records: list[WebNode] = []
        for wiki_token, node in nodes_map.items():
            if wiki_token == root_token:
                continue
            if int(node.get("entity_delete_flag") or 0) != 0:
                continue
            if int(node.get("wiki_node_type") or 0) == 2:
                continue
            records.append(
                WebNode(
                    node_token=wiki_token,
                    obj_token=str(node.get("obj_token") or ""),
                    title=str(node.get("title") or wiki_token),
                    obj_type="wiki",
                    parent_node_token=(
                        None
                        if str(node.get("parent_wiki_token") or "") == root_token
                        else (str(node.get("parent_wiki_token") or "") or None)
                    ),
                    has_child=bool(node.get("has_child")),
                    obj_edit_time=str((node.get("detail_info") or {}).get("edit_time") or "") or None,
                )
            )
        return records

    def _fetch_tree(self, *, space_id: str, wiki_token: str) -> dict[str, Any]:
        params = {
            "space_id": space_id,
            "wiki_token": wiki_token,
            "with_space": "true" if not wiki_token else "false",
            "with_perm": "true" if not wiki_token else "false",
            "expand_shortcut": "true",
            "need_shared": "true",
            "exclude_fields": "5",
            "with_deleted": "true",
        }
        last_error: RuntimeError | None = None
        for attempt in range(self.retries + 1):
            response = self.client.get("/space/api/wiki/v2/tree/get_info/", params=params)
            if response.status_code == 200:
                payload = response.json()
                if payload.get("code") == 0:
                    return payload.get("data") or {}
                last_error = RuntimeError(f"Failed to fetch wiki tree from web session: {payload.get('msg') or payload}.")
            else:
                last_error = RuntimeError(f"Failed to fetch wiki tree from web session: HTTP {response.status_code}.")

            retry_after = response.headers.get("Retry-After")
            if attempt < self.retries and response.status_code in {429, 500, 502, 503, 504}:
                delay = float(retry_after) if retry_after else min(2 ** attempt, 8)
                time.sleep(delay)
                continue
            break
        assert last_error is not None
        raise last_error


class FolderSelection(BaseModel):
    matched_existing_folder: bool = Field(
        description="True if folder_name exactly matches one existing candidate folder."
    )
    folder_name: str = Field(
        description="Use the exact existing candidate folder name when matched_existing_folder is true. Otherwise return a kebab-case slug for a new folder."
    )
    reason: str = Field(description="Short explanation for the routing choice.")


def normalize_route_key(value: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.strip().lower())
    return normalized


class NamePathArchiveClassifier:
    def _extract_hints(self, record: NodeRecord) -> list[str]:
        hints = [record.title, record.source_space_name]
        hints.extend(segment for segment in record.source_path_hint.split("/") if segment)
        return [hint for hint in hints if hint]

    def _match_existing_folder(self, record: NodeRecord, candidate_folders: Sequence[str]) -> str | None:
        hints = self._extract_hints(record)
        normalized_hints = [normalize_route_key(hint) for hint in hints]

        for folder in candidate_folders:
            normalized_folder = normalize_route_key(folder)
            if normalized_folder and normalized_folder in normalized_hints:
                return folder

        for folder in candidate_folders:
            normalized_folder = normalize_route_key(folder)
            if not normalized_folder:
                continue
            for hint in normalized_hints:
                if hint and (normalized_folder in hint or hint in normalized_folder):
                    return folder

        best_folder: str | None = None
        best_score = 0.0
        for folder in candidate_folders:
            normalized_folder = normalize_route_key(folder)
            if not normalized_folder:
                continue
            for hint in normalized_hints:
                if not hint:
                    continue
                score = SequenceMatcher(None, normalized_folder, hint).ratio()
                if score > best_score:
                    best_score = score
                    best_folder = folder
        if best_score >= 0.72:
            return best_folder
        return None

    def classify(
        self,
        record: NodeRecord,
        markdown_text: str,
        candidate_folders: Sequence[str],
    ) -> ClassificationResult:
        del markdown_text

        matched_folder = self._match_existing_folder(record, candidate_folders)
        if matched_folder is not None:
            return ClassificationResult(
                folder_name=matched_folder,
                matched_existing_folder=True,
                reason="Matched an existing top-level folder from the file name or source path.",
            )

        return ClassificationResult(
            folder_name=strict_two_word_slug(record.source_space_name),
            matched_existing_folder=False,
            reason="No existing folder matched, so the document was grouped by source knowledge base.",
        )


class OllamaArchiveClassifier:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
        )

    def ensure_available(self) -> None:
        try:
            response = self.client.get("/api/tags")
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Ollama service is unavailable at {self.base_url}. Start it with `ollama serve`."
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Ollama service check failed at {self.base_url}: HTTP {response.status_code}."
            )
        payload = response.json()
        models = payload.get("models") or []
        available_names = {item.get("model") for item in models if item.get("model")}
        if self.model not in available_names:
            raise RuntimeError(
                f"Ollama model `{self.model}` is not available. Run `ollama pull {self.model}` first."
            )

    def _fallback_classification(
        self,
        record: NodeRecord,
        excerpt: str,
        candidate_folders: Sequence[str],
        reason: str,
    ) -> ClassificationResult:
        searchable = " ".join(
            [
                record.source_space_name,
                record.title,
                record.source_path_hint,
                excerpt,
            ]
        ).lower()
        for folder in candidate_folders:
            tokens = [token for token in re.split(r"[^a-z0-9\u4e00-\u9fff]+", folder.lower()) if token]
            if tokens and any(token in searchable for token in tokens):
                return ClassificationResult(
                    folder_name=folder,
                    matched_existing_folder=True,
                    reason=f"{reason} Fallback matched existing folder `{folder}`.",
                )

        return ClassificationResult(
            folder_name=slugify(record.source_space_name),
            matched_existing_folder=False,
            reason=f"{reason} Fallback created a folder from the source space name.",
        )

    def classify(self, record: NodeRecord, markdown_text: str, candidate_folders: Sequence[str]) -> ClassificationResult:
        if not candidate_folders:
            return ClassificationResult(
                folder_name=slugify(record.source_space_name),
                matched_existing_folder=False,
                reason="No existing candidate folders.",
            )

        excerpt = compact_markdown_excerpt(markdown_text)
        prompt = "\n".join(
            [
                "Route this Feishu document into exactly one top-level folder under ~/Documents.",
                "Prefer an existing folder when it is semantically appropriate.",
                "Only create a new folder when none of the existing folders fit.",
                "If creating a new folder, folder_name must be a short kebab-case slug.",
                "Return only one JSON object with keys: matched_existing_folder, folder_name, reason.",
                "",
                f"Space: {record.source_space_name}",
                f"Title: {record.title}",
                f"Original path: {record.source_path_hint}",
                f"Doc type: {record.obj_type}",
                f"Excerpt: {excerpt}",
                "",
                "Existing top-level folders:",
                *[f"- {folder}" for folder in candidate_folders],
            ]
        )
        try:
            response = self.client.post(
                "/api/generate",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": FolderSelection.model_json_schema(),
                    "system": (
                        "You classify documents into one top-level folder. "
                        "If a folder clearly matches, use it exactly. "
                        "If none fit, invent one short kebab-case folder slug. "
                        "Return exactly one JSON object that matches the provided schema. "
                        "Never return markdown, prose, or code fences."
                    ),
                    "options": {
                        "temperature": 0,
                    },
                    "prompt": prompt,
                },
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Ollama request failed against {self.base_url}. "
                f"Ensure `ollama serve` is running and raise OLLAMA_TIMEOUT_SECONDS if the model is slow."
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(f"Ollama classification failed: HTTP {response.status_code} {response.text}")
        payload = response.json()
        raw_response = payload.get("response", "")
        try:
            parsed = FolderSelection.model_validate_json(raw_response)
        except ValidationError as exc:
            raise RuntimeError(f"Failed to parse Ollama classification response: {exc}") from exc

        folder_name = parsed.folder_name.strip()
        if parsed.matched_existing_folder:
            if folder_name not in candidate_folders:
                return self._fallback_classification(record, excerpt, candidate_folders, parsed.reason)
            return ClassificationResult(folder_name=folder_name, matched_existing_folder=True, reason=parsed.reason)

        return ClassificationResult(
            folder_name=slugify(folder_name),
            matched_existing_folder=False,
            reason=parsed.reason,
        )


class BackupStatePaths:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self.logs_dir = root_dir / "logs"
        self.trash_dir = root_dir / "trash"
        self.staging_dir = root_dir / ".staging"
        self.manifest_path = root_dir / "manifest.json"
        self.resume_state_path = root_dir / "resume-state.json"
        self.env_path = root_dir / "env"

    def ensure(self) -> None:
        for path in [self.root_dir, self.logs_dir, self.trash_dir, self.staging_dir]:
            path.mkdir(parents=True, exist_ok=True)


class FeishuBackupService:
    def __init__(
        self,
        config: BackupConfig,
        exporter: Any | None = None,
        classifier: Any | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ):
        self.config = config
        self.state_paths = BackupStatePaths(config.state_root)
        self.state_paths.ensure()
        self.documents_root = config.documents_root
        self.documents_root.mkdir(parents=True, exist_ok=True)
        self._exporter = exporter
        self._classifier = classifier
        self.now_fn = now_fn
        self._access_token: str | None = None
        self._web_client: FeishuWebSessionClient | None = None
        self._progress_log_handle: Any | None = None
        self.repo_root = Path(__file__).resolve().parent

    def _emit_progress(self, message: str) -> None:
        print(message, flush=True)
        if self._progress_log_handle is not None:
            self._progress_log_handle.write(f"{message}\n")
            self._progress_log_handle.flush()

    def _write_run_reports(self, *, started_at: datetime, result: dict[str, Any]) -> tuple[Path, Path]:
        stem = started_at.strftime("%Y%m%dT%H%M%SZ")
        summary_path = self.state_paths.logs_dir / f"{stem}.summary.txt"
        report_path = self.state_paths.logs_dir / f"{stem}.report.md"
        write_text(summary_path, render_shareable_summary(result) + "\n")
        write_text(report_path, render_backup_report(result))
        return summary_path, report_path

    def _write_manifest_snapshot(
        self,
        *,
        previous_manifest: dict[str, Any],
        documents: dict[str, Any],
        deleted: dict[str, Any],
        stats: dict[str, Any],
        finished_at: str | None = None,
    ) -> None:
        payload = {
            "generated_at": finished_at or previous_manifest.get("generated_at"),
            "last_success_at": finished_at or previous_manifest.get("last_success_at"),
            "documents": documents,
            "deleted": deleted,
            "stats": stats,
        }
        write_json(self.state_paths.manifest_path, payload)

    def _load_resume_state(self) -> dict[str, Any] | None:
        if not self.state_paths.resume_state_path.exists():
            return None
        return json.loads(self.state_paths.resume_state_path.read_text(encoding="utf-8"))

    def _save_resume_state(self, payload: dict[str, Any]) -> None:
        write_json(self.state_paths.resume_state_path, payload)

    def _is_resumable_state(self, payload: dict[str, Any] | None, mode: str) -> bool:
        if not payload:
            return False
        if payload.get("mode") != mode:
            return False
        if payload.get("status") not in {"running", "failed"}:
            return False
        return bool(payload.get("documents") and payload.get("document_order"))

    def _serialize_record(self, record: NodeRecord) -> dict[str, Any]:
        return {
            "document_key": record.document_key,
            "space_id": record.space_id,
            "source_space_name": record.source_space_name,
            "node_token": record.node_token,
            "obj_token": record.obj_token,
            "obj_type": record.obj_type,
            "title": record.title,
            "parent_node_token": record.parent_node_token,
            "obj_edit_time": record.obj_edit_time,
            "source_relative_markdown_path": record.source_relative_markdown_path,
            "source_relative_asset_dir": record.source_relative_asset_dir,
            "source_path_hint": record.source_path_hint,
        }

    def _deserialize_record(self, payload: dict[str, Any]) -> NodeRecord:
        return NodeRecord(
            document_key=payload["document_key"],
            space_id=payload["space_id"],
            source_space_name=payload["source_space_name"],
            node_token=payload["node_token"],
            obj_token=payload["obj_token"],
            obj_type=payload["obj_type"],
            title=payload["title"],
            parent_node_token=payload.get("parent_node_token"),
            obj_edit_time=payload.get("obj_edit_time"),
            source_relative_markdown_path=payload["source_relative_markdown_path"],
            source_relative_asset_dir=payload["source_relative_asset_dir"],
            source_path_hint=payload["source_path_hint"],
        )

    def _build_resume_state(self, *, mode: str, started_at: datetime, spaces: Sequence[SpaceRecord], records: Sequence[NodeRecord]) -> dict[str, Any]:
        return {
            "run_id": uuid.uuid4().hex,
            "mode": mode,
            "started_at": started_at.isoformat(),
            "last_event_at": started_at.isoformat(),
            "status": "running",
            "space_order": [space.space_id for space in spaces],
            "document_order": [record.document_key for record in records],
            "current_space_id": None,
            "completed_spaces": [],
            "documents": {
                record.document_key: {
                    "record": self._serialize_record(record),
                    "status": "pending",
                    "space_id": record.space_id,
                    "title": record.title,
                    "relative_doc_path": None,
                }
                for record in records
            },
            "failed_documents": [],
        }

    def _update_resume_document(
        self,
        payload: dict[str, Any],
        *,
        document_key: str,
        status: str,
        relative_doc_path: str | None = None,
        error: str | None = None,
    ) -> None:
        document = payload["documents"][document_key]
        document["status"] = status
        if relative_doc_path is not None:
            document["relative_doc_path"] = relative_doc_path
        payload["last_event_at"] = self.now_fn().isoformat()
        if error:
            failed_documents = [item for item in payload.get("failed_documents", []) if item.get("document_key") != document_key]
            failed_documents.append(
                {
                    "document_key": document_key,
                    "space_id": document["space_id"],
                    "title": document["title"],
                    "error": error,
                }
            )
            payload["failed_documents"] = failed_documents
        else:
            payload["failed_documents"] = [item for item in payload.get("failed_documents", []) if item.get("document_key") != document_key]

    def _mark_completed_spaces(self, payload: dict[str, Any]) -> None:
        completed_spaces: list[str] = []
        for space_id in payload.get("space_order", []):
            doc_keys = [
                key
                for key in payload.get("document_order", [])
                if payload["documents"][key]["space_id"] == space_id
            ]
            if doc_keys and all(payload["documents"][key]["status"] in {"archived", "skipped"} for key in doc_keys):
                completed_spaces.append(space_id)
        payload["completed_spaces"] = completed_spaces

    def _staging_parent(self, record: NodeRecord) -> Path:
        return self.state_paths.staging_dir / record.document_key.replace(":", "__")

    def _staging_paths(self, record: NodeRecord) -> tuple[Path, Path]:
        filename_stem = Path(record.source_relative_markdown_path).stem
        staging_parent = self._staging_parent(record)
        return staging_parent / f"{filename_stem}.md", staging_parent / filename_stem

    def _has_valid_staging_export(self, record: NodeRecord) -> bool:
        markdown_path, _ = self._staging_paths(record)
        return markdown_path.exists()

    @property
    def exporter(self) -> Any:
        if self._exporter is None:
            if FeishuExporter is None:
                raise ModuleNotFoundError("feishu_docx is required to run backups.")
            self._exporter = StatefulFeishuExporter(
                app_id=self.config.app_id,
                app_secret=self.config.app_secret,
                auth_mode=self.config.feishu_auth_mode,
                cache_dir=self.config.feishu_docx_cache_dir,
                redirect_port=self.config.oauth_redirect_port,
            )
        return self._exporter

    @property
    def classifier(self) -> Any:
        if self._classifier is None:
            self._classifier = NamePathArchiveClassifier()
        return self._classifier

    @property
    def web_client(self) -> FeishuWebSessionClient | None:
        if self._web_client is None:
            if self.config.web_base_url and self.config.web_session_cookie and self.config.web_csrf_token:
                self._web_client = FeishuWebSessionClient(
                    base_url=self.config.web_base_url,
                    session_cookie=self.config.web_session_cookie,
                    csrf_token=self.config.web_csrf_token,
                    retries=self.config.retries,
                )
        return self._web_client

    def run(self, mode: str) -> dict[str, Any]:
        if mode not in {"full", "sync"}:
            raise ValueError(f"Unsupported mode: {mode}")

        ensure_available = getattr(self.classifier, "ensure_available", None)
        if callable(ensure_available):
            ensure_available()
        if self.web_client is not None and self.config.feishu_auth_mode != "oauth":
            self.web_client.ensure_available()

        started_at = self.now_fn()
        progress_log_path = self.state_paths.logs_dir / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}.progress.log"
        with progress_log_path.open("a", encoding="utf-8") as progress_log:
            self._progress_log_handle = progress_log
            try:
                self._emit_progress(f"[backup] progress log: {progress_log_path}")
                self._emit_progress(f"[backup] start mode={mode}")
                manifest = self._load_manifest()
                previous_entries = manifest.get("documents", {})
                deleted_entries = manifest.get("deleted", {})
                resume_state = self._load_resume_state()
                resumed_from_previous_run = self._is_resumable_state(resume_state, mode)
                if resumed_from_previous_run:
                    spaces = [
                        SpaceRecord(space_id=space_id, name=space_id)
                        for space_id in resume_state.get("space_order", [])
                    ]
                    current_records = [
                        self._deserialize_record(resume_state["documents"][document_key]["record"])
                        for document_key in resume_state.get("document_order", [])
                    ]
                    self._emit_progress(f"[backup] resumed run_id={resume_state.get('run_id')}")
                    self._emit_progress(f"[backup] discovered spaces={len(spaces)}")
                    self._emit_progress(f"[backup] discovered documents={len(current_records)}")
                else:
                    spaces = self._list_spaces()
                    self._emit_progress(f"[backup] discovered spaces={len(spaces)}")
                    current_records = self._collect_records(spaces)
                    self._emit_progress(f"[backup] discovered documents={len(current_records)}")
                    resume_state = self._build_resume_state(
                        mode=mode,
                        started_at=started_at,
                        spaces=spaces,
                        records=current_records,
                    )
                    self._save_resume_state(resume_state)
                candidate_folders = self._existing_top_level_folders()
                current_keys = {record.document_key for record in current_records}

                result: dict[str, Any] = {
                    "mode": mode,
                    "started_at": started_at.isoformat(),
                    "space_count": len(spaces),
                    "document_count": len(current_records),
                    "documents_root": str(self.documents_root),
                    "state_root": str(self.state_paths.root_dir),
                    "progress_log_path": str(progress_log_path),
                    "resume_state_path": str(self.state_paths.resume_state_path),
                    "resumed_from_previous_run": resumed_from_previous_run,
                    "new": 0,
                    "updated": 0,
                    "skipped": 0,
                    "deleted": 0,
                    "failed": 0,
                    "created_folders": [],
                    "failed_documents": [],
                }

                next_entries: dict[str, Any] = dict(previous_entries)
                created_folders: set[str] = set()
                total_records = len(current_records)
                last_space_id: str | None = None
                space_index_by_id = {space_id: idx + 1 for idx, space_id in enumerate(resume_state.get("space_order", []))}
                for index, record in enumerate(current_records, start=1):
                    resume_document = resume_state["documents"][record.document_key]
                    if record.space_id != last_space_id:
                        last_space_id = record.space_id
                        resume_state["current_space_id"] = record.space_id
                        resume_state["last_event_at"] = self.now_fn().isoformat()
                        self._save_resume_state(resume_state)
                        self._emit_progress(
                            f"[space {space_index_by_id.get(record.space_id, 0)}/{len(spaces)}] "
                            f"start {record.source_space_name} ({record.space_id})"
                        )

                    self._emit_progress(
                        f"[doc {index}/{total_records}] start {record.title} ({record.document_key})"
                    )
                    previous_entry = previous_entries.get(record.document_key)
                    target_exists = self.documents_root.joinpath(previous_entry["relative_doc_path"]).exists() if previous_entry else False
                    should_export = mode == "full" or self._should_export(record, previous_entry, target_exists)

                    if resume_document["status"] in {"archived", "skipped"}:
                        result["skipped"] += 1
                        next_entries[record.document_key] = dict(previous_entry or next_entries.get(record.document_key) or {})
                        self._emit_progress(f"[skip {index}/{total_records}] {record.title}")
                        continue

                    if not should_export and previous_entry is not None:
                        entry = dict(previous_entry)
                        entry.update(
                            {
                                "space_id": record.space_id,
                                "source_space_name": record.source_space_name,
                                "node_token": record.node_token,
                                "obj_edit_time": record.obj_edit_time,
                                "title": record.title,
                                "status": "skipped",
                            }
                        )
                        next_entries[record.document_key] = entry
                        result["skipped"] += 1
                        self._update_resume_document(
                            resume_state,
                            document_key=record.document_key,
                            status="skipped",
                            relative_doc_path=entry.get("relative_doc_path"),
                        )
                        self._mark_completed_spaces(resume_state)
                        self._save_resume_state(resume_state)
                        self._emit_progress(f"[skip {index}/{total_records}] {record.title}")
                        continue

                    try:
                        if resume_document["status"] == "exported" and self._has_valid_staging_export(record):
                            exported_markdown, exported_assets = self._staging_paths(record)
                        else:
                            exported_markdown, exported_assets = self._export_to_staging(record)
                            self._update_resume_document(
                                resume_state,
                                document_key=record.document_key,
                                status="exported",
                            )
                            self._save_resume_state(resume_state)
                        export_result = self._archive_from_staging(
                            record,
                            candidate_folders,
                            exported_markdown=exported_markdown,
                            exported_assets=exported_assets,
                        )
                        if not export_result["classification"].matched_existing_folder:
                            created_folders.add(export_result["classification"].folder_name)
                            if export_result["classification"].folder_name not in candidate_folders:
                                candidate_folders.append(export_result["classification"].folder_name)

                        if previous_entry:
                            self._trash_previous_entry(previous_entry, started_at)
                            result["updated"] += 1
                            status = "updated"
                        else:
                            result["new"] += 1
                            status = "new"
                        self._emit_progress(
                            f"[{status} {index}/{total_records}] {record.title} -> "
                            f"{export_result['classification'].folder_name}"
                        )

                        next_entries[record.document_key] = {
                            "document_key": record.document_key,
                            "space_id": record.space_id,
                            "source_space_name": record.source_space_name,
                            "node_token": record.node_token,
                            "obj_edit_time": record.obj_edit_time,
                            "title": record.title,
                            "classified_folder": export_result["classification"].folder_name,
                            "classification_reason": export_result["classification"].reason,
                            "relative_doc_path": export_result["relative_doc_path"],
                            "relative_asset_dir": export_result["relative_asset_dir"],
                            "asset_files": export_result["asset_files"],
                            "checksum": export_result["checksum"],
                            "status": status,
                            "last_exported_at": started_at.isoformat(),
                            "source_path_hint": record.source_path_hint,
                        }
                        previous_entries[record.document_key] = next_entries[record.document_key]
                        self._update_resume_document(
                            resume_state,
                            document_key=record.document_key,
                            status="archived",
                            relative_doc_path=next_entries[record.document_key]["relative_doc_path"],
                        )
                        self._mark_completed_spaces(resume_state)
                        self._save_resume_state(resume_state)
                        self._write_manifest_snapshot(
                            previous_manifest=manifest,
                            documents=next_entries,
                            deleted=deleted_entries,
                            stats={
                                "space_count": len(spaces),
                                "active_documents": len(next_entries),
                                "deleted_documents": len(deleted_entries),
                                "new": result["new"],
                                "updated": result["updated"],
                                "skipped": result["skipped"],
                                "deleted": result["deleted"],
                                "failed": result["failed"],
                            },
                            finished_at=manifest.get("generated_at"),
                        )
                    except Exception as exc:
                        result["failed"] += 1
                        self._emit_progress(f"[fail {index}/{total_records}] {record.title}: {exc}")
                        result["failed_documents"].append(
                            {
                                "document_key": record.document_key,
                                "space_id": record.space_id,
                                "title": record.title,
                                "error": str(exc),
                            }
                        )
                        self._update_resume_document(
                            resume_state,
                            document_key=record.document_key,
                            status="failed",
                            error=str(exc),
                        )
                        self._save_resume_state(resume_state)
                        if previous_entry:
                            failed_entry = dict(previous_entry)
                            failed_entry["status"] = "failed"
                            next_entries[record.document_key] = failed_entry

                for document_key, previous_entry in previous_entries.items():
                    if document_key in current_keys:
                        continue
                    self._trash_previous_entry(previous_entry, started_at)
                    self._emit_progress(f"[delete] {previous_entry.get('title') or document_key}")
                    deleted_entries[document_key] = {
                        "document_key": document_key,
                        "space_id": previous_entry.get("space_id"),
                        "title": previous_entry.get("title"),
                        "deleted_at": started_at.isoformat(),
                        "relative_doc_path": previous_entry.get("relative_doc_path"),
                        "relative_asset_dir": previous_entry.get("relative_asset_dir"),
                    }
                    next_entries.pop(document_key, None)
                    result["deleted"] += 1
                    self._write_manifest_snapshot(
                        previous_manifest=manifest,
                        documents=next_entries,
                        deleted=deleted_entries,
                        stats={
                            "space_count": len(spaces),
                            "active_documents": len(next_entries),
                            "deleted_documents": len(deleted_entries),
                            "new": result["new"],
                            "updated": result["updated"],
                            "skipped": result["skipped"],
                            "deleted": result["deleted"],
                            "failed": result["failed"],
                        },
                        finished_at=manifest.get("generated_at"),
                    )

                finished_at = self.now_fn().isoformat()
                result["created_folders"] = sorted(created_folders)
                result["finished_at"] = finished_at

                manifest_payload = {
                    "generated_at": finished_at,
                    "last_success_at": finished_at if result["failed"] == 0 else manifest.get("last_success_at"),
                    "documents": next_entries,
                    "deleted": deleted_entries,
                    "stats": {
                        "space_count": len(spaces),
                        "active_documents": len(next_entries),
                        "deleted_documents": len(deleted_entries),
                        "new": result["new"],
                        "updated": result["updated"],
                        "skipped": result["skipped"],
                        "deleted": result["deleted"],
                        "failed": result["failed"],
                    },
                }
                write_json(self.state_paths.manifest_path, manifest_payload)
                resume_state["status"] = "completed" if result["failed"] == 0 else "failed"
                resume_state["current_space_id"] = None
                resume_state["last_event_at"] = finished_at
                self._mark_completed_spaces(resume_state)
                self._save_resume_state(resume_state)

                log_path = self.state_paths.logs_dir / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
                result["manifest_path"] = str(self.state_paths.manifest_path)
                result["log_path"] = str(log_path)
                summary_path, report_path = self._write_run_reports(started_at=started_at, result=result)
                result["summary_path"] = str(summary_path)
                result["report_path"] = str(report_path)
                write_json(log_path, result)
                self._cleanup_staging(resume_state)
                self._emit_progress(
                    f"[backup] done spaces={result['space_count']} docs={result['document_count']} "
                    f"new={result['new']} updated={result['updated']} skipped={result['skipped']} "
                    f"deleted={result['deleted']} failed={result['failed']}"
                )
                self._emit_progress(f"[backup] summary {summary_path}")
                self._emit_progress(f"[backup] report {report_path}")
                return result
            finally:
                self._progress_log_handle = None

    def authorize(self) -> dict[str, str]:
        self.config.feishu_docx_cache_dir.mkdir(parents=True, exist_ok=True)
        access_token = self._get_access_token()
        cache_files = sorted(str(path) for path in self.config.feishu_docx_cache_dir.glob("*.json"))
        return {
            "auth_mode": self.config.feishu_auth_mode,
            "access_token_prefix": access_token[:16],
            "cache_dir": str(self.config.feishu_docx_cache_dir),
            "cache_files": cache_files,
        }

    def write_env_template(self, overwrite: bool = False) -> Path:
        if self.state_paths.env_path.exists() and not overwrite:
            return self.state_paths.env_path
        self.state_paths.env_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_paths.env_path.write_text(self.config.env_file_payload(), encoding="utf-8")
        return self.state_paths.env_path

    def install_launchd(self, *, load: bool = False) -> Path:
        env_values = load_env_file(self.state_paths.env_path)
        required = ["APP_ID", "APP_SECRET"]
        missing = [key for key in required if not env_values.get(key)]
        if missing:
            raise SystemExit(
                f"Cannot install launchd job. Fill {self.state_paths.env_path} first: {', '.join(missing)}"
            )

        stdout_path = self.state_paths.logs_dir / "launchd.stdout.log"
        stderr_path = self.state_paths.logs_dir / "launchd.stderr.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        python_bin = self.repo_root / ".venv" / "bin" / "python"
        if not python_bin.exists():
            python_bin = Path(sys.executable)
        plist_payload = {
            "Label": self.config.launchd_label,
            "ProgramArguments": [
                str(python_bin),
                "-m",
                "feishu_backup",
                "sync",
            ],
            "WorkingDirectory": str(self.repo_root),
            "EnvironmentVariables": {
                "STATE_ROOT": str(self.config.state_root),
            },
            "StartCalendarInterval": {
                "Hour": self.config.launchd_hour,
                "Minute": self.config.launchd_minute,
            },
            "StandardOutPath": str(stdout_path),
            "StandardErrorPath": str(stderr_path),
            "RunAtLoad": False,
        }
        self.config.launch_agent_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.launch_agent_path.open("wb") as handle:
            plistlib.dump(plist_payload, handle)
        if load:
            self._load_launchd_agent()
        return self.config.launch_agent_path

    def _load_launchd_agent(self) -> None:
        domain = f"gui/{os.getuid()}"
        plist_path = str(self.config.launch_agent_path)
        subprocess.run(
            ["launchctl", "bootout", domain, plist_path],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["launchctl", "bootstrap", domain, plist_path], check=True)
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{self.config.launchd_label}"],
            check=True,
        )

    def _get_access_token(self) -> str:
        if self._access_token is None:
            self._access_token = self.exporter.get_access_token()
        return self._access_token

    def _list_spaces(self) -> list[SpaceRecord]:
        if self.web_client is not None and self.config.feishu_auth_mode != "oauth":
            return self._filter_spaces(self.web_client.list_spaces())

        spaces: list[SpaceRecord] = []
        try:
            access_token = self._get_access_token()
            if hasattr(self.exporter, "list_spaces"):
                spaces = [
                    SpaceRecord(space_id=item.space_id, name=item.name or item.space_id)
                    for item in self.exporter.list_spaces(access_token=access_token)
                ]
            else:
                page_token: str | None = None
                while True:
                    builder = ListSpaceRequest.builder().page_size(50)
                    if page_token:
                        builder.page_token(page_token)
                    request = builder.build()
                    response = self.exporter.sdk.client.wiki.v2.space.list(
                        request,
                        self.exporter.sdk._core.build_option(access_token),
                    )
                    if not response.success():
                        raise RuntimeError(f"Failed to list wiki spaces: {response.code} {response.msg}")
                    body = response.data
                    for item in body.items or []:
                        spaces.append(SpaceRecord(space_id=item.space_id, name=item.name or item.space_id))
                    if not body.has_more:
                        break
                    page_token = body.page_token
        except Exception:
            spaces = []

        if spaces:
            return self._filter_spaces(spaces)
        return []

    def _filter_spaces(self, spaces: Sequence[SpaceRecord]) -> list[SpaceRecord]:
        filtered: list[SpaceRecord] = []
        for space in spaces:
            if self.config.space_ids and space.space_id not in self.config.space_ids:
                continue
            if self.config.space_name_allowlist and space.name not in self.config.space_name_allowlist:
                continue
            filtered.append(space)
        return sorted(filtered, key=lambda item: item.name.lower())

    def _collect_records(self, spaces: Iterable[SpaceRecord]) -> list[NodeRecord]:
        records: list[NodeRecord] = []
        access_token: str | None = None if (self.web_client is not None and self.config.feishu_auth_mode != "oauth") else self._get_access_token()
        space_list = list(spaces)
        total_spaces = len(space_list)
        for index, space in enumerate(space_list, start=1):
            self._emit_progress(f"[space {index}/{total_spaces}] scan {space.name} ({space.space_id})")
            space_records = self._collect_space_records(space, access_token)
            records.extend(space_records)
            self._emit_progress(f"[space {index}/{total_spaces}] discovered documents={len(space_records)}")
        return records

    def _get_all_space_nodes_with_retry(
        self,
        *,
        space_id: str,
        access_token: str,
        parent_node_token: str | None,
    ) -> list[Any]:
        attempts = max(self.config.retries + 3, 4)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self.exporter.sdk.wiki.get_all_space_nodes(
                    space_id=space_id,
                    access_token=access_token,
                    parent_node_token=parent_node_token,
                )
            except Exception as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                self._emit_progress(
                    f"[retry] list nodes space={space_id} parent={parent_node_token or 'root'} "
                    f"attempt={attempt + 1}/{attempts} error={exc}"
                )
                time.sleep(min(2 ** attempt, 20))
        assert last_error is not None
        raise last_error

    def _collect_space_records(self, space: SpaceRecord, access_token: str | None) -> list[NodeRecord]:
        records: list[NodeRecord] = []

        if self.web_client is not None and self.config.feishu_auth_mode != "oauth":
            try:
                return self._collect_web_space_records(space)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to collect wiki nodes from the logged-in Feishu web session for space "
                    f"`{space.name}` ({space.space_id}): {exc}"
                ) from exc

        if not access_token:
            access_token = self._get_access_token()

        def walk(parent_node_token: str | None, depth: int, relative_dir: Path) -> None:
            if depth > self.config.max_depth:
                return

            nodes = self._get_all_space_nodes_with_retry(
                space_id=space.space_id,
                access_token=access_token,
                parent_node_token=parent_node_token,
            )
            seen_names: set[str] = set()
            for node in nodes:
                obj_type = getattr(node, "obj_type")
                node_token = getattr(node, "node_token")
                title = sanitize_segment(getattr(node, "title", None) or node_token)
                unique_title = ensure_unique_name(title, seen_names, node_token)
                has_child = bool(getattr(node, "has_child", False))
                current_dir = relative_dir

                if obj_type in SUPPORTED_NODE_TYPES:
                    if has_child:
                        current_dir = relative_dir / unique_title
                        markdown_rel = current_dir / f"{unique_title}.md"
                        asset_rel = current_dir / unique_title
                    else:
                        markdown_rel = relative_dir / f"{unique_title}.md"
                        asset_rel = relative_dir / unique_title
                    source_path_hint = "/".join(part for part in markdown_rel.with_suffix("").parts)
                    records.append(
                        NodeRecord(
                            document_key=f"{space.space_id}:{node_token}",
                            space_id=space.space_id,
                            source_space_name=space.name,
                            node_token=node_token,
                            obj_token=getattr(node, "obj_token"),
                            obj_type=obj_type,
                            title=getattr(node, "title", unique_title),
                            parent_node_token=getattr(node, "parent_node_token", None),
                            obj_edit_time=str(getattr(node, "obj_edit_time", "")) or None,
                            source_relative_markdown_path=markdown_rel.as_posix(),
                            source_relative_asset_dir=asset_rel.as_posix(),
                            source_path_hint=source_path_hint,
                        )
                    )

                if has_child:
                    next_dir = current_dir if obj_type in SUPPORTED_NODE_TYPES else relative_dir / unique_title
                    walk(node_token, depth + 1, next_dir)

        walk(parent_node_token=None, depth=0, relative_dir=Path())
        return records

    def _collect_web_space_records(self, space: SpaceRecord) -> list[NodeRecord]:
        if self.web_client is None:
            raise RuntimeError("Feishu web session is not configured.")

        nodes = self.web_client.list_space_nodes(space)
        nodes_by_parent: dict[str | None, list[WebNode]] = {}
        for node in nodes:
            nodes_by_parent.setdefault(node.parent_node_token, []).append(node)

        records: list[NodeRecord] = []

        def walk(parent_node_token: str | None, depth: int, relative_dir: Path) -> None:
            if depth > self.config.max_depth:
                return
            siblings = sorted(nodes_by_parent.get(parent_node_token, []), key=lambda item: item.title.lower())
            seen_names: set[str] = set()
            for node in siblings:
                title = sanitize_segment(node.title or node.node_token)
                unique_title = ensure_unique_name(title, seen_names, node.node_token)
                current_dir = relative_dir
                if node.obj_type in SUPPORTED_NODE_TYPES:
                    if node.has_child:
                        current_dir = relative_dir / unique_title
                        markdown_rel = current_dir / f"{unique_title}.md"
                        asset_rel = current_dir / unique_title
                    else:
                        markdown_rel = relative_dir / f"{unique_title}.md"
                        asset_rel = relative_dir / unique_title
                    source_path_hint = "/".join(part for part in markdown_rel.with_suffix("").parts)
                    records.append(
                        NodeRecord(
                            document_key=f"{space.space_id}:{node.node_token}",
                            space_id=space.space_id,
                            source_space_name=space.name,
                            node_token=node.node_token,
                            obj_token=node.obj_token,
                            obj_type=node.obj_type,
                            title=node.title or unique_title,
                            parent_node_token=node.parent_node_token,
                            obj_edit_time=node.obj_edit_time,
                            source_relative_markdown_path=markdown_rel.as_posix(),
                            source_relative_asset_dir=asset_rel.as_posix(),
                            source_path_hint=source_path_hint,
                        )
                    )
                if node.has_child:
                    next_dir = current_dir if node.obj_type in SUPPORTED_NODE_TYPES else relative_dir / unique_title
                    walk(node.node_token, depth + 1, next_dir)

        walk(parent_node_token=None, depth=0, relative_dir=Path())
        return records

    def _build_export_url(self, record: NodeRecord) -> str:
        export_base_url = (self.config.web_base_url or self.config.base_url).rstrip("/")
        if record.obj_type == "wiki":
            return f"{export_base_url}/wiki/{record.node_token}"
        if record.obj_type == "bitable":
            return f"{export_base_url}/wiki/{record.node_token}"
        doc_type = "docx" if record.obj_type == "doc" else record.obj_type
        return f"{export_base_url}/{doc_type}/{record.obj_token}"

    def _existing_top_level_folders(self) -> list[str]:
        folders = []
        for path in sorted(self.documents_root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir():
                continue
            if path.name.startswith(".") or path.name.startswith("_"):
                continue
            if path.name in DEFAULT_CATEGORY_EXCLUDES:
                continue
            if not is_strict_two_word_slug(path.name):
                continue
            folders.append(path.name)
        return folders

    def _should_export(self, record: NodeRecord, previous_entry: dict[str, Any] | None, target_exists: bool) -> bool:
        if previous_entry is None:
            return True
        if not target_exists:
            return True
        if previous_entry.get("obj_edit_time") != record.obj_edit_time:
            return True
        return previous_entry.get("status") == "failed"

    def _export_to_staging(self, record: NodeRecord) -> tuple[Path, Path]:
        filename_stem = Path(record.source_relative_markdown_path).stem
        staging_parent = self._staging_parent(record)
        if staging_parent.exists():
            shutil.rmtree(staging_parent)
        staging_parent.mkdir(parents=True, exist_ok=True)

        export_url = self._build_export_url(record)
        self._emit_progress(f"[doc] export {record.title} -> {export_url}")
        last_error = None
        for _ in range(self.config.retries + 1):
            try:
                exported_markdown = self.exporter.export(
                    url=export_url,
                    output_dir=staging_parent,
                    filename=filename_stem,
                    table_format="md",
                    silent=True,
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        exported_assets = staging_parent / filename_stem
        return exported_markdown, exported_assets

    def _archive_from_staging(
        self,
        record: NodeRecord,
        candidate_folders: Sequence[str],
        *,
        exported_markdown: Path,
        exported_assets: Path,
    ) -> dict[str, Any]:
        filename_stem = Path(record.source_relative_markdown_path).stem

        markdown_text = exported_markdown.read_text(encoding="utf-8")
        classification = self.classifier.classify(record, markdown_text, candidate_folders)
        classified_root = self.documents_root / classification.folder_name / sanitize_segment(record.source_space_name)
        doc_target = classified_root / record.source_relative_markdown_path
        asset_target = classified_root / record.source_relative_asset_dir
        self._emit_progress(f"[doc] archive {record.title} -> {classification.folder_name}")

        if doc_target.exists():
            doc_target.unlink()
        if asset_target.exists():
            shutil.rmtree(asset_target)
        doc_target.parent.mkdir(parents=True, exist_ok=True)
        asset_target.parent.mkdir(parents=True, exist_ok=True)

        asset_files: list[str] = []
        if exported_assets.exists():
            shutil.move(str(exported_assets), str(asset_target))
            asset_files = sorted(
                path.relative_to(asset_target).as_posix()
                for path in asset_target.rglob("*")
                if path.is_file()
            )
            markdown_text = rewrite_local_asset_links(
                markdown_text,
                original_folder=filename_stem,
                target_folder=relative_asset_target(doc_target, asset_target),
            )

        doc_target.write_text(markdown_text, encoding="utf-8")
        checksum = sha256_file(doc_target)
        return {
            "classification": classification,
            "relative_doc_path": doc_target.relative_to(self.documents_root).as_posix(),
            "relative_asset_dir": asset_target.relative_to(self.documents_root).as_posix(),
            "asset_files": asset_files,
            "checksum": checksum,
        }

    def _export_and_archive(
        self,
        record: NodeRecord,
        candidate_folders: Sequence[str],
        *,
        resume_status: str = "pending",
    ) -> dict[str, Any]:
        exported_markdown, exported_assets = self._staging_paths(record)
        if resume_status != "exported" or not exported_markdown.exists():
            exported_markdown, exported_assets = self._export_to_staging(record)
        return self._archive_from_staging(
            record,
            candidate_folders,
            exported_markdown=exported_markdown,
            exported_assets=exported_assets,
        )

    def _trash_previous_entry(self, entry: dict[str, Any], started_at: datetime) -> None:
        trash_root = self.state_paths.trash_dir / started_at.strftime("%Y%m%dT%H%M%SZ")
        relative_doc_path = entry.get("relative_doc_path")
        relative_asset_dir = entry.get("relative_asset_dir")

        if relative_doc_path:
            source_doc = self.documents_root / relative_doc_path
            if source_doc.exists():
                target_doc = trash_root / relative_doc_path
                target_doc.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_doc), str(target_doc))
                prune_empty_parents(source_doc.parent, self.documents_root)

        if relative_asset_dir:
            source_assets = self.documents_root / relative_asset_dir
            if source_assets.exists():
                target_assets = trash_root / relative_asset_dir
                target_assets.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_assets), str(target_assets))
                prune_empty_parents(source_assets.parent, self.documents_root)

    def _cleanup_staging(self, resume_state: dict[str, Any] | None = None) -> None:
        if self.state_paths.staging_dir.exists():
            if not resume_state:
                for child in list(self.state_paths.staging_dir.iterdir()):
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                return

            for document_key, document in resume_state.get("documents", {}).items():
                if document.get("status") in {"archived", "skipped"}:
                    staging_parent = self.state_paths.staging_dir / document_key.replace(":", "__")
                    if staging_parent.exists():
                        shutil.rmtree(staging_parent, ignore_errors=True)

    def _load_manifest(self) -> dict[str, Any]:
        if not self.state_paths.manifest_path.exists():
            return {}
        return json.loads(self.state_paths.manifest_path.read_text(encoding="utf-8"))

    def _backup_related_top_level_dirs(self, manifest: dict[str, Any]) -> list[Path]:
        related_names = set()
        for entry in (manifest.get("documents") or {}).values():
            classified_folder = entry.get("classified_folder")
            if classified_folder:
                related_names.add(classified_folder)
            source_space_name = entry.get("source_space_name")
            if source_space_name:
                related_names.add(source_space_name)
        paths: list[Path] = []
        for path in sorted(self.documents_root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir():
                continue
            if path.name.startswith(".") or path.name.startswith("_"):
                continue
            if path.name in DEFAULT_CATEGORY_EXCLUDES:
                continue
            if path.name in related_names or re.search(r"[^\x00-\x7F]", path.name):
                paths.append(path)
        return paths

    def _move_tree_without_overwrite(self, source: Path, target: Path, conflict_root: Path) -> None:
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in sorted(source.iterdir(), key=lambda item: item.name):
                self._move_tree_without_overwrite(child, target / child.name, conflict_root / child.name)
            try:
                source.rmdir()
            except OSError:
                pass
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            conflict_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(conflict_root))
        else:
            shutil.move(str(source), str(target))

    def normalize_folders(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        started_at = self.now_fn()
        conflict_root = self.state_paths.trash_dir / f"normalize-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
        renamed: list[dict[str, str]] = []
        related_dirs = self._backup_related_top_level_dirs(manifest)
        mappings: dict[str, str] = {}

        for path in related_dirs:
            if is_strict_two_word_slug(path.name):
                continue
            target_name = strict_two_word_slug(path.name)
            mappings[path.name] = target_name
            target_path = self.documents_root / target_name
            if path == target_path:
                continue
            target_path.mkdir(parents=True, exist_ok=True)
            self._move_tree_without_overwrite(path, target_path, conflict_root / path.name)
            renamed.append({"from": path.name, "to": target_name})
            prune_empty_parents(path, self.documents_root)

        if mappings:
            for bucket in ("documents", "deleted"):
                for entry in (manifest.get(bucket) or {}).values():
                    classified_folder = entry.get("classified_folder")
                    if classified_folder in mappings:
                        entry["classified_folder"] = mappings[classified_folder]
                    for field in ("relative_doc_path", "relative_asset_dir"):
                        value = entry.get(field)
                        if not value:
                            continue
                        for old_name, new_name in mappings.items():
                            prefix = f"{old_name}/"
                            if value == old_name or value.startswith(prefix):
                                entry[field] = value.replace(old_name, new_name, 1)
                                break
                    if bucket == "documents":
                        entry["classification_reason"] = "Normalized to strict two-word kebab-case."

            manifest["generated_at"] = started_at.isoformat()
            write_json(self.state_paths.manifest_path, manifest)

        return {
            "normalized": renamed,
            "conflict_trash_path": str(conflict_root),
            "manifest_path": str(self.state_paths.manifest_path),
        }

    def reset_resume(self) -> dict[str, Any]:
        if self.state_paths.resume_state_path.exists():
            self.state_paths.resume_state_path.unlink()
            return {"resume_state_path": str(self.state_paths.resume_state_path), "removed": True}
        return {"resume_state_path": str(self.state_paths.resume_state_path), "removed": False}



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up all accessible Feishu wiki spaces into ~/Documents.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("full", help="Re-export and reclassify all accessible documents.")
    subparsers.add_parser("sync", help="Incrementally sync all accessible documents.")
    subparsers.add_parser("authorize", help="Run Feishu OAuth and cache user tokens for future syncs.")
    subparsers.add_parser("normalize-folders", help="Normalize backup-generated folders to strict two-word kebab-case.")
    subparsers.add_parser("reset-resume", help="Delete the saved resume state and force a fresh run next time.")

    env_parser = subparsers.add_parser("write-env-template", help="Write the state env file template.")
    env_parser.add_argument("--overwrite", action="store_true")

    launchd_parser = subparsers.add_parser("install-launchd", help="Write the launchd plist for daily sync.")
    launchd_parser.add_argument("--load", action="store_true", help="Bootstrap and kickstart the launchd job.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "write-env-template":
        config = BackupConfig.from_env(
            require_app_credentials=False,
        )
    elif args.command == "install-launchd":
        config = BackupConfig.from_env(
            require_app_credentials=False,
        )
    elif args.command in {"normalize-folders", "reset-resume"}:
        config = BackupConfig.from_env(
            require_app_credentials=False,
        )
    else:
        config = BackupConfig.from_env()
    service = FeishuBackupService(config)

    if args.command in {"full", "sync"}:
        result = service.run(args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["failed"] == 0 else 1

    if args.command == "authorize":
        result = service.authorize()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "write-env-template":
        env_path = service.write_env_template(overwrite=args.overwrite)
        print(str(env_path))
        return 0

    if args.command == "install-launchd":
        plist_path = service.install_launchd(load=args.load)
        print(str(plist_path))
        return 0

    if args.command == "normalize-folders":
        result = service.normalize_folders()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "reset-resume":
        result = service.reset_resume()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
