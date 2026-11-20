import json
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from app.llm_player import OLLAMA_RUNNER_OPTIONS
from app.main import WINDOWS_OLLAMA_CONNECT_ATTEMPTS, WINDOWS_OLLAMA_RETRY_DELAYS, preload_ollama


class OllamaStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_the_installed_model_with_discussion_options(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "llama3:latest")
            self.assertEqual(payload["messages"], [])
            self.assertEqual(payload["options"], OLLAMA_RUNNER_OPTIONS)
            return httpx.Response(200, json={"model": "llama3:latest", "done": True})

        await self._preload_with(respond)
        self.assertEqual(paths, ["/api/tags", "/api/chat"])
        self.pause.assert_not_awaited()

    async def test_missing_model_stops_before_loading_or_downloading(self):
        def respond(request):
            self.assertEqual(request.url.path, "/api/tags")
            return httpx.Response(200, json={"models": []})

        with self.assertRaisesRegex(RuntimeError, "llama3:latest is not installed"):
            await self._preload_with(respond)
        self.pause.assert_not_awaited()

    async def test_unreachable_windows_ollama_explains_how_to_start(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            raise httpx.ConnectError("connection refused", request=request)

        with self.assertLogs("uvicorn.error", "WARNING") as diagnostics:
            with self.assertRaisesRegex(RuntimeError, "Start Windows Ollama") as caught:
                await self._preload_with(respond)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(paths, ["/api/tags"] * WINDOWS_OLLAMA_CONNECT_ATTEMPTS)
        self.assertEqual(self.pause.await_args_list, [call(delay) for delay in WINDOWS_OLLAMA_RETRY_DELAYS])
        output = "\n".join(diagnostics.output)
        self.assertIn("Waiting for Windows Ollama", output)
        self.assertIn("url=http://host.docker.internal:11434", output)
        self.assertIn("model=llama3:latest", output)
        self.assertIn("attempt=1/6", output)

    async def test_waits_for_delayed_windows_ollama_then_loads_without_downloading(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            if len(paths) <= 2:
                raise httpx.ConnectError("server not started yet", request=request)
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
            return httpx.Response(200, json={"model": "llama3:latest", "done": True})

        with self.assertLogs("uvicorn.error", "INFO") as diagnostics:
            await self._preload_with(respond)
        self.assertEqual(paths, ["/api/tags", "/api/tags", "/api/tags", "/api/chat"])
        self.assertEqual(self.pause.await_args_list, [call(2), call(4)])
        self.assertIn("Windows Ollama ready model=llama3:latest", "\n".join(diagnostics.output))

    async def test_temporary_gateway_errors_are_retried(self):
        for status in (502, 503, 504):
            paths = []

            def respond(request):
                paths.append(request.url.path)
                if len(paths) == 1:
                    return httpx.Response(status)
                if request.url.path == "/api/tags":
                    return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
                return httpx.Response(200, json={"model": "llama3:latest", "done": True})

            with self.subTest(status=status), self.assertLogs("uvicorn.error", "WARNING"):
                await self._preload_with(respond)
            self.assertEqual(paths, ["/api/tags", "/api/tags", "/api/chat"])
            self.pause.assert_awaited_once_with(2)

    async def test_connection_and_read_timeouts_are_retried(self):
        for error_type in (httpx.ConnectTimeout, httpx.ReadTimeout):
            paths = []

            def respond(request):
                paths.append(request.url.path)
                if len(paths) == 1:
                    raise error_type("temporary startup timeout", request=request)
                if request.url.path == "/api/tags":
                    return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
                return httpx.Response(200, json={"model": "llama3:latest", "done": True})

            with self.subTest(error_type=error_type), self.assertLogs("uvicorn.error", "WARNING"):
                await self._preload_with(respond)
            self.assertEqual(paths, ["/api/tags", "/api/tags", "/api/chat"])
            self.pause.assert_awaited_once_with(2)

    async def test_other_http_errors_fail_without_retrying_or_loading(self):
        for status in (400, 401, 404, 500):
            paths = []

            def respond(request):
                paths.append(request.url.path)
                return httpx.Response(status)

            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, f"HTTP {status} while listing models"):
                await self._preload_with(respond)
            self.assertEqual(paths, ["/api/tags"])
            self.pause.assert_not_awaited()

    async def test_invalid_model_lists_fail_without_retrying_or_loading(self):
        for body in ({}, {"models": None}, {"models": [None]}, {"models": [{}]}, []):
            paths = []

            def respond(request):
                paths.append(request.url.path)
                return httpx.Response(200, json=body)

            with self.subTest(body=body), self.assertRaisesRegex(RuntimeError, "invalid model list"):
                await self._preload_with(respond)
            self.assertEqual(paths, ["/api/tags"])
            self.pause.assert_not_awaited()

    async def test_load_failure_is_distinguished_from_unreachable_host(self):
        for failure in ("http", "timeout", "invalid_json", "wrong_model", "not_done"):
            paths = []

            def respond(request):
                paths.append(request.url.path)
                if request.url.path == "/api/tags":
                    return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
                if failure == "http":
                    return httpx.Response(500)
                if failure == "timeout":
                    raise httpx.ReadTimeout("model loading timed out", request=request)
                if failure == "invalid_json":
                    return httpx.Response(200, text="not JSON")
                return httpx.Response(200, json={
                    "model": "other:latest" if failure == "wrong_model" else "llama3:latest",
                    "done": failure != "not_done",
                })

            with self.subTest(failure=failure), self.assertRaisesRegex(RuntimeError, "reachable.*could not load") as caught:
                await self._preload_with(respond)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertEqual(paths, ["/api/tags", "/api/chat"])
            self.pause.assert_not_awaited()

    async def test_invalid_url_is_actionable_without_retrying(self):
        with self.assertRaisesRegex(RuntimeError, "OLLAMA_BASE_URL is invalid"):
            await preload_ollama("http://host.docker.internal:not-a-port", "llama3:latest")

    async def _preload_with(self, respond):
        client = httpx.AsyncClient
        with (
            patch("app.main.httpx.AsyncClient", side_effect=lambda **options: client(
                transport=httpx.MockTransport(respond), **options
            )),
            patch("app.main.asyncio.sleep", new_callable=AsyncMock) as pause,
        ):
            self.pause = pause
            await preload_ollama("http://host.docker.internal:11434", "llama3:latest")
