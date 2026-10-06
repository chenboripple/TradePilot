# TradePilot 项目分析与优化建议

## 当前定位

项目已经具备策略、行情加载、回测、监控和飞书通知的基本能力，但整体仍处于研究原型向服务化产品过渡的阶段。核心包位于 `src/ripple_tradePilot`，根目录同时保留了大量一次性研究、回测和诊断脚本，运行入口及配置格式尚未完全统一。

## P0：立即处理

### 密钥治理

本地 `config.yaml` 包含真实 Tushare Token 和飞书 Webhook。该文件当前未被 Git 跟踪，但此前 `.gitignore` 没有保护它。应立即轮换已经暴露过的飞书 Webhook，并检查 Git 历史、终端日志和备份中是否存在旧密钥。Docker 方案已将 `config.yaml`、`.env` 排除，并支持环境变量覆盖。

### 自动交易边界

当前系统适合作为研究和信号提示工具，不应直接接入真实下单。策略缺少独立的风控闸门、订单幂等、持仓对账、熔断、交易日历异常处理和完整审计日志。接入实盘前需将“信号生成”和“订单执行”拆成两个独立服务，并要求人工或风控规则确认。

## P1：近期优化

### 配置统一

~~项目同时使用 `feishu.webhook_url` 和 `notifiers.feishu.webhook` 两套结构~~（已统一为 `notifiers.feishu.{enabled,webhook,secret,dashboard_url}`，`TRADEPILOT_CONFIG` 可指定路径）。后续可进一步引入带版本号的配置模型，用 Pydantic 做启动时校验。

### 测试体系

`tests/` 已建立离线测试体系（策略/回测引擎/API/监控通知/数据清洗，CI 在 push/PR 上运行 `pytest tests/ -q`）；根目录访问真实接口的手工 `test_*.py` 已归档至 `experiments/`。后续可补充：带固定行情夹具的回归基线、显式标记的外部接口集成测试层。

### 依赖可复现性

~~`requirements.txt` 与 `pyproject.toml` 重复维护且版本范围较宽~~（已收敛：`pyproject.toml` 为唯一依赖源，`requirements.txt` 改为其生成的 freeze 快照勿手改；两处版本矛盾的 `setup.py`/`install.py` 已删除）。仍无 lock 文件，后续可 `uv lock` + Dependabot/Renovate。

### 异步与限流

监控循环使用 `asyncio.gather`，但 Tushare 和通知调用仍是同步阻塞请求，多个标的不会真正并发，并可能阻塞优雅退出。应使用线程池封装同步 SDK，或统一采用异步客户端，同时增加全局限流、指数退避和熔断。

### 状态持久化

信号去重状态只保存在内存中，容器重启后可能重复通知。回测结果散落在 JSON、CSV 和 SQLite 中。建议将运行状态、通知幂等键和策略版本统一存入 SQLite/PostgreSQL，并为数据表增加迁移机制。

## P2：结构治理

### 代码边界

~~将根目录研究脚本迁入 `experiments/`~~（已完成，38 个脚本归档，见 `experiments/README.md`；根目录现仅保留 `monitor_brief.py`——`heartbeat_tradepilot.py` 已迁移至统一回测引擎并以 `tradepilot heartbeat` 命令退役，`install.py`/`setup.py` 已随打包收敛删除）。~~CLI 中多个命令仍为 TODO~~（已清理，`tradepilot` 各命令均连接真实服务）。~~生成数据和报告移出源码树；`src/data` 不应存储运行期数据库~~（已完成：`data/`、`reports/`、`output/`、`src/data/`、`.DS_Store` 共 90+ 运行时产物解除跟踪并补 `.gitignore`；过程文档归档 `docs/history/`）。

### 可观测性

当前主要依赖文本日志。建议添加结构化 JSON 日志、任务耗时、行情延迟、接口错误率、最近成功检查时间和通知成功率指标；API 健康检查应区分存活与就绪状态。

### 回测可信度

