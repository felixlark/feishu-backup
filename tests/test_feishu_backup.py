import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx

from feishu_backup import (
    BackupConfig,
    ClassificationResult,
    FeishuBackupService,
    NamePathArchiveClassifier,
    OllamaArchiveClassifier,
    SpaceRecord,
    StatefulFeishuExporter,
    WebNode,
    sanitize_segment,
)


@dataclass
class FakeNode:
    node_token: str
    obj_token: str
    obj_type: str
    title: str
    parent_node_token: str | None = None
    obj_edit_time: str | None = None
    has_child: bool = False


@dataclass
class FakeSpace:
    space_id: str
    name: str


class FakeWikiAPI:
    def __init__(self, nodes_by_space: dict[str, dict[str | None, list[FakeNode]]]):
        self.nodes_by_space = nodes_by_space
        self.failures_by_parent: dict[tuple[str, str | None], int] = {}

    def get_all_space_nodes(self, *, space_id: str, access_token: str, parent_node_token: str | None = None):
        key = (space_id, parent_node_token)
        remaining = self.failures_by_parent.get(key, 0)
        if remaining > 0:
            self.failures_by_parent[key] = remaining - 1
            raise RuntimeError("获取知识空间子节点列表失败")
        return list(self.nodes_by_space.get(space_id, {}).get(parent_node_token, []))


class FakeSDK:
    def __init__(self, wiki: FakeWikiAPI):
        self.wiki = wiki


class FakeExporter:
    def __init__(
        self,
        *,
        spaces: list[FakeSpace],
        nodes_by_space: dict[str, dict[str | None, list[FakeNode]]],
        markdown_by_obj_token: dict[str, str] | None = None,
    ):
        self.spaces = spaces
        self.sdk = FakeSDK(FakeWikiAPI(nodes_by_space))
        self.markdown_by_obj_token = markdown_by_obj_token or {}
        self.calls: list[dict[str, str]] = []

    def get_access_token(self) -> str:
        return "tenant-token"

    def list_spaces(self, *, access_token: str):
        return list(self.spaces)

    def export(self, *, url: str, output_dir: Path, filename: str, table_format: str, silent: bool):
        self.calls.append({"url": url, "output_dir": str(output_dir), "filename": filename})
        obj_token = url.rstrip("/").split("/")[-1]
        output_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = output_dir / f"{filename}.md"
        asset_dir = output_dir / filename
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / "image.png").write_bytes(b"fake-image")
        markdown = self.markdown_by_obj_token.get(
            obj_token,
            f"# {filename}\n\n![image]({filename}/image.png)\n",
        )
        markdown_path.write_text(markdown, encoding="utf-8")
        return markdown_path


class FakeOAuthExporter(FakeExporter):
    def __init__(self):
        super().__init__(spaces=[], nodes_by_space={})
        self.token_calls = 0

    def get_access_token(self) -> str:
        self.token_calls += 1
        return "user_access_token_example"


class FakeClassifier:
    def __init__(self, mapping: dict[str, ClassificationResult]):
        self.mapping = mapping
        self.calls: list[dict[str, object]] = []

    def classify(self, record, markdown_text: str, candidate_folders):
        self.calls.append(
            {
                "title": record.title,
                "candidate_folders": list(candidate_folders),
                "space_name": record.source_space_name,
            }
        )
        if record.title not in self.mapping:
            raise AssertionError(f"Missing classifier mapping for {record.title}")
        return self.mapping[record.title]


class FakeWebClient:
    def __init__(self, spaces: list[SpaceRecord], nodes_by_space: dict[str, list[WebNode]]):
        self.spaces = spaces
        self.nodes_by_space = nodes_by_space
        self.ensure_available_calls = 0

    def ensure_available(self):
        self.ensure_available_calls += 1

    def list_spaces(self) -> list[SpaceRecord]:
        return list(self.spaces)

    def list_space_nodes(self, space: SpaceRecord) -> list[WebNode]:
        return list(self.nodes_by_space.get(space.space_id, []))


class FailingAvailabilityClassifier(FakeClassifier):
    def ensure_available(self):
        raise RuntimeError("Ollama service is unavailable at http://127.0.0.1:11434. Start it with `ollama serve`.")


