
# -*- coding: UTF-8 -*-

import logging
import random
import re
import socket
import time
import json
import urllib.request

from urllib.parse import urlsplit

import requests
import configNew as config

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from pydoll.sync import Chrome
from pydoll.exceptions import WaitTimeout


# ============================================================
# 基础配置
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)

# 防止 CDP 网络请求、Cookie 和验证信息进入 DEBUG 日志
for logger_name in (
    "pydoll",
    "websockets",
    "asyncio",
    "urllib3",
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)


# 只允许使用已确认的 GLaDOS 官方域名
ALLOWED_DOMAINS = {
    "glados.cloud",
    "glados.network",
    "glados.rocks",
    "glados.one",
    "glados.space",
    "glados.vip",
    "glados-facility.com",
}

CDP_HOST = "chromium"
CDP_PORT = 9223

TURNSTILE_DETECT_TIMEOUT = 20
TURNSTILE_VERIFY_TIMEOUT = 45
CHECKIN_RESPONSE_TIMEOUT = 90

EXCHANGE_THRESHOLDS = {
    "plan100": 100,
    "plan200": 200,
    "plan500": 500,
}

# 只检查是否生成验证响应，不读取、打印或保存令牌。
TURNSTILE_READY_SCRIPT = """
(() => {
    const fields = document.querySelectorAll(
        'input[name="cf-turnstile-response"], ' +
        'textarea[name="cf-turnstile-response"]'
    );

    return Array.from(fields).some(
        element => Boolean(element.value)
    );
})()
"""


# ============================================================
# HTTP Session：仅用于 Go-WXPush
# ============================================================

def get_robust_session(proxy=None):
    """保留原有 HTTP 代理和重试配置。"""

    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST", "GET"],
    )

    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_strategy)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if proxy:
        session.proxies = {
            "http": proxy,
            "https": proxy,
        }
        logging.info("已配置 Go-WXPush HTTP 代理")
    else:
        logging.info("未配置代理，使用直连")

    return session


# ============================================================
# 通用工具
# ============================================================

def to_int(value):
    """将接口数字安全转换为整数。"""

    try:
        if value is None or isinstance(value, bool):
            return None
        return int(float(value))
    except (ValueError, TypeError, OverflowError):
        return None


def get_earned_points(data):
    """提取签到奖励积分。"""

    records = data.get("list")

    if (
        isinstance(records, list)
        and records
        and isinstance(records[0], dict)
    ):
        value = to_int(records[0].get("change"))
        if value is not None:
            return value

    return to_int(data.get("points"))


def make_result(
    kind,
    message,
    email="Unknown",
    leftdays=None,
    points=None,
    points_gained=None,
    exchange="",
):
    """统一账号执行结果。"""

    return {
        "kind": kind,
        "message": message,
        "email": email,
        "leftdays": leftdays,
        "points": points,
        "points_gained": points_gained,
        "exchange": exchange,
        "ok": kind in ("success", "repeat"),
    }


def get_cdp_websocket():
    """获取现有 Chromium 的 CDP WebSocket 地址。"""

    ip = socket.gethostbyname(CDP_HOST)
    endpoint = f"http://{ip}:{CDP_PORT}/json/version"

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )

    with opener.open(endpoint, timeout=10) as response:
        metadata = json.load(response)

    ws_url = metadata.get("webSocketDebuggerUrl")

    if not ws_url:
        raise RuntimeError("Chromium 未返回 CDP WebSocket 地址")

    path = urlsplit(ws_url).path

    if not path.startswith("/devtools/browser/"):
        raise RuntimeError("CDP WebSocket 地址格式异常")

    return f"ws://{ip}:{CDP_PORT}{path}"


def read_json_response(response):
    """安全解析 Pydoll 返回的 JSON。"""

    try:
        data = response.json()
    except (ValueError, TypeError):
        return None

    return data if isinstance(data, dict) else None


def find_checkin_button(page):
    """等待 Checkin 按钮出现。"""

    for _ in range(6):
        buttons = page.find(
            tag_name="button",
            find_all=True,
            timeout=5,
            raise_exc=False,
        ) or []

        for button in buttons:
            label = button.text().strip().lower()
            normalized = re.sub(r"\s+", "", label)

            if normalized == "checkin":
                return button

        time.sleep(2)

    return None


