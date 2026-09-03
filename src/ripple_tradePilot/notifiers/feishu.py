"""
飞书机器人通知模块（Webhook 方式）

使用自定义机器人 Webhook 发送消息，支持签名校验。
（早期"自建应用"实现已移除：其 receive_id 永远无法配置，属于死代码。）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

import httpx

from ripple_tradePilot.models.types import Bar, Side


class FeishuWebhookNotifier:
    """飞书 Webhook 机器人通知器（支持签名校验）"""
    
    def __init__(self, webhook_url: str, secret: str = None):
        self.webhook_url = webhook_url
        self.secret = secret
    
    def _generate_signature(self, timestamp: str) -> str:
        """生成签名（飞书签名校验）

        飞书机器人签名规则：
        - key = f"{timestamp}\n{secret}".encode("utf-8")
        - msg = b""
        - sign = base64(hmac_sha256(key, msg))
        """
        if not self.secret:
            return ""

        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def _post(self, payload: dict) -> bool:
        """发送任意飞书 Webhook 消息；配置了 secret 时自动附加签名。"""
        try:
            body = dict(payload)
            if self.secret:
                timestamp = str(int(time.time()))
                body["timestamp"] = timestamp
                body["sign"] = self._generate_signature(timestamp)

            response = httpx.post(
                self.webhook_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            result = response.json()

            code = result.get("code", result.get("StatusCode", 0))
            if code != 0:
                print(f"飞书 Webhook 发送失败：{result}")
                return False

            print("✅ 飞书消息发送成功")
            return True

        except Exception as e:
            print(f"发送飞书通知异常：{e}")
            return False
    
    def send(self, symbol: str, name: str, side: Side, price: float,
             strategy: str, bar: Bar, extra_info: Optional[dict] = None) -> bool:
        """发送交易信号通知（带签名校验）"""
        
        # 颜色配置
        color_map = {
            Side.BUY: "red",
            Side.SELL: "green"
        }
        
        # 信号图标
        icon_map = {
            Side.BUY: "🟢",
            Side.SELL: "🔴"
        }
        
        content = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{icon_map.get(side, '⚪')} 交易信号提醒"
                    },
                    "template": color_map.get(side, "blue")
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**标的：** {name} ({symbol})\n**信号：** {side.value}\n**价格：** {price:.2f} 元\n**策略：** {strategy}\n**时间：** {bar.timestamp.strftime('%Y-%m-%d %H:%M')}"
                        }
                    },
                    {
                        "tag": "hr"
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**K 线详情：**\n• 开盘：{bar.open:.2f}\n• 最高：{bar.high:.2f}\n• 最低：{bar.low:.2f}\n• 收盘：{bar.close:.2f}\n• 成交量：{bar.volume/10000:.1f}万手"
                        }
                    },
                    {
                        "tag": "hr"
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "⚠️ 投资有风险，入市需谨慎"
                            }
                        ]
                    }
                ]
            }
        }
        
        return self._post(content)

    def _send_text(self, content: dict) -> bool:
        """发送文本消息（用于回测报告/定期报告）。"""
        return self._post(content)
