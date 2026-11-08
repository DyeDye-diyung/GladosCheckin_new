
# -*- coding: UTF-8 -*-

import requests
import configNew as config
import logging
import json
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import time
import random
import hashlib

# Challenge 验证参数（与参考代码一致）
CHALLENGE_SUFFIX = "glados-observe-v1"
CHALLENGE_DELAY_SECONDS = 2.5

# 服务端提示设备平台不匹配时使用的 User-Agent（与参考代码一致）
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "iOS": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

# 配置日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')


def get_robust_session(proxy=None):
    """创建带有重试机制的 Session

    :param proxy: 代理地址，例如 "http://192.168.31.6:7890" 或 "socks5://192.168.31.6:7890"
                  传 None 或空字符串表示直连。
    """
    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST", "GET"]
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # 配置代理（可选直连）
    if proxy:
        session.proxies = {
            "http": proxy,
            "https": proxy,
        }
        logging.info(f"已启用代理: {proxy}")
    else:
        logging.info("未配置代理，使用直连。")

    return session


# ========== 新增：Cookie 相关工具函数 ==========
# GLaDOS 现在的签到 Cookie 包含 4 项：
#   koa:sess, koa:sess.sig, gld:sess, gld:sess.sig
# 为方便排错，这里加两个小工具：解析键名、校验是否完整。
REQUIRED_COOKIE_KEYS = ("koa:sess", "koa:sess.sig", "gld:sess", "gld:sess.sig")


def parse_cookie_keys(cookie):
    """解析 Cookie 字符串中的键名（不打印值，避免泄露）"""
    keys = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        keys.append(part.split("=", 1)[0].strip())
    return keys


def check_cookie(cookie):
    """检查 Cookie 是否包含 GLaDOS 所需的全部键；返回缺失的键列表"""
    keys = set(parse_cookie_keys(cookie))
    logging.info(f"当前 Cookie 包含键: {sorted(keys)}")
    missing = [k for k in REQUIRED_COOKIE_KEYS if k not in keys]
    if missing:
        logging.warning(
            f"Cookie 缺少键: {missing}，可能导致签到失败。"
            f"请重新登录 https://glados.cloud/console/checkin 后，从 F12 完整复制 Cookie。"
        )
    return missing
# ========== 新增结束 ==========


