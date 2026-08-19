from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


Classifier = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    method: str
    confidence: float

CHINESE_GREETINGS = {
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "早上好",
    "上午好",
    "中午好",
    "下午好",
    "晚上好",
}
ENGLISH_GREETINGS = {"hi", "hello", "hey", "goodmorning", "goodafternoon", "goodevening"}

BANKING_TERMS = {
    "银行",
    "保险",
    "金融",
    "监管",
    "央行",
    "人民银行",
    "金融监管总局",
    "银保监",
    "贷款",
    "存款",
    "信贷",
    "利率",
    "资本充足率",
    "不良贷款",
    "流动性",
    "净息差",
    "资产负债",
    "总资产",
    "总负债",
    "保费",
    "偿付能力",
    "风险加权资产",
}
DOCUMENT_TERMS = {"知识库", "文档", "文件", "报告", "情况表", "监管指标", "经营情况表"}


def normalize_intent_text(value: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", str(value or "").lower()))


def _is_greeting(question: str) -> bool:
    normalized = normalize_intent_text(question)
    if normalized in CHINESE_GREETINGS:
        return True
    if normalized in ENGLISH_GREETINGS:
        return True
    return False


def has_clear_retrieval_signal(question: str) -> bool:
    if re.search(r"《[^》]+》", question):
        return True
    if re.search(r"(?<!\d)\d{3}(?!\d)", question):
        return True
    normalized = normalize_intent_text(question)
    return any(term in normalized for term in BANKING_TERMS | DOCUMENT_TERMS)


class IntentRouter:
    async def decide(
        self,
        question: str,
        *,
        manual_collection: str = "",
        classifier: Classifier | None = None,
    ) -> IntentDecision:
        if manual_collection.strip():
            return IntentDecision("retrieval", "manual_collection", 1.0)

        if _is_greeting(question):
            return IntentDecision("chat", "greeting_to_llm", 1.0)

        if has_clear_retrieval_signal(question):
            return IntentDecision("retrieval", "deterministic_retrieval", 0.96)

        if classifier is not None:
            try:
                raw = (await classifier(question)).strip().lower()
                if raw in {"chat", "retrieval"}:
                    return IntentDecision(raw, "model_classifier", 0.75)
            except Exception:
                pass

        return IntentDecision("chat", "safe_chat_fallback", 0.55)


intent_router = IntentRouter()
