import os
import sys
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.com/v1")
os.environ.setdefault("OPENAI_MODEL_ID", "deepseek-v4-flash")

from llm_manage import ReasoningChatOpenAI


class ReasoningChatOpenAITest(unittest.TestCase):
    def make_llm(self):
        return ReasoningChatOpenAI(
            model="deepseek-v4-flash",
            api_key="test-key",
            base_url="https://example.com/v1",
            temperature=0,
        )

    def test_request_payload_passes_reasoning_content_back_for_ai_messages(self):
        llm = self.make_llm()
        ai_message = AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "thinking trace"},
            tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}],
        )

        payload = llm._get_request_payload([HumanMessage(content="hi"), ai_message])

        self.assertEqual(payload["messages"][1]["reasoning_content"], "thinking trace")

    def test_chat_result_preserves_reasoning_content_from_provider_response(self):
        llm = self.make_llm()
        result = llm._create_chat_result({
            "id": "chatcmpl-test",
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "thinking trace",
                },
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

        message = result.generations[0].message
        self.assertEqual(message.additional_kwargs["reasoning_content"], "thinking trace")


if __name__ == "__main__":
    unittest.main()
