#!/usr/bin/env python3
"""Inspect one RAGAnything retrieval and expose an auditable query trace.

The script defaults to QA item Q004 because that item previously returned C
while the dataset label is A. It does not rebuild or modify the index. It can:

1. audit the selected LightRAG storage completely offline;
2. print the matched source file and suspicious cross-document references;
3. show the exact retrieved entities, relationships, chunks, and final prompt;
4. trace API-visible LLM inputs and outputs without logging credentials.

Examples:
    python examples/retrieval_xzl.py --offline
    python examples/retrieval_xzl.py
    python examples/retrieval_xzl.py --question-id Q081 --mode hybrid
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import traceback
from collections import Counter
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env", override=False)


def section(title: str) -> None:
    """Print a visible section boundary."""

    print(f"\n{'=' * 24} {title} {'=' * 24}")


def clip_text(value: Any, limit: int | None) -> str:
    """Convert a value to text and optionally truncate very large output."""

    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if limit is None or len(text) <= limit:
        return text
    omitted = len(text) - limit
    suffix = (
        f"\n... [已截断 {omitted} 个字符，使用 --no-truncate 查看完整内容]"
    )
    return f"{text[:limit]}{suffix}"


def print_value(title: str, value: Any, limit: int | None) -> None:
    """Print a titled string or JSON-compatible value."""

    section(title)
    print(clip_text(value, limit))


def load_json(path: Path) -> Any:
    """Load a UTF-8 JSON file with a useful missing-file error."""

    if not path.is_file():
        raise FileNotFoundError(f"找不到文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_question(qa_path: Path, question_id: str) -> dict[str, Any]:
    """Return one QA record by its id."""

    questions = load_json(qa_path)
    for question in questions:
        if question.get("id") == question_id:
            return question
    available = ", ".join(str(item.get("id")) for item in questions[:10])
    raise KeyError(f"QA 中不存在 {question_id}；前几个 id: {available}")


def extract_book_name(question: str) -> str | None:
    """Extract the first title enclosed by Chinese book-title brackets."""

    match = re.search(r"《(.*?)》", question)
    return match.group(1).strip() if match else None


def match_source_file(
    question: dict[str, Any], file_names: list[str]
) -> tuple[str, str]:
    """Match the source file without making an embedding API call.

    The original script prioritizes the title inside 《...》. The QA dataset also
    includes ``source_title`` and ``file_label``, which are deterministic
    fallbacks and are more appropriate for a diagnostic reproduction.
    """

    question_text = str(question.get("question", ""))
    targets = [
        extract_book_name(question_text),
        question.get("source_title"),
        Path(str(question.get("file_label", ""))).stem or None,
    ]

    for target in targets:
        if not target:
            continue
        candidates = [
            name
            for name in file_names
            if target in Path(name).stem or Path(name).stem in target
        ]
        if candidates:
            candidates.sort(key=lambda name: (len(Path(name).stem), name))
            return candidates[0], str(target)

    raise LookupError(
        "无法按书名号、source_title 或 file_label 匹配文件。"
        "这时应单独检查原脚本的 embedding 文件名回退逻辑。"
    )


def build_qa_prompt(question: dict[str, Any]) -> str:
    """Build the same multiple-choice prompt used by raganything_test.py."""

    return (
        f"{question['question']}\n"
        f"A. {question['option_a']}\n"
        f"B. {question['option_b']}\n"
        f"C. {question['option_c']}\n"
        f"D. {question['option_d']}\n"
        "单选题，只需回答选项，返回内容只要一个大写的选项字母，"
        "其余任何都不要返回。"
    )


def normalize_answer(answer: Any) -> str:
    """Extract the first standalone A-D option from an answer."""

    text = str(answer or "").strip().upper()
    match = re.search(r"(?<![A-Z])[ABCD](?![A-Z])", text)
    return match.group(0) if match else text


def storage_file_name(matched_file: str) -> str:
    """Map a corpus name such as 137_title.xlsx to LightRAG's 137.xlsx."""

    path = Path(matched_file)
    return f"{path.name[:3]}{path.suffix.lower()}"


def _load_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = load_json(path)
    return data if isinstance(data, dict) else {}


