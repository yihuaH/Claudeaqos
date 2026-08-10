# Routine 唤醒词 (2026-08-08 精简版)

原唤醒词每条 800-1500 字, 且与 `playbook.md` 高度重复 —— 一旦改流程要同时改两处, 界面里的那份
还改不了 (Routine 文本无法在线更新, 只能重建)。现把流程搬进 `scripts/session.py`,
唤醒词只保留「我是谁、跑哪个窗口」。

`session.py brief` 会自己判断时间窗口、检查幂等、读所有账本与待执行文件, 打印这一次的精确清单
(含要调的 MCP 与参数, 以及可直接复制的 `daily.py` 命令行)。**它只调度, 不做任何交易决策** (红线2)。

---

## 通用前缀 (三个 Routine 都一样)

```
cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main origin/Main \
  && python3 scripts/session.py brief --workdir <scratchpad>
```

照它输出的清单执行。规则以 `CLAUDE.md` 硬性红线 + `strategy/playbook.md` 为准。

---

## ① 收盘后主跑 (~17:45 ET / 21:45 UTC, 交易日)

```
Claudeaqos 每日主跑。cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main
origin/Main && python3 scripts/session.py brief --window main_run --workdir <scratchpad>
照输出的清单逐步执行; 规则以 CLAUDE.md 红线 + strategy/playbook.md 为准。
清单里的 MCP 取数必须会话亲自做 (驱动器碰不到), 其余走 scripts/daily.py。
```

## ② 晨间核查 (~10:45 ET / 14:45 UTC, 交易日)

```
Claudeaqos 晨间核查。cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main
origin/Main && python3 scripts/session.py brief --window morning --workdir <scratchpad>
照输出的清单执行。休市或无动作无异常 → 静默结束不打扰用户。
```

## ③ 收盘战报 (~18:45 ET / 22:45 UTC, 交易日)

```
Claudeaqos 收盘战报。cd /home/user/Claudeaqos && git fetch origin Main && git checkout -B Main
origin/Main && python3 scripts/session.py brief --window report --workdir <scratchpad>
照输出的清单执行。本窗口只读, 绝不自行代跑下单; 例外: 用户明确说「执行」时按 playbook §4C/§4D。
```

---

## 为什么保留 `--window` 而不全靠自动判断

`session.py` 能按 ET 时刻自动判窗口, 但 Routine 可能因平台排队而延迟触发 (实测有过 worker 重启导致
迟到)。显式指定窗口可以避免「主跑迟到 40 分钟被判成战报窗口」这类错配。自动判断保留给人工调用
(`python3 scripts/session.py brief`)。

## 维护约定

- **流程变更只改 `scripts/session.py` 的 `CHECKLIST` + `playbook.md`**, 唤醒词不动。
- 唤醒词里若仍残留旧的分支名或 confirm 闸门描述, 一律以 `CLAUDE.md` 与 `playbook.md` 为准
  (CLAUDE.md「分支约定」已有同款声明)。
- `session.py` 输出的清单是**摘要**, 细则 (4A/4C/4D 的逐步协议) 仍在 `playbook.md`;
  两者冲突以 playbook 为准。
