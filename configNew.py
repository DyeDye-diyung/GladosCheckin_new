# -*- coding: UTF-8 -*-
import os

# PushPlus Token
pushplus_token = os.environ["PUSH_PLUS_TOKEN"].strip()

# 自动兑换档位: plan100 (10天), plan200 (30天), plan500 (100天)
exchange_plan = os.environ["EXCHANGE_PLAN"].strip()

# GLaDOS Cookie 列表 (支持多账号)
# 格式：["koa:sess=xxx; koa:sess.sig=xxx;", "第二个账号..."]
cookies = [
    f"koa:sess={os.environ['KOA_SESS'].strip()}; koa:sess.sig={os.environ['KOA_SESS_SIG'].strip()}",
    f"koa:sess={os.environ['KOA_SESS_2'].strip()}; koa:sess.sig={os.environ['KOA_SESS_SIG_2'].strip()}",
]

# go-wxpush相关参数

# 你的自建服务地址，记得带上 /wxsend
wxpush_api_url = os.environ["WXPUSH_API_URL"].strip()

# 微信测试号参数
wxpush_appid = os.environ["WXPUSH_APPID"].strip()
wxpush_secret = os.environ["WXPUSH_SECRET"].strip()
wxpush_userid = os.environ["WXPUSH_USERID"].strip()
wxpush_template_id = os.environ["WXPUSH_TEMPLATE_ID"].strip()
