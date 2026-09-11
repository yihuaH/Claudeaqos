# Routine 唤醒词 (2026-08-08 精简版 · 2026-08-14 起全部唤醒到常驻对话)

原唤醒词每条 800-1500 字, 且与 `playbook.md` 高度重复 —— 一旦改流程要同时改两处, 界面里的那份
还改不了 (Routine 文本无法在线更新, 只能重建)。现把流程搬进 `scripts/session.py`,
唤醒词只保留「我是谁、跑哪个窗口」。

`session.py brief` 会自己判断时间窗口、检查幂等、读所有账本与待执行文件, 打印这一次的精确清单
(含要调的 MCP 与参数, 以及可直接复制的 `daily.py` 命令行)。**它只调度, 不做任何交易决策** (红线2)。

---

## 会话模式: 全部唤醒到常驻对话 (2026-08-14 用户「所有的主跑 晨间 等全部都推送到这个对话框」)

四条 Routine 已重建为 **persistent_session 自绑定** —— 每次触发唤醒同一个常驻对话
`session_01NYPNGq5j25cGqpoLrWiA7t`, 所有主跑/晨检/战报/周度刷新的过程与产出都出现在该对话里,
用户可直接在里面回复「执行」消费 pending 清单。新窗口模式 (2026-08-13) 的四条触发器停用保留。
当前触发器清单:

| Routine | cron (UTC) | trigger ID | 状态 |
|---|---|---|---|
| **收盘前主跑 (15:20 ET)** | `20 19 * * 1-5` | `trig_01PqeuvMEsyXbTKJQ7njyVcR` | ✅ 2026-09-11 创建并挂好连接器 → 常驻对话; 首跑 2026-09-11 15:20 ET |
| 收盘后收尾 wrapup **+ 战报** (17:45 ET) | `45 21 * * 1-5` | `trig_01W1rzTiiZBaRc2taYzV6tKX` | ✅ → 常驻对话 (2026-09-11 吞并 18:45 战报) |
| 晨间核查 (10:45 ET) | `45 14 * * 1-5` | `trig_01CtgM6KvCBKywWzEtAEkNia` | ✅ → 常驻对话 |
| ~~收盘战报 (18:45 ET)~~ | `45 22 * * 1-5` | `trig_01DHhgMt8zbcyfwR9AfwTn85` | ⏸ **2026-09-11 停用, 已并入 17:45** (保留可回退) |
| 周度股票池刷新 (周一 **13:00** ET) | `0 17 * * 1` | `trig_01PvM5Mj89pokXgPwkMECZrd` | ✅ → 常驻对话 (2026-09-11 由 15:00 提前, 避开与 15:20 收盘前主跑只差 20 分钟的相撞) |
| (旧) 主跑 · 每次新窗口 | `45 21 * * 1-5` | `trig_01Y566pgrN57xjs2RXZN3Mad` | ⏸ 2026-08-14 停用 (保留可回退) |
| (旧) 晨间核查 · 每次新窗口 | `45 14 * * 1-5` | `trig_019cdJ9TLSZfZhQDN77bEGrX` | ⏸ 2026-08-14 停用 (保留可回退) |
| (旧) 收盘战报 · 每次新窗口 | `45 22 * * 1-5` | `trig_01LSG22K25Jgqc9SR19YNVef` | ⏸ 2026-08-14 停用 (保留可回退) |
| (旧) 周度股票池刷新 · 每次新窗口 | `0 19 * * 1` | `trig_01W7opsqjExxvDSHGsxUeKYq` | ⏸ 2026-08-14 停用 (保留可回退) |
| (旧) 晨检首跑健康检查 08-14 一次性 | run_once 15:15 UTC | `trig_01FBwEJyR9fQMu1UQWQPtSy6` | ⏸ 2026-08-14 停用 (专查新窗口模式, 切换后会误报) |
| (更旧) 主跑 → 长久会话 session_017Jqv… | `45 21 * * 1-5` | `trig_01JjYFiFewuCPxSEDauXmafN` | ⏸ 2026-08-13 停用 |
| (更旧) 晨检+战报 → 长久会话 session_017Jqv… | `45 14,22 * * 1-5` | `trig_01MieoRch6D7Um9fY65yBGCW` | ⏸ 2026-08-13 停用 |
| (更旧) 周度股票池刷新 → 长久会话 session_017Jqv… | `0 19 * * 1` | `trig_01Mum8dwjyp3khnYeFYtipbw` | ⏸ 2026-08-13 停用 |

