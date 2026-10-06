# TradePilot

波段交易系统 - 回测与实盘共用策略逻辑。

## 功能特性

- **策略引擎**: 10+ 种技术指标策略（MA、RSI、MACD、Bollinger 等）
- **回测系统**: 多周期回测，支持参数优化
- **实时监控**: 定时扫描，飞书通知
- **数据接口**: Tushare、AkShare、东方财富妙想
- **跨平台**: 支持 macOS、Linux、Windows

## 快速安装

### 方式一：使用 uv（推荐）

```bash
git clone https://github.com/chenboripple/TradePilot.git
cd TradePilot
uv venv --python 3.11 .venv   # uv 自动下载对应 Python
uv pip install -e .
source .venv/bin/activate     # Windows: .venv\Scripts\activate
```

### 方式二：使用 pip

```bash
git clone https://github.com/chenboripple/TradePilot.git
cd TradePilot
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

> 依赖声明唯一来源是 `pyproject.toml`；`requirements.txt` 是生成的锁定快照
> （`uv pip freeze | grep -v '^-e' > requirements.txt`），用于 Docker 构建与可复现安装。

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

# 飞书通知（配好 webhook 即生效；旧的顶层 feishu.webhook_url 结构仍兼容）
notifiers:
  feishu:
    enabled: true
    webhook: "https://open.feishu.cn/..."
    secret: "your_secret"
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

# 期货观察池扫描（P1：主力映射/倾向/风险测算/去重提醒；建议 cron 定时运行）
tradepilot futures scan

# 期货回测（P2：读取 scan 落库数据，真实合约撮合 + 逐日盯市 + 换月 + 强平；
# --walkforward 切换滚动样本外验证；订单/成交/账户逐日快照按 run_id 落库可重放）
tradepilot futures backtest -p RB
tradepilot futures backtest -p RB --walkforward

# 启动 Web 监控台（浏览器访问 http://127.0.0.1:8000）
tradepilot serve
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
TradePilot/
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
├── docs/                     # 文档（history/ 为归档的过程报告）
├── pyproject.toml            # 项目配置（依赖唯一来源）
└── README.md                 # 本文件
```

## 更新

```bash
./scripts/update_from_github.sh release
```

升级脚本会保留服务器上的 `.env`、`config.yaml`、`data/` 和 `output/`，从 GitHub 下载最新源码、在服务器本地构建镜像、重建服务，并自动检查及修复 SQLite 表结构。

## Docker 部署

本地构建并启动 API 与监控服务：

```bash
cp .env.example .env
./scripts/docker-deploy.sh local
```

生产环境首次启动：

```bash
./scripts/docker-deploy.sh production
```

部署时会自动创建 Docker SQLite 数据卷。每个应用容器启动前都会检查数据库文件、创建缺失表、补齐缺失字段并执行完整性检查。

服务器不会后台轮询 GitHub 或 GHCR。后续版本升级由管理员手工执行：

```bash
./scripts/update_from_github.sh release
```

完整说明见 [Docker 部署与手工升级](docs/docker-deployment.md)，项目风险与优化优先级见 [项目分析](docs/project-analysis.md)。

## 交易监控台

本地启动 API 服务（无需 Docker）：

```bash
tradepilot serve          # 默认 127.0.0.1:8000
tradepilot serve --host 0.0.0.0 --port 8000   # 远程访问
```

Docker 部署则由 `./scripts/docker-deploy.sh local` 启动。服务运行后访问 `http://localhost:8000`。监控台提供：

- 股票与期货独立观察池和市场统计
- K 线、均线、布林带、成交量及方向变化
- 策略参数、当前倾向和回测记录
- 数据新鲜度、存储与读取异常状态
- 期货观察池（P1）：主力映射、量额、Donchian 倾向与 §5 风险测算（数据来自 `tradepilot futures scan`，倾向为参考判断而非可执行委托）
- 期货回测（P2）：`tradepilot futures backtest`——真实合约撮合（信号后下一时点成交、滑点、涨跌停与成交量约束、部分成交）、逐日盯市账本（平今/平昨分档、保证金与强平）、显式换月、滚动样本外验证（保留集不参与选参）；费率/保证金为快照近似口径，报告如实标注（股票回测入口对期货码指路本命令）

现阶段股票读取 `data/<代码>.csv`。期货行情由 `tradepilot futures scan` 从新浪源同步落库（首期 RB/HC/CU/I/M），风险预算参数在 `config.yaml` 的 `futures_risk` 中配置；期货回测消费 scan 落库的 60m/日线数据，不联网补数。

## 卸载

安装只涉及仓库目录、虚拟环境与 `~/.tradepilot/`（配置），删除这三处即完成卸载：

```bash
rm -rf .venv ~/.tradepilot
```

## 许可证

MIT License
