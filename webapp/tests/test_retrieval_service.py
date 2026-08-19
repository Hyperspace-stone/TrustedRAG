import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from webapp.document_router import RouteResult
from webapp.evidence_cleaning import clean_evidence_content
from webapp.knowledge_bases import KnowledgeBase
from webapp.retrieval_service import (
    EmptyFinalContentError,
    ModelCompletion,
    RetrievalService,
    collapse_trusted_document_sources,
    normalize_source_citations,
    sanitize_answer_text,
    sanitize_reasoning_text,
)
from webapp.schemas import SourceItem
from webapp.settings import Settings
from webapp.trusted_document import TrustedDocumentContext


class RetrievalServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_list_content_analysis_keeps_only_document_list_items(self) -> None:
        content = (
            "List Content Analysis: Content: {'type': 'list', 'sub_type': 'text', "
            "'list_items': ['第一项真实正文。', '第二项真实正文。'], "
            "'bbox': [1, 2, 3, 4], 'page_idx': 2}\n\n"
            "Analysis: parser generated summary"
        )
        self.assertEqual(
            clean_evidence_content(content),
            "第一项真实正文。\n\n第二项真实正文。",
        )

    def test_hybrid_sources_exclude_chunks_owned_by_another_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kv_store_doc_status.json").write_text(
                '{"doc": {"file_path": "130.xlsx"}, "foreign": {"file_path": "999.xlsx"}}',
                encoding="utf-8",
            )
            knowledge_base = KnowledgeBase(
                "130",
                "130_2023年商业银行主要指标分机构类情况表.xlsx",
                root,
            )
            payload = {
                "data": {
                    "chunks": [
                        {"file_path": "999.xlsx", "content": "其他文件中的资本工具规则"},
                        {"file_path": "130.xlsx", "content": "外资银行不良贷款余额为119"},
                    ]
                }
            }
            sources = RetrievalService(Settings())._hybrid_sources(payload, knowledge_base)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].content, "外资银行不良贷款余额为119")
        self.assertEqual(sources[0].title, knowledge_base.display_name)

    def test_sanitize_answer_removes_raw_table_analysis_block(self) -> None:
        answer = (
            "[来源 1] Table Analysis: Image Path: /tmp/a.jpg Caption: 表格 Structure: 很长的表格数据\n\n"
            "原保险保费收入为 38732 亿元 [来源 1]。"
        )
        self.assertEqual(sanitize_answer_text(answer), "原保险保费收入为 38732 亿元 [来源 1]。")

    def test_sanitize_answer_preserves_normal_cited_answer(self) -> None:
        answer = "根据可信文档，该指标为 38732 亿元 [1]。"
        self.assertEqual(sanitize_answer_text(answer), answer)

    def test_sanitize_reasoning_hides_internal_metadata_paths_and_secrets(self) -> None:
        reasoning = (
            "先检查可信证据。\n"
            "Image Path: /root/private/a.jpg\n"
            "读取 /data1/internal/report.xlsx，api_key=test-value。\n"
            "因此应比较两列。"
        )
        cleaned = sanitize_reasoning_text(reasoning)
        self.assertNotIn("Image Path", cleaned)
        self.assertNotIn("/data1/", cleaned)
        self.assertNotIn("test-value", cleaned)
        self.assertIn("[内部路径已隐藏]", cleaned)
        self.assertIn("因此应比较两列", cleaned)

    def test_trusted_document_sources_collapse_to_one_file_index(self) -> None:
        sources = [
            SourceItem(id=1, title="128.xlsx", content="第一段证据"),
            SourceItem(id=2, title="128.xlsx", content="第二段证据"),
            SourceItem(id=3, title="128.xlsx", content="第二段证据"),
        ]
        answer, collapsed = collapse_trusted_document_sources(
            "结论一 [来源 2]，结论二 [来源 3]。",
            sources,
            max_content_chars=200,
        )
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0].id, 1)
        self.assertIn("第一段证据", collapsed[0].content)
        self.assertIn("第二段证据", collapsed[0].content)
        self.assertNotIn("来源 2", answer)
        self.assertNotIn("来源 3", answer)
        self.assertIn("[来源 1]", answer)

    def test_final_source_ids_and_answer_citations_are_aligned(self) -> None:
        sources = [SourceItem(id=2, title="130.xlsx", content="可信证据")]
        answer, normalized = normalize_source_citations(
            "结论由该文档支持 [1]，也可写作 [来源 1]。",
            sources,
        )
        self.assertEqual([source.id for source in normalized], [1])
        self.assertIn("[1]", answer)
        self.assertIn("[来源 1]", answer)

    def test_old_source_id_is_rewritten_to_new_contiguous_id(self) -> None:
        sources = [SourceItem(id=2, title="130.xlsx", content="可信证据")]
        answer, normalized = normalize_source_citations("结论 [来源 2]。", sources)
        self.assertEqual(normalized[0].id, 1)
        self.assertEqual(answer, "结论 [来源 1]。")

    def test_original_file_metadata_replaces_generic_source_title(self) -> None:
        source = SourceItem(id=1, title="Table Analysis:", content="evidence")
        knowledge_base = KnowledgeBase("100", "100_original_report.xls", Path("/tmp/100"))
        with patch(
            "webapp.retrieval_service.original_viewer_service.source_metadata",
            return_value={"original_file_name": "100.xls"},
        ):
            result = RetrievalService._attach_original_files([source], knowledge_base)
        self.assertEqual(result[0].title, "100_original_report.xls")
        self.assertEqual(result[0].original_file_name, "100.xls")

    async def test_unsupported_reasoning_mode_retries_plain_generation(self) -> None:
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        response = httpx.Response(400, request=request)
        unsupported = httpx.HTTPStatusError(
            "unsupported reasoning mode",
            request=request,
            response=response,
        )
        service = RetrievalService(Settings())
        service._request_generation_completion = AsyncMock(
            side_effect=[unsupported, ModelCompletion("普通生成回答。")]
        )
        effective = SimpleNamespace(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model_name="deepseek-chat",
        )
        with patch(
            "webapp.retrieval_service.model_settings_store.effective_generation",
            return_value=effective,
        ):
            result = await service._call_generation_llm("question", stream=False)
        self.assertEqual(result.content, "普通生成回答。")
        calls = service._request_generation_completion.await_args_list
        self.assertIs(calls[0].kwargs["enable_reasoning"], True)
        self.assertIs(calls[1].kwargs["enable_reasoning"], False)

    async def test_reasoning_without_final_content_retries_plain_generation(self) -> None:
        service = RetrievalService(Settings())
        service._request_generation_completion = AsyncMock(
            side_effect=[
                EmptyFinalContentError("reasoning budget exhausted"),
                ModelCompletion("关闭思考后的最终回答。"),
            ]
        )
        effective = SimpleNamespace(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model_name="deepseek-chat",
        )
        with patch(
            "webapp.retrieval_service.model_settings_store.effective_generation",
            return_value=effective,
        ):
            result = await service._call_generation_llm("question", stream=False)
        self.assertEqual(result.content, "关闭思考后的最终回答。")
        calls = service._request_generation_completion.await_args_list
        self.assertIs(calls[0].kwargs["enable_reasoning"], True)
        self.assertIs(calls[1].kwargs["enable_reasoning"], False)

    async def test_embedding_free_trusted_document_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge_base = KnowledgeBase("100", "100_example.xls", root)
            route = RouteResult(knowledge_base, "lexical", 0.8)
            document = TrustedDocumentContext(
                context="[来源 1] evidence",
                sources=[SourceItem(id=1, title="100_example.xls", content="evidence")],
                target_file="100.xls",
                total_chunks=1,
                selected_chunks=1,
                foreign_chunks=0,
            )
            config = Settings(project_root=root, embedding_base_url="", embedding_api_key="")
            service = RetrievalService(config)
            service._call_generation_llm = AsyncMock(
                return_value=ModelCompletion("基于 [来源 1] 的回答。", "分析可信证据。")
            )
            with patch("webapp.retrieval_service.document_router.route", new=AsyncMock(return_value=route)), patch(
                "webapp.retrieval_service.trusted_document_loader.load", return_value=document
            ):
                result = await service.answer("question", [], "")
            self.assertEqual(result.metadata["retrieval_mode"], "trusted_document")
            self.assertEqual(result.metadata["collection_id"], "100")
            self.assertEqual(len(result.sources), 1)
            self.assertEqual(result.reasoning, "分析可信证据。")
            service._call_generation_llm.assert_awaited_once()

    async def test_llm_failure_returns_extractive_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge_base = KnowledgeBase("100", "100_example.xls", root)
            route = RouteResult(knowledge_base, "lexical", 0.8)
            document = TrustedDocumentContext(
                context="[来源 1] evidence",
                sources=[SourceItem(id=1, title="100_example.xls", content="<p>trusted evidence</p>")],
                target_file="100.xls",
                total_chunks=1,
                selected_chunks=1,
                foreign_chunks=0,
            )
            config = Settings(project_root=root, embedding_base_url="", embedding_api_key="")
            service = RetrievalService(config)
            service._call_generation_llm = AsyncMock(side_effect=RuntimeError("expired"))
            with patch("webapp.retrieval_service.document_router.route", new=AsyncMock(return_value=route)), patch(
                "webapp.retrieval_service.trusted_document_loader.load", return_value=document
            ):
                result = await service.answer("question", [], "")
            self.assertEqual(result.metadata["generation_mode"], "extractive_fallback")
            self.assertIn("trusted evidence", result.answer)
            self.assertEqual(len(result.sources), 1)

    async def test_reverse_route_leads_with_document_name_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge_base = KnowledgeBase(
                "090",
                "090_2024年9月人身险公司经营情况表_2024年9月人身险公司经营情况表.xlsx",
                root,
            )
            route = RouteResult(
                knowledge_base,
                "evidence_reverse",
                0.95,
                evidence_terms=("原保险保费收入", "本年累计", "截至当期"),
                evidence_values=("34879",),
                evidence_excerpts=("原保险保费收入 本年累计/截至当期 34879",),
            )
            document = TrustedDocumentContext(
                context="[来源 1] 原保险保费收入为34879",
                sources=[SourceItem(id=1, title="090.xlsx", content="原保险保费收入为34879")],
                target_file="090.xlsx",
                total_chunks=1,
                selected_chunks=1,
                foreign_chunks=0,
            )
            service = RetrievalService(Settings(project_root=root, embedding_base_url="", embedding_api_key=""))
            service._call_generation_llm = AsyncMock(return_value=ModelCompletion("该值为34879 [来源 1]。"))
            with patch("webapp.retrieval_service.document_router.route", new=AsyncMock(return_value=route)), patch(
                "webapp.retrieval_service.trusted_document_loader.load", return_value=document
            ), patch(
                "webapp.retrieval_service.original_viewer_service.source_metadata",
                return_value={"original_file_name": "090.xlsx"},
            ):
                result = await service.answer(
                    '原保险保费收入在本年累计口径下为34879的是哪个文档',
                    [],
                    "",
                )
            self.assertTrue(result.answer.startswith("对应的文档是《090_"))
            self.assertEqual(result.metadata["routing_method"], "evidence_reverse")
            self.assertEqual(result.metadata["routing_evidence_values"], ["34879"])
