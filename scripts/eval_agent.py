"""在扣子终端里直接调用智能体流程跑评测（真实地图、搜索和模型；会消耗模型和搜索额度）。

用法（项目根目录 /workspace/projects 下）：
  python3 scripts/eval_agent.py --label w5_1             # 跑全部用例
  python3 scripts/eval_agent.py --only N01,F01 --label t # 只跑指定用例
  python3 scripts/eval_agent.py --skip-faults            # 不跑故障用例
报告写到 reports/agent_eval_<label>_<时间>.md，逐条记录写到同名 .jsonl。
故障用例通过环境变量 LL_FAULT 在本进程里注入，跑完自动清除。
"""
import argparse
import json
import os
import re
import sys
import time
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import locallife_tools as L  # noqa: E402
from agents import agent as A  # noqa: E402

CJK = re.compile(r"[一-鿿]")


def split_reply(content):
    m = re.search(r"```json\n(.*?)\n```", content, re.S)
    payload = json.loads(m.group(1)) if m else {}
    return content.split("```json")[0].strip(), payload


ENV_FALLBACK = {"map_snapshot", "map_unavailable", "geocode_offline"}  # 未注入故障的地图、定位降级


def env_fallback(case, js):
    deg = set(js.get("degraded") or [])
    return not case.get("fault") and bool(deg) and deg <= ENV_FALLBACK


