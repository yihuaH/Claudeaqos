# Routine 唤醒词 (2026-08-08 精简版 · 2026-08-13 起每次开新窗口)

原唤醒词每条 800-1500 字, 且与 `playbook.md` 高度重复 —— 一旦改流程要同时改两处, 界面里的那份
还改不了 (Routine 文本无法在线更新, 只能重建)。现把流程搬进 `scripts/session.py`,
唤醒词只保留「我是谁、跑哪个窗口」。

`session.py brief` 会自己判断时间窗口、检查幂等、读所有账本与待执行文件, 打印这一次的精确清单
(含要调的 MCP 与参数, 以及可直接复制的 `daily.py` 命令行)。**它只调度, 不做任何交易决策** (红线2)。

---

## 会话模式: 每次开新窗口 (2026-08-13 用户「每日的主跑, 战报, 收盘等还是开新窗口吧」)

三个每日 Routine 已重建为 **create_new_session_on_fire** —— 每次触发开一个全新会话,
不再唤回长久会话 (原 session_017Jqv…「Daily report sending window」, 该会话保留,
仅不再被每日 Routine 唤醒)。当前触发器清单:

| Routine | cron (UTC) | trigger ID | 状态 |
|---|---|---|---|
| 每日收盘后主跑 (17:45 ET) | `45 21 * * 1-5` | `trig_01Y566pgrN57xjs2RXZN3Mad` | ✅ 新窗口 |
| 晨间核查 (10:45 ET) | `45 14 * * 1-5` | `trig_019cdJ9TLSZfZhQDN77bEGrX` | ✅ 新窗口 |
| 收盘战报 (18:45 ET) | `45 22 * * 1-5` | `trig_01LSG22K25Jgqc9SR19YNVef` | ✅ 新窗口 |
| (旧) 主跑 → 长久会话 | `45 21 * * 1-5` | `trig_01JjYFiFewuCPxSEDauXmafN` | ⏸ 2026-08-13 停用 (保留可回退) |
| (旧) 晨检+战报 → 长久会话 | `45 14,22 * * 1-5` | `trig_01MieoRch6D7Um9fY65yBGCW` | ⏸ 2026-08-13 停用 (保留可回退) |
| 周度股票池刷新 (周一 15:00 ET) | `0 19 * * 1` | `trig_01Mum8dwjyp3khnYeFYtipbw` | 未动, 仍指长久会话 |

⚠️ **连接器注意** (2026-08-13 实测): 经 API 从任务会话重建的触发器**带不上 Robinhood
(cash-printer) 连接器** (平台限制: 连接器只能从持有可传递授权的会话或 Routines 界面带入)。
用户需在 claude.ai Routines 界面为三条新 Routine 手工添加 Robinhood 工具
(用户 2026-08-13「我可以自己加 robinhood 工具」)。三条唤醒词已内置缺连接器时的安全行为:
主跑不交易只通知 / 晨检不动单只通知 / 战报照发但注明「券商侧未核对」。

注: cron 为 UTC, 按夏令时 (ET=UTC−4) 换算; 冬令时 (ET=UTC−5) 需整体 +1 小时重调。

---

## 通用约定 (三个 Routine 都一样)

```
cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main origin/Main \
  && python3 scripts/session.py brief --window <窗口> --workdir <scratchpad>
```

照它输出的清单执行。规则以 `CLAUDE.md` 硬性红线 + `strategy/playbook.md` 为准。
新窗口回写一律 push origin Main; 若被拒则 push 工作分支再开 PR 合并回 Main (CLAUDE.md 分支约定)。

---

## ① 收盘后主跑 (~17:45 ET / 21:45 UTC, 交易日) — 已部署唤醒词

