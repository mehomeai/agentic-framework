"""R9 local privacy: Ollama loopback must ignore ambient proxy env."""
from __future__ import annotations

import os
import unittest


class LocalPrivacyTests(unittest.TestCase):
    def test_local_ignores_proxy_sync_and_async(self):
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ["https_proxy"] = "http://127.0.0.1:9"
        try:
            from me_runtime.models import ModelConfig, build_chat_model
            m = build_chat_model(ModelConfig(provider="local",
                                             model="llama3.1"))
            self.assertEqual(m.sync_client_kwargs.get("trust_env"), False)
            self.assertEqual(m.async_client_kwargs.get("trust_env"), False)
            # Underlying httpx clients must actually carry trust_env=False,
            # otherwise kwargs would be decorative.
            sync_http = m._client._client
            async_http = m._async_client._client
            self.assertFalse(getattr(sync_http, "_trust_env", True),
                             "sync httpx must not trust proxy env")
            self.assertFalse(getattr(async_http, "_trust_env", True),
                             "async httpx must not trust proxy env")
        finally:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                      "https_proxy"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
