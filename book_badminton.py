"""本人会话订场；默认只显示计划。仅 book / schedule 会创建待支付订单。"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
from decimal import Decimal, InvalidOperation
import getpass
from html import unescape
from html.parser import HTMLParser
from http.cookies import SimpleCookie
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
ORIGIN = "https://webssl.xports.cn"
ROOT = "/aisports-weixin"
HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("booking")


class BookingError(Exception): pass
class SessionExpired(BookingError): pass
class NeedsHuman(BookingError): pass
class NotAvailable(BookingError): pass
class AlreadyAttempted(BookingError): pass
class UnknownResult(BookingError): pass


def now():
    return dt.datetime.now(TZ)


def safe_message(value):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value))
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", text)[:600]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def half_hour(value):
    h, m = map(int, value.split(":"))
    if not (0 <= h <= 23 and m in (0, 30)):
        raise ValueError("时间须为 HH:00 或 HH:30")
    return 2 * h + m // 30


def next_release(current, cfg):
    days = (cfg["release_weekday"] - current.weekday()) % 7
    clock = dt.time.fromisoformat(cfg["release_time"])
    release = dt.datetime.combine(current.date() + dt.timedelta(days=days), clock, TZ)
    if release < current and (current-release).total_seconds() > cfg["catch_up_seconds"]:
        release += dt.timedelta(days=7)
    return release


def target_date(release, cfg):
    return release.date() + dt.timedelta(days=cfg["target_days_after_release"])


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = ["venue_id", "venue_name", "service_id", "field_type_priority", "start_time", "end_time",
                "release_weekday", "release_time", "target_days_after_release", "catch_up_seconds",
                "warmup_seconds", "poll_interval_seconds", "max_wait_seconds", "max_commit_attempts",
                "http_timeout_seconds", "reqable_mcp_path"]
    for key in required:
        if key not in cfg:
            raise BookingError(f"配置缺少 {key}")
    if not 0 <= int(cfg["release_weekday"]) <= 6:
        raise BookingError("release_weekday 应为 0–6，周四为 3")
    if half_hour(cfg["start_time"]) >= half_hour(cfg["end_time"]):
        raise BookingError("结束时间须晚于开始时间")
    if not (1 <= cfg["poll_interval_seconds"] <= 30 and 1 <= cfg["max_wait_seconds"] <= 120):
        raise BookingError("轮询间隔须为 1–30 秒，放号窗口须为 1–120 秒")
    if not 1 <= cfg["max_commit_attempts"] <= 3:
        raise BookingError("max_commit_attempts 应为 1–3")
    if not 1 <= cfg["http_timeout_seconds"] <= 30:
        raise BookingError("请求超时应为 1–30 秒")
    if not cfg["field_type_priority"]:
        raise BookingError("需要至少一个场地类型")
    cfg["field_type_priority"] = [str(x) for x in cfg["field_type_priority"]]
    cfg["venue_id"], cfg["service_id"] = str(cfg["venue_id"]), str(cfg["service_id"])
    return cfg


def runtime_path(args, cfg):
    value = args.runtime_dir or cfg.get("runtime_dir")
    if value:
        return Path(os.path.expandvars(value)).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()/"AppData"/"Local"))) / "BadmintonBooking"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local"/"state"))) / "badminton-booking"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 不将本站 Cookie 随重定向转发至其他站点。


class Client:
    def __init__(self, session, cfg):
        self.cookies = dict(session["cookies"])
        self.agent = session.get("user_agent") or "Python BadmintonBooking/1.0"
        self.timeout = cfg["http_timeout_seconds"]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, referer=None):
        if not path.startswith(ROOT + "/") or "\r" in path or "\n" in path:
            raise BookingError("拒绝非场馆路径")
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                   "User-Agent": self.agent, "Accept": "application/json,text/html;q=0.9",
                   "X-Requested-With": "XMLHttpRequest",
                   "Referer": ORIGIN + (referer or ROOT+"/venue/venueQueryList?key=&serviceId=1002")}
        data = None
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(ORIGIN+path, headers=headers, data=data,
                                     method="POST" if payload is not None else "GET")
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        for value in response.headers.get_all("Set-Cookie", []):
            cookie = SimpleCookie()
            cookie.load(value)
            for key, item in cookie.items():
                self.cookies[key] = item.value
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise BookingError("响应过大，停止处理")
        return response.code, raw.decode("utf-8", errors="replace")

    def get_json(self, path):
        status, body = self.request(path)
        if "未查询到公众号信息" in body:
            raise SessionExpired("站点会话失效，请登录并 refresh-session")
        if status != 200:
            raise BookingError(f"只读请求 HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError:
            raise SessionExpired("只读接口未返回 JSON，请刷新会话") from None
        if not isinstance(data, dict) or data.get("error") != 0:
            raise BookingError("只读接口失败：" + safe_message(data.get("message") if isinstance(data, dict) else "格式异常"))
        return data


class CourtPage(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.inputs, self.radios, self.settings = {}, {}, {}
        self.days, self.slots = [], []
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            if a.get("id"):
                self.inputs[a["id"]] = a.get("value", "")
            if a.get("name") == "field":
                self.radios[a.get("value")] = a
        if tag == "dd" and a.get("date"):
            self.days.append(a["date"])
        if a.get("field-segment-id"):
            self.slots.append(a)
        if a.get("id", "").startswith("field-type-"):
            self.settings[a["id"][11:]] = a
    def config(self, ident):
        pattern = r'<[^>]+\bid=["\x27]' + re.escape(ident) + r'["\x27][^>]*>(.*?)</[^>]+>'
        match = re.search(pattern, self.text, re.S)
        value = unescape(match.group(1).strip()) if match else ""
        return json.loads(value) if value else []


def slot_price(slot):
    try:
        price = Decimal(slot.get("price", ""))
        return price if price.is_finite() and price >= 0 else None
    except InvalidOperation:
        return None


def number(value):
    return int(value) if value == int(value) else float(value)


def continuous_choices(page, kind, start, end):
    """只接受同一 field-id 对目标时间的无缝完整覆盖。"""
    if page.inputs.get("wechat-court-human-verify-tag") not in (None, "", "0"):
        raise NeedsHuman("当前场地需要人机验证，请使用正常页面完成")
    if page.config("piece-wise-type"):
        raise NeedsHuman("当前页面有额外分段预约规则，需要人工处理")
    if any(x.get("packageBook") == "1" for x in page.config("channel-time-config")):
        raise NeedsHuman("当前页面有套餐预约规则，需要人工处理")
    settings, radio = page.settings.get(kind), page.radios.get(kind)
    if not settings or not radio:
        raise NotAvailable("缺少当前场地的预约限制")
    book_hour = int(radio.get("data-book-hour", "1"))
    divisor, unit_hours = (1, .5) if book_hour == 0 else (2*book_hour, float(book_hour))
    try:
        max_hours = float(settings["data-max-hour"])/divisor*unit_hours
        total_hours = float(settings["data-total-hour"])/divisor*unit_hours
        min_hours = float(settings["data-min-serial-num"])/divisor*unit_hours
        gap_limit = float(settings["data-pieces-unit"])/divisor*unit_hours
        max_fields = int(settings["data-max-field"])
    except (KeyError, ValueError):
        raise NeedsHuman("页面预约限制格式改变，需要更新脚本") from None
    hours = (end-start)/2
    if max_fields < 1 or not min_hours <= hours <= min(max_hours, total_hours):
        raise NotAvailable("目标时长不符合当前场馆规则")
    groups = {}
    for slot in page.slots:
        if slot.get("state") != "0" or "disabled" in slot.get("class", "").split() or slot_price(slot) is None:
            continue
        try:
            a, b = int(slot["start-time"]), int(slot["end-time"])
        except (KeyError, ValueError):
            continue
        if b <= a or not slot.get("field-id") or not slot.get("field-num"):
            continue
        groups.setdefault(slot["field-id"], []).append(slot)
    choices = []
    for field_id, slots in groups.items():
        selected, cursor = [], start
        while cursor < end:
            matches = [s for s in slots if int(s["start-time"]) == cursor and int(s["end-time"]) <= end]
            if len(matches) != 1:
                break
            selected.append(matches[0])
            cursor = int(matches[0]["end-time"])
        if cursor != end or not selected or len({s["field-num"] for s in selected}) != 1:
            continue
        # 检查选择后新留下的短碎片；原有不可订间隔作为边界。
        left, right = start, end
        while True:
            previous = [s for s in slots if int(s["end-time"]) == left]
            if len(previous) != 1: break
            left = int(previous[0]["start-time"])
        while True:
            following = [s for s in slots if int(s["start-time"]) == right]
            if len(following) != 1: break
            right = int(following[0]["end-time"])
        if any(0 < gap <= gap_limit for gap in ((start-left)/2, (right-end)/2)):
            continue
        choices.append(selected)
    return sorted(choices, key=lambda s: (slot_price(s[0]), int(s[0]["field-num"])))


def payload_for(cfg, date, kind, slots):
    first, last = slots[0], slots[-1]
    price = sum((slot_price(s) for s in slots), Decimal(0))
    listing = sum((Decimal(s.get("listing-price") or "0") for s in slots), Decimal(0))
    merged = {"price": number(price), "startSegment": first["start-time"], "endSegment": last["end-time"],
              "fieldNum": last["field-num"], "fieldId": last["field-id"],
              "fieldSegmentId": last["field-segment-id"], "listingPrice": number(listing),
              "venueName": last.get("venue-name", last["field-num"]+"号场")}
    # 无验证码调用时 JS 会省略未定义的 ticket/randStr；这里保持一致。
    return {"venueId": cfg["venue_id"], "serviceId": cfg["service_id"], "fieldType": kind,
            "day": date.strftime("%Y%m%d"), "fieldInfo": ",".join(s["field-segment-id"] for s in slots),
            "fieldInfoList": [merged]}


def load_session(runtime):
    path = runtime/"session.json"
    if not path.exists():
        raise SessionExpired("没有本地会话，请先运行 refresh-session")
    session = json.loads(path.read_text(encoding="utf-8"))
    cookies = session.get("cookies", {})
    if not cookies.get("JSESSIONID") or any("\r" in str(v) or "\n" in str(v) for v in cookies.values()):
        raise SessionExpired("会话文件格式不正确，请 refresh-session")
    return session


def probe(client, cfg):
    query = urllib.parse.urlencode({"venueId": cfg["venue_id"], "serviceId": cfg["service_id"], "date": now().strftime("%Y%m%d")})
    data = client.get_json(ROOT+"/field/fieldTypes?"+query)
    available = {str(x["fieldType"]): x["fieldTypeName"] for x in data.get("fieldTypeList", [])}
    if not any(k in available for k in cfg["field_type_priority"]):
        raise BookingError("没有找到配置中的场地类型，请核对场馆配置")
    return available


def court_path(cfg, kind, date):
    query = urllib.parse.urlencode({"venueName": cfg["venue_name"], "defaultFullTag": "0", "fullTag": "0", "serviceName": "羽毛球"})
    return ROOT+f'/court/{cfg["venue_id"]}/{cfg["service_id"]}/{kind}/{date:%Y%m%d}?'+query


def scan_kind(session, cfg, date, kind):
    client = Client(session, cfg)
    path = court_path(cfg, kind, date)
    status, text = client.request(path)
    if "未查询到公众号信息" in text:
        raise SessionExpired("站点会话失效，请 refresh-session")
    if status != 200:
        raise NotAvailable(f"场地类型 {kind} 暂未能读取，HTTP {status}")
    page = CourtPage(text)
    day = date.strftime("%Y%m%d")
    if page.inputs.get("date") != day or day not in page.days or page.inputs.get("fieldType") != kind:
        raise NotAvailable(f"场地类型 {kind} 的目标日期尚未开放或无数据")
    if page.inputs.get("venueId") != cfg["venue_id"] or page.inputs.get("serviceId") != cfg["service_id"]:
        raise NeedsHuman("选场页的场馆或项目与配置不一致，停止使用该页面")
    choices = continuous_choices(page, kind, half_hour(cfg["start_time"]), half_hour(cfg["end_time"]))
    return [(kind, payload_for(cfg, date, kind, slots), client, path) for slots in choices]


def gather(session, cfg, date):
    results, reasons = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(cfg["field_type_priority"]))) as pool:
        jobs = {pool.submit(scan_kind, session, cfg, date, kind): kind for kind in cfg["field_type_priority"]}
        for future in concurrent.futures.as_completed(jobs):
            kind = jobs[future]
            try:
                results[kind] = future.result()
            except SessionExpired:
                raise
            except Exception as exc:
                reasons.append(f"{kind}: {safe_message(exc)}")
    candidates = [candidate for kind in cfg["field_type_priority"] for candidate in results.get(kind, [])]
    return candidates, reasons


def state_path(runtime, cfg, date):
    key = f'{cfg["venue_id"]}_{date.isoformat()}_{cfg["start_time"].replace(":", "")}_{cfg["end_time"].replace(":", "")}'
    return runtime/"orders"/(key+".json")


def acquire_state(path, details):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as out:
            json.dump(details, out, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise AlreadyAttempted("已有该日期时段的提交记录。先核对手机订单；不会自动重复下单。") from None


def result_summary(data, payload):
    trade = data.get("trade") if isinstance(data.get("trade"), dict) else {}
    fee = None
    try:
        raw_fee = Decimal(str(data["payFee"]))
        if raw_fee.is_finite() and raw_fee >= 0:
            fee = number(raw_fee/100)
    except (InvalidOperation, KeyError, TypeError, ValueError, OverflowError):
        pass
    return {"created_at": data.get("sysdate"), "trade_id": data.get("tradeId"),
            "pay_state_at_creation": trade.get("payState"), "pay_fee_yuan": fee,
            "expires_at": data.get("tradeExpireTime") or trade.get("expireTime"),
            "field_type": payload["fieldType"], "court": payload["fieldInfoList"][0]["venueName"],
            "date": payload["day"], "preview_yuan": payload["fieldInfoList"][0]["price"]}


def commit_once(client, payload, referer, journal, path):
    journal["state"] = "sending"
    journal["attempts"].append({"at": now().isoformat(), "field_type": payload["fieldType"],
                                 "court": payload["fieldInfoList"][0]["venueName"], "field_info": payload["fieldInfo"]})
    atomic_json(path, journal)  # 网络发送前落盘；崩溃和超时不会被当成未提交。
    try:
        status, body = client.request(ROOT+"/court/commit", payload, referer)
        if status != 200:
            raise UnknownResult(f"提交返回 HTTP {status}，结果未知，请先查手机订单")
        try:
            data = json.loads(body)
        except ValueError:
            raise UnknownResult("提交返回非 JSON，结果未知，请先查手机订单") from None
        if not isinstance(data, dict) or "error" not in data:
            raise UnknownResult("提交响应格式改变，结果未知，请先查手机订单")
        if data["error"] == 0:
            if not data.get("tradeId"):
                raise UnknownResult("提交成功但缺少订单号，请查手机订单")
            # 先保存已明确返回的订单号，再整理可选金额/期限，避免附加字段异常丢失成功凭据。
            journal.update(state="created", result={"trade_id": data["tradeId"]})
            atomic_json(path, journal)
            summary = result_summary(data, payload)
            journal.update(state="created_unpaid" if str(summary["pay_state_at_creation"]) == "0" else "created", result=summary)
            atomic_json(path, journal)
            return journal["result"]
        if data.get("tradeId"):
            raise UnknownResult("响应同时含错误和订单号，请查手机订单")
        message = safe_message(data.get("message", "未返回原因"))
        journal.update(state="rejected", error=data["error"], message=message)
        atomic_json(path, journal)
        if data["error"] == 404:
            raise SessionExpired("站点要求重新登录会员，请登录后抓取新会话；业务码 404")
        # 只在明确说明场地已售/占用、且无订单号时，允许本次运行换场。
        if re.search(r"已被.{0,12}(预订|预定|占用)|已(预订|预定|售罄)|场地.{0,10}(被占用|已售|不可用)", message):
            raise NotAvailable("库存冲突："+message)
        raise BookingError("下单被拒绝："+message)
    except (SessionExpired, NotAvailable, BookingError) as exc:
        if isinstance(exc, UnknownResult) or journal.get("state") == "sending":
            journal.update(state="unknown", message=safe_message(exc))
            atomic_json(path, journal)
        raise
    except BaseException as exc:
        journal.update(state="unknown", message="网络中断或处理异常；先查手机订单，不自动重发")
        atomic_json(path, journal)
        raise UnknownResult(journal["message"]) from exc


def booking_rules(client, cfg):
    keys = "guo_xin_ca_flag,field_notice_countdown,wechat_field_order_delay,wechat_verify_tag"
    rules = client.get_json(ROOT+"/common/getVenueParam?"+urllib.parse.urlencode({"paramKeys": keys}))
    if rules.get("guo_xin_ca_flag") == "1":
        extra = client.get_json(ROOT+"/common/getVenueParam?"+urllib.parse.urlencode({"paramKeys": "is_football_park", "centerId": cfg["venue_id"]}))
        if extra.get("is_football_park") == "1":
            raise NeedsHuman("当前场馆启用了联动场地规则，需要使用正常页面")
    opening = client.get_json(ROOT+"/common/getVenueParam?"+urllib.parse.urlencode({"paramKeys": "wechat_field_open_time", "centerId": cfg["venue_id"]}))
    rules["wechat_field_open_time"] = opening.get("wechat_field_open_time", "")
    return rules


def honor_delay(rules):
    countdown = float(rules.get("field_notice_countdown") or 0)
    if countdown > 0:
        LOG.info("遵循场馆须知倒计时 %.0f 秒", countdown)
        end = time.monotonic()+countdown
        while time.monotonic() < end:
            time.sleep(min(1, end-time.monotonic()))
    opening = rules.get("wechat_field_open_time") or ""
    if rules.get("wechat_field_order_delay") == "1" and re.match(r"^\d{4}", opening):
        limit = now().replace(hour=int(opening[:2]), minute=int(opening[2:4]), second=5, microsecond=0)
        if now() <= limit:
            LOG.info("遵循场馆当前提交等待设置（3秒）")
            time.sleep(3)


def prepare(cfg, runtime):
    session = load_session(runtime)
    client = Client(session, cfg)
    probe(client, cfg)
    rules = booking_rules(client, cfg)
    session = dict(session, cookies=dict(client.cookies))
    return {"session": session, "rules": rules}


def book(cfg, runtime, date, dry=False, prepared=None):
    if not dry and state_path(runtime, cfg, date).exists():
        raise AlreadyAttempted("该日期时段已有提交记录，不会重复创建；请用 status 或手机查单")
    if dry:
        session = load_session(runtime)
        probe(Client(session, cfg), cfg)
        rules = {}
    else:
        prepared = prepared or prepare(cfg, runtime)
        session, rules = prepared["session"], prepared["rules"]
    deadline = time.monotonic()+(0 if dry else cfg["max_wait_seconds"])
    journal = {"state": "preparing", "date": date.isoformat(), "start_time": cfg["start_time"],
               "end_time": cfg["end_time"], "attempts": []}
    path, acquired, tried, last_reasons = state_path(runtime, cfg, date), False, set(), []
    while True:
        candidates, reasons = gather(session, cfg, date)
        last_reasons = reasons
        if dry:
            plans = [{"field_type": k, "court": p["fieldInfoList"][0]["venueName"], "preview_yuan": p["fieldInfoList"][0]["price"]} for k, p, c, r in candidates]
            print(json.dumps({"date": date.isoformat(), "time": cfg["start_time"]+"–"+cfg["end_time"], "candidates": plans, "notes": reasons, "submitted": False}, ensure_ascii=False, indent=2))
            return None
        candidate = next((c for c in candidates if c[1]["fieldInfo"] not in tried), None)
        if candidate:
            if not acquired:
                acquire_state(path, journal)
                acquired = True
            kind, payload, candidate_client, referer = candidate
            tried.add(payload["fieldInfo"])
            LOG.info("提交 %s %s %s %s–%s；预览 %.2f 元（最终金额以服务器为准）", date, kind,
                     payload["fieldInfoList"][0]["venueName"], cfg["start_time"], cfg["end_time"], payload["fieldInfoList"][0]["price"])
            honor_delay(rules)
            try:
                result = commit_once(candidate_client, payload, referer, journal, path)
                LOG.info("已创建订单 %s，应付 %s 元，截止 %s，创建时支付状态 %s（0=未支付）。请在手机核对。",
                         result["trade_id"], result["pay_fee_yuan"], result["expires_at"], result["pay_state_at_creation"])
                notify()
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return result
            except NotAvailable as exc:
                LOG.warning("%s", safe_message(exc))
                if len(journal["attempts"]) >= cfg["max_commit_attempts"]:
                    raise BookingError("明确库存冲突已达到本次提交次数上限") from None
        if time.monotonic() >= deadline:
            raise NotAvailable("窗口内未找到目标同场连续时段。" + "；".join(last_reasons))
        time.sleep(min(cfg["poll_interval_seconds"], max(0, deadline-time.monotonic())))


def notify():
    if os.name == "nt":
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass


class ReqableReader:
    ALLOWED = {"capture_live_filter", "capture_live_get_by_id"}
    def __init__(self, exe, token):
        if not Path(exe).is_file():
            raise BookingError("找不到 Reqable mcp-server.exe，请修改配置路径")
        env = os.environ.copy()
        env["REQABLE_TOKEN"] = token
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen([exe, "--scope", "minimal"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, creationflags=flags)
        self.inbox, self.request_id = queue.Queue(), 0
        def reader():
            for line in self.process.stdout:
                try: self.inbox.put(json.loads(line))
                except ValueError: pass
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "badminton-booking-reader", "version": "1.0"}})
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})+"\n")
            self.process.stdin.flush()
        except BaseException:
            self.close()
            raise
    def rpc(self, method, params):
        self.request_id += 1
        rid = self.request_id
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})+"\n")
        self.process.stdin.flush()
        deadline = time.monotonic()+20
        while True:
            try: reply = self.inbox.get(timeout=max(.01, deadline-time.monotonic()))
            except queue.Empty:
                raise BookingError("Reqable 读取超时，请确认软件正在运行、API 服务已开启") from None
            if reply.get("id") == rid:
                if reply.get("error"): raise BookingError("Reqable 接口错误")
                return reply.get("result", {})
    def call(self, name, args):
        if name not in self.ALLOWED:
            raise BookingError("只允许读取 Reqable 抓包记录")
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            message = " ".join(x.get("text", "") for x in result.get("content", []))
            raise BookingError("Reqable："+safe_message(message))
        if "structuredContent" in result:
            return result["structuredContent"]
        return json.loads("\n".join(x["text"] for x in result.get("content", []) if x.get("type") == "text"))
    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)


def refresh_session(cfg, runtime):
    token = os.environ.get("REQABLE_TOKEN") or getpass.getpass("Reqable API 令牌（隐藏输入，不存盘）：")
    if not token.strip(): raise BookingError("令牌不能为空")
    reader = ReqableReader(cfg["reqable_mcp_path"], token.strip())
    try:
        ids = reader.call("capture_live_filter", {"filters": [{"type": "host", "hosts": ["webssl.xports.cn"]}]}).get("items", [])
        for rid in sorted(ids, reverse=True):
            record = reader.call("capture_live_get_by_id", {"id": rid})
            if urllib.parse.urlsplit(record.get("url", "")).hostname != "webssl.xports.cn": continue
            cookies, agent = {}, None
            for h in record.get("request", {}).get("headers", []):
                if h["name"].lower() == "user-agent": agent = h["value"]
                if h["name"].lower() == "cookie":
                    for item in h["value"].split(";"):
                        if "=" in item:
                            k, v = item.strip().split("=", 1)
                            cookies[k] = v
            if cookies.get("JSESSIONID"):
                session = {"cookies": cookies, "user_agent": agent, "saved_at": now().isoformat(),
                           "capture_id": rid, "captured_at": record.get("connection", {}).get("timestamp")}
                probe(Client(session, cfg), cfg)  # 先验证，再替换原会话。
                atomic_json(runtime/"session.json", session)
                LOG.info("会话已更新（抓包记录 %s）；仅验证读取有效，未提交订单。", rid)
                return
        raise SessionExpired("没有找到带 JSESSIONID 的场馆请求。请先登录并抓取一次营业厅访问。")
    finally:
        reader.close()


def show_plan(cfg):
    release = next_release(now(), cfg)
    print(f"北京时间：{now():%Y-%m-%d %H:%M:%S}\n下次放号：{release:%Y-%m-%d %A %H:%M:%S}\n"
          f"目标日期：{target_date(release, cfg)}\n时段：{cfg['start_time']}–{cfg['end_time']}，同一片连续场地\n"
          f"场馆：{cfg['venue_name']}；类型优先顺序：{', '.join(cfg['field_type_priority'])}\n"
          "当前只显示计划，没有预约或下单。")


def schedule(cfg, runtime):
    LOG.info("定时预约已启动；保持电脑开机、联网，按 Ctrl+C 退出。")
    while True:
        release = next_release(now(), cfg)
        date = target_date(release, cfg)
        LOG.info("等待北京时间 %s；目标 %s %s–%s", release.isoformat(), date, cfg["start_time"], cfg["end_time"])
        warmup = release-dt.timedelta(seconds=cfg["warmup_seconds"])
        while now() < warmup:
            time.sleep(min(10, max(.01, (warmup-now()).total_seconds())))
        prepared = None
        try:
            prepared = prepare(cfg, runtime)
            LOG.info("放号前会话和场馆规则预读通过。")
        except Exception as exc:
            LOG.error("放号前检查失败：%s", safe_message(exc))
            notify()
        while now() < release:
            time.sleep(min(.2, max(.001, (release-now()).total_seconds())))
        try:
            book(cfg, runtime, date, prepared=prepared)
        except Exception as exc:
            LOG.error("本周预约结束：%s", safe_message(exc))
            notify()
        # 本周无论成功或失败都不在同一放号时刻反复启动。
        next_week = release+dt.timedelta(days=7)
        while now() < next_week-dt.timedelta(seconds=cfg["warmup_seconds"]):
            time.sleep(min(10, max(.01, (next_week-dt.timedelta(seconds=cfg["warmup_seconds"])-now()).total_seconds())))


def status(runtime):
    files = sorted((runtime/"orders").glob("*.json"), reverse=True)[:20]
    if not files:
        print("没有本脚本的提交记录。手机中既有订单不会自动导入。")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps({k: data.get(k) for k in ["date", "start_time", "end_time", "state", "result", "message"] if k in data}, ensure_ascii=False))
    print("上述是本地提交记录；最新支付/取消状态以手机场馆订单为准。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="plan", choices=["plan", "refresh-session", "check", "dry-run", "book", "schedule", "status"])
    parser.add_argument("--config", default=str(HERE/"config.json"))
    parser.add_argument("--runtime-dir", help="本地会话、日志及防重复记录目录")
    parser.add_argument("--date", type=dt.date.fromisoformat, help="dry-run/book 的明确目标日期 YYYY-MM-DD")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "plan":
        show_plan(cfg)
        return 0
    runtime = runtime_path(args, cfg)
    runtime.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), logging.FileHandler(runtime/"booking.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.Formatter.converter = lambda *unused: now().timetuple()
    LOG.info("本地运行目录：%s", runtime)
    try:
        if args.command == "refresh-session": refresh_session(cfg, runtime)
        elif args.command == "check":
            session = load_session(runtime)
            names = probe(Client(session, cfg), cfg)
            LOG.info("站点会话读取有效：%s；不代表已验证会员下单权限。", "、".join(names.values()))
        elif args.command in ("book", "dry-run"):
            if args.date is None: raise BookingError("book/dry-run 需要明确提供 --date YYYY-MM-DD")
            if args.date < now().date(): raise BookingError("不接受已过去的日期")
            book(cfg, runtime, args.date, dry=args.command == "dry-run")
        elif args.command == "schedule": schedule(cfg, runtime)
        elif args.command == "status": status(runtime)
        return 0
    except KeyboardInterrupt:
        LOG.info("已退出。若在提交中断，先核对手机订单，再处理本地记录。")
        return 130
    except Exception as exc:
        LOG.error("%s", safe_message(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