def check(exp, text, js, env=False):
    """返回未通过的检查项列表；地图完全不可用的环境波动改查正文是否说明无法算距离。"""
    bad = []
    req = js.get("request") or {}
    loc = js.get("location") or {}
    shops = js.get("shops") or []
    srcs = js.get("sources") or []
    body = text.split("Sources:")[0].split("来源：")[0]
    map_unavailable = env and "map_unavailable" in (js.get("degraded") or [])
    if js.get("version") != A.AGENT_VERSION:
        bad.append(f"版本 {js.get('version')} ≠ {A.AGENT_VERSION}")
    if map_unavailable:
        if not re.search(r"can['’]t check distances|没法算距离", body, re.I):
            bad.append("地图完全不可用时，正文没有说明无法计算距离")
    else:
        status = js.get("status")
        expected_statuses = exp.get("status", [status])
        if status not in expected_statuses and not (env and status == "degraded" and "ok" in expected_statuses):
            bad.append(f"status={status}，期望 {expected_statuses}")
        if "min_shops" in exp and len(shops) < exp["min_shops"]:
            bad.append(f"门店 {len(shops)} < {exp['min_shops']}")
    if "min_sources" in exp and len(srcs) < exp["min_sources"]:
        bad.append(f"来源 {len(srcs)} < {exp['min_sources']}")
    if "max_sources" in exp and len(srcs) > exp["max_sources"]:
        bad.append(f"来源 {len(srcs)} > {exp['max_sources']}")
    if "keywords_any" in exp and not set(exp["keywords_any"]) & set(req.get("keywords") or []):
        bad.append(f"关键词 {req.get('keywords')} 不含 {exp['keywords_any']}")
    if "category" in exp and req.get("category") != exp["category"]:
        bad.append(f"类别 {req.get('category')} ≠ {exp['category']}")
    if exp.get("open_now") and not req.get("open_now"):
        bad.append("没识别出「现在营业」")
    if exp.get("no_closed") and any(s["open_now"] == "closed" for s in shops):
        bad.append("推荐里有已打烊的店")
    if "budget" in exp and req.get("budget_gbp") != exp["budget"]:
        bad.append(f"预算 {req.get('budget_gbp')} ≠ {exp['budget']}")
    if exp.get("trending") and not req.get("want_trending"):
        bad.append("没识别出「热门」")
    if exp.get("deals") and not req.get("want_deals"):
        bad.append("没识别出「优惠」")
    if "city" in exp and exp["city"].lower() not in (loc.get("city") or "").lower():
        bad.append(f"城市 {loc.get('city')} ≠ {exp['city']}")
    if "location_contains" in exp and exp["location_contains"].lower() not in (loc.get("name") or "").lower():
        bad.append(f"位置 {loc.get('name')} 不含 {exp['location_contains']}")
    if "location_source" in exp and loc.get("source") != exp["location_source"]:
        bad.append(f"定位来源 {loc.get('source')} ≠ {exp['location_source']}")
    if "origin" in exp and loc.get("origin") not in exp["origin"]:
        bad.append(f"位置来历 {loc.get('origin')} 不在 {exp['origin']}")
    if exp.get("from_snapshot") and not (js.get("map") or {}).get("from_snapshot"):
        bad.append("没有走快照")
    if "degraded_has" in exp and exp["degraded_has"] not in (js.get("degraded") or []):
        bad.append(f"降级原因缺少 {exp['degraded_has']}（实际 {js.get('degraded')}）")
    if "lang" in exp:
        got = "zh" if len(CJK.findall(body)) >= 5 else "en"
        if got != exp["lang"]:
            bad.append(f"回答语言 {got} ≠ {exp['lang']}")
    if "must_include" in exp and not re.search(exp["must_include"], text, re.I):
        bad.append(f"回答里没有 /{exp['must_include']}/")
    if "max_latency_ms" in exp and (js.get("latency_ms") or 0) > exp["max_latency_ms"]:
        bad.append(f"耗时 {js.get('latency_ms')} ms > {exp['max_latency_ms']}")
    if req.get("want_trending") and js.get("status") in ("ok", "degraded") and shops:
        if not all(s.get("sources") for s in shops) and not re.search(r"simply the closest|只是按距离推荐", text):
            bad.append("问热门却推荐了没被任何来源提到的店，也没有说明")
    # 推荐类回答的通用检查：通过核对或用了模板；正文里的店名都在门店列表里
    if js.get("status") in ("ok", "degraded"):
        v = js.get("verify") or {}
        if not (v.get("ok") or v.get("templated")):
            bad.append("核对没通过却没有改用模板")
        names = {s["name"].lower() for s in shops}
        for b in re.findall(r"\*\*(.+?)\*\*", body):
            if b.strip().lower() not in names:
                bad.append(f"正文加粗了不在门店列表里的「{b}」")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-faults", action="store_true")
    ap.add_argument("--set", default=os.path.join(ROOT, "scripts", "agent_eval_set.json"))
    args = ap.parse_args()

    cases = json.load(open(args.set, encoding="utf-8"))["cases"]
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c["id"] in want]
    if args.skip_faults:
        cases = [c for c in cases if not c.get("fault")]
    graph = A.build_agent(checkpointer=MemorySaver())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    base = os.path.join(ROOT, "reports", f"agent_eval_{args.label}_{stamp}")
    rows = []
    print(f"英国时间 {L.london_now():%Y-%m-%d %H:%M}，{len(cases)} 条用例，版本 {A.AGENT_VERSION}\n")
    with open(base + ".jsonl", "w", encoding="utf-8") as jl:
        for c in cases:
            fault = c.get("fault", "")
            os.environ["LL_FAULT"] = fault
            if fault:
                L._CACHE.clear()
                A._SEARCH_CACHE.clear()
            t0 = time.time()
            text, js, err = "", {}, ""
            try:
                for turn in c["turns"]:
                    out = graph.invoke({"messages": [HumanMessage(content=turn)]},
                                       config={"configurable": {"thread_id": f"eval-{stamp}-{c['id']}"}})
                    text, js = split_reply(out["messages"][-1].content)
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            finally:
                os.environ["LL_FAULT"] = ""
            env = bool(js) and env_fallback(c, js)
            bad = [f"运行出错：{err}"] if err else check(c["expect"], text, js, env)
            row = {"id": c["id"], "category": c["category"], "fault": fault, "turns": c["turns"], "pass": not bad, "env": env,
                   "problems": bad, "seconds": round(time.time() - t0, 1), "reply": text, "json": js}
            rows.append(row)
            jl.write(json.dumps(row, ensure_ascii=False) + "\n")
            v = js.get("verify") or {}
            env_note = f"（环境波动，{'、'.join(js['degraded'])}）" if env else ""
            print(f"{'✓' if not bad else '✗'} {c['id']:4} {js.get('status', '-'):15} 店 {len(js.get('shops') or []):>2} "
                  f"源 {len(js.get('sources') or []):>2} 核对 {'模板' if v.get('templated') else v.get('attempts', '-')}"
                  f"  {row['seconds']:>5} 秒  {env_note}{'；'.join(bad)}")

    # ---- 报告
    by_cat = defaultdict(lambda: [0, 0])
    map_counts = defaultdict(int)
    for r in rows:
        by_cat[r["category"]][0] += r["pass"]
        by_cat[r["category"]][1] += 1
        js = r["json"]
        mi = js.get("map") or {}
        server = mi.get("server")
        if "map_unavailable" in (js.get("degraded") or []):
            map_counts["unavailable"] += 1
        elif server == "snapshot":
            map_counts["planned" if mi.get("snapshot_planned") else "fallback"] += 1
        elif server:
            map_counts["online"] += 1
    rec = [r for r in rows if (r["json"].get("status") in ("ok", "degraded"))]
    templ = sum(bool((r["json"].get("verify") or {}).get("templated")) for r in rec)
    retried = sum((r["json"].get("verify") or {}).get("attempts", 0) >= 2 for r in rec)
    lat = sorted(r["json"].get("latency_ms", 0) for r in rows if r["json"])
    p50 = lat[len(lat) // 2] if lat else 0
    p90 = lat[min(len(lat) - 1, int(len(lat) * 0.9))] if lat else 0
    passed = sum(r["pass"] for r in rows)
    lines = [f"# 本地生活推荐智能体评测：{args.label}", "",
             f"- 时间：英国 {L.london_now():%Y-%m-%d %H:%M}；版本：{A.AGENT_VERSION}；用例集：{os.path.basename(args.set)}",
             f"- 通过：**{passed}/{len(rows)}**",
             f"- 推荐类回答 {len(rec)} 条：重写过 {retried} 条，改用模板 {templ} 条",
             f"- 环境波动 {sum(r['env'] for r in rows)} 条（未注入故障，且仅发生地图或定位降级）",
             f"- 在线地图成功 {map_counts['online']} 条，按计划用快照 {map_counts['planned']} 条，"
             f"因故障转快照 {map_counts['fallback']} 条，地图完全不可用 {map_counts['unavailable']} 条",
             f"- 单轮耗时（最后一轮）：中位 {p50 / 1000:.1f} 秒，P90 {p90 / 1000:.1f} 秒", "",
             "| 类别 | 通过 |", "|---|---|"]
    lines += [f"| {k} | {a}/{b} |" for k, (a, b) in by_cat.items()]
    lines += ["", "| 用例 | 类别 | 故障 | 状态 | 店 | 源 | 降级 | 核对 | 秒 | 结果 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        js, v = r["json"], r["json"].get("verify") or {}
        result = "✓" if r["pass"] else "✗ " + "；".join(r["problems"])
        env_note = "（环境波动）" if r["env"] else ""
        lines.append(f"| {r['id']} | {r['category']} | {r['fault'] or '-'} | {js.get('status', '-')} | {len(js.get('shops') or [])} | "
                     f"{len(js.get('sources') or [])} | {','.join(js.get('degraded') or []) or '-'} | "
                     f"{'模板' if v.get('templated') else v.get('attempts', '-')} | {r['seconds']} | {result}{env_note} |")
    lines += ["", "## 逐条回答（供人工检查语气、理由是否有依据）", ""]
    for r in rows:
        js = r["json"]
        v = js.get("verify") or {}
        lines += [f"### {r['id']}｜{' → '.join(r['turns'])}", "",
                  f"状态 `{js.get('status')}`；需求 `{json.dumps(js.get('request'), ensure_ascii=False)}`；"
                  f"位置 `{(js.get('location') or {}).get('name')}`（{(js.get('location') or {}).get('origin')}）；"
                  f"地图 `{(js.get('map') or {}).get('server', '-')}`；搜索词 `{(js.get('search') or {}).get('queries')}`", ""]
        if v.get("problems"):
            lines.append(f"核对发现：{'；'.join(v['problems'])}\n")
        lines += ["```text", r["reply"][:1500], "```", ""]
        for attempt, draft in enumerate(v.get("rejected") or [], 1):
            lines += [f"被拒草稿 {attempt}（前 300 字）", ""]
            lines.extend(f"    {line}" for line in (draft or "（空草稿）").splitlines())
            lines.append("")
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\n通过 {passed}/{len(rows)}；报告：{os.path.relpath(base + '.md', ROOT)}")


if __name__ == "__main__":
    main()
