"""
飞书机器人通知模块

使用自建应用方式发送消息
需要：App ID, App Secret
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

import httpx

from ripple_tradePilot.models.types import Bar, Side


class FeishuNotifier:
    """飞书机器人通知器"""
    
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._access_token: Optional[str] = None
        self._token_expire: int = 0
    
    def _get_access_token(self) -> str:
        """获取访问令牌（自动刷新）"""
        now = int(time.time())
        
        # Token 未过期则复用
        if self._access_token and now < self._token_expire:
            return self._access_token
        
        # 请求新 Token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }
        
        response = httpx.post(url, json=payload, timeout=10)
        result = response.json()
        
        if result.get("code") != 0:
            raise Exception(f"获取飞书 Token 失败：{result}")
        
        self._access_token = result["tenant_access_token"]
        self._token_expire = now + result["expire"] - 60  # 提前 60 秒刷新
        
        return self._access_token
    
    def send(self, symbol: str, name: str, side: Side, price: float, 
             strategy: str, bar: Bar, extra_info: Optional[dict] = None) -> bool:
        """发送交易信号通知"""
        
        # 颜色配置
        color_map = {
            Side.BUY: "red",      # 买入用红色（A 股红涨）
            Side.SELL: "green"    # 卖出用绿色（A 股绿跌）
        }
        
        # 信号图标
        icon_map = {
            Side.BUY: "🟢",
            Side.SELL: "🔴"
        }
        
        # 构建消息内容
        content = {
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
                        "content": f"**标的：** {name} ({symbol})\n**信号：** {side.value}\n**价格：** {price:.2f} 元\n**策略：** {strategy}"
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**时间：** {bar.timestamp.strftime('%Y-%m-%d %H:%M')}\n"
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
                            "content": "⚠️ 投资有风险，入市需谨慎。本信号仅供参考，请自行判断是否交易。"
                        }
                    ]
                }
            ]
        }
        
        # 添加额外信息
        if extra_info:
            extra_text = "\n".join(f"• {k}: {v}" for k, v in extra_info.items())
            content["elements"].insert(2, {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**其他信息：**\n{extra_text}"
                }
            })
        
        # 发送消息
        return self._send_card(content)
    
    def _send_card(self, card_content: dict) -> bool:
        """发送交互式卡片消息"""
        try:
            token = self._get_access_token()
            
            url = "https://open.feishu.cn/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            # 注意：这里需要 webhook 地址或者 chat_id
            # 飞书自建应用需要知道发送给谁
            # 方案 1: 使用 webhook（推荐，简单）
            # 方案 2: 使用 chat_id（需要知道群组/用户 ID）
            
            # 由于用户只提供了 App ID/Secret，我们需要 webhook
            # 但飞书自建应用的 webhook 需要从机器人详情页面获取
            # 这里我们先尝试用通用方式
            
            payload = {
                "receive_id_type": "chat_id",  # 或 open_id, user_id
                "receive_id": "oc_XXXXXXXX",    # 需要用户提供
                "msg_type": "interactive",
                "content": card_content
            }
            
            response = httpx.post(url, headers=headers, json=payload, timeout=10)
            result = response.json()
            
            if result.get("code") != 0:
                print(f"飞书消息发送失败：{result}")
                return False
            
            print("✅ 飞书消息发送成功")
            return True
            
        except Exception as e:
            print(f"发送飞书通知异常：{e}")
            return False
    
    def send_test(self) -> bool:
        """发送测试消息"""
        from datetime import datetime
        from ripple_tradePilot.models.types import Bar
        
        test_bar = Bar(
            timestamp=datetime.now(),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=100000
        )
        
        return self.send(
            symbol="000001.SZ",
            name="测试股票",
            side=Side.BUY,
            price=10.2,
            strategy="test",
            bar=test_bar
        )


# Webhook 方式的简化版本（推荐，支持签名校验）
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
        """发送任意飞书 Webhook 消息。"""
        try:
            body = dict(payload)
            if self.secret:
                # 飞书机器人使用 10 位秒级时间戳，timestamp/sign 放在 JSON body 中。
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

            if result.get("code") != 0 and result.get("StatusCode") != 0:
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
    
    def send_test(self) -> bool:
        """发送测试消息"""
        from datetime import datetime
        from ripple_tradePilot.models.types import Bar
        
        test_bar = Bar(
            timestamp=datetime.now(),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=100000
        )
        
        return self.send(
            symbol="000001.SZ",
            name="测试股票",
            side=Side.BUY,
            price=10.2,
            strategy="test",
            bar=test_bar
        )
