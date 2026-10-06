"""config_loader 缓存与行为测试（P7：yaml 按 (path, mtime_ns) 缓存）。"""

from __future__ import annotations

import os
import time

import pytest

from ripple_tradePilot.config_loader import (
    clear_config_cache,
    load_config,
    resolve_config_path,
)


@pytest.fixture(autouse=True)
def _isolated_cache():
    """每个用例前后清空配置缓存，且不读用户真实 ~/.tradepilot/config.yaml。"""
    clear_config_cache()
    old_env = {key: os.environ.pop(key, None) for key in (
        'TRADEPILOT_CONFIG', 'TUSHARE_TOKEN', 'TUSHARE_CACHE_DIR',
        'TUSHARE_RATE_LIMIT', 'MX_APIKEY', 'FEISHU_WEBHOOK_URL',
        'FEISHU_WEBHOOK_SECRET', 'FEISHU_ENABLED')}
    yield
    for key, value in old_env.items():
        if value is not None:
            os.environ[key] = value
    clear_config_cache()


class TestConfigCache:
    def test_same_file_same_mtime_uses_cache(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tushare:\n  token: abc\n", encoding="utf-8")
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(config_file))

        first = load_config()
        assert first['tushare']['token'] == 'abc'
        # 调用方改写返回值不得污染缓存（后续 load 应拿到原始值）
        first['tushare']['token'] = 'MUTATED'
        second = load_config()
        assert second['tushare']['token'] == 'abc'

    def test_mtime_change_invalidates_cache(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tushare:\n  token: v1\n", encoding="utf-8")
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(config_file))
        assert load_config()['tushare']['token'] == 'v1'

        config_file.write_text("tushare:\n  token: v2\n", encoding="utf-8")
        # 确保 mtime_ns 前进（同一纳秒内改写会被误判未变化）
        now = time.time_ns()
        os.utime(config_file, ns=(now, now + 1_000_000))
        assert load_config()['tushare']['token'] == 'v2'

    def test_env_override_applied_per_call(self, tmp_path, monkeypatch):
        """缓存的是 yaml 解析；环境变量覆盖每次重放——改 env 立即生效。"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tushare:\n  token: from-file\n", encoding="utf-8")
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(config_file))

        assert load_config()['tushare']['token'] == 'from-file'
        monkeypatch.setenv('TUSHARE_TOKEN', 'from-env')
        assert load_config()['tushare']['token'] == 'from-env'

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        """所有候选都不存在 → 空配置（本机真实 ~/.tradepilot 不得渗入用例）。"""
        monkeypatch.setattr('ripple_tradePilot.config_loader.USER_CONFIG_FILE',
                            tmp_path / 'home.yaml')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(tmp_path / "nope.yaml"))
        assert load_config() == {}


class TestResolveConfigPath:
    def test_explicit_path_wins(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(tmp_path / "env.yaml"))
        assert resolve_config_path(str(config_file)) == config_file

    def test_missing_returns_highest_priority(self, tmp_path, monkeypatch):
        monkeypatch.setattr('ripple_tradePilot.config_loader.USER_CONFIG_FILE',
                            tmp_path / 'home.yaml')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('TRADEPILOT_CONFIG', str(tmp_path / "env.yaml"))
        assert resolve_config_path() == tmp_path / "env.yaml"