```
Claudeaqos 每日主跑 (收盘后单跑窗口)。先执行: cd /home/user/Claudeaqos && git fetch origin Main
&& git checkout -B Main origin/Main && python3 scripts/session.py brief --window main_run
--workdir <sp> (<sp>=本会话的 scratchpad 临时目录)。照输出的清单逐步执行; 规则以 CLAUDE.md
硬性红线 + strategy/playbook.md 为准。清单里的 MCP 取数必须会话亲自做 (驱动器碰不到),
其余走 scripts/daily.py。回写提交 push origin Main; 若 push Main 被拒 → push 到工作分支并开 PR
合并回 Main, 并在通知用户时说明。若本会话没有 mcp__cash_printer__* 工具 → 按红线6 不交易、
写日志、通知用户「新窗口缺 Robinhood 连接器, 请在 Routines 界面为本 Routine 添加」。
```

## ② 晨间核查 (~10:45 ET / 14:45 UTC, 交易日) — 已部署唤醒词

```
Claudeaqos 晨间核查。先执行: cd /home/user/Claudeaqos && git fetch origin Main && git checkout
-B Main origin/Main && python3 scripts/session.py brief --window morning --workdir <sp>
(<sp>=本会话的 scratchpad 临时目录)。照输出的清单执行; 规则以 CLAUDE.md 硬性红线 +
strategy/playbook.md 为准。休市或无动作无异常 → 静默结束不打扰用户。有回写则 push origin Main;
若被拒 → push 到工作分支并开 PR 合并回 Main。若本会话没有 mcp__cash_printer__* 工具 →
不做任何交易/撤单动作, 通知用户「新窗口缺 Robinhood 连接器, 请在 Routines 界面为本 Routine
添加」后结束。
```

## ③ 收盘战报 (~18:45 ET / 22:45 UTC, 交易日) — 已部署唤醒词

```
Claudeaqos 收盘战报。先执行: cd /home/user/Claudeaqos && git fetch origin Main && git checkout
-B Main origin/Main && python3 scripts/session.py brief --window report --workdir <sp>
(<sp>=本会话的 scratchpad 临时目录)。照输出的清单执行; 规则以 CLAUDE.md 硬性红线 +
strategy/playbook.md 为准。本窗口只读, 绝不自行代跑下单; 例外: 用户在本窗口明确说「执行」时按
playbook §4C/§4D 执行 pending 清单。有回写则 push origin Main; 若被拒 → push 到工作分支并开 PR
合并回 Main。若本会话没有 mcp__cash_printer__* 工具 → 仍发战报但注明「券商侧未核对 (缺
Robinhood 连接器, 请在 Routines 界面为本 Routine 添加)」。
```

---

## 为什么保留 `--window` 而不全靠自动判断

`session.py` 能按 ET 时刻自动判窗口, 但 Routine 可能因平台排队而延迟触发 (实测有过 worker 重启导致
迟到)。显式指定窗口可以避免「主跑迟到 40 分钟被判成战报窗口」这类错配。自动判断保留给人工调用
(`python3 scripts/session.py brief`)。

## 新窗口模式的注意点

- **用户回复「执行」**: pending 清单存在 Main 上, 与会话无关 — 用户可在任何有人值守窗口
  (当天的战报/晨检新窗口、或任意手开会话) 说「执行」, 按 playbook 4C/4D 消费。
- **跨会话 context**: 新窗口没有前一天的对话记忆, 一切以 Main 上的账本/journal/pending 文件为准
  (本来就是既定原则, `state/weekly_call_*_last_orders.json` 等回收 context 文件因此存在)。
- **旧长久会话**: 仍可手动使用 (用户在里面说「执行」依然有效), 只是不再被每日 Routine 唤醒。

## 维护约定

- **流程变更只改 `scripts/session.py` 的 `CHECKLIST` + `playbook.md`**, 唤醒词不动。
- Routine 的**会话绑定方式** (唤回长久会话 vs 每次新窗口) 无法在线修改, 只能删掉重建;
  经 API 重建后需在界面补挂 Robinhood 连接器 (见上)。
- 唤醒词里若仍残留旧的分支名或 confirm 闸门描述, 一律以 `CLAUDE.md` 与 `playbook.md` 为准
  (CLAUDE.md「分支约定」已有同款声明)。
- `session.py` 输出的清单是**摘要**, 细则 (4A/4C/4D 的逐步协议) 仍在 `playbook.md`;
  两者冲突以 playbook 为准。