class FeishuBackupServiceTestCase(unittest.TestCase):
    def make_service(
        self,
        *,
        documents_root: Path,
        state_root: Path,
        spaces: list[FakeSpace],
        nodes_by_space: dict[str, dict[str | None, list[FakeNode]]],
        classifier_mapping: dict[str, ClassificationResult],
        markdown_by_obj_token: dict[str, str] | None = None,
        now: datetime,
    ) -> tuple[FeishuBackupService, FakeExporter, FakeClassifier]:
        exporter = FakeExporter(
            spaces=spaces,
            nodes_by_space=nodes_by_space,
            markdown_by_obj_token=markdown_by_obj_token,
        )
        classifier = FakeClassifier(classifier_mapping)
        config = BackupConfig(
            app_id="app-id",
            app_secret="app-secret",
            documents_root=documents_root,
            state_root=state_root,
        )
        service = FeishuBackupService(config, exporter=exporter, classifier=classifier, now_fn=lambda: now)
        return service, exporter, classifier

    def test_full_backup_discovers_multiple_spaces_and_archives_into_documents(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            (documents_root / "ops").mkdir()

            service, exporter, classifier = self.make_service(
                documents_root=documents_root,
                state_root=state_root,
                spaces=[
                    FakeSpace(space_id="space-a", name="Ops Wiki"),
                    FakeSpace(space_id="space-b", name="Product Wiki"),
                ],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]
                    },
                    "space-b": {
                        None: [FakeNode("node-2", "obj-2", "docx", "Roadmap", obj_edit_time="200")]
                    },
                },
                classifier_mapping={
                    "Runbook": ClassificationResult(
                        folder_name="ops",
                        matched_existing_folder=True,
                        reason="Operational content belongs with ops.",
                    ),
                    "Roadmap": ClassificationResult(
                        folder_name="product-roadmap",
                        matched_existing_folder=False,
                        reason="No existing folder matches product planning notes.",
                    ),
                },
                now=datetime(2026, 3, 30, 4, 0, tzinfo=timezone.utc),
            )

            result = service.run("full")

            self.assertEqual(result["space_count"], 2)
            self.assertEqual(result["document_count"], 2)
            self.assertEqual(result["new"], 2)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["created_folders"], ["product-roadmap"])
            self.assertTrue(Path(result["summary_path"]).exists())
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertEqual(len(exporter.calls), 2)
            self.assertEqual(classifier.calls[0]["candidate_folders"], [])
            self.assertEqual(classifier.calls[1]["candidate_folders"], [])

            ops_doc = documents_root / "ops" / "Ops Wiki" / "Runbook.md"
            product_doc = documents_root / "product-roadmap" / "Product Wiki" / "Roadmap.md"
            self.assertTrue(ops_doc.exists())
            self.assertTrue(product_doc.exists())
            self.assertIn("Runbook/image.png", ops_doc.read_text(encoding="utf-8"))
            self.assertIn("Roadmap/image.png", product_doc.read_text(encoding="utf-8"))

            manifest = json.loads((state_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stats"]["space_count"], 2)
            self.assertEqual(manifest["documents"]["space-a:node-1"]["classified_folder"], "ops")
            self.assertEqual(manifest["documents"]["space-b:node-2"]["classified_folder"], "product-roadmap")
            self.assertEqual(manifest["documents"]["space-b:node-2"]["source_space_name"], "Product Wiki")

    def test_sync_updates_reclassified_document_and_trashes_previous_location(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            (documents_root / "ops").mkdir()
            (documents_root / "projects").mkdir()

            full_service, _, _ = self.make_service(
                documents_root=documents_root,
                state_root=state_root,
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]
                    }
                },
                classifier_mapping={
                    "Runbook": ClassificationResult(
                        folder_name="ops",
                        matched_existing_folder=True,
                        reason="Operational note.",
                    )
                },
                now=datetime(2026, 3, 30, 4, 0, tzinfo=timezone.utc),
            )
            full_service.run("full")

            sync_service, sync_exporter, _ = self.make_service(
                documents_root=documents_root,
                state_root=state_root,
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="101")]
                    }
                },
                classifier_mapping={
                    "Runbook": ClassificationResult(
                        folder_name="projects",
                        matched_existing_folder=True,
                        reason="Moved under project execution.",
                    )
                },
                now=datetime(2026, 3, 30, 5, 0, tzinfo=timezone.utc),
            )

            result = sync_service.run("sync")

            self.assertEqual(result["updated"], 1)
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(len(sync_exporter.calls), 1)
            self.assertFalse((documents_root / "ops" / "Ops Wiki" / "Runbook.md").exists())
            self.assertTrue((documents_root / "projects" / "Ops Wiki" / "Runbook.md").exists())

            trash_root = state_root / "trash" / "20260330T050000Z"
            self.assertTrue((trash_root / "ops" / "Ops Wiki" / "Runbook.md").exists())
            self.assertTrue((trash_root / "ops" / "Ops Wiki" / "Runbook" / "image.png").exists())

    def test_sync_removes_deleted_document_from_documents_and_tracks_tombstone(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            (documents_root / "ops").mkdir()

            full_service, _, _ = self.make_service(
                documents_root=documents_root,
                state_root=state_root,
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]
                    }
                },
                classifier_mapping={
                    "Runbook": ClassificationResult(
                        folder_name="ops",
                        matched_existing_folder=True,
                        reason="Operational note.",
                    )
                },
                now=datetime(2026, 3, 30, 4, 0, tzinfo=timezone.utc),
            )
            full_service.run("full")

            sync_service, _, _ = self.make_service(
                documents_root=documents_root,
                state_root=state_root,
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={"space-a": {None: []}},
                classifier_mapping={},
                now=datetime(2026, 3, 30, 6, 0, tzinfo=timezone.utc),
            )
            result = sync_service.run("sync")

            self.assertEqual(result["deleted"], 1)
            self.assertFalse((documents_root / "ops" / "Ops Wiki" / "Runbook.md").exists())
            trash_root = state_root / "trash" / "20260330T060000Z"
            self.assertTrue((trash_root / "ops" / "Ops Wiki" / "Runbook.md").exists())

            manifest = json.loads((state_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stats"]["active_documents"], 0)
            self.assertIn("space-a:node-1", manifest["deleted"])

    def test_run_fails_fast_when_classifier_backend_is_unavailable(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            exporter = FakeExporter(
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={"space-a": {None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]}},
            )
            classifier = FailingAvailabilityClassifier({})
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=documents_root,
                state_root=state_root,
            )
            service = FeishuBackupService(config, exporter=exporter, classifier=classifier)

            with self.assertRaisesRegex(RuntimeError, "ollama serve"):
                service.run("sync")
            self.assertEqual(exporter.calls, [])

    def test_web_session_fallback_discovers_spaces_and_exports_wiki_urls(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            (documents_root / "ops").mkdir()

            exporter = FakeExporter(spaces=[], nodes_by_space={})
            classifier = FakeClassifier(
                {
                    "Runbook": ClassificationResult(
                        folder_name="ops",
                        matched_existing_folder=True,
                        reason="Operational note.",
                    )
                }
            )
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=documents_root,
                state_root=state_root,
                feishu_auth_mode="tenant",
                base_url="https://feishu.cn",
                web_base_url="https://xmu-mars.feishu.cn",
                web_session_cookie="session=abc",
                web_csrf_token="csrf-token",
            )
            service = FeishuBackupService(
                config,
                exporter=exporter,
                classifier=classifier,
                now_fn=lambda: datetime(2026, 3, 30, 7, 0, tzinfo=timezone.utc),
            )
            service._web_client = FakeWebClient(
                spaces=[SpaceRecord(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": [
                        WebNode(
                            node_token="wiki-node-1",
                            obj_token="obj-1",
                            title="Runbook",
                            obj_type="wiki",
                            parent_node_token=None,
                            has_child=False,
                            obj_edit_time="123",
                        )
                    ]
                },
            )

            result = service.run("sync")

            self.assertEqual(result["space_count"], 1)
            self.assertEqual(result["document_count"], 1)
            self.assertEqual(result["new"], 1)
            self.assertEqual(exporter.calls[0]["url"], "https://xmu-mars.feishu.cn/wiki/wiki-node-1")
            self.assertEqual(service.web_client.ensure_available_calls, 1)
            self.assertTrue((documents_root / "ops" / "Ops Wiki" / "Runbook.md").exists())

    def test_install_launchd_writes_stable_plist_and_uses_state_root_env(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            launch_agents = state_root / "Library" / "LaunchAgents"

            with patch("pathlib.Path.home", return_value=state_root):
                config = BackupConfig(
                    app_id="app-id",
                    app_secret="app-secret",
                    documents_root=documents_root,
                    state_root=state_root,
                    launchd_label="com.example.feishu-backup",
                )
                service = FeishuBackupService(
                    config,
                    exporter=FakeExporter(spaces=[], nodes_by_space={}),
                    classifier=FakeClassifier({}),
                )
                service.write_env_template(overwrite=True)

                plist_path = service.install_launchd(load=False)
                self.assertEqual(plist_path, launch_agents / "com.example.feishu-backup.plist")

                payload = plistlib.loads(plist_path.read_bytes())
                self.assertEqual(payload["Label"], "com.example.feishu-backup")
                self.assertEqual(payload["ProgramArguments"][-1], "sync")
                self.assertEqual(payload["WorkingDirectory"], str(Path(__file__).resolve().parents[1]))
                self.assertEqual(payload["EnvironmentVariables"]["STATE_ROOT"], str(state_root))

    def test_backup_config_can_bootstrap_template_without_existing_secrets(self):
        with tempfile.TemporaryDirectory() as state_dir:
            state_root = Path(state_dir)
            with patch.dict("os.environ", {"STATE_ROOT": str(state_root)}, clear=True):
                config = BackupConfig.from_env(
                    require_app_credentials=False,
                )
            self.assertEqual(config.state_root, state_root)
            self.assertTrue(str(config.documents_root).endswith("Documents"))
            self.assertEqual(config.feishu_auth_mode, "oauth")
            self.assertEqual(config.feishu_docx_cache_dir, state_root / "feishu-docx-auth")
            self.assertEqual(config.ollama_timeout_seconds, 180.0)

    def test_service_uses_stateful_feishu_exporter_for_oauth(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=Path(documents_dir),
                state_root=Path(state_dir),
                feishu_auth_mode="oauth",
                oauth_redirect_port=9542,
            )
            service = FeishuBackupService(config, classifier=FakeClassifier({}))

            exporter = service.exporter

            self.assertIsInstance(exporter, StatefulFeishuExporter)
            self.assertEqual(exporter.auth_mode, "oauth")
            self.assertEqual(exporter.cache_dir, config.feishu_docx_cache_dir)
            self.assertEqual(exporter.redirect_port, 9542)
            self.assertEqual(service.config.ollama_timeout_seconds, 180.0)

    def test_service_defaults_to_name_path_classifier(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=Path(documents_dir),
                state_root=Path(state_dir),
            )
            service = FeishuBackupService(config)
            self.assertIsInstance(service.classifier, NamePathArchiveClassifier)

    def test_authorize_uses_exporter_token_and_reports_cache_dir(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            state_root = Path(state_dir)
            fake_exporter = FakeOAuthExporter()
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=Path(documents_dir),
                state_root=state_root,
                feishu_auth_mode="oauth",
            )
            service = FeishuBackupService(config, exporter=fake_exporter, classifier=FakeClassifier({}))
            cache_file = config.feishu_docx_cache_dir / "token.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("{}", encoding="utf-8")

            result = service.authorize()

            self.assertEqual(fake_exporter.token_calls, 1)
            self.assertEqual(result["auth_mode"], "oauth")
            self.assertEqual(result["cache_dir"], str(config.feishu_docx_cache_dir))
            self.assertEqual(result["cache_files"], [str(cache_file)])

    def test_normalize_folders_moves_backup_related_directories_and_updates_manifest(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            legacy_root = documents_root / "丽娟的知识库"
            legacy_root.mkdir()
            (legacy_root / "首页.md").write_text("# home\n", encoding="utf-8")
            manifest_path = state_root / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "documents": {
                            "doc-1": {
                                "classified_folder": "丽娟的知识库",
                                "relative_doc_path": "丽娟的知识库/首页.md",
                                "relative_asset_dir": "丽娟的知识库/首页",
                                "source_space_name": "丽娟的知识库",
                            }
                        },
                        "deleted": {},
                        "stats": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            service = FeishuBackupService(
                BackupConfig(
                    app_id="app-id",
                    app_secret="app-secret",
                    documents_root=documents_root,
                    state_root=state_root,
                )
            )

            result = service.normalize_folders()

            self.assertTrue((documents_root / "lijuan-docs" / "首页.md").exists())
            self.assertFalse(legacy_root.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["documents"]["doc-1"]["classified_folder"], "lijuan-docs")
            self.assertTrue(result["normalized"])

    def test_reset_resume_removes_resume_state_file(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            state_root = Path(state_dir)
            resume_state_path = state_root / "resume-state.json"
            resume_state_path.parent.mkdir(parents=True, exist_ok=True)
            resume_state_path.write_text("{}", encoding="utf-8")
            service = FeishuBackupService(
                BackupConfig(
                    app_id="app-id",
                    app_secret="app-secret",
                    documents_root=Path(documents_dir),
                    state_root=state_root,
                )
            )

            result = service.reset_resume()

            self.assertTrue(result["removed"])
            self.assertFalse(resume_state_path.exists())

    def test_collect_space_records_retries_rate_limited_node_listing(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            exporter = FakeExporter(
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]
                    }
                },
            )
            exporter.sdk.wiki.failures_by_parent[("space-a", None)] = 2
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=Path(documents_dir),
                state_root=Path(state_dir),
            )
            service = FeishuBackupService(config, exporter=exporter, classifier=FakeClassifier({}))

            with patch("feishu_backup.time.sleep", return_value=None):
                records = service._collect_space_records(
                    SpaceRecord(space_id="space-a", name="Ops Wiki"),
                    access_token="tenant-token",
                )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].title, "Runbook")

    def test_run_emits_realtime_progress(self):
        with tempfile.TemporaryDirectory() as documents_dir, tempfile.TemporaryDirectory() as state_dir:
            documents_root = Path(documents_dir)
            state_root = Path(state_dir)
            exporter = FakeExporter(
                spaces=[FakeSpace(space_id="space-a", name="Ops Wiki")],
                nodes_by_space={
                    "space-a": {
                        None: [FakeNode("node-1", "obj-1", "docx", "Runbook", obj_edit_time="100")]
                    }
                },
            )
            classifier = FakeClassifier(
                {
                    "Runbook": ClassificationResult(
                        folder_name="ops-wiki",
                        matched_existing_folder=False,
                        reason="Fallback to space folder.",
                    )
                }
            )
            config = BackupConfig(
                app_id="app-id",
                app_secret="app-secret",
                documents_root=documents_root,
                state_root=state_root,
            )
            service = FeishuBackupService(
                config,
                exporter=exporter,
                classifier=classifier,
                now_fn=lambda: datetime(2026, 3, 31, 0, 0, tzinfo=timezone.utc),
            )

            stream = StringIO()
            with redirect_stdout(stream):
                result = service.run("sync")

            output = stream.getvalue()
            self.assertEqual(result["failed"], 0)
            self.assertTrue(Path(result["progress_log_path"]).exists())
            self.assertIn("[backup] progress log:", output)
            self.assertIn("[backup] start mode=sync", output)
            self.assertIn("[space 1/1] scan Ops Wiki (space-a)", output)
            self.assertIn("[doc 1/1] start Runbook (space-a:node-1)", output)
            self.assertIn("[new 1/1] Runbook -> ops-wiki", output)
            self.assertIn("[backup] done spaces=1 docs=1 new=1", output)

    def test_sanitize_segment_strips_invalid_characters(self):
        self.assertEqual(sanitize_segment(' Ops:/Runbook* '), "Ops__Runbook_")