def wait_for_turnstile(page, timeout=45):
    """
    等待 Turnstile 生成非空验证响应。

    如果官网没有使用标准响应字段，返回 False。
    不读取、保存或打印令牌。
    """

    logging.info("正在等待 Turnstile 验证响应...")

    try:
        page.wait_for_script(
            TURNSTILE_READY_SCRIPT,
            timeout=timeout,
        )

        logging.info("检测到 Turnstile 非空响应字段")
        return True

    except WaitTimeout:
        logging.warning(
            "Turnstile 验证未确认完成，"
            "或官网没有提供标准响应字段"
        )
        return False


def bring_page_to_front(page):
    """尽可能将需要人工处理的页面放到 VNC 前台。"""

    try:
        page.bring_to_front()
    except Exception:
        pass


# ============================================================
# 单个域名的签到流程
# ============================================================

def browser_checkin(
    domain,
    seen_emails,
    expected_email=None,
    exchange_plan=None,
    auto_exchange=True,
):
    """
    使用现有 Chromium 处理一个 GLaDOS 域名。

    每个域名：
    - 使用该域名保存的浏览器登录会话
    - 检查服务端账号身份
    - 按邮箱去重
    - 处理 Turnstile
    - 点击一次 Checkin
    - 查询积分
    - 按配置自动兑换

    seen_emails 是本次任务已处理过的邮箱集合。
    """

    if domain not in ALLOWED_DOMAINS:
        return make_result(
            "config_error",
            f"不支持的 GLaDOS 域名：{domain}",
        )

    origin = f"https://{domain}"

    checkin_page = origin + "/console/checkin"
    status_api = origin + "/api/user/status"
    points_api = origin + "/api/user/points"
    exchange_api = origin + "/api/user/exchange"

    # 动态匹配当前域名的签到接口
    checkin_url_pattern = re.compile(
        rf"^{re.escape(origin)}"
        r"/api/user/checkin/?(?:\?.*)?$"
    )

    browser = Chrome()
    page = None
    keep_page_open = False

    email = "Unknown"
    leftdays = None
    current_points = None

    try:
        # ----------------------------------------------------
        # 1. 连接 Chromium
        # ----------------------------------------------------

        logging.info("[%s] 正在连接 Chromium...", domain)

        browser.connect(get_cdp_websocket())

        page = browser.new_tab()

        logging.info("[%s] 正在打开签到页面...", domain)

        # 保留已经验证成功的 Turnstile 处理方式
        with page.expect_cloudflare_turnstile(
            time_to_wait_captcha=TURNSTILE_DETECT_TIMEOUT
        ):
            page.go_to(
                checkin_page,
                timeout=60,
            )

        logging.info("[%s] 页面加载完成", domain)

        # ----------------------------------------------------
        # 2. 检查域名是否跳转
        # ----------------------------------------------------

        actual_domain = urlsplit(
            page.current_url()
        ).hostname

        if actual_domain != domain:
            keep_page_open = True

            return make_result(
                "domain_mismatch",
                f"配置域名为 {domain}，"
                f"但页面实际跳转到了 {actual_domain}。"
                "为防止账号混淆，已跳过签到和兑换。",
            )

        # ----------------------------------------------------
        # 3. 检查当前登录状态
        # ----------------------------------------------------

        logging.info("[%s] 正在检查登录状态...", domain)

        status_resp = page.request.get(status_api)

        status_http = status_resp.status_code
        status_data = read_json_response(status_resp)

        status_code = (
            to_int(status_data.get("code"))
            if isinstance(status_data, dict)
            else None
        )

        if status_http == 401 or status_code == -2:
            keep_page_open = True

            return make_result(
                "login_expired",
                f"❌ {domain} 登录状态已失效。"
                f"请通过 NAS Chromium VNC "
                f"重新登录该域名对应的 GLaDOS 账号。"
                "本次未执行签到和兑换。",
            )

        if status_http != 200:
            return make_result(
                "status_error",
                f"❌ {domain} 登录状态检查失败，"
                f"HTTP {status_http}。"
                "可能是网络或 Cloudflare 问题，"
                "暂不能确定 Cookie 是否过期。",
            )

        if (
            not isinstance(status_data, dict)
            or status_code != 0
            or not isinstance(status_data.get("data"), dict)
        ):
            keep_page_open = True

            return make_result(
                "status_error",
                f"❌ {domain} 无法确认登录状态。"
                "请通过 VNC 检查该域名的账号。",
            )

        account_info = status_data["data"]

        email = str(
            account_info.get("email") or ""
        ).strip()

        leftdays = to_int(
            account_info.get("leftDays")
        )

        # ----------------------------------------------------
        # 4. 校验账号身份
        # ----------------------------------------------------

        if not email:
            return make_result(
                "identity_error",
                "❌ 登录接口未返回账号邮箱。"
                "无法确认账号身份，已跳过签到和兑换。",
                leftdays=leftdays,
            )

        email_key = email.casefold()

        if expected_email:
            expected_key = expected_email.strip().casefold()

            if email_key != expected_key:
                keep_page_open = True

                return make_result(
                    "identity_mismatch",
                    f"❌ 当前域名实际登录的是 {email}，"
                    "与 configNew.py 中的预期账号不一致。"
                    "已跳过签到和兑换。",
                    email=email,
                    leftdays=leftdays,
                )

        # ----------------------------------------------------
        # 5. 防止同一个账号重复签到
        # ----------------------------------------------------

        if email_key in seen_emails:
            logging.warning(
                "[%s] 检测到重复账号，跳过",
                domain,
            )

            return make_result(
                "duplicate",
                "该账号已经通过本次任务中的其他域名处理，"
                "为了避免重复提交签到及兑换，"
                "本域名已跳过。",
                email=email,
                leftdays=leftdays,
            )

        # 身份确认后立即登记。
        # 即使之后结果不明，也不会换一个域名重复提交。
        seen_emails.add(email_key)

        logging.info(
            "[%s] 登录有效，账号身份已确认",
            domain,
        )

        # ----------------------------------------------------
        # 6. 等待 Turnstile
        # ----------------------------------------------------

        verified = wait_for_turnstile(
            page,
            timeout=TURNSTILE_VERIFY_TIMEOUT,
        )

        if not verified:
            keep_page_open = True
            bring_page_to_front(page)

            return make_result(
                "verification_required",
                f"⚠️ {domain} 登录有效，"
                "但 Turnstile 验证尚未确认完成。"
                "请通过 NAS Chromium VNC "
                "检查保留的该域名签到页面。"
                "本次未点击签到，也未执行兑换。",
                email=email,
                leftdays=leftdays,
            )

        # ----------------------------------------------------
        # 7. 查找 Checkin 按钮
        # ----------------------------------------------------

        checkin_button = find_checkin_button(page)

        if checkin_button is None:
            keep_page_open = True
            bring_page_to_front(page)

            return make_result(
                "unknown",
                "❌ 未找到 Checkin 按钮。"
                "可能是页面发生变化，"
                "请通过 VNC 检查保留的标签页。",
                email=email,
                leftdays=leftdays,
            )

        if not checkin_button.is_enabled:
            keep_page_open = True
            bring_page_to_front(page)

            return make_result(
                "unknown",
                "⚠️ Checkin 按钮不可用，"
                "本次未提交签到。",
                email=email,
                leftdays=leftdays,
            )

        # ----------------------------------------------------
        # 8. 只点击一次，捕获签到响应
        # ----------------------------------------------------

        logging.info("[%s] 正在执行 Checkin...", domain)

        try:
            with page.expect_response(
                checkin_url_pattern,
                timeout=CHECKIN_RESPONSE_TIMEOUT,
            ) as checkin_response:
                checkin_button.click()

        except WaitTimeout:
            keep_page_open = True
            bring_page_to_front(page)

            return make_result(
                "unknown",
                "⚠️ 点击 Checkin 后未收到匹配的签到响应。"
                "可能是验证未通过、网页异常或接口发生变化。"
                "已保留标签页供检查。"
                "结果不明确，不会重复提交或自动兑换。",
                email=email,
                leftdays=leftdays,
            )

        # ----------------------------------------------------
        # 9. 解析签到响应
        # ----------------------------------------------------

        checkin_http = checkin_response.status

        try:
            checkin_data = checkin_response.json()
        except (ValueError, TypeError):
            keep_page_open = True

            return make_result(
                "unknown",
                f"⚠️ 签到响应不是有效 JSON "
                f"（HTTP {checkin_http}）。"
                "请检查官网签到记录。",
                email=email,
                leftdays=leftdays,
            )

        if not isinstance(checkin_data, dict):
            keep_page_open = True

            return make_result(
                "unknown",
                "⚠️ 签到响应格式异常，"
                "无法确认本次签到结果。",
                email=email,
                leftdays=leftdays,
            )

        code = to_int(checkin_data.get("code"))
        msg = str(checkin_data.get("message", ""))
        reason = str(checkin_data.get("reason", ""))

        logging.info(
            "[%s] 签到响应 HTTP=%s, code=%s, reason=%s",
            domain,
            checkin_http,
            code,
            reason,
        )

        # ----------------------------------------------------
        # 10. 分类处理
        # ----------------------------------------------------

        if checkin_http == 401 or code == -2:
            keep_page_open = True

            return make_result(
                "login_expired",
                f"❌ {domain} 签到时认证失效。"
                "请在该域名重新登录。",
                email=email,
                leftdays=leftdays,
            )

        if code == 5 and reason == "turnstile":
            keep_page_open = True

            return make_result(
                "turnstile_failed",
                f"❌ {domain} 的 Turnstile "
                "验证被服务器拒绝。"
                "请通过 VNC 检查。",
                email=email,
                leftdays=leftdays,
            )

        if code == 4 and reason == "device-mismatch":
            return make_result(
                "device_mismatch",
                "❌ 登录设备不匹配。"
                f"建议在 NAS Chromium 中重新登录 {domain}。",
                email=email,
                leftdays=leftdays,
            )

        if "please checkin via" in msg.lower():
            return make_result(
                "domain_mismatch",
                f"❌ 签到域名不匹配：{msg}",
                email=email,
                leftdays=leftdays,
            )

        if checkin_http != 200:
            return make_result(
                "rejected",
                f"❌ 签到请求被拒绝："
                f"HTTP {checkin_http}，code={code}。",
                email=email,
                leftdays=leftdays,
            )

        if code == 0:
            kind = "success"

            points_gained = get_earned_points(
                checkin_data
            )

            if points_gained is None:
                message_status = (
                    "✅ 签到成功，奖励积分以官网流水为准"
                )
            else:
                message_status = (
                    f"✅ 签到成功，获得 {points_gained} 积分"
                )

        elif code == 1:
            kind = "repeat"
            points_gained = None
            message_status = "✅ 今日已签到"

        else:
            return make_result(
                "rejected",
                f"❌ 签到异常：code={code}，"
                f"reason={reason}，message={msg}",
                email=email,
                leftdays=leftdays,
            )

        logging.info(
            "[%s] %s",
            domain,
            message_status,
        )

        # ----------------------------------------------------
        # 11. 查询当前积分
        # ----------------------------------------------------

        exchange_message = ""

        try:
            points_resp = page.request.get(points_api)
            points_data = read_json_response(points_resp)

            if points_resp.status_code != 200:
                exchange_message = (
                    "⚠️ 积分查询失败，已跳过自动兑换"
                )

            elif not isinstance(points_data, dict):
                exchange_message = (
                    "⚠️ 积分响应异常，已跳过自动兑换"
                )

            elif to_int(points_data.get("code", 0)) != 0:
                exchange_message = (
                    "⚠️ 积分查询被拒绝，已跳过自动兑换"
                )

            else:
                current_points = to_int(
                    points_data.get("points")
                )

                if current_points is None:
                    exchange_message = (
                        "⚠️ 积分解析失败，已跳过自动兑换"
                    )
                else:
                    logging.info(
                        "[%s] 当前积分：%s",
                        domain,
                        current_points,
                    )

        except Exception as exc:
            logging.warning(
                "[%s] 积分查询异常：%s",
                domain,
                type(exc).__name__,
            )

            exchange_message = (
                "⚠️ 积分查询异常，已跳过自动兑换"
            )

        # ----------------------------------------------------
        # 12. 自动兑换
        # ----------------------------------------------------

        if exchange_plan is None:
            exchange_plan = getattr(
                config,
                "exchange_plan",
                "plan500",
            )

        if not auto_exchange:
            if current_points is not None:
                exchange_message = (
                    "自动兑换已关闭"
                )

        elif exchange_plan not in EXCHANGE_THRESHOLDS:
            exchange_message = (
                "⚠️ 自动兑换套餐配置错误，已跳过兑换"
            )

        elif current_points is not None:
            threshold = EXCHANGE_THRESHOLDS[exchange_plan]

            if current_points >= threshold:
                logging.info(
                    "[%s] 积分达到兑换门槛，准备兑换 %s",
                    domain,
                    exchange_plan,
                )

                try:
                    exch_resp = page.request.post(
                        exchange_api,
                        json={
                            "planType": exchange_plan
                        },
                    )

                    exch_data = read_json_response(
                        exch_resp
                    )

                    if (
                        exch_resp.status_code == 200
                        and isinstance(exch_data, dict)
                        and to_int(exch_data.get("code")) == 0
                    ):
                        exchange_message = (
                            f"✅ 自动兑换成功 ({exchange_plan})"
                        )

                    else:
                        reason_text = (
                            str(exch_data.get("message", "接口异常"))
                            if isinstance(exch_data, dict)
                            else "接口响应异常"
                        )

                        exchange_message = (
                            f"❌ 自动兑换失败：{reason_text}"
                        )

                except Exception as exc:
                    logging.error(
                        "[%s] 自动兑换异常：%s",
                        domain,
                        type(exc).__name__,
                    )

                    exchange_message = (
                        "⚠️ 自动兑换结果不明确，"
                        "请检查官网积分流水，"
                        "不要立即重复兑换"
                    )

        # ----------------------------------------------------
        # 13. 返回该账号结果
        # ----------------------------------------------------

        return make_result(
            kind,
            message_status,
            email=email,
            leftdays=leftdays,
            points=current_points,
            points_gained=points_gained,
            exchange=exchange_message,
        )

    except Exception as exc:
        # 不打印异常详情，避免其中包含 CDP 敏感信息
        logging.error(
            "[%s] 浏览器签到执行异常：%s",
            domain,
            type(exc).__name__,
        )

        return make_result(
            "unknown",
            "❌ 浏览器签到执行异常，"
            "无法确认本次签到是否完成。"
            "请检查青龙日志及官网记录，"
            "本次不自动重试。",
            email=email,
            leftdays=leftdays,
            points=current_points,
        )

    finally:
        # 失败时可保留页面供 VNC 人工检查
        if page is not None and not keep_page_open:
            try:
                page.close()
            except Exception:
                pass

        elif page is not None:
            logging.warning(
                "[%s] 已保留标签页供 VNC 检查",
                domain,
            )

        # 只断开 CDP，不停止已有 Chromium
        try:
            browser.close()
        except Exception:
            pass


