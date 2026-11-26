"""生成离线门店快照 assets/poi_snapshot.json（用于优先查询和在线失败时兜底）。

默认：伯明翰 New Street 站为中心、半径 6 公里内所有有名字的餐饮门店。
离线运行，可以慢：依次试 mail.ru 镜像和 Overpass 主站，每台最多等 90 秒。
建议每天跑一次，便于优先使用快照；回答里会写明快照的数据日期。

用法（项目根目录下）：
  python3 scripts/build_poi_snapshot.py
  python3 scripts/build_poi_snapshot.py --radius 6000
  python3 scripts/build_poi_snapshot.py --force  # 跳过数据完整性检查
"""
import argparse
import json
import os
import sys
import time
from tempfile import TemporaryDirectory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import locallife_tools as L  # noqa: E402

KEEP_TAGS = ("name", "amenity", "cuisine", "opening_hours", "addr:housenumber", "addr:street",
             "addr:postcode", "website", "contact:website", "phone", "contact:phone",
             "diet:vegan", "diet:vegetarian", "diet:halal")  # v4：素食/清真标签，快照兜底时也能按饮食筛


def _snapshot_rejection(data, shop_count, existing):
    """返回阻止覆盖快照的原因，空字符串表示可以写入。"""
    if "remark" in data:
        return "响应含 remark，可能只返回了部分数据"
    if shop_count < 500:
        return f"可用门店仅 {shop_count} 家，少于 500 家"
    old_elements = existing.get("elements") if isinstance(existing, dict) else None
    if isinstance(old_elements, list) and shop_count * 10 < len(old_elements) * 7:
        return f"可用门店 {shop_count} 家，少于现有快照 {len(old_elements)} 家的 70%"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=int, default=6000)
    ap.add_argument("--force", action="store_true", help="跳过 remark 和门店数检查")
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
        if not isinstance(data, dict) or not isinstance(data.get("elements", []), list):
            print(f"✗ {url}（{time.time() - t0:.0f} 秒）→ 响应格式无效")
            continue
        els = []
        for el in data.get("elements", []):
            center = el.get("center") or {}
            plat = el.get("lat", center.get("lat"))
            plon = el.get("lon", center.get("lon"))
            tags = {k: v for k, v in (el.get("tags") or {}).items() if k in KEEP_TAGS}
            if plat is not None and plon is not None and (tags.get("name") or "").strip():
                els.append({"type": el.get("type"), "id": el.get("id"), "lat": plat, "lon": plon, "tags": tags})
        if not args.force:
            reason = _snapshot_rejection(data, len(els), L.load_snapshot())
            if reason:
                print(f"✗ {url}（{time.time() - t0:.0f} 秒）→ {reason}")
                continue
        snap = {"center": [lat, lon], "radius": args.radius,
                "data_time": (data.get("osm3s") or {}).get("timestamp_osm_base", ""),
                "built_at": L.london_now().isoformat(timespec="minutes"), "source": url, "elements": els}
        output_dir = os.path.dirname(L.SNAPSHOT_PATH)
        os.makedirs(output_dir, exist_ok=True)
        with TemporaryDirectory(dir=output_dir) as tmp:
            staged = os.path.join(tmp, "poi_snapshot.json")
            with open(staged, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(staged, L.SNAPSHOT_PATH)
        by_kind = {}
        for e in els:
            by_kind[e["tags"].get("amenity")] = by_kind.get(e["tags"].get("amenity"), 0) + 1
        with_hours = sum(1 for e in els if e["tags"].get("opening_hours"))
        print(f"✓ {url}（{time.time() - t0:.0f} 秒）：{len(els)} 家，按类型 {by_kind}，"
              f"有营业时间的 {with_hours} 家；数据时间 {snap['data_time']}")
        print(f"  已写入 {os.path.relpath(L.SNAPSHOT_PATH, ROOT)}（{os.path.getsize(L.SNAPSHOT_PATH) // 1024} KB）")
        return
    sys.exit("所有服务器均失败或数据不合格，快照未更新")


if __name__ == "__main__":
    main()
