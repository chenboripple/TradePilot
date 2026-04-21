# TradePilot 实时监控使用指南

## 📋 项目定位

**实时行情监控 + 策略分析 + 信号通知**，人工决策交易。

- ✅ 自动监控股票行情
- ✅ 自动运行交易策略
- ✅ 自动发送交易信号通知
- ❌ 不自动下单交易（由人工决策）

---

## 🚀 快速开始

### 1. 环境准备

```bash
cd /Users/ripple/work\ space/ripple_tradePilot

# 激活虚拟环境
source .venv/bin/activate
```

### 2. 测试单次检查

```bash
# 测试监控功能（单次检查，不循环）
python3 test_monitor.py
```

**输出示例：**
```
🔍 TradePilot 监控测试 - 单次检查
📈 获取 科华生物 (002022.SZ) 最近 60 天数据...
   获取到 241 条数据
📊 生成信号...
⏸️  无交易信号（持有观望）
📉 均线状态:
   MA5:  5.98
   MA20: 6.06
   差值：-0.08 (死叉)
```

### 3. 启动实时监控

```bash
# 启动监控（每 5 分钟检查一次）
PYTHONPATH=src python3 src/ripple_tradePilot/monitor/main.py
```

**输出示例：**
```
2026-03-13 10:30:00 - TradePilot - INFO - 🚀 TradePilot 启动监控...
2026-03-13 10:30:00 - TradePilot - INFO - 📊 监控标的数：1
2026-03-13 10:30:00 - TradePilot - INFO - ⏱️ 检查间隔：300 秒
2026-03-13 10:30:01 - TradePilot - INFO - 检查 002022.SZ，最新价格：5.97
```

### 4. 后台运行（可选）

```bash
# 使用 nohup 后台运行
nohup bash -c "source .venv/bin/activate && PYTHONPATH=src python3 src/ripple_tradePilot/monitor/main.py" > monitor.log 2>&1 &

# 查看日志
tail -f monitor.log

# 停止监控
pkill -f "monitor/main.py"
```

---

## 📝 配置文件说明

配置文件：`config.yaml`

### 监控标的配置

```yaml
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategies: ["ma_cross"]  # 使用的策略
    notify_on: ["BUY", "SELL"]  # 通知的信号类型
```

**添加更多股票：**
```yaml
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategies: ["ma_cross"]
  
  - code: "600519.SH"
    name: "贵州茅台"
    strategies: ["ma_cross"]
  
  - code: "000001.SZ"
    name: "平安银行"
    strategies: ["ma_cross", "rsi"]  # 可使用多个策略
```

### 策略配置

```yaml
strategies:
  ma_cross:  # 双均线交叉
    enabled: true
    params:
      fast: 5   # 快线周期
      slow: 20  # 慢线周期
  
  rsi:  # RSI 超买超卖
    enabled: false
    params:
      period: 14
      oversold: 30   # 超卖线（买入）
      overbought: 70 # 超买线（卖出）
```

### 监控频率

```yaml
monitor:
  interval_seconds: 300  # 5 分钟检查一次
  trading_hours:
    start: "09:30"
    end: "15:00"
  check_non_trading: false  # 非交易时段不检查
```

---

## 🔔 通知配置

### 企业微信机器人

1. 在企业微信群添加机器人
2. 获取 Webhook URL
3. 配置到 `config.yaml`：

```yaml
notifiers:
  wechat:
    enabled: true
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"
```

### 钉钉机器人

1. 在钉钉群添加自定义机器人
2. 获取 Webhook URL
3. 配置到 `config.yaml`：

```yaml
notifiers:
  dingtalk:
    enabled: true
    webhook: "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
```

### 邮件通知

```yaml
notifiers:
  email:
    enabled: true
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
    sender: "your@email.com"
    password: "your_password"  # 建议使用应用专用密码
    recipients: ["your@email.com"]
```

### 控制台输出（调试用）

```yaml
notifiers:
  console:
    enabled: true  # 默认开启
```

---

## 📊 策略说明

### 双均线交叉 (MA Cross)

**买入信号：** 快线上穿慢线（金叉）
**卖出信号：** 快线下穿慢线（死叉）

**参数：**
- `fast`: 快线周期（默认 5）
- `slow`: 慢线周期（默认 20）

**适合：** 趋势行情
**不适合：** 震荡行情（会频繁假信号）

---

## 🛠️ 常见问题

### Q1: 提示"没有接口访问权限"
**A:** Tushare 基础账号（100 积分）权限有限，但足够获取日线数据。不要频繁调用 `stock_basic` 接口（限流 1 次/分钟）。

### Q2: 收不到通知
**A:** 检查：
1. 通知渠道是否启用（`enabled: true`）
2. Webhook URL 是否正确
3. 防火墙是否阻止出站请求
4. 查看日志：`tail -f monitor.log`

### Q3: 信号太多/太少
**A:** 调整策略参数：
- 信号太多：增大 `slow` 参数（如 20→30）
- 信号太少：减小 `slow` 参数（如 20→15）

### Q4: 非交易时间也想监控
**A:** 设置 `check_non_trading: true`

---

## 📈 下一步优化建议

1. **增加策略**：RSI、MACD、布林带等
2. **多周期监控**：同时监控日线/60 分钟/15 分钟
3. **信号回测**：记录历史信号，事后验证准确率
4. **Web 面板**：可视化查看监控状态和信号历史
5. **价格预警**：非策略信号，单纯价格突破提醒

---

## 📞 技术支持

遇到问题查看日志：
```bash
tail -f monitor.log
```

或查看详细日志：
```bash
# 修改 config.yaml 中 log_level: "DEBUG"
```

---

**🎯 记住：信号仅供参考，交易决策请自行判断！**