# ============================================================
# 多账号调度
# ============================================================

def checkin_all_accounts():
    """按照 configNew.glados_accounts 顺序串行签到。"""

    raw_accounts = getattr(
        config,
        "glados_accounts",
        None,
    )

    if not isinstance(raw_accounts, (list, tuple)) or not raw_accounts:
        return [{
            **make_result(
                "config_error",
                "❌ configNew.py 未正确配置 glados_accounts",
            ),
            "account_name": "配置错误",
            "domain": "未配置",
        }]

    results = []
    seen_emails = set()
    seen_domains = set()

    for index, item in enumerate(raw_accounts, start=1):

        if not isinstance(item, dict):
            results.append({
                **make_result(
                    "config_error",
                    "账号配置必须是字典",
                ),
                "account_name": f"账号{index}",
                "domain": "配置错误",
            })
            continue

        name = str(
            item.get("name") or f"账号{index}"
        ).strip()

        domain = str(
            item.get("domain") or ""
        ).strip().lower()

        expected_email = str(
            item.get("expected_email") or ""
        ).strip()

        exchange_plan = item.get(
            "exchange_plan",
            getattr(config, "exchange_plan", "plan500"),
        )

        auto_exchange = item.get(
            "auto_exchange",
            getattr(config, "auto_exchange", True),
        )

        # 只允许固定白名单中的域名，避免访问意外站点
        if domain not in ALLOWED_DOMAINS:
            result = make_result(
                "config_error",
                f"❌ 域名 {domain} 不在允许的 GLaDOS 列表中",
            )

        elif domain in seen_domains:
            result = make_result(
                "duplicate",
                "同一个域名在配置中出现多次，"
                "已跳过重复配置",
            )

        else:
            seen_domains.add(domain)

            logging.info(
                "========== 开始处理 %s (%s) ==========",
                name,
                domain,
            )

            result = browser_checkin(
                domain=domain,
                seen_emails=seen_emails,
                expected_email=expected_email or None,
                exchange_plan=exchange_plan,
                auto_exchange=auto_exchange,
            )

        result["account_name"] = name
        result["domain"] = domain

        results.append(result)

        logging.info(
            "账号 %s 处理完成，状态：%s",
            name,
            result["kind"],
        )

        # 串行执行，避免同时操作共享的 Chromium Profile
        if index < len(raw_accounts):
            time.sleep(2)

    return results


