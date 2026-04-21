#!/usr/bin/env python3
"""
飞书 Webhook 签名测试
"""

import hashlib
import hmac
import base64
import time
import httpx

# 配置
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/859cba37-0ce9-4381-90d4-dc15140af209"
SECRET = "aWsbZogAYvx4dSMBAePj4d"

print("="*70)
print("📱 飞书签名测试")
print("="*70)

# 生成签名
timestamp = str(int(time.time()))
print(f"\n时间戳：{timestamp}")
print(f"密钥：{SECRET}")

# 飞书签名格式
string_to_sign = f"{timestamp}\n{SECRET}"
print(f"待签名字符串：{string_to_sign[:30]}...")

hmac_code = hmac.new(
    string_to_sign.encode("utf-8"),
    digestmod=hashlib.sha256
).digest()
signature = base64.b64encode(hmac_code).decode('utf-8')

print(f"签名：{signature}")

# 发送消息
content = {
    "msg_type": "text",
    "content": {
        "text": "🧪 TradePilot 测试消息\n这是一条测试消息，请忽略。"
    }
}

headers = {
    "Content-Type": "application/json"
}

# 尝试不同的签名传递方式
print("\n📝 尝试方式 1: X-Signature header...")
headers["X-Timestamp"] = timestamp
headers["X-Signature"] = signature

response = httpx.post(WEBHOOK, json=content, headers=headers, timeout=10)
result = response.json()
print(f"结果：{result}")

if result.get("code") == 0:
    print("✅ 发送成功！")
else:
    print("\n📝 尝试方式 2: 无签名...")
    headers.pop("X-Timestamp", None)
    headers.pop("X-Signature", None)
    
    response = httpx.post(WEBHOOK, json=content, headers=headers, timeout=10)
    result = response.json()
    print(f"结果：{result}")
    
    if result.get("code") == 0:
        print("✅ 发送成功！")
    else:
        print("\n📝 尝试方式 3: timestamp 在 URL 中...")
        response = httpx.post(f"{WEBHOOK}?timestamp={timestamp}&sign={signature}", json=content, timeout=10)
        result = response.json()
        print(f"结果：{result}")

print("\n" + "="*70)
