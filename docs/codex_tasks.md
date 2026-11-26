# 给 Codex 的指令（复制到 Cursor 的 Codex 面板里用）

每条指令开头都已经要求 Codex 先读 AGENTS.md。用 Agent 模式（会在执行命令、写文件前征求同意），不要用 Full Access。

---

## 指令 0：熟悉项目（只读，第一次用）

```
先读 AGENTS.md，再读 src/locallife_tools.py、src/agents/agent.py 和两个测试脚本。
这一步不要修改任何文件。
1. 用 10 行以内说明工作流的 8 个节点各做什么，以及每个工具失败时的降级路径。
2. 运行 python3 scripts/test_locallife_tools.py 和 python3 scripts/test_agent_offline.py，报告结果。
3. 列出你认为最容易出错的 3 个地方（指出文件和函数名），只说明，不要改。
```

---

## 指令 1：修复评测里失败的用例（模板）

把扣子里评测报告中失败的那一行和对应的回答原文贴到「」里。

```
先读 AGENTS.md。下面是扣子里评测失败的用例（版本 w5.3）：
「
（贴报告表格里那一行，例如：| T03 | trending | - | ok | 2 | 5 | - | 1 | 9.1 | ✗ 关键词 [] 不含 ['brunch','breakfast'] |）
（贴「逐条回答」里这条用例的需求 json 和回答原文）
」
要求：
1. 先说明失败的根本原因在哪个节点、哪个函数，给出你的判断依据；如果是评测期望本身不合理，直接说明，不要硬改代码去迎合。
2. 用最小改动修复；在 scripts/test_agent_offline.py 或 scripts/test_locallife_tools.py 里加一条能复现这个问题的测试（修复前失败、修复后通过）。
3. AGENT_VERSION 加一。
4. 运行两个测试，0 失败后停下，给我：改动文件清单、diff 摘要、测试结果、需要我在扣子里重跑的用例 id（例如 --only T03,T01）。
不要 git push，不要读 .env。
```

---

## 指令 2：新增一个异常场景（模板，以 postcodes.io 限流为例）

```
先读 AGENTS.md。新增一个异常场景：postcodes.io 返回 429（限流）。
期望行为：
- geocode 对 postcodes.io 的 429 等 1 秒重试一次；还是 429 就抛 ToolError("RATE_LIMITED")。
- 工作流里 RATE_LIMITED 时：如果用户说的是已知地标，用 geocode_offline 兜底（degraded，开头说明）；否则请用户稍后再试。
- 在 FAULTS 里加故障开关 postcode_429，并在 scripts/agent_eval_set.json 里加一条用例（id 用 F08，fault 用 postcode_429）。
- 补离线测试：重试一次成功、重试后仍失败、工作流兜底三种情况。
- AGENT_VERSION 加一，工具模块文档字符串里记一条变更。
运行两个测试，0 失败后停下，报告改动和测试结果。不要 git push，不要读 .env。
```

---

## 指令 3：提交前自查

```
先读 AGENTS.md。检查当前未提交的改动（git diff），逐条确认：
1. 没有任何密钥、token、.env 内容、个人邮箱进入代码或测试；
2. 没有在本地直接 import 扣子专有模块而不加 try/except；
3. 所有「现在」都用的英国时间；
4. verify_draft 的核对规则没有被放松（如有，指出位置和理由）；
5. AGENT_VERSION 是否已更新；
6. 两个测试 0 失败。
只报告，不要修改文件。全部通过的话，给我一条中文 commit message（一行标题 + 3 行以内要点）。
```
