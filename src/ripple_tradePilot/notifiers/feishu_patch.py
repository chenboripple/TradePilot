"""
飞书 Webhook 通知补丁 - 添加_send_text 方法
"""

import sys
from pathlib import Path

# 导入原有模块
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier

# 添加新方法
def _send_text(self, content: dict) -> bool:
    """发送文本消息（用于定期报告）"""
    try:
        # 生成时间戳和签名（飞书需要 13 位毫秒级时间戳）
        timestamp = str(int(__import__('time').time() * 1000))
        signature = self._generate_signature(timestamp)
        
        # 飞书签名校验：timestamp 和 sign 作为 URL 参数
        url = self.webhook_url
        if signature:
            url = f"{self.webhook_url}?timestamp={timestamp}&sign={signature}"
        
        headers = {
            "Content-Type": "application/json"
        }
        
        response = __import__('httpx').post(url, json=content, headers=headers, timeout=10)
        result = response.json()
        
        if result.get("code") != 0 and result.get("StatusCode") != 0:
            print(f"飞书 Webhook 发送失败：{result}")
            return False
        
        return True
        
    except Exception as e:
        print(f"发送飞书通知异常：{e}")
        return False

# 绑定方法到类
FeishuWebhookNotifier._send_text = _send_text

print("✅ 飞书通知补丁已加载")