⚠️ **连接器注意** (2026-08-13 实测, 2026-08-14 依旧): 经 API 建的触发器**带不上 Robinhood
(cash-printer) 连接器** (平台限制: 连接器只能从持有可传递授权的会话或 Routines 界面带入;
本次四条新触发器创建时平台同样警告 no passable connector grants)。**用户需在 claude.ai
Routines 界面为四条新 Routine 手工添加 Robinhood 工具** (老流程, 用户 2026-08-13「我可以
自己加 robinhood 工具」)。常驻对话本身持有 cash-printer, 触发唤醒时连接器是否随会话自带
待 08-14 首跑实测; 唤醒词均已内置缺连接器时的安全行为: 主跑不交易只通知 / 晨检不动单只通知 /
战报照发但注明「券商侧未核对」/ 周度刷新不刷新只通知。

**仓库源**: 常驻对话已绑定仓库 `yihuaH/Claudeaqos` (本会话即在其中工作), 新窗口模式的
缺仓库问题理论上消失; 四条唤醒词仍保留 `add_repo` 自举兜底 (容器重建后仓库源丢失时自救)。

注: cron 为 UTC, 按夏令时 (ET=UTC−4) 换算; 冬令时 (ET=UTC−5) 需整体 +1 小时重调。

---

## 通用约定 (三个 Routine 都一样)

```
cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main origin/Main \
  && python3 scripts/session.py brief --window <窗口> --workdir <scratchpad>
```

照它输出的清单执行。规则以 `CLAUDE.md` 硬性红线 + `strategy/playbook.md` 为准。
回写一律 push origin Main; 若被拒则 push 工作分支再开 PR 合并回 Main (CLAUDE.md 分支约定)。

下方 ①②③ 为 2026-08-13 新窗口版唤醒词存档; 2026-08-14 常驻对话版仅措辞微调
(「新窗口缺 Robinhood 连接器」→「本会话缺 Robinhood 连接器」+ 各自的 add_repo 自举兜底),
流程完全一致, 以触发器内实际文本为准。

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

## 常驻对话模式的注意点 (2026-08-14 起)

- **用户回复「执行」**: pending 清单存在 Main 上, 与会话无关 — 最顺手的是直接在常驻对话里说
  「执行」(战报/晨检就在里面), 也可在任意手开的有人值守会话说, 按 playbook 4C/4D 消费。
- **跨会话 context**: 虽然常驻对话有连续记忆, 但一切仍以 Main 上的账本/journal/pending 文件
  为准 (既定原则, 对话可能被平台压缩/摘要, `state/weekly_call_*_last_orders.json` 等回收
  context 文件继续存在)。
- **长对话风险**: 所有窗口挤同一对话, 会话上下文会持续变长被摘要; 各窗口本就设计成
  幂等 + 以 Main 文件为准, 摘要丢细节不影响正确性。
- **旧会话**: 新窗口模式的历史会话与更早的 session_017Jqv… 均保留可查, 只是不再被唤醒。

## 维护约定

- **流程变更只改 `scripts/session.py` 的 `CHECKLIST` + `playbook.md`**, 唤醒词不动。
- Routine 的**会话绑定方式** (常驻对话 vs 每次新窗口) 无法在线修改, 只能删掉重建;
  经 API 重建后需在界面补挂 Robinhood 连接器 (见上)。
- 唤醒词里若仍残留旧的分支名或 confirm 闸门描述, 一律以 `CLAUDE.md` 与 `playbook.md` 为准
  (CLAUDE.md「分支约定」已有同款声明)。
- `session.py` 输出的清单是**摘要**, 细则 (4A/4C/4D 的逐步协议) 仍在 `playbook.md`;
  两者冲突以 playbook 为准。


---

## ④ 收盘前主跑 (~15:20 ET / 19:20 UTC, 交易日) — 2026-09-11 已创建 `trig_01PqeuvMEsyXbTKJQ7njyVcR`

**背景**: 4C 执行协议 2026-09-10 换代后, 实测收益全在「不跨夜进场」这一项 (+0.42 pp/笔,
研究见 `journal/2026-09-10-preclose-research.md`)。故把**时间敏感的关键路径**移到收盘前,
纸面轨道/行情核对/journal 留在 17:45 的 `--phase wrapup`。

