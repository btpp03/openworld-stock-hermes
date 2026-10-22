# openworld-stock-hermes

openworld.eu.org 免费 VPS 库存监控（免费档 PROXMOX，1 vCore / 512MB / 5GB）。

- 每 2 分钟由 cron-job.org 触发 `workflow_dispatch`
- 用 `OW_COOKIE`（已登录会话）拉 `/createvps`，解析页内 `data-plan` JSON 的 `stock`
- 免费档 stock > 0 → 推 TG（@btppnode_bot，30 分钟冷却）
- 会话失效 → 推 TG 提醒换 cookie（6 小时冷却）
- 无货 / 冷却期 → 完全静默

## Secrets
- `OW_COOKIE`：openworld.eu.org 的 Cookie 串（含 `__session`、`csrf_token`、`sessioncookie` 等）
- `TG_BOT_TOKEN` / `TG_CHAT_ID`

## 下单一事
`/createvps` 的 POST 需要站方自建 captcha 的 `captcha_token`（`wss://openworld.eu.org/ws/captcha`，
6 阶段 rotate/odd/match 点选题），这个不自动破解，脚本只负责报警，最后一步人工点。
