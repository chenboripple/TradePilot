#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${TRADEPILOT_GITHUB_REPOSITORY:-chenboripple/TradePilot}"
BRANCH="${1:-release}"
TARGET_DIR="${TRADEPILOT_DIR:-$(pwd)}"
ARCHIVE_URL="https://github.com/${REPOSITORY}/archive/refs/heads/${BRANCH}.tar.gz"
TIMESTAMP="$(date +%Y%m%d%H%M%S)"

log() { printf '[tradepilot-update] %s\n' "$*"; }
fail() { printf '[tradepilot-update] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -d "$TARGET_DIR" ]] || fail "部署目录不存在: $TARGET_DIR"
[[ -f "$TARGET_DIR/compose.yaml" ]] || fail "当前目录不是 TradePilot 部署目录"
command -v docker >/dev/null 2>&1 || fail "未安装 Docker"
docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose plugin"

if command -v curl >/dev/null 2>&1; then
    download() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    download() { wget -q -O "$2" "$1"; }
else
    fail "需要 curl 或 wget"
fi

for file in .env config.yaml; do
    if [[ -f "$TARGET_DIR/$file" ]]; then
        cp -a "$TARGET_DIR/$file" "$TARGET_DIR/${file}.bak.${TIMESTAMP}"
        log "已备份 $file"
    fi
done

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

log "下载 ${REPOSITORY}:${BRANCH}"
download "$ARCHIVE_URL" "$WORK_DIR/source.tar.gz" || fail "下载失败，请检查仓库和分支"
tar -xzf "$WORK_DIR/source.tar.gz" -C "$WORK_DIR"
SOURCE_DIR="$(find "$WORK_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$SOURCE_DIR" && -f "$SOURCE_DIR/compose.yaml" ]] || fail "下载内容不是有效的 TradePilot 项目"

if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='.env' \
        --exclude='config.yaml' \
        --exclude='data/' \
        --exclude='output/' \
        --exclude='.git/' \
        --exclude='*.bak.*' \
        "$SOURCE_DIR/" "$TARGET_DIR/"
else
    log "未安装 rsync，使用覆盖更新模式"
    tar -czf "$WORK_DIR/payload.tar.gz" -C "$SOURCE_DIR" \
        --exclude='./.env' \
        --exclude='./config.yaml' \
        --exclude='./data' \
        --exclude='./output' \
        --exclude='./.git' .
    tar -xzf "$WORK_DIR/payload.tar.gz" -C "$TARGET_DIR"
fi

for file in .env config.yaml; do
    if [[ -f "$TARGET_DIR/${file}.bak.${TIMESTAMP}" && ! -f "$TARGET_DIR/$file" ]]; then
        cp -a "$TARGET_DIR/${file}.bak.${TIMESTAMP}" "$TARGET_DIR/$file"
    fi
done

[[ -f "$TARGET_DIR/.env" ]] || fail "缺少 .env，请先从 .env.example 创建并填写配置"
mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/output"

log "使用最新源码构建并重建服务"
(
    cd "$TARGET_DIR"
    docker compose -f compose.yaml -f compose.local.yaml up -d --build --remove-orphans
    docker compose -f compose.yaml -f compose.local.yaml run --rm --no-deps api python -m ripple_tradePilot.storage
    if docker ps --format '{{.Names}}' | grep -qx nginx; then
        docker exec nginx nginx -t
        docker exec nginx nginx -s reload
    fi
    docker compose -f compose.yaml -f compose.local.yaml ps
)

log "升级完成；SQLite 已完成建库、表结构补齐和完整性检查"