统一回测引擎已落地：次日开盘撮合（消除前视）、涨跌停拦截、佣金/印花税/滑点、回撤闸门，并提供 walk-forward 样本外验证（`tradepilot walkforward`）；历史研究报告已逐份加注可信度横幅，研究期脚本归档至 `experiments/`。剩余：停牌、除权复权、成交量约束的固定测试；记录策略与数据版本。

### 元数据一致性

~~README 中仓库名写成了 `TradePolot`~~（已修正）。许可证已统一为 MIT：README、`pyproject.toml` classifier 与 `LICENSE` 文件三处一致。

## 已落地的部署改进

- 非 root、只读根文件系统、移除 capabilities。
- API 与监控拆分为独立容器，并配置重启策略和健康检查。
- 数据及输出目录持久化，不随镜像更新丢失。
- GitHub Actions 自动发布 amd64/arm64 镜像。
- Watchtower 仅更新带标签的 TradePilot 服务，支持滚动重启和旧镜像清理。
- 本地构建与生产镜像部署采用同一份 Compose 服务定义。

## 已落地（2026-09 复核）

- **断链修复**：monitor 配置路径、定期报告发送（原构建卡片后丢弃）、游客首屏公开行情链路。
- **可信度与口径**：持仓日夏普口径及文档纠偏、A 股红涨绿跌全仓统一（`REC_BUY/REC_SELL/REC_HOLD` 常量）、监控信号附止损/止盈参考位、行情脏值中位数过滤、24 份历史报告加注可信度横幅。
- **体验打磨**：飞书 interactive 卡片 + 监控台跳转按钮、K 线/权益曲线触摸十字线、回测表单选项由 `/api/meta/backtest-options` 后端驱动、CLI 输出脱敏飞书密钥。
- **结构治理**：38 个研究脚本归档 `experiments/`（含 README 口径警示）、文档失效引用与旧仓库路径批量修正、测试扩至 138 个并由 CI 执行。

## 已落地（2026-10 优化轮：32cc299 → a6045e6）

系统化的一轮正确性/性能/工程化治理，全部独立 commit、每步全量测试绿（最终 1111 passed + 206 subtests）。

### 正确性修复（3 处，均带"修前必挂修后可断言"的回归测试）

- **C1 期货 walkforward 换月断层**（32cc299）：`_run_once` 漏传 `roll_schedule`，段内永不换月（旧仓悬挂 + 新仓双开），污染样本外验证。修复后覆盖切换日的每个 test/holdout 段 `rolls ≥ 1` 且含换月腿费用；既有单合约用例数值不变。
- **C2 复权静默降级**（b48bff0）：qfq 拉取失败时静默回退不复权数据，overlap 合并可能把不复权行情接进库内 qfq 历史（除权日假跳空污染信号与库存）。现 `get_daily_bars` 默认抛 `AdjustedDataUnavailableError`，显式 `allow_unadjusted=True` 才降级且帧带 `attrs["adjust"]="none"` 标记；`stock_service.refresh` 二道闸拒收非 qfq 帧——不复权数据从此进不了库。
- **C3 Sharpe 年化虚高**（f5bf7f2）：持仓日稀疏收益 ×√252 年化，低覆盖策略 Sharpe 系统性虚高，带偏 `select_by="sharpe"` 选参。现按 √(252×覆盖率) 缩放（`held-coverage-scaled-v2`），满仓策略数值与旧实现逐位一致。
  - ⚠️ **口径声明：Sharpe v2 与历史记录不可直接对比**——覆盖率 <100% 的策略在新口径下 Sharpe 会低于旧记录；payload 中 `sharpe_annualization` 字段标明口径版本，跨版本比较前先看该字段。

### 性能

