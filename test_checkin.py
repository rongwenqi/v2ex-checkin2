import unittest
from unittest.mock import Mock, patch

import requests

import v2ex_checkin as checkin


def page(text, status=200, url=checkin.BASE_URL + '/mission/daily'):
    return Mock(text=text, status_code=status, url=url)


class CheckinTests(unittest.TestCase):
    def test_already_signed_does_not_redeem(self):
        session = Mock()
        session.get.return_value = page('每日登录奖励已领取')
        self.assertEqual(checkin._checkin('test', session), (True, '今日已签过'))
        self.assertEqual(session.get.call_count, 1)

    def test_bad_cookie_stops_before_redeem(self):
        session = Mock()
        session.get.return_value = page('You need to sign in', url=checkin.BASE_URL + '/signin')
        self.assertFalse(checkin._checkin('test', session)[0])
        self.assertEqual(session.get.call_count, 1)

    def test_http_error_is_failure(self):
        session = Mock()
        session.get.return_value = page('', status=403)
        self.assertFalse(checkin._checkin('test', session)[0])
        self.assertEqual(session.get.call_count, 1)

    @patch.object(checkin.time, 'sleep')
    def test_redeem_and_verify_only_visit_daily_mission(self, sleep):
        session = Mock()
        session.get.side_effect = [
            page('/mission/daily/redeem?once=123'),
            page('已成功领取每日登录奖励 18 铜币'),
            page('每日登录奖励已领取'),
        ]
        self.assertEqual(checkin._checkin('test', session), (True, '获得 18 铜币'))
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            checkin.BASE_URL + '/mission/daily',
            checkin.BASE_URL + '/mission/daily/redeem?once=123',
            checkin.BASE_URL + '/mission/daily',
        ])

    @patch.object(checkin.time, 'sleep')
    @patch.object(checkin, 'send_telegram')
    @patch.object(checkin, 'checkin', side_effect=[(True, '今日已签过（获得 10 铜币）'), (True, '获得 18 铜币')])
    @patch.object(checkin, 'load_config', return_value=[('<主号>', 'cookie1'), ('小号', 'cookie2')])
    def test_daily_summary_counts_only_new_rewards(self, load, sign, notify, sleep):
        with self.assertRaises(SystemExit) as result:
            checkin.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(sign.call_count, 2)
        message = notify.call_args.args[0]
        self.assertIn('&lt;主号&gt;', message)
        self.assertIn('本次新领签到奖励 · <b>18 铜币</b>', message)
        self.assertNotIn('活跃度', message)

    @patch.object(checkin.time, 'sleep')
    @patch.object(checkin, 'send_telegram')
    @patch.object(checkin, 'checkin', side_effect=[requests.Timeout('timeout'), (True, '今日已签过')])
    @patch.object(checkin, 'load_config', return_value=[('主号', 'cookie1'), ('小号', 'cookie2')])
    def test_failed_account_does_not_block_next_account(self, load, sign, notify, sleep):
        with self.assertRaises(SystemExit) as result:
            checkin.main()
        self.assertEqual(result.exception.code, 1)
        self.assertEqual(sign.call_count, 2)


if __name__ == '__main__':
    unittest.main()