def _vector_items(storage_dir: Path, name: str) -> list[dict[str, Any]]:
    data = _load_optional_json(storage_dir / name)
    items = data.get("data", [])
    return [item for item in items if isinstance(item, dict)]


def _number_occurrences(text: str, number: str) -> int:
    pattern = rf"(?<![\d.]){re.escape(number)}(?![\d.])"
    return len(re.findall(pattern, text))


def option_evidence(question: dict[str, Any], content: str) -> list[dict[str, Any]]:
    """Show whether exact or rounded option values exist in indexed chunks."""

    diagnostics = []
    for letter, key in zip("ABCD", ("option_a", "option_b", "option_c", "option_d")):
        value = question[key]
        exact = str(value)
        rounded = str(round(float(value)))
        diagnostics.append(
            {
                "option": letter,
                "value": value,
                "exact_occurrences": _number_occurrences(content, exact),
                "rounded_value": rounded,
                "rounded_occurrences": _number_occurrences(content, rounded),
            }
        )
    return diagnostics


def _relevant_table_rows(question: dict[str, Any], content: str) -> list[list[str]]:
    rows: list[list[str]] = []
    quoted_terms = re.findall(r"“([^”]+)”", str(question.get("question", "")))
    option_keys = ("option_a", "option_b", "option_c", "option_d")
    option_values = {str(question[key]) for key in option_keys}
    option_values.update(str(round(float(question[key]))) for key in option_keys)

    for row_html in re.findall(r"<tr>(.*?)</tr>", content, flags=re.DOTALL):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<.*?>", "", cell)).strip()
            for cell in re.findall(r"<td>(.*?)</td>", row_html, flags=re.DOTALL)
        ]
        row_text = " | ".join(cells)
        if any(term in row_text for term in quoted_terms) or any(
            value in row_text for value in option_values
        ):
            rows.append(cells)
    return rows


def audit_storage(
    storage_dir: Path,
    target_file: str,
    question: dict[str, Any],
) -> dict[str, Any]:
    """Inspect index isolation, source references, and numeric precision offline."""

    if not storage_dir.is_dir():
        raise FileNotFoundError(f"LightRAG 工作目录不存在: {storage_dir}")

    doc_status = _load_optional_json(storage_dir / "kv_store_doc_status.json")
    text_chunks = _load_optional_json(storage_dir / "kv_store_text_chunks.json")
    chunk_items = _vector_items(storage_dir, "vdb_chunks.json")
    entity_items = _vector_items(storage_dir, "vdb_entities.json")
    relation_items = _vector_items(storage_dir, "vdb_relationships.json")

    document_paths = [
        value.get("file_path")
        for value in doc_status.values()
        if isinstance(value, dict) and value.get("file_path")
    ]
    foreign_documents = sorted(
        {path for path in document_paths if path != target_file}
    )

    target_chunks = []
    for chunk_id, value in text_chunks.items():
        if isinstance(value, dict) and value.get("file_path") == target_file:
            target_chunks.append(
                {
                    "chunk_id": chunk_id,
                    "file_path": value.get("file_path"),
                    "content": value.get("content", ""),
                }
            )

    contaminated_entities = []
    foreign_source_counts: Counter[str] = Counter()
    for entity in entity_items:
        source_ids = str(entity.get("source_id", "")).split("<SEP>")
        foreign_sources = []
        for source_id in source_ids:
            chunk = text_chunks.get(source_id, {})
            file_path = chunk.get("file_path") if isinstance(chunk, dict) else None
            if file_path and file_path != target_file:
                foreign_sources.append(
                    {"chunk_id": source_id, "file_path": file_path}
                )
                foreign_source_counts[file_path] += 1
        if foreign_sources:
            contaminated_entities.append(
                {
                    "entity_name": entity.get("entity_name"),
                    "declared_file_path": entity.get("file_path"),
                    "foreign_source_count": len(foreign_sources),
                    "foreign_sources": foreign_sources[:20],
                }
            )

    combined_target_content = "\n\n".join(
        str(chunk.get("content", "")) for chunk in target_chunks
    )
    vector_file_counts = {
        "chunks": dict(Counter(item.get("file_path") for item in chunk_items)),
        "entities": dict(Counter(item.get("file_path") for item in entity_items)),
        "relationships": dict(
            Counter(item.get("file_path") for item in relation_items)
        ),
    }

    return {
        "storage_dir": str(storage_dir),
        "target_file": target_file,
        "document_status": {
            "total_documents": len(doc_status),
            "target_document_present": target_file in document_paths,
            "foreign_document_count": len(foreign_documents),
            "foreign_document_sample": foreign_documents[:30],
        },
        "vector_file_counts": vector_file_counts,
        "target_chunks": target_chunks,
        "option_evidence": option_evidence(question, combined_target_content),
        "relevant_table_rows": _relevant_table_rows(
            question, combined_target_content
        ),
        "entity_source_contamination": {
            "contaminated_entity_count": len(contaminated_entities),
            "foreign_source_file_counts": dict(
                foreign_source_counts.most_common(30)
            ),
            "entities": contaminated_entities,
        },
    }


