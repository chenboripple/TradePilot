# TradePilot

波段交易系统 - 回测与实盘共用策略逻辑。

## 功能特性

- **策略引擎**: 10+ 种技术指标策略（MA、RSI、MACD、Bollinger 等）
- **回测系统**: 多周期回测，支持参数优化
- **实时监控**: 定时扫描，飞书通知
- **数据接口**: Tushare、AkShare、东方财富妙想
- **跨平台**: 支持 macOS、Linux、Windows

## 快速安装

### 方式一：使用安装脚本（推荐）

```bash
# macOS / Linux
git clone https://github.com/chenboripple/TradePolot.git
cd TradePolot
python3 install.py

# Windows
git clone https://github.com/chenboripple/TradePolot.git
cd TradePolot
python install.py
```

### 方式二：使用 pip

```bash
git clone https://github.com/chenboripple/TradePolot.git
cd TradePolot
pip install -e .
```

### 方式三：使用虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows
pip install -r requirements.txt
pip install -e .
```

## 配置

### 方式一：环境变量（推荐）

```bash
export TUSHARE_TOKEN="your_token"
export FEISHU_WEBHOOK_SECRET="your_secret"
```

### 方式二：配置文件

```bash
tradepilot init  # 创建 ~/.tradepilot/config.yaml
```

编辑配置文件：

```yaml
tushare:
  token: "your_token"

feishu:
  webhook_url: "https://open.feishu.cn/..."
  webhook_secret: "your_secret"
```

## 使用方法

### CLI 命令

```bash
# 显示帮助
tradepilot --help

# 初始化配置
tradepilot init

# 查看配置
tradepilot config

# 运行回测
tradepilot backtest 000999.SZ --strategy rsi

# 监控股票
tradepilot monitor 002022.SZ

# 筛选股票
tradepilot screen 002022.SZ
```

### Python API

```python
from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.tushare_loader import TushareDataLoader

config = load_config()
loader = TushareDataLoader(config['tushare']['token'])
df = loader.get_daily('000999.SZ')
```

## 项目结构

```
TradePolot/
├── src/ripple_tradePilot/    # 核心代码
│   ├── strategies/           # 策略模块
│   ├── backtest/             # 回测引擎
│   ├── data/                 # 数据加载
│   ├── execution/            # 执行模块
│   ├── risk/                 # 风控模块
│   ├── notifiers/            # 通知模块
│   ├── monitor/              # 监控模块
│   ├── api/                  # API 服务
│   ├── config_loader.py      # 配置加载
│   └── cli.py                # 命令行工具
├── examples/                 # 示例脚本
├── docs/                     # 文档
├── install.py                # 安装脚本
├── pyproject.toml            # 项目配置
└── README.md                 # 本文件
```

## 更新

```bash
python install.py --update
```

## 卸载

```bash
python install.py --uninstall
```

## 许可证

MIT License
