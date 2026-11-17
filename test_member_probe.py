"""会员订单页保活测试；不读取本地会话，不访问场馆。"""
import unittest
from unittest.mock import Mock

import book_badminton as booking


class MemberSessionProbeTests(unittest.TestCase):
    def test_reads_empty_order_page_with_navigation_headers(self):
        client = booking.Client({"cookies": {"JSESSIONID": "test"}}, {"http_timeout_seconds": 1})
        self.addCleanup(client.close)
        connection = Mock()
        client._connection = connection
        response = connection.getresponse.return_value
        response.status = 200
        response.headers.get_all.return_value = []
        response.read.return_value = "<title> 我的订单 </title><div>暂无订单</div>".encode()
        response.length = 0

        self.assertIsNone(booking.member_session_probe(client))

        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", booking.ROOT + "/order/query"))
        self.assertIsNone(kwargs["body"])
        headers = kwargs["headers"]
        self.assertEqual(headers["Referer"], booking.ORIGIN + booking.ROOT + "/member/homePage")
        self.assertTrue(headers["Accept"].startswith("text/html"))
        self.assertNotIn("X-Requested-With", headers)
        self.assertEqual(headers["Cookie"], "JSESSIONID=test")

    def test_explicit_auth_failures(self):
        for status, html in [(200, "<title>失败</title><p>未查询到公众号信息</p>"),
                             (200, "<title>会员登录</title>"), (401, "")]:
            with self.subTest(status=status, html=html):
                client = Mock()
                client.request.return_value = (status, html)
                with self.assertRaises(booking.SessionExpired):
                    booking.member_session_probe(client)

    def test_inconclusive_responses_do_not_report_expiration(self):
        failed = "<title>失败</title><p>未查询到公众号信息</p>"
        for status, html in [(302, ""), (403, ""), (429, failed), (503, failed),
                             (200, "<title>失败</title><script>msg='未查询到公众号信息'</script>"),
                             (200, "<title>维护中</title><a>我的订单</a>"),
                             (200, "<script>var s='<title>我的订单</title>';</script>")]:
            with self.subTest(status=status, html=html):
                client = Mock()
                client.request.return_value = (status, html)
                with self.assertRaises(booking.BookingError) as result:
                    booking.member_session_probe(client)
                self.assertNotIsInstance(result.exception, booking.SessionExpired)

    def test_timeout_is_propagated_without_reporting_expiration(self):
        client = Mock()
        client.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            booking.member_session_probe(client)


if __name__ == "__main__":
    unittest.main()
