import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import requests

import v2ex_activity as activity
import v2ex_checkin as checkin


def balance(gold=0, silver=0, copper=0):
    return f'<div class="balance_area bigger">{gold}<img alt="G">{silver}<img alt="S">{copper}<img alt="B"></div>'


class ActivityTests(unittest.TestCase):
    def test_daily_target_increases_and_repeats_after_60(self):
        for offset in range(33):
            day = date.fromordinal(date(2026, 10, 24).toordinal() + offset)
            self.assertEqual(activity.daily_topic_limit(day), 50 + offset % 11)

    @patch.object(activity, "datetime")
    def test_daily_target_uses_beijing_date(self, clock):
        instant = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc)
        clock.now.side_effect = lambda tz: instant.astimezone(tz)
        self.assertEqual(activity.daily_topic_limit(), activity.daily_topic_limit(date(2026, 10, 24)))

    @patch.object(activity, "daily_topic_limit", return_value=58)
    def test_default_limits_follow_daily_target(self, limit):
        with patch.dict(os.environ, {}, clear=True):
            config = activity.settings()
            self.assertEqual((config["min_topics"], config["max_topics"]), (58, 58))
            self.assertEqual((config["gap_min"], config["gap_max"], config["max_seconds"]), (10, 15, 19800))

    @patch.object(activity, "daily_topic_limit", return_value=58)
    def test_explicit_limit_still_overrides_daily_target(self, limit):
        with patch.dict(os.environ, {"V2EX_ACTIVITY_MAX_TOPICS": "2"}, clear=True):
            config = activity.settings()
            self.assertEqual((config["min_topics"], config["max_topics"]), (2, 2))

    def config(self, **changes):
        config = dict(enabled=True, max_topics=2, min_topics=0, gap_min=5,
                      gap_max=5, max_seconds=60)
        config.update(changes)
        return config

    def test_parse_balance_all_denominations(self):
        self.assertEqual(activity.PageParser(balance(1, 23, 99)).balance, (1, 23, 99))
        self.assertGreater(activity.PageParser(balance(1, 24, 0)).balance, (1, 23, 99))

    def test_missing_balance_is_not_zero(self):
        self.assertIsNone(activity.PageParser('<div>99<img alt="B"></div>').balance)

    def test_nested_balance_markup(self):
        self.assertEqual(activity.PageParser('<div class="balance_area"><span>1,200</span> <img alt="B" /></div>').balance, (0, 0, 1200))

    def test_topics_unique_same_origin(self):
        html = '''<a href="/t/123#reply1">a</a><a href='/t/123?p=2'>b</a>
        <a href="https://evil.example/t/456">x</a><a href="/t/789">c</a>'''
        self.assertEqual(activity.PageParser(html).topics, ["/t/123", "/t/789"])

    def test_blocks_offsite_redirect_before_sending_cookie(self):
        session = Mock()
        session.get.return_value = Mock(status_code=302, headers={"Location": "https://evil.example/"})
        with self.assertRaises(activity.ActivityError):
            activity.get_page(session, "/balance")
        self.assertEqual(session.get.call_count, 1)

    def test_auth_and_challenge_and_rate_limit_stop(self):
        for code, html in [(429, ""), (403, ""), (200, "Just a moment"),
                           (200, '<a href="/signin">登录</a>')]:
            with self.subTest(code=code, html=html):
                session = Mock()
                session.get.return_value = Mock(status_code=code, text=html)
                with self.assertRaises(activity.ActivityError):
                    activity.get_page(session, "/balance")

    def test_login_redirect_stops(self):
        session = Mock()
        session.get.return_value = Mock(status_code=302, headers={"Location": "/signin"})
        with self.assertRaises(activity.ActivityError):
            activity.get_page(session, "/balance")
        self.assertEqual(session.get.call_count, 1)

    def test_settings_empty_defaults_and_invalid_interval(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(activity.settings()["enabled"])
        with patch.dict(os.environ, {"V2EX_ACTIVITY_ENABLED": "false"}, clear=True):
            self.assertFalse(activity.settings()["enabled"])
        with patch.dict(os.environ, {"V2EX_ACTIVITY_GAP_MIN": "30", "V2EX_ACTIVITY_GAP_MAX": "5"}, clear=True):
            with self.assertRaises(ValueError):
                activity.settings()

    @patch.object(activity.time, "sleep")
    @patch.object(activity, "get_page")
    def test_read_limit_and_no_reward(self, get, sleep):
        get.side_effect = [balance(copper=10), '<a href="/t/1">1</a><a href="/t/1">1</a><a href="/t/2">2</a>',
                           '<div class="topic_content">one</div>', '<div class="topic_content">two</div>', balance(copper=10)]
        ok, msg = activity.read_activity(Mock(), "test", self.config(), Mock())
        self.assertTrue(ok)
        self.assertIn("已读 2 篇", msg)
        self.assertIn("未确认奖励到账", msg)
        self.assertEqual(get.call_count, 5)

    @patch.object(activity.time, "sleep")
    @patch.object(activity, "get_page")
    def test_two_increases_stop_and_rollover(self, get, sleep):
        listing = ''.join(f'<a href="/t/{i}">{i}</a>' for i in range(1, 31))
        body = '<div class="topic_content">body</div>'
        get.side_effect = [balance(silver=1, copper=99), listing] + [body] * 10 + [balance(silver=2)] + [body] * 10 + [balance(silver=3)]
        ok, msg = activity.read_activity(Mock(), "test", self.config(max_topics=30), Mock())
        self.assertTrue(ok)
        self.assertIn("已读 20 篇，余额增加 2 次", msg)

    @patch.object(activity.time, "sleep")
    @patch.object(activity, "get_page")
    def test_can_read_1000_topics_across_more_than_30_pages(self, get, sleep):
        def response(session, path):
            if path == "/balance":
                return balance(copper=10)
            if path.startswith("/recent?p="):
                page = int(path.split("=")[1])
                return ''.join(f'<a href="/t/{i}">topic</a>' for i in range((page - 1) * 20 + 1, page * 20 + 1))
            return '<div class="topic_content">body</div>'

        get.side_effect = response
        ok, msg = activity.read_activity(Mock(), "test", self.config(max_topics=1000, min_topics=1000), Mock())
        self.assertTrue(ok)
        self.assertIn("已读 1000 篇", msg)
        self.assertEqual(sum(call.args[1].startswith('/t/') for call in get.call_args_list), 1000)
        self.assertEqual(sum(call.args[1].startswith('/recent') for call in get.call_args_list), 50)

    @patch.object(activity, "get_page", return_value="<html>unknown</html>")
    def test_unparseable_balance_stops(self, get):
        ok, msg = activity.read_activity(Mock(), "test", self.config(), Mock())
        self.assertFalse(ok)
        self.assertEqual(get.call_count, 1)

    @patch.object(activity, "get_page", side_effect=requests.Timeout("secret-cookie"))
    def test_network_failure_does_not_expose_exception(self, get):
        ok, msg = activity.read_activity(Mock(), "test", self.config(), Mock())
        self.assertFalse(ok)
        self.assertNotIn("secret-cookie", msg)

    @patch.object(activity.time, "sleep")
    @patch.object(activity.time, "monotonic", side_effect=[0, 0, 61, 61])
    @patch.object(activity, "get_page", return_value=balance())
    def test_runtime_limit(self, get, monotonic, sleep):
        ok, msg = activity.read_activity(Mock(), "test", self.config(), Mock())
        self.assertTrue(ok)
        self.assertIn("达到时长上限", msg)
        self.assertEqual(get.call_count, 1)

    @patch.object(checkin.time, "sleep")
    @patch.object(checkin, "send_telegram")
    @patch.object(checkin, "read_activity", return_value=(True, "阅读完成"))
    @patch.object(checkin, "checkin", side_effect=[(True, "今日已签过（获得 10 铜币）"), (False, "Cookie 失效")])
    @patch.object(checkin, "load_config", return_value=[("<主号>", "cookie1"), ("小号", "cookie2")])
    @patch.object(checkin, "activity_settings", return_value={"enabled": True})
    def test_already_signed_reads_but_failed_account_skips(self, config, load, sign, read, notify, sleep):
        with self.assertRaises(SystemExit) as result:
            checkin.main()
        self.assertEqual(result.exception.code, 1)
        read.assert_called_once()
        message = notify.call_args.args[0]
        self.assertIn("&lt;主号&gt;", message)
        self.assertIn("本次新领签到奖励 · 无", message)


if __name__ == "__main__":
    unittest.main()