- **database init-once**（02293ab）：49 个 CRUD 调用点原先每次全量重跑 628 行 DDL + 每表 PRAGMA + 全库 integrity_check；现进程内同路径只引导一次，`force=True` 为逃生口，`integrity_check` 移到部署入口显式执行（频率从"每次读写"合理化为"每次部署"）。全量测试套件 132s → 102s（约 −23%），`user_store`/`app` 零改动受益。
- **期货信号 O(n²)→O(n)**（34557a2）：`tilt_series` 全序列一次算完 ATR/唐奇安通道，引擎与 walkforward 改消费后抽边沿；parity 测试钉死与逐前缀参考实现 bit-exact。实测 2500 根 60m bar：1.60s → 0.005s（约 319×），网格搜索按参数数放大受益。
- **CLI 走库**（1df1166）：backtest/walkforward/screen 三处直连 tushare 全量拉取改 `load_symbol_bars_db_first`（SQLite 优先，库新鲜零网络，不足时有界补拉一次带复权审计的 refresh）。DB 足够时**无 token 离线可跑**；数据口径（vol×100 手转股 + 脏价清洗）与 loader 逐字一致。
- **热路径小项**（7f7f755）：tushare 限频时刻提为类属性（跨实例共享配额）、全市场 spot 快照 10s 类级 TTL 缓存、`load_config` 按 (path, mtime_ns) 缓存 + deepcopy 隔离、回测取数 `iterrows`→`to_dict(records)`。

### 密钥事件时间线（诚实记录）

- 初始提交起 tushare token 与飞书 webhook 以明文进入 git 历史（多个历史文件）；工作区已于 788c958 脱敏（webhook 替换、App ID 清理、config.yaml 保持未跟踪 + 600 权限）。
- **历史重写（filter-repo + force-push）尚未执行**：前置条件是用户完成 tushare token 与飞书 webhook 的轮换（新值只需更新本地 config.yaml/.env，无需告知仓库）。重写完成后仍需注意：GitHub 对不可达 commit 在 GC 前可能按 SHA 访问，轮换使密钥失效才是根除手段，重写消除的是"再 clone 即得密钥"的主通道。
- 防再犯（gitleaks pre-commit + CI 全历史扫描）随历史重写一并落地。

### 结构

- **storage 按域拆分**（4d067fd → 8909c79）：`database.py`（1100+ 行）拆为 `schema.py`（DDL/迁移/init-once/完整性校验）、`market.py`（日线/指数/市场宽度/行业）、`ml_store.py`（数据集/模型注册表）、`futures_store.py`（合约/行情/委托/成交/账户）、`catalog.py`（股票目录/快照）；`database.py` 收敛为**永久 facade**（re-export），~28 处外部导入零改动。
- **heartbeat 退役**（1a8e834）：根目录脚本迁 `monitor/heartbeat.py`，撮合走统一引擎（next_open/印花税/T+1/涨跌停全生效），入口 `tradepilot heartbeat`；状态文件路径与 schema 向后兼容。**默认停用自动参数再拟合**（96 组合网格对 250 根日线的再拟合是过拟合制造机），需 `--allow-refit` 或 `TRADEPILOT_AUTOFIT=1` 显式开启；cron 调度方需改用新命令。

### 工程化

- **ruff**（7f7f755、bf525b9）：首批规则 E4/E7/E9/F/T201，45 处 print→logger 清扫（数据层告警走 `logger.warning`；monitor 控制台通知通道的 2 处 print 是通道本体，noqa 注明保留）；storage facade 的 re-export 用 per-file-ignores 保护。
- **CI**（a6045e6）：测试矩阵 3.10/3.11/3.12（`requires-python >= 3.10`，比旧声明 3.9 从不测试更诚实）、ruff lint job、mypy job（渐进：先只查 `storage/ + signals/`，当前 0 错误，`continue-on-error` 不阻塞）、coverage 只出报告不设门槛。
- **其他**：`init_config` 返回语义修复（文件已存在时 CLI 不再误报"已创建"）；mx_loader 裸 `except:` 收窄；monitor 模块级 `logging.basicConfig` 移入 `main()` 消除 import 副作用。

### 本轮明确推迟（未做）

user_store.py 拆分、uv.lock 锁体系、py.typed + mypy strict、iterrows 全量清扫（只改了回测热路径）、app.py/cli.py/monitor 全面拆分、experiments/ 迁移、股/期货引擎合并。
