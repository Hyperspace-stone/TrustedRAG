import json
import tempfile
import unittest
from pathlib import Path

from webapp.corpus_catalog import CorpusCatalog, CorpusCatalogError
from webapp.knowledge_bases import KnowledgeBaseCatalog
from webapp.settings import Settings


class CorpusCatalogTest(unittest.TestCase):
    def make_catalog(self, directory: str) -> CorpusCatalog:
        root = Path(directory)
        storage = root / "rag_storage"
        corpus = root / "corpus"
        storage.mkdir()
        corpus.mkdir()
        for identifier in ("001", "394"):
            (storage / identifier).mkdir()
        mapping = root / "name2id.json"
        mapping.write_text(
            json.dumps({"001_first.xls": 1, "394_duplicate.xlsx": 394}),
            encoding="utf-8",
        )
        (corpus / "001.xls").write_bytes(b"xls")
        (corpus / "394.xls").write_bytes(b"old")
        (corpus / "394.xlsx").write_bytes(b"new")
        config = Settings(
            project_root=root,
            name_map_path=mapping,
            storage_root=storage,
            corpus_root=corpus,
        )
        kb_catalog = KnowledgeBaseCatalog(config)
        return CorpusCatalog(config, kb_catalog)

    def test_resolves_numeric_id_and_prefers_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            first = catalog.resolve("1")
            self.assertEqual(first.collection_id, "001")
            duplicate = catalog.resolve("394")
            self.assertEqual(duplicate.preferred.name, "394.xlsx")
            self.assertEqual([item.name for item in duplicate.variants], ["394.xlsx", "394.xls"])

    def test_rejects_unknown_and_traversal_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            for value in ("", "abc", "../001", "1000", "999"):
                with self.subTest(value=value):
                    with self.assertRaises(CorpusCatalogError):
                        catalog.resolve(value)

    def test_rejects_unlisted_variant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.make_catalog(directory)
            with self.assertRaises(CorpusCatalogError):
                catalog.resolve("001").resolve_variant("../../etc/passwd")
