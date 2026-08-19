import json
import tempfile
import unittest
from pathlib import Path

from webapp.knowledge_bases import KnowledgeBase
from webapp.settings import Settings
from webapp.trusted_document import TrustedDocumentLoader


class TrustedDocumentLoaderTest(unittest.TestCase):
    def test_loads_only_target_document_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "100"
            storage.mkdir()
            (storage / "kv_store_doc_status.json").write_text(
                json.dumps({"doc": {"file_path": "100.xls"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (storage / "kv_store_text_chunks.json").write_text(
                json.dumps(
                    {
                        "chunk-1": {"file_path": "100.xls", "content": "2024年7月保险业保费收入数据。"},
                        "chunk-2": {"file_path": "100.xls", "content": "保险业资产和负债指标。"},
                        "foreign": {"file_path": "999.xls", "content": "其他文档。"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = Settings(project_root=root, max_sources=8, max_context_chars=10000)
            loader = TrustedDocumentLoader(config)
            knowledge_base = KnowledgeBase(
                identifier="100",
                display_name="100_2024年7月保险业经营情况表_2024年7月保险业经营情况表.xls",
                storage_dir=storage,
            )
            result = loader.load(knowledge_base, "2024年7月保险业保费收入")
            self.assertEqual(result.target_file, "100.xls")
            self.assertEqual(result.total_chunks, 2)
            self.assertEqual(result.foreign_chunks, 1)
            self.assertGreaterEqual(len(result.sources), 1)
            self.assertNotIn("其他文档", result.context)
