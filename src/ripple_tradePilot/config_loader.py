"""
TradePilot 配置加载器
支持环境变量和配置文件

配置优先级（从高到低）：
1. 环境变量（如 TUSHARE_TOKEN）
2. 用户配置文件 ~/.tradepilot/config.yaml

注意：不提供代码默认值，必须由用户配置
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional

import yaml


# 用户配置目录
USER_CONFIG_DIR = Path.home() / ".tradepilot"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置，支持环境变量覆盖
    
    Args:
        config_path: 指定配置文件路径（可选，覆盖默认路径）
        
    Returns:
        配置字典
        
    Raises:
        FileNotFoundError: 配置文件不存在且未设置对应环境变量
    """
    # 1. 加载用户配置
    configured_path = config_path or os.getenv('TRADEPILOT_CONFIG')
    target_path = Path(configured_path) if configured_path else USER_CONFIG_FILE
    config = {}
    if target_path.exists():
        with open(target_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    
    # 2. 环境变量覆盖
    env_mappings = {
        # Tushare
        'TUSHARE_TOKEN': ['tushare', 'token'],
        'TUSHARE_CACHE_DIR': ['tushare', 'cache_dir'],
        'TUSHARE_RATE_LIMIT': ['tushare', 'rate_limit_delay'],
        
        # MX (东方财富妙想)
        'MX_APIKEY': ['mx', 'api_key'],
        
        # Feishu
        'FEISHU_WEBHOOK_URL': ['feishu', 'webhook_url'],
        'FEISHU_WEBHOOK_SECRET': ['feishu', 'webhook_secret'],
    }
    
    for env_var, key_path in env_mappings.items():
        value = os.getenv(env_var)
        if value:
            target = config
            for key in key_path[:-1]:
                if key not in target:
                    target[key] = {}
                target = target[key]
            target[key_path[-1]] = value

    notifier_env_mappings = {
        'FEISHU_WEBHOOK_URL': ['notifiers', 'feishu', 'webhook'],
        'FEISHU_WEBHOOK_SECRET': ['notifiers', 'feishu', 'secret'],
    }
    for env_var, key_path in notifier_env_mappings.items():
        value = os.getenv(env_var)
        if value:
            target = config
            for key in key_path[:-1]:
                target = target.setdefault(key, {})
            target[key_path[-1]] = value

    feishu_enabled = os.getenv('FEISHU_ENABLED')
    if feishu_enabled:
        enabled_values = {'1', 'true', 'yes', 'on'}
        config.setdefault('notifiers', {}).setdefault('feishu', {})['enabled'] = (
            feishu_enabled.strip().lower() in enabled_values
        )
    
    return config


def init_config(config_path: Optional[Path] = None):
    """初始化用户配置文件

    Args:
        config_path: 可选，指定生成位置；默认 ~/.tradepilot/config.yaml
    """
    target = Path(config_path) if config_path else USER_CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        print(f"⚠️  配置文件已存在: {target}")
        return

    default_config = """# TradePilot 用户配置
# 设置环境变量或使用此配置文件

# Tushare 配置
tushare:
  token: ""  # 或设置环境变量 TUSHARE_TOKEN
  cache_dir: "data/cache"
  rate_limit_delay: 1.5

# 东方财富妙想配置（可选）
mx:
  api_key: ""  # 或设置环境变量 MX_APIKEY

# 飞书机器人配置
feishu:
  webhook_url: ""  # 或设置环境变量 FEISHU_WEBHOOK_URL
  webhook_secret: ""  # 或设置环境变量 FEISHU_WEBHOOK_SECRET

# 监控配置
monitor:
  interval_seconds: 300
  bar_freq: "1min"
  trading_hours:
    start: "09:30"
    end: "15:00"
  check_non_trading: false
  report_interval_seconds: 3600  # 无信号时的例行心跳间隔
  use_watchlist: true            # 是否联动用户观察池

# 监控标的（不填 strategy_profile 时使用默认 combo_vote 画像）
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    notify_on: ["BUY", "SELL"]
"""

    target.write_text(default_config, encoding='utf-8')
    print(f"✅ 配置文件已创建: {target}")
    print("请编辑配置文件设置您的 API Key")


def get_tushare_token(config: Dict[str, Any]) -> str:
    """获取 Tushare Token"""
    token = config.get('tushare', {}).get('token', '')
    if not token:
        raise ValueError(
            "Tushare token not found. "
            "Set TUSHARE_TOKEN env var or add to ~/.tradepilot/config.yaml"
        )
    return token
