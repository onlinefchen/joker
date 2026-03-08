# Polymarket Insider Tracker (CLI版)

基于 `polymarket` CLI 的可疑地址筛选脚本，已包含 GitHub Actions 高频扫描工作流。

## GitHub Actions Secrets

在仓库 Settings → Secrets and variables → Actions 里添加：

- `POLYMARKET_PRIVATE_KEY`（你的 Polymarket 私钥）
- `TELEGRAM_BOT_TOKEN`（可选，用于通知）
- `TELEGRAM_CHAT_ID`（可选，通知到哪个 chat）

工作流文件：`.github/workflows/polymarket-scan.yml`（默认每 15 分钟跑一次）

---

基于你本地 `polymarket` CLI 的可疑地址筛选脚本：

- 钱包新旧 + 大额突发下注
- 垂类集中度（只做某赛道）
- 异常仓位（相对自己历史仓位突增）
- 时机（默认用“距市场结束前 2-24h”做近似）
- 快进快出行为

> 说明：这里的“时机”是近似代理变量，不等于真实新闻发布时间。若你后面接入外部新闻时间线，准确度会明显提升。

## 运行

```bash
python3 polymarket_insider_tracker.py \
  --wallet-limit 80 \
  --trades-limit 160 \
  --min-score 70 \
  --top 20 \
  --out suspects.json
```

## 常用参数

- `--wallet-limit`：从 leaderboard 扫多少钱包
- `--trades-limit`：每个钱包拉多少条交易
- `--period`：day/week/month/all
- `--order-by`：pnl/vol
- `--big-bet`：绝对大额阈值（默认 15000）
- `--pre-end-hours-min/max`：默认 2~24 小时
- `--min-score`：可疑阈值（默认 70）
- `--out`：输出完整 JSON 结果

## 自动跟单 (Copy Trader)

`copy_trader.py` 在 tracker 扫描后自动跟单 core 级鲸鱼的大额买入。

### 工作流程
1. 读取 `suspects.json` 中 core 级钱包的近期大单
2. 筛选：只跟 BUY、单笔 ≥$100K、价格在 5¢-92¢ 之间
3. 通过 `polymarket clob create-order` 下限价单
4. 同一市场+方向不重复跟单
5. Telegram 通知执行结果

### 风控参数（通过 GitHub Secrets 配置）

| Secret | 默认值 | 说明 |
|---|---|---|
| `COPY_DRY_RUN` | `true` | **默认模拟模式**，改 `false` 开启真实下单 |
| `COPY_MAX_PER_TRADE` | `5` | 单笔跟单金额（USD） |
| `COPY_MAX_EXPOSURE` | `20` | 最大总仓位（USD） |

### 开启真实跟单

在 repo Settings → Secrets → Actions 添加：
```
COPY_DRY_RUN = false
```

## 实战建议

1. 先用高阈值（70+）做小样本，减少噪音。
2. 观察“减仓/清仓”行为比只看买入更有价值。
3. 连续 3 次失效就下调该地址权重。
4. 低流动市场单独处理，避免被表演单污染。
