# AGENTS.md：给 Codex 的项目说明（每次任务都要遵守）

## 项目是什么
TikTok 英国本地生活推荐智能体（默认城市伯明翰）。用户说位置和口味，智能体查附近门店（OpenStreetMap）、
算距离和步行时间、判断营业状态（英国时间），联网搜索 TikTok 热门和优惠，最后给出有来源的推荐。
代码最终运行在「扣子编程」项目里（服务器在中国、时钟是北京时间），本仓库是它的本地开发副本。

## 目录
- `src/locallife_tools.py`：地图与搜索工具（定位、周边门店、距离、营业时间、搜索、快照兜底、熔断、故障开关 LL_FAULT）。
- `src/agents/agent.py`：LangGraph 工作流 understand → locate → nearby → search → rank → answer → verify → format。
  `AGENT_VERSION` 写进每条回答的 json，评测靠它确认版本。
- `scripts/test_locallife_tools.py`、`scripts/test_agent_offline.py`：离线测试（不联网、不调模型）。
- `scripts/eval_agent.py` + `scripts/agent_eval_set.json`：37 条评测，**只能在扣子终端里跑**（要用扣子的模型和搜索）。
- `scripts/smoke_tools.py`、`scripts/build_poi_snapshot.py`、`scripts/set_prompt.py`：在扣子终端里用的脚本。
- `scripts/pack_for_coze.sh`：把改动打成 zip，上传到扣子。

## 必须遵守
1. **不读、不打印、不修改 `.env` 和任何密钥文件**，不在代码、测试、日志里写 token/API key。需要配置时用 `os.getenv`。
2. **不执行 `git push`、不改远程仓库、不部署。** 可以 `git add` / `git commit`，提交前先给我看 diff 摘要。
3. **不在本地调用扣子专有接口**（`coze_coding_dev_sdk`、`coze_coding_utils`、`storage.*`），这些只在扣子里有；
   新增依赖它们的代码必须用 `try/except ImportError` 包住，保证本地测试能跑。
4. **不访问真实的地图/搜索服务来做测试**；测试一律用假的 http / 假的搜索客户端 / 假的模型函数（参照现有测试）。
5. 任何「现在」都用 `L.london_now()`（英国时间），不要用 `datetime.now()`。
6. 模型只负责理解和写回答；**挑店、排序、距离、营业状态、核对都由程序完成**。不要把这些交给模型。
7. 回答里的店名、距离、价格、评分、优惠必须能在工具数据里找到来源；不要放松 `verify_draft` 的核对规则，
   除非任务明确要求，并且要补测试说明为什么放松是安全的。
8. 改了 `agent.py` 的行为就把 `AGENT_VERSION` 加一（w5.3 → w5.4）；改了工具行为在模块文档字符串里记一条变更。
9. 代码注释、文案用中文；面向用户的回答文案在 `T` 字典里，中英文都要有。
10. 每次改动都要补或改测试，并在完成时运行：
    ```
    python3 scripts/test_locallife_tools.py
    python3 scripts/test_agent_offline.py
    ```
    两个都必须 0 失败。完成时报告：改了哪些文件、为什么、测试结果、需要我在扣子里验证什么。

## 风格
- Python 3.12（扣子环境），只用标准库 + langgraph / langchain-core / langchain-openai。
- 改动尽量小，不做无关的重构和格式化。
- 新的异常场景：在 `ToolError` 里用明确的 code，在工作流里给出降级路径和面向用户的说明，再加一条评测用例。
