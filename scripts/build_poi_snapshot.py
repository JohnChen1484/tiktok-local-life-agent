"""生成离线门店快照 assets/poi_snapshot.json（用于优先查询和在线失败时兜底）。

默认：伯明翰 New Street 站为中心、半径 6 公里内所有有名字的餐饮门店。
离线运行，可以慢：依次试 mail.ru 镜像和 Overpass 主站，每台最多等 90 秒。
建议每天跑一次，便于优先使用快照；回答里会写明快照的数据日期。

用法（项目根目录下）：
  python3 scripts/build_poi_snapshot.py
  python3 scripts/build_poi_snapshot.py --radius 6000
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import locallife_tools as L  # noqa: E402

KEEP_TAGS = ("name", "amenity", "cuisine", "opening_hours", "addr:housenumber", "addr:street",
             "addr:postcode", "website", "contact:website", "phone", "contact:phone",
             "diet:vegan", "diet:vegetarian", "diet:halal")  # v4：素食/清真标签，快照兜底时也能按饮食筛


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=int, default=6000)
    args = ap.parse_args()
    lat, lon = L.DEFAULT_CENTER
    query = L._overpass_query(lat, lon, args.radius, L.FOOD_AMENITIES).replace(
        "[timeout:12]", "[timeout:80]").replace("out center tags 300;", "out center tags;")
    for url in (L.OVERPASS_SERVERS[0][0], L.OVERPASS_MAIN):
        t0 = time.time()
        try:
            data = L._http_json(url, data={"data": query}, timeout=90)
        except L.ToolError as exc:
            print(f"✗ {url}（{time.time() - t0:.0f} 秒）→ {exc.code}")
            continue
        els = []
        for el in data.get("elements", []):
            plat = el.get("lat") or (el.get("center") or {}).get("lat")
            plon = el.get("lon") or (el.get("center") or {}).get("lon")
            tags = {k: v for k, v in (el.get("tags") or {}).items() if k in KEEP_TAGS}
            if plat is not None and tags.get("name"):
                els.append({"type": el.get("type"), "id": el.get("id"), "lat": plat, "lon": plon, "tags": tags})
        snap = {"center": [lat, lon], "radius": args.radius,
                "data_time": (data.get("osm3s") or {}).get("timestamp_osm_base", ""),
                "built_at": L.london_now().isoformat(timespec="minutes"), "source": url, "elements": els}
        os.makedirs(os.path.dirname(L.SNAPSHOT_PATH), exist_ok=True)
        with open(L.SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False)
        by_kind = {}
        for e in els:
            by_kind[e["tags"].get("amenity")] = by_kind.get(e["tags"].get("amenity"), 0) + 1
        with_hours = sum(1 for e in els if e["tags"].get("opening_hours"))
        print(f"✓ {url}（{time.time() - t0:.0f} 秒）：{len(els)} 家，按类型 {by_kind}，"
              f"有营业时间的 {with_hours} 家；数据时间 {snap['data_time']}")
        print(f"  已写入 {os.path.relpath(L.SNAPSHOT_PATH, ROOT)}（{os.path.getsize(L.SNAPSHOT_PATH) // 1024} KB）")
        return
    sys.exit("所有服务器都失败，快照没有生成")


if __name__ == "__main__":
    main()