```
Claudeaqos 收盘前主跑 (关键路径)。先执行: cd /home/user/Claudeaqos && git fetch origin Main
&& git checkout -B Main origin/Main && python3 scripts/session.py brief --window preclose
--workdir <sp> (<sp>=本会话的 scratchpad 临时目录)。照输出的清单逐步执行; 规则以 CLAUDE.md
硬性红线 + strategy/playbook.md 为准。⚠️ 本窗口时间敏感: 若现在已过 15:40 ET 就**不要开跑**,
写日志说明并结束 (17:45 的 wrapup 会 fail-safe 退化为完整主跑, 出场照下)。⚠️ 当日实时价必须用
mcp__cash_printer__get_equity_quotes 的原始输出传 --quotes, 绝不可用 integrations.py quotes
(延迟 15 分钟)。买单只写 pending 绝不 place (红线9); 通知用户务必写明**两个执行窗**:
期权 今日 15:30-15:45 ET (窗口更窄, 先发), 股票 今日 15:30-15:55 ET。回写 push origin Main; 被拒则 push 工作分支开 PR。若本会话没有
mcp__cash_printer__* 工具 → 按红线6 不交易、写日志、通知用户。
```

**与 17:45 窗口的关系**: 17:45 的唤醒词不变 (仍 `--window main_run`), 但 `session.py` 现在会
按 `state/preclose_status.json` 提示该跑 `--phase wrapup`, 且 `daily.py` 自己会在当日
preclose 未完成时 **fail-safe 退化为完整主跑**。**部署收盘前 Routine 后无需改 17:45 的唤醒词。**

⚠️ **连接器仍需用户手工挂 (2026-09-11 复验, 老限制未解)**: `create_trigger` 虽已有
`connectors` 参数, 但本组织**不可用** (实测返回 "the connectors parameter is not available
for this organization")。故本 Routine 建成时 `mcp_connections: []`, 创建工具亦明确告警。
**已由用户于 2026-09-11 16:27 UTC 在 claude.ai Routines 界面手工挂上 "cash printer"**
(uuid `30bad367-f2b5-4d4f-bdda-d19719ecc17a`, 与另四条同一连接器; 界面编辑同时补全了
`allowed_tools`)。**今后经 API 新建任何 Routine 仍须重复这一步**, 别忘。
挂之前触发会按红线6 不交易、只写日志通知, 不会造成损害。

**实际部署记录**: 唤醒词在创建时已按 2026-09-11 的最新契约写全 (含 `--emit-symbols` 取清单、
券商实时报价两道硬闸、两个执行窗 15:30-15:45 / 15:30-15:55、`preclose: completed` 标记键、
首日观察项)。与本文下方的存档文本若有出入, **以触发器内实际文本为准**。


---

## ⚠️ 平台限制: 经本工具**改不了别的会话的唤醒词** (2026-09-11 实测)

`mcp__Claude_Code_Remote__update_trigger` 对唤醒到**常驻对话** (非本会话) 的 Routine:

| 字段 | 能否经 API 改 |
|---|---|
| `name` / `cron_expression` / `enabled` | ✅ 可以 (本次已用) |
| **`prompt` (唤醒词正文)** | ❌ **拒绝** — "editing the prompt of a routine whose fires deliver into a session that is not your own is not available via this tool" |

(创建时带 prompt 是可以的 —— 收盘前主跑那条就是这么建的; 只有**事后编辑**被拦。)

**因此流程变更一律走 `scripts/session.py` 的 `CHECKLIST`**, 不碰唤醒词 —— 这本来就是本仓库既定的
维护约定 (见上「维护约定」段), 现在它从"最佳实践"变成了"唯一可行路径"。唤醒词只说
「照 `session.py brief` 输出的清单执行」, 所以改清单等于改流程。

**唤醒词正文确实需要改时, 只能由用户在 claude.ai Routines 界面手工改。** 当前已知两处待改
(都只是措辞, 不影响行为, 不急):
1. **收盘前主跑** (`trig_01PqeuvMEsyXbTKJQ7njyVcR`): 正文里仍写「盘前」, 应为「收盘前」
   (2026-09-11 改名, 见 `journal/2026-09-10-preclose-impl.md` 的改名段)。
2. **周度股票池刷新** (`trig_01PvM5Mj89pokXgPwkMECZrd`): 正文仍写「须在当天 17:45 ET 收盘后主跑
   之前跑完」, 真实死线已是 **15:20**。**缓解措施已就位**: 该唤醒词自带「读 CLAUDE.md」, 而
   CLAUDE.md 的「每日节奏」段已写明周一硬约束与 15:10 停手护栏。