def get_headers(cookie):
    """构建请求头"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36",
        "Cookie": cookie.strip(),   # 顺带去掉首尾空白，避免复制时带入空格/换行
        "Referer": "https://glados.cloud/console/checkin",
        "Origin": "https://glados.cloud",
        "Content-Type": "application/json;charset=UTF-8"
    }


def checkin_with_challenge(session, check_in_url, headers, token):
    """获取 Challenge，等待后计算 Proof，再以 JSON 提交签到。"""
    challenge_url = check_in_url + "/challenge"
    logging.info("正在获取签到 Challenge...")
    challenge_resp = session.get(challenge_url, headers=headers, timeout=30)
    challenge_resp.raise_for_status()
    challenge_data = challenge_resp.json()

    if not isinstance(challenge_data, dict) or challenge_data.get("code") != 0:
        raise RuntimeError(f"获取 Challenge 失败: {challenge_data}")

    nonce = challenge_data.get("nonce")
    if not nonce:
        raise ValueError("Challenge 响应缺少 nonce")

    logging.info("Challenge 获取成功")
    time.sleep(CHALLENGE_DELAY_SECONDS)
    proof = hashlib.sha256(f"{nonce}.{CHALLENGE_SUFFIX}".encode("utf-8")).hexdigest()
    logging.info("Challenge Proof 计算完成")

    payload = {"token": token, "nonce": str(nonce), "proof": proof}
    logging.info("正在提交 Challenge 签到请求...")
    checkin_resp = session.post(check_in_url, headers=headers, json=payload, timeout=30)
    checkin_resp.raise_for_status()
    checkin_result = checkin_resp.json()
    if not isinstance(checkin_result, dict):
        raise ValueError("签到响应格式异常（非 JSON 对象）")
    logging.info(
        f"签到响应: code={checkin_result.get('code')}, "
        f"message={checkin_result.get('message')}"
    )
    return checkin_result


def checkin_with_device_retry(session, check_in_url, headers, token):
    """设备平台不匹配时按 loginDevice 切换 UA，并重新获取 Challenge 重试一次。"""
    result = checkin_with_challenge(session, check_in_url, headers, token)
    if result.get("code") == 4 and result.get("reason") == "device-mismatch":
        device = result.get("loginDevice")
        retry_ua = PLATFORM_UA.get(device)
        if retry_ua:
            logging.warning(f"设备平台不匹配(loginDevice={device})，切换 User-Agent 重试一次")
            retry_headers = headers.copy()
            retry_headers["User-Agent"] = retry_ua
            result = checkin_with_challenge(session, check_in_url, retry_headers, token)
        else:
            logging.error(f"无法自动匹配登录设备平台(loginDevice={device})")

        if result.get("code") == 4 and result.get("reason") == "device-mismatch":
            logging.error("重试后设备仍不匹配，请将 get_headers() 中的 User-Agent 改为提取 Cookie 时浏览器的实际 User-Agent")

    return result


if __name__ == '__main__':
    # 随机等待
    min_wait_time = int(config.min_random_delay)
    max_wait_time = int(config.max_random_delay)
    wait_time = random.randint(min_wait_time, max_wait_time)
    print(f"正在等待 {wait_time} 秒...")
    time.sleep(wait_time)
    print("等待结束。")

    # 基础配置
    check_in_url = "https://glados.cloud/api/user/checkin"
    status_url = "https://glados.cloud/api/user/status"
    points_url = "https://glados.cloud/api/user/points"
    exchange_url = "https://glados.cloud/api/user/exchange"
    payload = {"token": "glados.cloud"}

    exchange_thresholds = {"plan100": 100, "plan200": 200, "plan500": 500}
    exchange_plan = getattr(config, 'exchange_plan', 'plan500')

    # 读取代理配置（为空或不存在则直连）
    proxy = getattr(config, 'proxy', '') or None

    session = get_robust_session(proxy=proxy)
    summary_content = ""
    success_count, fail_count = 0, 0

    for cookie in config.cookies:
        # ===== 修改点：循环开始处做 Cookie 规范化 + 完整性检查 =====
        cookie = cookie.strip()
        check_cookie(cookie)
        # ========================================================

        headers = get_headers(cookie)
        email = "Unknown"
        try:
            # 1. 获取 Challenge 并签到；设备平台不匹配时自动重试一次
            logging.info("正在尝试 Challenge 签到...")
            checkin_result = checkin_with_device_retry(
                session, check_in_url, headers, payload["token"]
            )

            # 2. 获取状态
            state_resp = session.get(status_url, headers=headers, timeout=30)
            state_result = state_resp.json()
            email = state_result['data']['email']
            leftdays = int(float(state_result['data']['leftDays']))

            # 3. 获取积分信息
            points_resp = session.get(points_url, headers=headers, timeout=30)
            points_result = points_resp.json()
            current_points = int(float(points_result.get("points", "0")))

            # 逻辑判断：依据服务端 code 判断，优先识别签到域名不匹配
            code = checkin_result.get('code', -2)
            msg = str(checkin_result.get('message', ''))
            points_gained = checkin_result.get('points', 0)
            checkin_ok = False
            if "please checkin via" in msg.lower():
                message_status = f"签到域名不匹配: {msg}"
                fail_count += 1
            elif code == 0:
                message_status = f"签到成功~ 获得{points_gained}点"
                success_count += 1
                checkin_ok = True
            elif code == 1:
                message_status = "今日已签到"
                success_count += 1
                checkin_ok = True
            else:
                message_status = f"签到异常(code={code}): {msg}"
                fail_count += 1

            # 4. 自动兑换逻辑
            message_exchange = ""
            threshold = exchange_thresholds.get(exchange_plan, 500)
            if checkin_ok and current_points >= threshold:
                logging.info(f"积分({current_points})已达标，尝试自动兑换 {exchange_plan}...")
                exch_resp = session.post(exchange_url, headers=headers, data=json.dumps({"planType": exchange_plan}), timeout=30)
                exch_result = exch_resp.json()
                if exch_result.get("code") == 0:
                    message_exchange = f"✅ 自动兑换成功 ({exchange_plan})"
                else:
                    message_exchange = f"❌ 兑换失败: {exch_result.get('message')}"

            account_info = f"\n{'-'*3}\n\n账号: {email}\n状态: {message_status}\n剩余: {leftdays} 天\n积分: {current_points}\n"
            if message_exchange:
                account_info += f"兑换: {message_exchange}\n"
            summary_content += account_info

        except Exception as e:
            logging.error(f"账号 {email} 执行出错: {e}")
            fail_count += 1
            summary_content += f"\n{'-'*3}\n\n账号: {email}\n运行错误: {str(e)}\n"

    # 构建最终的推送内容
    header = f"GLaDOS签到 成功：{success_count} 失败：{fail_count}\n"
    final_summary = header + summary_content

    # 5. Go-WXPush 统一推送
    push_title = f"GLaDOS签到: 成功{success_count}, 失败{fail_count}"
    logging.info("正在通过 go-wxpush 发送通知...")

    try:
        wxpush_url = getattr(config, 'wxpush_api_url', 'https://push.hzz.cool/wxsend')

        wx_payload = {
            "title": push_title,
            "content": final_summary,
            "appid": config.wxpush_appid,
            "secret": config.wxpush_secret,
            "userid": config.wxpush_userid,
            "template_id": config.wxpush_template_id
        }

        response = session.post(wxpush_url, json=wx_payload, timeout=30, verify=False)
        response_data = response.json()

        if response_data.get("errcode") == 0:
            print(f"任务完成，go-wxpush 推送成功。\n{final_summary}")
        else:
            logging.error(f"go-wxpush 推送返回错误: {response_data}")

    except Exception as e:
        logging.error(f"推送失败: {e}")