def load_previous_result(path: Path, question_id: str) -> dict[str, Any] | None:
    """Load the previous answer produced by raganything_test.py, if present."""

    if not path.is_file():
        return None
    results = load_json(path)
    result = results.get(question_id) if isinstance(results, dict) else None
    return result if isinstance(result, dict) else None


def summarize_root_cause(
    audit: dict[str, Any],
    question: dict[str, Any],
    previous_result: dict[str, Any] | None,
) -> list[str]:
    """Produce evidence-based diagnostic conclusions, not hidden model reasoning."""

    conclusions = []
    status = audit["document_status"]
    contamination = audit["entity_source_contamination"]
    if status["target_document_present"]:
        conclusions.append(
            "文件路由正确：目标文档存在于选中的 LightRAG 工作目录。"
        )
    if status["foreign_document_count"]:
        conclusions.append(
            "工作目录的 KV 状态并非单文档隔离："
            f"还包含 {status['foreign_document_count']} 个其他文档。"
        )
    if contamination["contaminated_entity_count"]:
        conclusions.append(
            "知识图谱实体的 source_id 指向其他文档 chunk："
            f"共发现 {contamination['contaminated_entity_count']} 个受影响实体，"
            "这些旧 chunk 有机会进入 hybrid 检索上下文。"
        )

    expected = str(question.get("answer", ""))
    expected_entry = next(
        (
            item
            for item in audit["option_evidence"]
            if item["option"] == expected
        ),
        None,
    )
    if expected_entry and not expected_entry["exact_occurrences"]:
        conclusions.append(
            f"正确选项 {expected} 的精确值 {expected_entry['value']} "
            "没有出现在目标 chunk 中；索引只保留了其取整值 "
            f"{expected_entry['rounded_value']}，说明表格解析阶段发生精度丢失。"
        )

    if previous_result:
        actual = normalize_answer(previous_result.get("answer"))
        conclusions.append(
            f"历史运行结果为 {actual or '<空>'}，数据集标签为 {expected}。"
        )
        actual_entry = next(
            (
                item
                for item in audit["option_evidence"]
                if item["option"] == actual
            ),
            None,
        )
        relevant_rows = audit.get("relevant_table_rows", [])
        if (
            actual_entry
            and actual != expected
            and actual_entry["rounded_occurrences"]
            and any("寿险" in " | ".join(row) for row in relevant_rows)
        ):
            conclusions.append(
                f"错误选项 {actual} 的取整值 "
                f"{actual_entry['rounded_value']} 对应表中的“其中:寿险”行，"
                "不是问题要求的“原保险保费收入”行；"
                "这表明回答阶段发生了行语义错配。"
            )
    return conclusions