class OllamaArchiveClassifierTestCase(unittest.TestCase):
    def make_client(self, handler):
        transport = httpx.MockTransport(handler)
        return httpx.Client(base_url="http://127.0.0.1:11434", transport=transport)

    def make_record(self):
        return type(
            "Record",
            (),
            {
                "source_space_name": "Ops Wiki",
                "title": "Runbook",
                "source_path_hint": "Runbook",
                "obj_type": "docx",
            },
        )()

    def test_ensure_available_and_classify_existing_folder(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:7b"}]})
            if request.url.path == "/api/generate":
                return httpx.Response(
                    200,
                    json={
                        "response": json.dumps(
                            {
                                "matched_existing_folder": True,
                                "folder_name": "ops",
                                "reason": "Closest semantic match.",
                            }
                        )
                    },
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        classifier.ensure_available()
        result = classifier.classify(self.make_record(), "# Runbook", ["ops", "projects"])

        self.assertTrue(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "ops")

    def test_classify_new_folder_slugifies_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:7b"}]})
            if request.url.path == "/api/generate":
                return httpx.Response(
                    200,
                    json={
                        "response": json.dumps(
                            {
                                "matched_existing_folder": False,
                                "folder_name": "Product Roadmap",
                                "reason": "No existing folder fits.",
                            }
                        )
                    },
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        result = classifier.classify(self.make_record(), "# Runbook", ["ops"])
        self.assertFalse(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "product-roadmap")

    def test_classify_rejects_invalid_json(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:7b"}]})
            if request.url.path == "/api/generate":
                return httpx.Response(200, json={"response": "not-json"})
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        with self.assertRaisesRegex(RuntimeError, "Failed to parse Ollama classification response"):
            classifier.classify(self.make_record(), "# Runbook", ["ops"])

    def test_classify_falls_back_when_existing_folder_is_unknown(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:7b"}]})
            if request.url.path == "/api/generate":
                return httpx.Response(
                    200,
                    json={
                        "response": json.dumps(
                            {
                                "matched_existing_folder": True,
                                "folder_name": "finance",
                                "reason": "Wrong.",
                            }
                        )
                    },
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        result = classifier.classify(self.make_record(), "# Runbook", ["ops"])
        self.assertTrue(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "ops")
        self.assertIn("Fallback matched existing folder", result.reason)

    def test_classify_falls_back_to_space_slug_when_folder_name_is_blank(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "qwen2.5:7b"}]})
            if request.url.path == "/api/generate":
                return httpx.Response(
                    200,
                    json={
                        "response": json.dumps(
                            {
                                "matched_existing_folder": True,
                                "folder_name": " ",
                                "reason": "Model output was incomplete.",
                            }
                        )
                    },
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        result = classifier.classify(self.make_record(), "# Runbook", ["finance"])
        self.assertFalse(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "ops-wiki")
        self.assertIn("Fallback created a folder", result.reason)

    def test_ensure_available_reports_missing_model(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"model": "llama3.1:8b"}]})
            raise AssertionError(f"Unexpected request: {request.url}")

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        with self.assertRaisesRegex(RuntimeError, "ollama pull qwen2.5:7b"):
            classifier.ensure_available()

    def test_ensure_available_reports_unreachable_service(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("boom", request=request)

        classifier = OllamaArchiveClassifier(
            "http://127.0.0.1:11434",
            "qwen2.5:7b",
            http_client=self.make_client(handler),
        )
        with self.assertRaisesRegex(RuntimeError, "ollama serve"):
            classifier.ensure_available()


class NamePathArchiveClassifierTestCase(unittest.TestCase):
    def make_record(self, *, title: str, source_space_name: str, source_path_hint: str):
        return type(
            "Record",
            (),
            {
                "source_space_name": source_space_name,
                "title": title,
                "source_path_hint": source_path_hint,
                "obj_type": "docx",
            },
        )()

    def test_matches_existing_folder_from_title(self):
        classifier = NamePathArchiveClassifier()
        record = self.make_record(
            title="Impact Assessment Report",
            source_space_name="Liujuan Knowledge Base",
            source_path_hint="Impact Assessment Report",
        )
        result = classifier.classify(record, "# ignored", ["fire-impact-assessment", "attachments"])
        self.assertTrue(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "fire-impact-assessment")

    def test_matches_existing_folder_from_source_path(self):
        classifier = NamePathArchiveClassifier()
        record = self.make_record(
            title="Meeting Notes",
            source_space_name="Ops Wiki",
            source_path_hint="attachments/Meeting Notes",
        )
        result = classifier.classify(record, "# ignored", ["attachments", "ops"])
        self.assertTrue(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "attachments")

    def test_falls_back_to_source_space_name(self):
        classifier = NamePathArchiveClassifier()
        record = self.make_record(
            title="Unmatched Document",
            source_space_name="丽娟的知识库",
            source_path_hint="misc/Unmatched Document",
        )
        result = classifier.classify(record, "# ignored", ["attachments", "ops"])
        self.assertFalse(result.matched_existing_folder)
        self.assertEqual(result.folder_name, "lijuan-docs")


if __name__ == "__main__":
    unittest.main()
