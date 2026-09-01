# Docker 部署与手工升级

## 方案结构

- `api`：FastAPI 服务，默认监听 `8000`。
- `monitor`：A 股行情监控常驻进程。
- `tradepilot_db`：Docker named volume，持久化 SQLite 数据库。
- GitHub Actions：向 `release` 或 `release-ripple` 推送后，自动构建 amd64/arm64 镜像并发布到 GHCR。

API 根路径提供交易监控台，股票和期货使用独立观察池。行情文件格式统一为：

```text
trade_date,open,high,low,close,vol
```

股票配置在 `symbols`，期货配置在 `futures`；期货策略可单独放在 `futures_strategy_profiles`。

## 本地验证

本地需要 Docker Desktop 或 Docker Engine + Compose plugin。

```bash
cp .env.example .env
# 编辑 .env，至少填写 TUSHARE_TOKEN；需要通知时设置 FEISHU_ENABLED=true 并填写飞书配置
docker compose -f compose.yaml -f compose.local.yaml up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

也可以使用幂等部署脚本：

```bash
./scripts/docker-deploy.sh local
```

查看日志和停止服务：

```bash
docker compose -f compose.yaml -f compose.local.yaml logs -f monitor
docker compose -f compose.yaml -f compose.local.yaml down
```

## 生产环境首次部署

1. 在 GitHub 仓库的 Packages 页面确认镜像已经生成，并将 package visibility 设为 public；私有镜像需要先执行 `docker login ghcr.io`。
2. 服务器安装 Docker Engine 和 Compose plugin。
3. 将 `compose.yaml`、`.env.example` 和 `config.example.yaml` 放到部署目录。
4. 创建 `.env`，填写 Token、Webhook 和镜像地址。

```bash
cp .env.example .env
docker compose pull
docker compose up -d
docker compose ps
```

等价的单命令部署：

```bash
./scripts/docker-deploy.sh production
```

首次启动不需要手工创建 SQLite 文件。容器入口会自动执行：

1. 创建 `tradepilot_db` 数据卷和数据库目录。
2. 数据库不存在时自动创建。
3. 创建缺失的 `backtest_results` 表和索引。
4. 使用 `PRAGMA table_info` 检查并补齐旧数据库缺失字段。
5. 使用 `PRAGMA integrity_check` 检查 SQLite 完整性；检查失败时阻止服务启动。

`release` 分支默认发布为：

```text
ghcr.io/chenboripple/tradepilot:release
```

如仓库名称或所有者发生变化，修改 `.env` 中的 `TRADEPILOT_IMAGE`。

## 一条命令升级

在服务器的 TradePilot 部署目录执行：

```bash
./scripts/update_from_github.sh release
```

脚本会备份并保留 `.env`、`config.yaml`、`data/` 和 `output/`，从 GitHub 下载最新源码，然后在服务器本地构建镜像、重建服务并再次执行 SQLite 自检。

已经同步好源码，只希望重新本地构建并重启时，可以执行：

```bash
./scripts/docker-deploy.sh update
```

单独运行数据库自检：

```bash
./scripts/docker-deploy.sh check
```

## 手工升级流程

1. 代码合并或推送到 `release`。
2. 管理员登录服务器并进入 TradePilot 部署目录。
3. 执行 `./scripts/update_from_github.sh release`。
4. 脚本更新源码并使用 `compose.local.yaml` 在服务器本地构建。
5. 服务重建后自动执行 SQLite 自检；存在名为 `nginx` 的容器时自动校验并重载代理配置。

服务器不会后台轮询 GitHub 或 GHCR，也不会无人值守地重启服务。

## 配置与数据

- `.env` 和 `config.yaml` 已被 Git 与 Docker 构建上下文排除。
- 环境变量优先于 YAML 中的 Tushare 和飞书密钥。
- `data/` 与 `output/` 挂载到宿主机，容器升级后不会丢失。
- SQLite 位于 `tradepilot_db` named volume，容器重建和镜像更新后不会丢失。
- 容器根文件系统只读，并移除了 Linux capabilities。

## 常用运维命令

```bash
docker compose ps
docker compose logs --tail=200 api monitor
docker compose restart monitor
docker compose pull
docker compose up -d
docker image prune -f
```

一次性执行盘前提醒：

```bash
docker compose run --rm monitor python monitor_brief.py preopen --send-feishu
```

一次性执行盘中复核：

```bash
docker compose run --rm monitor python monitor_brief.py intraday --send-feishu
```