# ============================================================
# Go-WXPush 多账号汇总通知
# ============================================================

def send_wxpush(session, results):
    """汇总全部账号，发送一次 Go-WXPush。"""

    success_count = sum(
        1 for r in results if r["ok"]
    )

    skip_count = sum(
        1 for r in results if r["kind"] == "duplicate"
    )

    fail_count = (
        len(results) - success_count - skip_count
    )

    lines = [
        f"GLaDOS签到 成功:{success_count} 失败:{fail_count} 跳过:{skip_count}",
    ]

    for result in results:
        name = result.get(
            "account_name",
            "未命名账号",
        )

        domain = result.get(
            "domain",
            "未知域名",
        )

        email = result.get(
            "email",
            "Unknown",
        )

        leftdays = result.get("leftdays")
        points = result.get("points")
        exchange = result.get("exchange", "")

        lines.extend([
            "",
            "---",
            f"账号：{name}",
            f"域名：{domain}",
            f"邮箱：{email}",
            f"状态：{result['message']}",
            f"剩余：{leftdays if leftdays is not None else '未获取'} 天",
            f"积分：{points if points is not None else '未获取'}",
        ])

        if exchange:
            lines.append(
                f"兑换：{exchange}"
            )

    final_summary = "\n".join(lines)

    # 某些账号登录过期时，在通知标题中明确提示
    login_expired_count = sum(
        1 for r in results
        if r["kind"] == "login_expired"
    )

    verification_count = sum(
        1 for r in results
        if r["kind"] in (
            "verification_required",
            "turnstile_failed",
        )
    )

    if login_expired_count:
        push_title = (
            f"GLaDOS签到：{login_expired_count}个账号需重新登录"
        )

    elif verification_count:
        push_title = (
            f"GLaDOS签到：{verification_count}个账号需验证"
        )

    else:
        push_title = (
            f"GLaDOS签到：成功{success_count}，失败{fail_count}"
        )

    logging.info("正在通过 Go-WXPush 发送汇总通知...")

    try:
        wxpush_url = getattr(
            config,
            "wxpush_api_url",
            "https://push.hzz.cool/wxsend",
        )

        payload = {
            "title": push_title,
            "content": final_summary,
            "appid": config.wxpush_appid,
            "secret": config.wxpush_secret,
            "userid": config.wxpush_userid,
            "template_id": config.wxpush_template_id,
        }

        response = session.post(
            wxpush_url,
            json=payload,
            timeout=30,
            verify=False,
        )

        response.raise_for_status()

        response_data = response.json()

        if response_data.get("errcode") == 0:
            print(
                "Go-WXPush 推送成功。\n"
                + final_summary
            )
            return True

        logging.error(
            "Go-WXPush 返回错误码：%s",
            response_data.get("errcode"),
        )

    except Exception as exc:
        logging.error(
            "Go-WXPush 推送异常：%s",
            type(exc).__name__,
        )

    return False


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":

    # 保留原来的随机等待配置
    min_wait_time = int(
        config.min_random_delay
    )

    max_wait_time = int(
        config.max_random_delay
    )

    wait_time = random.randint(
        min_wait_time,
        max_wait_time,
    )

    print(f"正在等待 {wait_time} 秒...")
    time.sleep(wait_time)
    print("等待结束。")

    # Go-WXPush 代理配置保持不变
    proxy = getattr(config, "proxy", "") or None

    session = get_robust_session(
        proxy=proxy
    )

    try:
        # 串行处理所有配置域名
        results = checkin_all_accounts()

        # 只推送一次汇总结果
        send_wxpush(
            session,
            results,
        )

    finally:
        session.close()
