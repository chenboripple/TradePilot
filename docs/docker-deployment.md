# Docker 部署与自动升级

## 方案结构

- `api`：FastAPI 服务，默认监听 `8000`。
- `monitor`：A 股行情监控常驻进程。
- `watchtower`：可选，只更新明确打了标签的 TradePilot 容器。
- GitHub Actions：向 `release` 或 `release-ripple` 推送后，自动构建 amd64/arm64 镜像并发布到 GHCR。

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
docker compose --profile auto-update pull
docker compose --profile auto-update up -d
docker compose ps
```

等价的单命令部署：

```bash
./scripts/docker-deploy.sh production
```

`release` 分支默认发布为：

```text
ghcr.io/chenboripple/tradepilot:release
```

如仓库名称或所有者发生变化，修改 `.env` 中的 `TRADEPILOT_IMAGE`。

## 自动升级流程

1. 代码合并或推送到 `release`。
2. GitHub Actions 构建并发布新的 `release` 镜像。
3. Watchtower 按 `WATCHTOWER_INTERVAL` 拉取新镜像。
4. Watchtower 依次重建 `api` 和 `monitor`，旧镜像清理，数据目录保留。

自动升级只覆盖镜像内代码。`compose.yaml`、服务器 `.env` 和配置文件的结构变更仍需人工审阅后更新，避免无人值守地扩大权限或破坏配置兼容性。

## 配置与数据

- `.env` 和 `config.yaml` 已被 Git 与 Docker 构建上下文排除。
- 环境变量优先于 YAML 中的 Tushare 和飞书密钥。
- `data/` 与 `output/` 挂载到宿主机，容器升级后不会丢失。
- 容器根文件系统只读，并移除了 Linux capabilities。

Watchtower 需要挂载 Docker socket，这等价于较高的宿主机权限。生产环境应限制服务器登录权限，只使用固定版本的 Watchtower，并保持 `--label-enable`，不要让它管理无关容器。

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
