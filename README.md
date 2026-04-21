# ripple_tradePilot

Python 波段交易系统项目（回测与实盘共用策略逻辑）。

## 初始化

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 目录结构

```
src/ripple_tradePilot/
  strategies/     # 策略逻辑（单一源码）
  backtest/       # 回测引擎与指标
  execution/      # 模拟执行/实盘对接
  risk/           # 风控模块
  data/           # 数据加载
  models/         # 领域模型（Bar/Signal/Order/Fill）
examples/         # 示例脚本
```

## 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn ripple_tradePilot.api:app --host 0.0.0.0 --port 8000
```

## 服务器部署（示例：systemd）

```ini
[Unit]
Description=ripple_tradePilot service
After=network.target

[Service]
User=ripple
WorkingDirectory=/Users/ripple/work space/ripple_tradePilot
ExecStart=/Users/ripple/work space/ripple_tradePilot/.venv/bin/uvicorn ripple_tradePilot.api:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

## 示例

```bash
python examples/run_backtest.py
```
