import unittest
from types import SimpleNamespace
from app.clients.llm import LLMClient, _unavailable_models, _unavailable_until
from app.config import Settings


class ModelFallbackTests(unittest.TestCase):
    def setUp(self):
        _unavailable_models.clear()
        _unavailable_until.clear()
        self.client = LLMClient(Settings(llm_model="best", llm_fallback_models="middle,small", llm_fallback_model="", vlm_fallback_models="", vlm_fallback_model=""))

    def test_order_and_actual_model_trace(self):
        visited = []
        def call(name):
            visited.append(name)
            if name != "small": raise RuntimeError("403 Free quota exhausted")
            return SimpleNamespace(model="small-version")
        result = self.client._run_with_fallback(call,"best","","对话模型")
        self.assertEqual(visited,["best","middle","small"])
        self.assertEqual(result.model,"small-version")
        self.assertEqual(self.client.call_trace()[0]["selected_model"],"small")
        self.assertTrue(self.client.call_trace()[0]["degraded"])
        visited.clear()
        self.client._run_with_fallback(call,"best","","对话模型")
        self.assertEqual(visited,["small"])

    def test_network_error_does_not_change_quality_tier(self):
        visited = []
        def call(name):
            visited.append(name)
            raise RuntimeError("network failure")
        with self.assertRaises(RuntimeError): self.client._run_with_fallback(call,"best","","对话模型",attempts=1)
        self.assertEqual(visited,["best"])

    def test_stream_uses_text_fallback_chain(self):
        visited = []
        def call(name):
            visited.append(name)
            if name == "best": raise RuntimeError("403 Free quota exhausted")
            return SimpleNamespace(model=name)
        self.client._run_with_fallback(call,"best","","流式对话")
        self.assertEqual(visited,["best","middle"])

    def test_vision_does_not_fall_back_to_text_or_ocr(self):
        visited = []
        def call(name):
            visited.append(name)
            raise RuntimeError("403 Free quota exhausted")
        with self.assertRaises(RuntimeError): self.client._run_with_fallback(call,"vision-best","","视觉模型")
        self.assertEqual(visited,["vision-best"])

    def tearDown(self):
        _unavailable_models.clear()
        _unavailable_until.clear()


if __name__ == '__main__': unittest.main()
