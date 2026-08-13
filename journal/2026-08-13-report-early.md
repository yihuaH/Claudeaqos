# 2026-08-13 (周四) 收盘战报窗口 — 触发时间异常, 未出报告

## 异常

本会话 (`--window report` 强制指定) 于 **12:25:03 ET** 被触发。按 `strategy/routines.md`,
收盘战报 Routine 应为 **18:45 ET (22:45 UTC, cron `45 22 * * 1-5`)**, 本次唤醒早了约 6.3 小时。
彼时市场仍在盘中 (16:00 ET 收盘), 今日收盘后主跑 (~17:45 ET / 21:45 UTC) 尚未发生,
`journal/2026-08-13.md` 因此不存在 —— 这不是主跑失败 (已知失败模式 = worker 重启),
而是本次唤醒本身时间不对, 与 `journal/2026-08-13-morning.md` §5 记录的「触发器带不上仓库源」
是另一类问题 (本次仓库绑定正常, `git fetch`/`checkout` 均成功)。

## 已核查, 无异常

- 今日晨检已于 11:47–12:00 ET 人工代跑完成 (`journal/2026-08-13-morning.md`), 4C 终局填充率
  100%, `position_check` 8/8 一致, 无残单。
- `state/pending_orders.json` (2026-08-12 生成, 已 `status=closed`) 与
  `state/pending_option_orders.json` (2026-08-05 生成, 已 `status=cancelled_unfilled`) 均为
  历史已结清记录, 当前无待执行订单。
- 未做任何交易/回写动作 (红线2/6, 本窗口只读)。

## 处理

未按「journal 缺失 → 20 分钟后复查」流程处理 —— 因为今晚主跑本就排在 17:45 ET,
此刻复查无意义。已用 PushNotification 告知用户本次触发时间异常, 建议核对该 Routine 的
cron/触发器配置 (`trig_01LSG22K25Jgqc9SR19YNVef`, 预期 `45 22 * * 1-5`) 是否被误改或误触发
(例如手动测试触发)。今晚 18:45 ET 的正常收盘战报窗口预计如期发出。
