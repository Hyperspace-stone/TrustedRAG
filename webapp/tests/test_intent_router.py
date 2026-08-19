import unittest

from webapp.intent_router import IntentRouter


class IntentRouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_chinese_greeting_routes_to_llm_chat(self) -> None:
        classifier_called = False

        async def classifier(question: str) -> str:
            nonlocal classifier_called
            classifier_called = True
            return "retrieval"

        decision = await IntentRouter().decide("你好", classifier=classifier)
        self.assertEqual(decision.intent, "chat")
        self.assertEqual(decision.method, "greeting_to_llm")
        self.assertIs(classifier_called, False)

    async def test_greeting_with_punctuation_bypasses_classifier(self) -> None:
        decision = await IntentRouter().decide(" 您好！ ")
        self.assertEqual(decision.intent, "chat")
        self.assertEqual(decision.method, "greeting_to_llm")

    async def test_banking_question_is_deterministic_retrieval(self) -> None:
        decision = await IntentRouter().decide("商业银行资本充足率的监管要求是什么？")
        self.assertEqual(decision.intent, "retrieval")
        self.assertEqual(decision.method, "deterministic_retrieval")

    async def test_manual_collection_forces_retrieval(self) -> None:
        decision = await IntentRouter().decide("你好", manual_collection="012")
        self.assertEqual(decision.intent, "retrieval")
        self.assertEqual(decision.method, "manual_collection")

    async def test_classifier_accepts_only_exact_allowed_label(self) -> None:
        async def valid_classifier(question: str) -> str:
            return "chat"

        valid = await IntentRouter().decide("你是谁", classifier=valid_classifier)
        self.assertEqual(valid.intent, "chat")
        self.assertEqual(valid.method, "model_classifier")

        async def invalid_classifier(question: str) -> str:
            return "The answer is retrieval"

        invalid = await IntentRouter().decide("帮我看看这个", classifier=invalid_classifier)
        self.assertEqual(invalid.intent, "chat")
        self.assertEqual(invalid.method, "safe_chat_fallback")

    async def test_classifier_failure_falls_back_to_chat(self) -> None:
        async def broken_classifier(question: str) -> str:
            raise TimeoutError("timeout")

        decision = await IntentRouter().decide("今天过得怎么样", classifier=broken_classifier)
        self.assertEqual(decision.intent, "chat")
        self.assertEqual(decision.method, "safe_chat_fallback")
