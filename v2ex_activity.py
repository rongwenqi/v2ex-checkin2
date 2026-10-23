"""读取帖子以积累活跃度；余额变化仅作为奖励到账的间接信号。"""

import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

BASE_URL = "https://www.v2ex.com"


class ActivityError(Exception):
    pass


class PageParser(HTMLParser):
    """只接受本站帖子链接，并从 balance_area 读取完整币种快照。"""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.topics = []
        self.coins = {}
        self.balance_depth = 0
        self.last_text = ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            url = urlsplit(urljoin(BASE_URL, attrs.get("href", "")))
            if url.netloc == "www.v2ex.com" and url.scheme == "https":
                if re.fullmatch(r"/t/\d+", url.path):
                    path = url.path
                    if path not in self.topics:
                        self.topics.append(path)
        if tag == "div":
            if self.balance_depth:
                self.balance_depth += 1
            elif "balance_area" in attrs.get("class", "").split():
                self.balance_depth = 1
                self.last_text = ""
        if self.balance_depth and tag == "img":
            coin = attrs.get("alt", "").upper()
            amount = re.search(r"(\d[\d,]*)\s*$", self.last_text)
            if coin in ("G", "S", "B") and amount:
                self.coins[coin] = int(amount[1].replace(",", ""))
            self.last_text = ""

    def handle_endtag(self, tag):
        if tag == "div" and self.balance_depth:
            self.balance_depth -= 1

    def handle_data(self, data):
        if self.balance_depth:
            self.last_text += data

    @property
    def balance(self):
        if "B" not in self.coins:
            return None
        return tuple(self.coins.get(coin, 0) for coin in ("G", "S", "B"))


def get_page(session, path):
    url = urljoin(BASE_URL, path)
    for _ in range(4):
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != ("https", "www.v2ex.com"):
            raise ActivityError("拒绝向站外地址发送 Cookie")
        if parsed.path.startswith(("/signin", "/2fa")):
            raise ActivityError("未登录或 Cookie 已失效（含 2FA 验证）")
        response = session.get(url, timeout=20, allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if not location:
                raise ActivityError("重定向缺少目标地址")
            url = urljoin(url, location)
            continue
        if response.status_code != 200:
            raise ActivityError(f"访问失败 HTTP {response.status_code}，停止阅读")
        html = response.text
        if re.search(r"cf-challenge|cf-browser-verification|just a moment|attention required", html, re.I):
            raise ActivityError("遇到网站验证页面，停止阅读")
        if re.search(r'href=[\"\']/signin(?:[?\"\'])|需要先登录|请先登录|You need to sign in', html, re.I):
            raise ActivityError("未登录或 Cookie 已失效")
        return html
    raise ActivityError("重定向次数过多")


def daily_topic_limit(today=None):
    """每天随机选择 500～1000 篇，同一北京时间日期保持一致。"""
    if today is None:
        today = datetime.now(timezone(timedelta(hours=8))).date()
    return random.Random(f"v2ex-activity:{today.isoformat()}").randint(500, 1000)


def settings():
    def number(key, default, low, high):
        value = int(os.getenv(key, "").strip() or default)
        if not low <= value <= high:
            raise ValueError(f"{key} 必须在 {low}~{high} 之间")
        return value

    enabled = os.getenv("V2EX_ACTIVITY_ENABLED", "true").strip().lower() or "true"
    if enabled not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        raise ValueError("V2EX_ACTIVITY_ENABLED 必须为 true/false")
    target = number("V2EX_ACTIVITY_MAX_TOPICS", daily_topic_limit(), 1, 1000)
    config = {
        "enabled": enabled in ("true", "1", "yes", "on"),
        "max_topics": target,
        "min_topics": number("V2EX_ACTIVITY_MIN_TOPICS", min(250, target), 0, 1000),
        "gap_min": number("V2EX_ACTIVITY_GAP_MIN", 10, 5, 300),
        "gap_max": number("V2EX_ACTIVITY_GAP_MAX", 15, 5, 300),
        "max_seconds": number("V2EX_ACTIVITY_MAX_SECONDS", 19800, 60, 21600),
    }
    if config["gap_min"] > config["gap_max"]:
        raise ValueError("阅读最小间隔不能大于最大间隔")
    return config


def read_activity(session, name, config, log):
    """在签到之后建立基线，避免将签到收入计为阅读奖励。"""
    started = time.monotonic()
    read = changes = 0
    seen = set()
    baseline = None

    def pause():
        remaining = config["max_seconds"] - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(min(random.uniform(config["gap_min"], config["gap_max"]), remaining))

    def expired():
        return time.monotonic() - started >= config["max_seconds"]

    def check_balance():
        nonlocal baseline, changes
        current = PageParser(get_page(session, "/balance")).balance
        if current is None:
            raise ActivityError("未识别完整余额，停止阅读以避免错误统计")
        if baseline is not None and current > baseline:
            changes += 1
            log(name, f"余额增加 {baseline} → {current}（金/银/铜），疑似活跃度奖励第 {changes} 次")
        baseline = current

    reason = "已遍历可用帖子"
    try:
        check_balance()
        log(name, f"开始活跃度阅读，最多 {config['max_topics']} 篇 / {config['max_seconds']} 秒")
        # 分页按需加载，每次运行仅访问去重后的帖子，不重复刷新同一帖子。
        for page in range(1, 101):
            pause()
            if expired():
                reason = "达到时长上限"
                break
            topics = PageParser(get_page(session, f"/recent?p={page}")).topics
            if not topics:
                raise ActivityError("帖子列表为空或页面结构变化")
            for path in topics:
                if path in seen:
                    continue
                pause()
                if expired():
                    reason = "达到时长上限"
                    break
                html = get_page(session, path)
                if not re.search(r'class=[\"\'][^\"\']*\btopic_content\b', html):
                    raise ActivityError("帖子正文未识别，停止阅读")
                seen.add(path)
                read += 1
                if read % 10 == 0 or read == config["max_topics"]:
                    pause()
                    check_balance()
                    log(name, f"活跃度进度：已读 {read} 篇，余额增加 {changes} 次")
                if changes >= 2 and read >= config["min_topics"]:
                    reason = "已达到阅读目标且观察到两次余额增加"
                    break
                if read >= config["max_topics"]:
                    reason = "达到篇数上限"
                    break
            else:
                continue
            break
        # 非整十篇结束时也检查最后一批阅读后的余额。
        if read and read % 10 and read != config["max_topics"]:
            pause()
            check_balance()
    except (ActivityError, requests.RequestException) as error:
        # 不输出请求对象/响应正文，避免把 Cookie 或页面中的账号信息写入日志。
        reason = str(error) if isinstance(error, ActivityError) else "网络请求失败"
        message = f"活跃度失败：{reason}；已读 {read} 篇，余额增加 {changes} 次"
        log(name, message)
        return False, message
    message = f"活跃度：已读 {read} 篇，余额增加 {changes} 次（疑似奖励）；{reason}"
    if not changes:
        message += "；未确认奖励到账"
    log(name, message)
    return True, message
