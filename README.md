# TikTok 英国本地生活推荐智能体（默认伯明翰）

本仓库是扣子编程项目「本地生活推荐智能体」的本地开发副本，用 Cursor + Codex 修改，测试通过后打包上传到扣子。

## 本地准备（第一次）
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python3 scripts/test_locallife_tools.py
python3 scripts/test_agent_offline.py
```

## 日常流程
1. 在 Cursor 里让 Codex 改代码（规则见 AGENTS.md），本地两个测试 0 失败。
2. `git commit`（自己执行）。
3. `bash scripts/pack_for_coze.sh` → 生成 `dist/coze_patch.zip`。
4. 上传到扣子项目 `/workspace/projects`，在扣子终端里：
   ```
   cd /workspace/projects
   python3 -m zipfile -e coze_patch.zip /workspace/projects && rm coze_patch.zip
   python3 scripts/test_locallife_tools.py | tail -1
   python3 scripts/test_agent_offline.py 2>&1 | tail -1
   python3 scripts/eval_agent.py --label <版本号>
   ```
5. 评测通过后在扣子里部署（确认部署的是新提交）。

## 只能在扣子里做的事
联网搜索（coze_coding_dev_sdk）、调用模型、跑评测、重建快照（`scripts/build_poi_snapshot.py`）。
