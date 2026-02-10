# -*- coding: UTF-8 -*-

import requests
import configNew as config
import logging
import json
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import time
import random

# 配置日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def get_robust_session():
    """创建带有重试机制的 Session"""
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
    return session

def get_headers(cookie):
    """构建请求头"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": "https://glados.cloud/console/checkin",
        "Origin": "https://glados.cloud",
        "Content-Type": "application/json;charset=UTF-8"
    }

if __name__ == '__main__':
    # 随机等待
    min_wait_time = 30
    max_wait_time = 300
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
    
    session = get_robust_session()
    summary_content = ""
    success_count, fail_count = 0, 0

    for cookie in config.cookies:
        headers = get_headers(cookie)
        email = "Unknown"
        try:
            # 1. 发送签到请求
            logging.info(f"正在尝试签到...")
            checkin_resp = session.post(check_in_url, headers=headers, data=json.dumps(payload), timeout=30)
            checkin_resp.raise_for_status()
            checkin_result = checkin_resp.json()
            
            # 2. 获取状态
            state_resp = session.get(status_url, headers=headers, timeout=30)
            state_result = state_resp.json()
            email = state_result['data']['email']
            leftdays = int(float(state_result['data']['leftDays']))
            
            # 3. 获取积分信息
            points_resp = session.get(points_url, headers=headers, timeout=30)
            points_result = points_resp.json()
            current_points = int(float(points_result.get("points", "0")))
            
            # 逻辑判断
            msg = checkin_result.get('message', '')
            points_gained = checkin_result.get('points', 0) 
            if "Checkin!" in msg:
                message_status = f"签到成功~ 获得{points_gained}点"
                success_count += 1
            elif "Checkin Repeats!" in msg:
                message_status = "今日已签到"
                success_count += 1
            else:
                message_status = f"签到异常: {msg}"
                fail_count += 1

            # 4. 自动兑换逻辑
            message_exchange = ""
            threshold = exchange_thresholds.get(exchange_plan, 500)
            if current_points >= threshold:
                logging.info(f"积分({current_points})已达标，尝试自动兑换 {exchange_plan}...")
                exch_resp = session.post(exchange_url, headers=headers, data=json.dumps({"planType": exchange_plan}), timeout=30)
                exch_result = exch_resp.json()
                if exch_result.get("code") == 0:
                    message_exchange = f"✅ 自动兑换成功 ({exchange_plan})"
                else:
                    message_exchange = f"❌ 兑换失败: {exch_result.get('message')}"
            
            account_info = f"{'-'*30}\n账号: {email}\n状态: {message_status}\n剩余: {leftdays} 天\n积分: {current_points}\n"
            if message_exchange:
                account_info += f"兑换: {message_exchange}\n"
            summary_content += account_info

        except Exception as e:
            logging.error(f"账号 {email} 执行出错: {e}")
            fail_count += 1
            summary_content += f"{'-'*30}\n账号: {email}\n运行错误: {str(e)}\n"

    # --- 修改部分：构建最终的推送内容 ---
    
    # 1. 先准备好统计头部（不再以 --- 开头）
    header = f"成功账号：{success_count}，失败账号：{fail_count}\n"
    
    # 2. 将统计头部拼接到详细内容之前
    final_summary = header + summary_content
    
    # --- 5. Go-WXPush 统一推送 (修改部分) ---
    push_title = f"GLaDOS签到: 成功{success_count}, 失败{fail_count}"
    logging.info("正在通过 go-wxpush 发送通知...")
    
    try:
        # 这里的 config.wxpush_api_url 请填写为你自建服务的地址，例如 http://1.2.3.4:5566/wxsend
        wxpush_url = getattr(config, 'wxpush_api_url', 'https://push.hzz.cool/wxsend')
        
        # 构造 Webhook POST 请求体
        wx_payload = {
            "title": push_title,
            "content": final_summary,  # 使用拼接了头部的完整内容
            "appid": config.wxpush_appid,
            "secret": config.wxpush_secret,
            "userid": config.wxpush_userid,
            "template_id": config.wxpush_template_id
        }
        
        # 使用 Webhook (POST) 方式发送
        response = session.post(wxpush_url, json=wx_payload, timeout=30, verify=False)
        response_data = response.json()
        
        if response_data.get("errcode") == 0:
            print(f"任务完成，go-wxpush 推送成功。\n{final_summary}")
        else:
            logging.error(f"go-wxpush 推送返回错误: {response_data}")
            
    except Exception as e:
        logging.error(f"推送失败: {e}")
      
