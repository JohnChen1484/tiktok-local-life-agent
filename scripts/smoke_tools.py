"""在扣子终端里实测地图和搜索工具（不调用大模型）。

用法（项目根目录 /workspace/projects 下）：
  python3 scripts/smoke_tools.py            # 正常调用：定位、周边、距离、营业状态、搜索
  python3 scripts/smoke_tools.py --faults   # 再用故障开关跑一遍异常分支
  python3 scripts/smoke_tools.py --no-search  # 不调用联网搜索（不花积分）
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import locallife_tools as L  # noqa: E402


def host(server):
    return server.split("/")[2] if "://" in server else server


def timed(label, fn):
    t0 = time.time()
    try:
        out = fn()
        print(f"  ✓ {label}（{time.time() - t0:.1f} 秒）")
        return out
    except L.ToolError as exc:
        print(f"  ✗ {label}（{time.time() - t0:.1f} 秒）→ {exc.code}：{exc.message}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faults", action="store_true")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--only-faults", action="store_true", help="只跑故障开关部分")
    args = ap.parse_args()

    now = L.london_now()
    print(f"英国时间 {now:%Y-%m-%d %H:%M %a}（服务器时间 {time.strftime('%Y-%m-%d %H:%M')}）\n")

    lat, lon = L.DEFAULT_CENTER
    snap = L.load_snapshot()
    print(f"离线快照：{'有，' + str(len(snap['elements'])) + ' 家，数据时间 ' + snap['data_time'] if snap else '没有'}\n")
    if args.only_faults:
        args.faults = True
    else:
        normal(args)
    if args.faults:
        faults(args, lat, lon)


def normal(args):
    print("【1 位置查询】")
    for q in ["B1 1BB", "B5", "Bullring", "New Street Station Birmingham", "Selly Oak", "Victoria Square",
              "Eiffel Tower Paris", "Qwxyzzz Nowhere"]:
        p = timed(q, lambda q=q: L.geocode(q))
        if p:
            amb = f"｜歧义，候选：{[c['city'] for c in p.candidates[:3]]}" if p.ambiguous else ""
            print(f"      → {p.name}，{p.city}，({p.lat:.4f}, {p.lon:.4f})，来源 {p.source}{amb}")

    print("\n【2 周边门店：New Street 站，印度菜/咖喱（Balti）】")
    lat, lon = L.DEFAULT_CENTER
    r = timed("nearby_expanding(indian, curry, balti)",
              lambda: L.nearby_expanding(lat, lon, keywords=["indian", "curry", "balti", "pakistani"]))
    if r:
        print(f"      服务器 {host(r['server'])}，数据时间 {r['data_time']}（{r['data_age_days']} 天前），"
              f"试过半径 {r['tried_radii']}，服务器返回 {r['total_in_radius']} 条，匹配 {len(r['shops'])} 家")
        for s in r["shops"][:6]:
            print(f"      - {s['name']}｜{s['cuisine'] or '无菜系'}｜{s['distance_m']} 米，步行约 {s['walk_min']} 分钟"
                  f"｜{s['open_now']}｜{s['opening_hours'] or '无营业时间'}")
        hit = timed("同一口味再查一次（应走缓存）",
                    lambda: L.nearby(lat, lon, radius=r["tried_radii"][-1], keywords=["indian", "curry", "balti", "pakistani"]))
        if hit:
            print(f"      cached={hit['cached']}")
    v = timed("素食（v4 新增：同时查 diet:vegan 标签，验证并集查询语法）", lambda: L.nearby(lat, lon, radius=800, keywords=["vegan"]))
    if v:
        print(f"      服务器 {host(v['server'])}，{len(v['shops'])} 家：" + "、".join(s["name"] for s in v["shops"][:6]))
    a = timed("不限口味：800 米内全部餐饮（v3 限 300 条，这里应明显多于 300）", lambda: L.nearby(lat, lon, radius=800))
    if a:
        print(f"      服务器 {host(a['server'])}，{len(a['shops'])} 家；营业状态：" +
              "、".join(f"{k} {sum(s['open_now'] == k for s in a['shops'])}" for k in ("open", "closed", "unknown")))

    if not args.no_search:
        print("\n【3 热门榜单 / 优惠（联网搜索）】")
        for q in ["TikTok viral restaurants Birmingham", "site:tiktok.com Birmingham restaurant",
                  "best indian restaurants Birmingham", "Birmingham restaurant set menu deals"]:
            s = timed(q, lambda q=q: L.search_hot(q, count=8, city="Birmingham"))
            if s:
                print(f"      新 {s['fresh']} 条 / 旧 {s['stale']} 条；提到伯明翰 {sum(i['relevant'] for i in s['items'])} 条；"
                      f"与 TikTok 相关 {sum(i['tiktok'] for i in s['items'])} 条；tiktok.com 链接 {sum('tiktok.com' in i['url'] for i in s['items'])} 条")
                for it in s["items"][:5]:
                    flag = "".join(f for f, on in (("（过时）", it["stale"]), ("（不相关）", not it["relevant"]),
                                                    ("［TikTok］", it["tiktok"])) if on)
                    print(f"      - {it['title'][:40]}｜{it['site']}｜{it['age_days']} 天前{flag}")



def faults(args, lat, lon):
    if True:
        print("\n【4 故障开关】")
        for fault, fn in [
            ("geocode_down", lambda: L.geocode("Bullring")),
            ("overpass_slow", lambda: (L._CACHE.clear(), L.nearby(lat, lon, radius=500))[1]),
            ("overpass_down", lambda: (L._CACHE.clear(), L.nearby(lat, lon, radius=500))[1]),
            ("overpass_empty", lambda: (L._CACHE.clear(), L.nearby(lat, lon, radius=500))[1]),
            ("search_down", lambda: L.search_hot("TikTok viral restaurants Birmingham")),
        ]:
            if fault.startswith("search") and args.no_search:
                continue
            os.environ["LL_FAULT"] = fault
            out = timed(f"LL_FAULT={fault}", fn)
            if isinstance(out, dict) and "shops" in out:
                extra = f"（快照兜底，原因：{out['fallback_reason'][:60]}）" if out.get("from_snapshot") else ""
                print(f"      → 服务器 {host(out['server'])}，{len(out['shops'])} 家，数据时间 {out['data_time']}{extra}")
        os.environ["LL_FAULT"] = ""


if __name__ == "__main__":
    main()
