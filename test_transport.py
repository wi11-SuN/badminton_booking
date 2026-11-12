"""离线传输回归测试：仅连接临时启动的 127.0.0.1 HTTP 服务。"""
import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import unittest
from unittest import mock

import book_badminton as booking


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests = []
        self.connections = 0
        self.idle_closed = threading.Event()

    def get_request(self):
        connection = super().get_request()
        self.connections += 1
        return connection

    def handle_error(self, request, client_address):
        # 超大响应测试会主动关闭连接，服务端无需输出无关堆栈。
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self, body=b'{"error":0}', status=200, extra=()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def record(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, self.headers.get("Cookie"), body))

    def do_GET(self):
        self.record()
        route = self.path.split("?", 1)[0]
        if route.endswith("/redirect"):
            self.respond(status=302, extra=[("Location", "https://example.invalid/private")])
        elif route.endswith("/cookie"):
            self.respond(extra=[("Set-Cookie", "JSESSIONID=updated; Path=/; HttpOnly")])
        elif route.endswith("/idle-close"):
            self.respond()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.server.idle_closed.set()
        elif route.endswith("/truncated"):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"short")
            self.wfile.flush()
            self.close_connection = True
        elif route.endswith("/oversized"):
            self.respond(b"x" * 5_000_001)
        else:
            self.respond()

    def do_POST(self):
        self.record()
        if self.path.endswith("/drop-post"):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
        else:
            self.respond(json.dumps({"error": 0, "tradeId": "local-test-only"}).encode())


class ClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer()
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        # 保持生产 Client 的固定 HTTPS 主机校验，仅将连接工厂替换为回环连接。
        def connection(host, port=None, timeout=None):
            self.assertEqual(host, "webssl.xports.cn")
            return http.client.HTTPConnection(*self.server.server_address, timeout=timeout)
        self.connection_patch = mock.patch.object(booking.http.client, "HTTPSConnection", side_effect=connection)
        self.connection_patch.start()
        self.client = booking.Client({"cookies": {"JSESSIONID": "initial-secret"}}, {"http_timeout_seconds": 2})

    def tearDown(self):
        self.client.close()
        self.connection_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def path(self, suffix):
        return booking.ROOT + suffix

    def test_get_then_post_reuses_connection_and_updated_cookie(self):
        self.client.get_json(self.path("/cookie"))
        status, body = self.client.request(self.path("/court/commit"), {"fieldInfo": "fixture"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tradeId"], "local-test-only")
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(self.server.requests[1][2], "JSESSIONID=updated")

    def test_post_disconnect_is_never_replayed(self):
        self.client.get_json(self.path("/warmup"))
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError)):
            self.client.request(self.path("/drop-post"), {"fieldInfo": "fixture"})
        self.assertEqual([r[0] for r in self.server.requests], ["GET", "POST"])
        self.assertEqual(self.server.connections, 1)
        # 坏连接已释放，下一次显式只读查询可以正常重连。
        self.client.get_json(self.path("/after-error"))
        self.assertEqual(self.server.connections, 2)

    def test_post_timeout_is_never_replayed(self):
        connection = mock.Mock()
        connection.getresponse.side_effect = TimeoutError("test response timeout")
        self.client._connection = connection
        with self.assertRaises(TimeoutError):
            self.client.request(self.path("/court/commit"), {"fieldInfo": "fixture"})
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()
        self.assertEqual(self.server.connections, 0)

    def test_reused_get_reconnects_at_most_once(self):
        self.client.get_json(self.path("/warmup"))
        self.client._disconnect()
        # 两个已知断连分别模拟失效的旧连接和重连后仍失败的新连接。
        stale, replacement = mock.Mock(), mock.Mock()
        stale.getresponse.side_effect = http.client.RemoteDisconnected("test old connection")
        replacement.getresponse.side_effect = http.client.RemoteDisconnected("test new connection")
        self.client._connection = stale
        with mock.patch.object(booking.http.client, "HTTPSConnection", return_value=replacement) as factory:
            with self.assertRaises(http.client.RemoteDisconnected):
                self.client.request(self.path("/read"))
        self.assertEqual(stale.request.call_count, 1)
        self.assertEqual(replacement.request.call_count, 1)
        factory.assert_called_once()

    def test_idle_disconnect_can_reconnect_get_once(self):
        self.client.get_json(self.path("/idle-close"))
        self.assertTrue(self.server.idle_closed.wait(timeout=2))
        self.client.get_json(self.path("/after-idle"))
        self.assertEqual(self.server.connections, 2)
        self.assertEqual(len(self.server.requests), 2)

    def test_redirect_is_returned_without_following(self):
        status, _ = self.client.request(self.path("/redirect"))
        self.assertEqual(status, 302)
        self.assertEqual(len(self.server.requests), 1)

    def test_partial_response_is_an_error_and_connection_is_discarded(self):
        with self.assertRaises(http.client.IncompleteRead):
            self.client.request(self.path("/truncated"))
        self.client.get_json(self.path("/after-truncated"))
        self.assertEqual(self.server.connections, 2)

    def test_oversized_response_is_an_error_and_connection_is_discarded(self):
        with self.assertRaises(booking.BookingError):
            self.client.request(self.path("/oversized"))
        self.client.get_json(self.path("/after-oversized"))
        self.assertEqual(self.server.connections, 2)

    def test_request_logs_omit_query_cookies_and_payload(self):
        with self.assertLogs(booking.LOG, level="INFO") as logs:
            self.client.request(self.path("/court/commit?token=query-secret"), {"private": "payload-secret"})
        text = "\n".join(logs.output)
        self.assertIn("elapsed_ms=", text)
        self.assertIn("status=200", text)
        for private in ("initial-secret", "query-secret", "payload-secret"):
            self.assertNotIn(private, text)

    def test_parallel_callers_serialize_on_one_connection(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.client.get_json, self.path("/first"))
            second = pool.submit(self.client.get_json, self.path("/second"))
            self.assertEqual(first.result(timeout=2)["error"], 0)
            self.assertEqual(second.result(timeout=2)["error"], 0)
        self.assertEqual(self.server.connections, 1)

    def test_closed_client_and_foreign_path_cannot_send(self):
        with self.assertRaises(booking.BookingError):
            self.client.request("https://example.invalid/private")
        self.client.close()
        with self.assertRaises(booking.BookingError):
            self.client.request(self.path("/closed"))
        self.assertEqual(self.server.connections, 0)


if __name__ == "__main__":
    unittest.main()
