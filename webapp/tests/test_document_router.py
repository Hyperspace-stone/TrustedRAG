import tempfile
import unittest
import json
from pathlib import Path

from webapp.document_router import DocumentRouter, DocumentRoutingError
from webapp.knowledge_bases import KnowledgeBaseCatalog
from webapp.settings import Settings


class DocumentRouterTest(unittest.IsolatedAsyncioTestCase):
    def make_router(self, directory: str, *, min_score: float = 0.2, min_margin: float = 0.025) -> DocumentRouter:
        root = Path(directory)
        storage = root / "rag_storage"
        for identifier in ("001", "090", "100", "333", "500"):
            (storage / identifier).mkdir(parents=True)
        mapping = root / "name2id.json"
        mapping.write_text(
            "{"
            '"001_2026年银行业总资产、总负债（月度）_2026年银行业总资产、总负债（月度）.xls":1,'
            '"090_2024年9月人身险公司经营情况表_2024年9月人身险公司经营情况表.xlsx":90,'
            '"100_2024年7月保险业经营情况表_2024年7月保险业经营情况表.xls":100,'
            '"333_2020年7月保险业经营情况表_保险业经营情况表.xls":333,'
            '"500_商业银行主要监管指标情况表（季度）_商业银行主要监管指标情况表（季度）.xls":500'
            "}",
            encoding="utf-8",
        )
        config = Settings(
            project_root=root,
            name_map_path=mapping,
            storage_root=storage,
            route_min_score=min_score,
            route_min_margin=min_margin,
        )
        return DocumentRouter(KnowledgeBaseCatalog(config), config)

    @staticmethod
    def write_chunk(directory: str, identifier: str, content: str) -> None:
        path = Path(directory) / "rag_storage" / identifier / "kv_store_text_chunks.json"
        path.write_text(
            json.dumps(
                {f"chunk-{identifier}": {"content": content, "file_path": f"{identifier}.xlsx"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    async def test_book_title_and_manual_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            title = await router.route("请根据《2024年7月保险业经营情况表》回答")
            self.assertEqual(title.knowledge_base.identifier, "100")
            self.assertEqual(title.method, "book_title")
            manual = await router.route("任意问题", manual_collection="001")
            self.assertEqual(manual.knowledge_base.identifier, "001")
            self.assertEqual(manual.method, "manual")

    async def test_book_title_overrides_conflicting_manual_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            result = await router.route(
                "请根据《2024年7月保险业经营情况表》回答",
                manual_collection="333",
            )
            self.assertEqual(result.knowledge_base.identifier, "100")
            self.assertEqual(result.method, "book_title")

    async def test_option_number_is_never_treated_as_collection_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            question = (
                "根据 Excel 附件《2024年7月保险业经营情况表》，指标是多少？\n"
                '"option_a": 34878.78,\n'
                '"option_d": 333.3'
            )
            result = await router.route(question)
            self.assertEqual(result.knowledge_base.identifier, "100")
            self.assertEqual(result.method, "book_title")

    async def test_explicit_collection_id_requires_a_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            result = await router.route("请查询知识库编号 333")
            self.assertEqual(result.knowledge_base.identifier, "333")
            self.assertEqual(result.method, "explicit_id")

    async def test_free_form_banking_question_routes_lexically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            result = await router.route("请概括2024年7月保险业经营情况并列出关键指标")
            self.assertEqual(result.knowledge_base.identifier, "100")
            self.assertIn(result.method, {"contained_title", "lexical"})

    async def test_model_selection_is_limited_to_shortlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory, min_score=1.0, min_margin=1.0)

            async def selector(question, candidates):
                del question
                self.assertTrue(any(item.knowledge_base.identifier == "500" for item in candidates))
                return "500"

            result = await router.route("商业银行季度监管指标", model_selector=selector)
            self.assertEqual(result.knowledge_base.identifier, "500")
            self.assertEqual(result.method, "model_shortlist")

    async def test_reverse_evidence_routes_without_document_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory, min_score=1.0, min_margin=1.0)
            self.write_chunk(
                directory,
                "090",
                "2024年9月人身险公司经营情况表：原保险保费收入，本年累计/截至当期为34879。",
            )
            self.write_chunk(directory, "333", "2020年保险业经营情况表：原保险保费收入为30062。")
            result = await router.route(
                '“原保险保费收入”在“本年累计/截至当期”口径下的数值是34879的是哪个文档'
            )
            self.assertEqual(result.knowledge_base.identifier, "090")
            self.assertEqual(result.method, "evidence_reverse")
            self.assertIn("34879", result.evidence_values)

    async def test_unrelated_question_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = self.make_router(directory)
            with self.assertRaises(DocumentRoutingError):
                await router.route("请推荐今天晚上吃什么")
