import hashlib
import hmac
import io
import json
import os
import unittest
from unittest.mock import patch

from tools.request_import import main, signature


class RequestImportTests(unittest.TestCase):
    def test_main_posts_signed_manifest_identity(self) -> None:
        sha256 = "a" * 64
        manifest = {
            "snapshot": {"sha256": sha256},
            "generatedAt": "2026-11-26T00:00:00Z",
        }
        requests = []

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return io.BytesIO(json.dumps(self.value).encode())

            def __exit__(self, *_args):
                return None

        def open_url(request, timeout):
            self.assertEqual(timeout, 30)
            if isinstance(request, str):
                return Response(manifest)
            requests.append(request)
            body = request.data
            timestamp = request.headers["X-static-import-timestamp"]
            delivery = request.headers["X-static-import-delivery"]
            expected = hmac.new(
                b"test-secret",
                f"{timestamp}.{delivery}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            self.assertEqual(request.headers["X-static-import-signature"], expected)
            self.assertEqual(json.loads(body)["sha256"], sha256)
            return Response({"status": "queued", "deliveryId": delivery})

        environment = {
            "STATIC_IMPORT_WEBHOOK_URL": "https://jp-2.example.test/api/static-import",
            "STATIC_IMPORT_WEBHOOK_SECRET": "test-secret",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("tools.request_import.urlopen", side_effect=open_url),
        ):
            main()
        self.assertEqual(len(requests), 1)

    def test_signature_depends_on_secret(self) -> None:
        self.assertNotEqual(
            signature("one", "1", "delivery-123456", b"{}"),
            signature("two", "1", "delivery-123456", b"{}"),
        )


if __name__ == "__main__":
    unittest.main()
