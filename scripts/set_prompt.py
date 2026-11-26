"""把 config/agent_llm_config.json 里的系统提示词（sp）改成 TikTok 英国本地生活的定位。

工作流里的提示词写在 src/agents/agent.py（PARSE_SYS / ANSWER_SYS），这里的 sp 只是扣子页面上显示的人设说明，
改成一致，避免页面上还写着旧的「暂不提供推荐」。只改 sp 一个字段，其他配置原样保留；不读取、不打印任何密钥。
用法（项目根目录）：python3 scripts/set_prompt.py
"""
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config", "agent_llm_config.json")

SP = ("You are a local food & drink guide for TikTok users in the UK (default city: Birmingham). "
      "You find nearby shops and set menus/deals: a map tool gives locations, distances and opening hours "
      "(OpenStreetMap); web search gives trending lists and offers with sources. "
      "Answer in the user's language, only from tool data, and cite sources; never invent prices, ratings or deals.\n"
      "面向英国 TikTok 用户的本地生活推荐助手（默认伯明翰）：地图查位置、距离和营业时间，联网搜索查热门榜单和优惠，"
      "用用户的语言回答，只依据工具数据并标注来源，不编造价格、评分和优惠。")

with open(PATH, encoding="utf-8") as fh:
    cfg = json.load(fh)
shutil.copy(PATH, "/tmp/agent_llm_config.json.bak")  # 备份放 /tmp，不进 git
old = cfg.get("sp", "")
cfg["sp"] = SP
with open(PATH, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
print(f"已更新 sp（原来 {len(old)} 字 → 现在 {len(SP)} 字），备份在 /tmp/agent_llm_config.json.bak")
print(f"模型：{(cfg.get('config') or {}).get('model')}")