class TracedLLM:
    """Wrap LightRAG's OpenAI function and print API-visible calls.

    This exposes prompts, retrieval context passed as a system prompt, and text
    returned by the endpoint. It cannot expose private reasoning that the API
    itself does not return. If an endpoint returns ``reasoning_content`` and
    LightRAG surfaces it as ``<think>...</think>``, that text will be visible.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None,
        output_limit: int | None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.output_limit = output_limit
        self.call_count = 0

    async def __call__(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str | AsyncIterator[str]:
        from lightrag.llm.openai import openai_complete_if_cache

        self.call_count += 1
        stage = (
            "关键词提取"
            if kwargs.get("keyword_extraction")
            else "回答生成或查询辅助调用"
        )
        safe_parameters = {
            key: kwargs.get(key)
            for key in (
                "keyword_extraction",
                "enable_cot",
                "stream",
                "temperature",
                "max_tokens",
                "_priority",
            )
            if key in kwargs
        }
        print_value(
            f"LLM 调用 {self.call_count}: {stage} - 参数",
            {"model": self.model, **safe_parameters},
            self.output_limit,
        )
        if system_prompt:
            print_value(
                f"LLM 调用 {self.call_count}: system prompt / 检索上下文",
                system_prompt,
                self.output_limit,
            )
        if history_messages:
            print_value(
                f"LLM 调用 {self.call_count}: history",
                history_messages,
                self.output_limit,
            )
        print_value(
            f"LLM 调用 {self.call_count}: user prompt",
            prompt,
            self.output_limit,
        )

        result = await openai_complete_if_cache(
            self.model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=self.api_key,
            base_url=self.base_url,
            **kwargs,
        )
        if isinstance(result, str):
            print_value(
                f"LLM 调用 {self.call_count}: API 可见输出",
                result,
                self.output_limit,
            )
        else:
            print_value(
                f"LLM 调用 {self.call_count}: API 可见输出",
                "返回了流式迭代器；本脚本默认 stream=False。",
                self.output_limit,
            )
        return result


def make_query_param(args: argparse.Namespace, *, only_need_prompt: bool = False):
    """Create a fresh QueryParam so query calls cannot mutate shared state."""

    from lightrag import QueryParam

    return QueryParam(
        mode=args.mode,
        only_need_prompt=only_need_prompt,
        response_type=args.response_type,
        stream=False,
        top_k=args.top_k,
        chunk_top_k=args.chunk_top_k,
        enable_rerank=not args.disable_rerank,
        include_references=False,
    )


async def run_live_query(
    args: argparse.Namespace,
    storage_dir: Path,
    qa_prompt: str,
    expected_answer: str,
) -> dict[str, Any]:
    """Load the existing LightRAG index and print prompt, retrieval, and answer."""

    from lightrag import LightRAG
    from lightrag.llm.openai import openai_embed
    from lightrag.utils import EmbeddingFunc

    traced_llm = TracedLLM(
        model=args.llm_model,
        api_key=args.api_key,
        base_url=args.base_url,
        output_limit=args.output_limit,
    )
    embedding_func = EmbeddingFunc(
        embedding_dim=args.embedding_dim,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=args.embedding_model,
            api_key=args.api_key,
            base_url=args.base_url,
        ),
    )
    rag = LightRAG(
        working_dir=str(storage_dir),
        llm_model_func=traced_llm,
        embedding_func=embedding_func,
        enable_llm_cache=not args.disable_cache,
    )

    initialized = False
    try:
        await rag.initialize_storages()
        initialized = True

        final_prompt = await rag.aquery(
            qa_prompt,
            param=make_query_param(args, only_need_prompt=True),
        )
        print_value("LightRAG 最终组装 Prompt", final_prompt, args.output_limit)

        result = await rag.aquery_llm(
            qa_prompt,
            param=make_query_param(args),
        )
        retrieval_view = {
            "status": result.get("status"),
            "message": result.get("message"),
            "metadata": result.get("metadata", {}),
            "data": result.get("data", {}),
        }
        print_value("实际结构化检索结果", retrieval_view, args.output_limit)

        llm_response = result.get("llm_response", {})
        answer = llm_response.get("content")
        normalized = normalize_answer(answer)
        comparison = {
            "raw_answer": answer,
            "normalized_answer": normalized,
            "expected_answer": expected_answer,
            "is_correct": normalized == expected_answer,
        }
        print_value("最终答案对比", comparison, args.output_limit)
        return {"retrieval": retrieval_view, "answer": comparison}
    finally:
        if initialized:
            await rag.finalize_storages()


def configure_logging(verbose: bool) -> None:
    """Configure console-only logging; do not create or modify log files."""

    from lightrag.utils import logger, set_verbose_debug

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(level)
    set_verbose_debug(verbose)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="诊断 RAGAnything 单题检索、Prompt 和回答链路"
    )
    parser.add_argument("--question-id", default="Q004", help="QA.json 中的题目 id")
    parser.add_argument(
        "--qa-file", type=Path, default=DATA_DIR / "QA.json", help="QA 数据路径"
    )
    parser.add_argument(
        "--name-map",
        type=Path,
        default=DATA_DIR / "name2id.json",
        help="语料文件名映射路径",
    )
    parser.add_argument(
        "--previous-results",
        type=Path,
        default=DATA_DIR / "result_dir.json",
        help="历史答题结果路径",
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=EXAMPLES_DIR / "rag_storage",
        help="按三位文件编号划分的 LightRAG 根目录",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只审计本地索引，不调用 embedding 或 LLM API",
    )
    parser.add_argument(
        "--mode",
        default="hybrid",
        choices=["local", "global", "hybrid", "naive", "mix"],
    )
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--chunk-top-k", type=int, default=20)
    parser.add_argument("--response-type", default="Multiple Paragraphs")
    parser.add_argument("--disable-rerank", action="store_true")
    parser.add_argument("--disable-cache", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-truncate", action="store_true", help="完整打印超长 Prompt 和检索 JSON"
    )
    parser.add_argument(
        "--max-output-chars", type=int, default=30000, help="每个输出区块的最大字符数"
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("LLM_BINDING_API_KEY") or os.getenv("OPENAI_API_KEY"),
        help="默认读取 LLM_BINDING_API_KEY 或 OPENAI_API_KEY",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LLM_BINDING_HOST") or os.getenv("OPENAI_BASE_URL"),
        help="OpenAI 兼容接口地址",
    )
    parser.add_argument("--llm-model", default=os.getenv("LLM_MODEL", "gpt-4o-mini"))
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("EMBEDDING_MODEL", "text-embedding-3-large"),
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=int(os.getenv("EMBEDDING_DIM", "3072")),
    )
    args = parser.parse_args()
    args.output_limit = None if args.no_truncate else args.max_output_chars
    return args


async def async_main(args: argparse.Namespace) -> int:
    configure_logging(args.verbose)

    question = load_question(args.qa_file.resolve(), args.question_id)
    name_map = load_json(args.name_map.resolve())
    if not isinstance(name_map, dict):
        raise TypeError(f"name2id.json 应为对象，实际为 {type(name_map).__name__}")

    matched_file, matched_by = match_source_file(question, list(name_map.keys()))
    prefix = matched_file[:3]
    storage_dir = args.storage_root.resolve() / prefix
    target_file = storage_file_name(matched_file)
    qa_prompt = build_qa_prompt(question)
    previous_result = load_previous_result(
        args.previous_results.resolve(), args.question_id
    )

    print_value(
        "题目与文件路由",
        {
            "question_id": args.question_id,
            "question": question.get("question"),
            "options": {
                letter: question[f"option_{letter.lower()}"] for letter in "ABCD"
            },
            "expected_answer": question.get("answer"),
            "previous_result": previous_result,
            "matched_by": matched_by,
            "matched_corpus_file": matched_file,
            "storage_dir": str(storage_dir),
            "storage_target_file": target_file,
        },
        args.output_limit,
    )
    print_value("复现用 QA Prompt", qa_prompt, args.output_limit)

    audit = audit_storage(storage_dir, target_file, question)
    audit_summary = {key: value for key, value in audit.items() if key != "target_chunks"}
    print_value("离线索引审计", audit_summary, args.output_limit)
    print_value("目标文档本地 Chunk", audit["target_chunks"], args.output_limit)
    print_value(
        "基于现有证据的原因判断",
        summarize_root_cause(audit, question, previous_result),
        args.output_limit,
    )

    if args.offline:
        print("\n离线审计完成；未调用任何外部 API。")
        return 0
    if not args.api_key:
        raise ValueError(
            "在线检索需要 API key。请设置 LLM_BINDING_API_KEY/OPENAI_API_KEY，"
            "或使用 --api-key；只看本地索引可加 --offline。"
        )

    await run_live_query(
        args,
        storage_dir=storage_dir,
        qa_prompt=qa_prompt,
        expected_answer=str(question.get("answer", "")),
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n用户中断。")
        return 130
    except Exception as exc:
        section("诊断脚本异常")
        print(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
