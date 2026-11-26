"""TikTok 本地生活推荐（英国，默认伯明翰）的地图与搜索工具。

位置查询：英国邮编 → postcodes.io；地标/地址 → Photon（OpenStreetMap 数据）。
周边门店：Overpass（OpenStreetMap），多个服务器依次尝试，结果在进程内缓存。
距离计算：程序按经纬度算直线距离，步行时间按路网绕行系数估算（标明「估算」）。
热门榜单/优惠：平台联网搜索（coze_coding_dev_sdk.SearchClient）——TikTok 没有开放本地门店数据接口，
  TikTok 上的爆款门店、本地榜单和优惠套餐只能从公开网页搜索，按发布时间判断新旧。
返回给模型的状态值一律用英文代码（open / closed / unknown），回答用什么语言由生成步骤决定。

v4（2026-11-25 实测后）：
  - 口味关键词放进 Overpass 查询里过滤（之前程序端过滤，但查询限 300 条，New Street 800 米内实际 380+ 家，
    截断后的 300 家里再筛口味会漏店）；查询不再设条数上限；关键词先清洗（只留字母数字），防止注入查询语句。
  - 地图查询加总时限：每台服务器 ≤10 秒、整个周边查询 ≤15 秒，超时就转快照（urllib 的 timeout 只管单次读取，
    实测服务器慢慢吐数据时 8 秒的 timeout 拖到了 19.7 秒）。
  - 营业时间支持不写星期的「09:00-20:00」（= 每天）。
  - 搜索结果标出是否提到城市（relevant）、是否与 TikTok 相关（tiktok）、语言（lang）；
    mentions() 按店名把搜索结果和地图门店对上，作为「热度」信号。
  - 定位服务不可用时，geocode_offline() 用内置的伯明翰常见地标坐标兜底（近似坐标，来源标 builtin）。

所有失败都抛 ToolError(code, message)，由工作流决定重试、降级还是追问用户，不让模型自己猜。
设置环境变量 LL_FAULT 可以人为制造故障，用于评测异常分支，例如：
  LL_FAULT=overpass_down,search_down   多个用逗号分隔，取值见 FAULTS。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

UA = "local-life-agent/0.1 (course project)"
LONDON = ZoneInfo("Europe/London")
DEFAULT_CITY = "Birmingham"
DEFAULT_CENTER = (52.4778, -1.8990)  # Birmingham New Street
UK_BBOX = (-8.65, 49.86, 1.77, 60.86)  # min_lon, min_lat, max_lon, max_lat

# 扣子服务器访问实测（2026-11-25）：mail.ru 5–8 秒且数据当天；private.coffee 有时 2 秒、有时超时，
# 数据停在 2026-07；主站 28 秒或满载（502），不放进在线链路，只给离线快照脚本用。
OVERPASS_SERVERS: List[Tuple[str, float]] = [
    ("https://maps.mail.ru/osm/tools/overpass/api/interpreter", 10.0),
    ("https://overpass.private.coffee/api/interpreter", 8.0),
]
OVERPASS_MAIN = "https://overpass-api.de/api/interpreter"
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "poi_snapshot.json")
PHOTON_URL = "https://photon.komoot.io/api/"
POSTCODES_URL = "https://api.postcodes.io"

NEARBY_BUDGET = 15.0  # 整个周边查询（所有服务器加起来）的时限，秒；超了就转快照

WALK_SPEED_M_PER_MIN = 80.0
ROUTE_FACTOR = 1.3  # 街道绕行：直线距离 × 1.3 ≈ 步行距离（估算）

FAULTS = {
    "geocode_down",     # 定位服务全部不可用
    "overpass_down",    # 所有 Overpass 服务器不可用
    "overpass_slow",    # 第一台服务器超时（测试切换到下一台）
    "overpass_empty",   # 周边没有任何门店
    "search_down",      # 联网搜索不可用
    "search_empty",     # 搜索没有结果
    "search_stale",     # 搜索结果全部过时
}


class ToolError(Exception):
    """工具失败。code 供程序分支，message 是给日志和开发者看的。"""

    def __init__(self, code: str, message: str = "", detail: Optional[Dict[str, Any]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


def _faults() -> set:
    return {f.strip() for f in os.getenv("LL_FAULT", "").split(",") if f.strip()}


def _with_deadline(fn: Callable[[], Any], seconds: float) -> Any:
    """在 seconds 秒内拿不到结果就抛 TIMEOUT（后台线程自己结束，不阻塞回答）。"""
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(0.1, seconds))
    if t.is_alive():
        raise ToolError("TIMEOUT", f"超过 {seconds:.0f} 秒没有完成")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _http_json(url: str, *, data: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> Any:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ToolError("RATE_LIMITED", f"{url} 返回 429") from exc
        if exc.code == 404:
            raise ToolError("HTTP_404", f"{url} 返回 404") from exc
        raise ToolError("HTTP_ERROR", f"{url} 返回 {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError("TIMEOUT", f"{url} 无法访问或超时：{exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ToolError("BAD_RESPONSE", f"{url} 返回的不是 JSON：{raw[:120]!r}") from exc


# ---------------------------------------------------------------- 位置查询

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_OUTCODE_RE = re.compile(r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*$", re.I)


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    source: str                  # postcodes.io / photon
    city: str = ""
    country: str = ""
    postcode: str = ""
    candidates: List[Dict[str, Any]] = field(default_factory=list)  # 同名的其他地点，供追问
    ambiguous: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _in_uk(lat: float, lon: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = UK_BBOX
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# 实测：「New Street station」排第一的是车站地下通道（footway），坐标偏了 200 米。
# 用户说「station」时优先车站本身；人行道、台阶、站台、入口这类要素一律往后排。
_MINOR_FEATURES = {"footway", "path", "steps", "corridor", "service", "platform", "entrance", "subway_entrance",
                   "stop_position", "pedestrian", "cycleway", "track"}


def _cand_rank(c: Dict[str, Any], text: str) -> int:
    rank = 0
    if "station" in text.lower() and c["osm_value"] in ("station", "halt") and c["osm_key"] in ("railway", "public_transport", "building"):
        rank -= 2
    if c["osm_value"] in _MINOR_FEATURES:
        rank += 1
    return rank


def geocode(text: str, *, http: Callable[..., Any] = _http_json) -> Place:
    """把用户说的位置变成坐标。

    - 完整邮编（B1 1BB）或邮编前半段（B1）走 postcodes.io；
    - 其余走 Photon，偏向伯明翰；英国境内找不到、境外找得到 → OUT_OF_COVERAGE；
    - 英国境内同名地点分布在不同城市 → ambiguous=True，并给出候选。
    """
    text = (text or "").strip()
    if not text:
        raise ToolError("NO_LOCATION", "没有提供位置")
    if "geocode_down" in _faults():
        raise ToolError("TIMEOUT", "人为故障：geocode_down")

    m = _POSTCODE_RE.search(text)
    if m:
        pc = f"{m.group(1)} {m.group(2)}".upper()
        try:
            r = http(f"{POSTCODES_URL}/postcodes/{urllib.parse.quote(pc)}")
        except ToolError as exc:
            if exc.code == "HTTP_404":
                raise ToolError("LOCATION_NOT_FOUND", f"邮编 {pc} 不存在") from exc
            raise
        res = r.get("result") or {}
        return Place(name=pc, lat=res["latitude"], lon=res["longitude"], source="postcodes.io",
                     city=res.get("admin_district") or "", country=res.get("country") or "England", postcode=pc)
    m = _OUTCODE_RE.match(text)
    if m:
        oc = m.group(1).upper()
        try:
            r = http(f"{POSTCODES_URL}/outcodes/{urllib.parse.quote(oc)}")
        except ToolError as exc:
            if exc.code == "HTTP_404":
                raise ToolError("LOCATION_NOT_FOUND", f"邮编区 {oc} 不存在") from exc
            raise
        res = r.get("result") or {}
        district = (res.get("admin_district") or [""])[0]
        return Place(name=oc, lat=res["latitude"], lon=res["longitude"], source="postcodes.io",
                     city=district, country="England", postcode=oc)

    # 先不限范围搜（只偏向伯明翰）：排第一的在英国以外，就说明用户说的是境外地点。
    # 实测教训：一开始就限定英国范围的话，境外地点（实测用的是一个中国景点）会被硬匹配成
    # 西米德兰兹名字相近的公园湖。
    lat0, lon0 = DEFAULT_CENTER
    params = {"q": text, "limit": "8", "lang": "en", "lat": str(lat0), "lon": str(lon0)}
    feats = (http(f"{PHOTON_URL}?{urllib.parse.urlencode(params)}") or {}).get("features") or []
    if not feats:
        raise ToolError("LOCATION_NOT_FOUND", f"找不到 {text}")
    top_lon, top_lat = feats[0]["geometry"]["coordinates"]
    if not _in_uk(top_lat, top_lon):
        p = feats[0]["properties"]
        raise ToolError("OUT_OF_COVERAGE", f"{text} 在 {p.get('country', '英国以外')}",
                        {"country": p.get("country"), "name": p.get("name")})

    def as_cand(f: Dict[str, Any]) -> Dict[str, Any]:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        return {"name": p.get("name") or text, "city": p.get("city") or p.get("county") or "",
                "lat": lat, "lon": lon, "postcode": p.get("postcode") or "", "country": p.get("country") or "",
                "osm_key": p.get("osm_key") or "", "osm_value": p.get("osm_value") or ""}

    cands = [as_cand(f) for f in feats if _in_uk(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])]
    if not cands:
        raise ToolError("OUT_OF_COVERAGE", f"{text} 不在英国")
    cands.sort(key=lambda c: _cand_rank(c, text))  # 稳定排序：同分保持 Photon 原顺序
    top = cands[0]
    named_city = any(c.lower() in text.lower() for c in {x["city"] for x in cands if x["city"]})
    other_cities = [c for c in cands[1:] if c["city"] and c["city"] != top["city"]]
    return Place(name=top["name"], lat=top["lat"], lon=top["lon"], source="photon", city=top["city"],
                 country=top["country"], postcode=top["postcode"], candidates=cands[1:],
                 ambiguous=bool(other_cities) and not named_city and top["city"] != DEFAULT_CITY)


# 定位服务不可用时的兜底：伯明翰常见地标的近似坐标（误差约 100–300 米），来源标 builtin
LANDMARKS: Dict[str, Tuple[str, float, float]] = {
    "new street": ("Birmingham New Street", 52.4778, -1.8990),
    "grand central": ("Grand Central", 52.4776, -1.8987),
    "bullring": ("Bullring", 52.4769, -1.8950),
    "bull ring": ("Bullring", 52.4769, -1.8950),
    "moor street": ("Moor Street Station", 52.4791, -1.8922),
    "snow hill": ("Snow Hill Station", 52.4834, -1.8990),
    "victoria square": ("Victoria Square", 52.4798, -1.9031),
    "library of birmingham": ("Library of Birmingham", 52.4796, -1.9083),
    "centenary square": ("Centenary Square", 52.4793, -1.9087),
    "broad street": ("Broad Street", 52.4770, -1.9120),
    "brindleyplace": ("Brindleyplace", 52.4785, -1.9140),
    "brindley place": ("Brindleyplace", 52.4785, -1.9140),
    "mailbox": ("The Mailbox", 52.4755, -1.9033),
    "jewellery quarter": ("Jewellery Quarter", 52.4890, -1.9120),
    "digbeth": ("Digbeth", 52.4750, -1.8850),
    "chinatown": ("Chinatown (Arcadian)", 52.4740, -1.8970),
    "arcadian": ("The Arcadian", 52.4740, -1.8970),
    "selly oak": ("Selly Oak", 52.4402, -1.9383),
    "university of birmingham": ("University of Birmingham", 52.4508, -1.9305),
    "aston university": ("Aston University", 52.4867, -1.8890),
    "edgbaston": ("Edgbaston", 52.4650, -1.9300),
    "harborne": ("Harborne", 52.4600, -1.9530),
    "moseley": ("Moseley", 52.4460, -1.8890),
    "kings heath": ("Kings Heath", 52.4330, -1.8930),
    "balti triangle": ("Balti Triangle", 52.4550, -1.8700),
    "city centre": ("Birmingham city centre", 52.4778, -1.8990),
    "birmingham": ("Birmingham city centre", 52.4778, -1.8990),
    "伯明翰": ("Birmingham city centre", 52.4778, -1.8990),
}


def geocode_offline(text: str) -> Optional[Place]:
    """在线定位全部失败时，用内置地标表匹配；匹配不到返回 None（由工作流请用户稍后重试）。"""
    low = (text or "").lower()
    for key in sorted(LANDMARKS, key=len, reverse=True):  # 长的先匹配：「library of birmingham」先于「birmingham」
        if key in low:
            name, lat, lon = LANDMARKS[key]
            return Place(name=name, lat=lat, lon=lon, source="builtin", city=DEFAULT_CITY, country="England")
    return None


# ---------------------------------------------------------------- 距离

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点直线距离（米，球面公式）。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_minutes(meters: float) -> int:
    """步行时间估算：直线距离 × 绕行系数 ÷ 步速，至少 1 分钟。"""
    return max(1, math.ceil(meters * ROUTE_FACTOR / WALK_SPEED_M_PER_MIN))


# ---------------------------------------------------------------- 营业时间（OSM opening_hours 的常见子集）

_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _day_set(spec: str) -> Optional[set]:
    days: set = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in _DAYS or b not in _DAYS:
                return None
            i, j = _DAYS.index(a), _DAYS.index(b)
            rng = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
            days.update(_DAYS[k] for k in rng)
        elif part in _DAYS:
            days.add(part)
        else:
            return None
    return days


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def open_status(opening_hours: str, now: Optional[datetime] = None) -> str:
    """按英国时间判断是否营业：返回 open / closed / unknown。

    只解析常见写法（Mo-Fr 12:00-22:00; Sa,Su 11:00-23:00、24/7、跨午夜、PH off 忽略），
    解析不了就返回 unknown，不猜。
    """
    oh = (opening_hours or "").strip()
    if not oh:
        return "unknown"
    if oh == "24/7":
        return "open"
    now = (now or datetime.now(LONDON)).astimezone(LONDON)
    today, yesterday = _DAYS[now.weekday()], _DAYS[(now.weekday() - 1) % 7]
    cur = now.hour * 60 + now.minute
    rules: Dict[str, List[Tuple[int, int]]] = {}
    for rule in oh.split(";"):
        rule = rule.strip()
        if not rule or rule.startswith("PH"):
            continue
        if re.match(r"^[\d:,\- ]+$", rule):
            rule = "Mo-Su " + rule  # 不写星期 = 每天
        m = re.match(r"^([A-Za-z,\- ]+?)\s+(off|closed|[\d:,\- ]+)$", rule)
        if not m:
            return "unknown"
        days = _day_set(m.group(1).replace(" ", ""))
        if days is None:
            return "unknown"
        spans: List[Tuple[int, int]] = []
        if m.group(2) not in ("off", "closed"):
            for span in m.group(2).split(","):
                mm = re.match(r"^\s*(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s*$", span)
                if not mm:
                    return "unknown"
                spans.append((_minutes(mm.group(1)), _minutes(mm.group(2))))
        for d in days:
            rules[d] = spans  # 后写的规则覆盖前面的，符合 OSM 习惯
    if not rules:
        return "unknown"
    # OSM 习惯：写出来的规则之外的日子都不营业
    for a, b in rules.get(today, []):
        if (a <= cur < b) if b > a else (cur >= a):
            return "open"
    for a, b in rules.get(yesterday, []):
        if b <= a and cur < b:  # 昨天开到今天凌晨
            return "open"
    return "closed"


# ---------------------------------------------------------------- 周边门店

FOOD_AMENITIES = ("restaurant", "cafe", "fast_food", "pub", "bar", "food_court", "ice_cream")
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL = 6 * 3600


@dataclass
class Shop:
    name: str
    lat: float
    lon: float
    amenity: str
    cuisine: str = ""
    opening_hours: str = ""
    address: str = ""
    postcode: str = ""
    website: str = ""
    phone: str = ""
    osm_id: str = ""
    distance_m: int = 0
    walk_min: int = 0
    open_now: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def clean_keywords(keywords: Optional[List[str]]) -> List[str]:
    """口味关键词清洗：小写、只留字母数字空格下划线连字符、去重、最多 6 个。

    关键词来自模型对用户问题的理解，会拼进 Overpass 查询语句，所以必须先清洗，防止查询注入。
    """
    out: List[str] = []
    for k in keywords or []:
        k = re.sub(r"[^a-z0-9 _\-]", "", str(k).lower()).strip()
        if 2 <= len(k) <= 30 and k not in out:
            out.append(k)
    return out[:6]


def _kw_pattern(kws: List[str]) -> str:
    """关键词 → 正则：下划线和空格可互换（hot_pot 匹配 Hot Pot / hotpot）。"""
    return "|".join(re.sub(r"[ _]", "[ _]?", k) for k in kws)


DIET_KW = ("vegan", "vegetarian", "halal")  # OSM 用 diet:vegan=yes 这类标签表示，不写在 cuisine 里


def _diet_words(tags: Dict[str, str]) -> str:
    return " ".join(d for d in DIET_KW if tags.get(f"diet:{d}") in ("yes", "only"))


def _overpass_query(lat: float, lon: float, radius: int, amenities: Tuple[str, ...],
                    keywords: Optional[List[str]] = None) -> str:
    pattern = "|".join(amenities)
    kws = clean_keywords(keywords)
    base = f'nwr(around:{radius},{lat:.6f},{lon:.6f})[amenity~"^({pattern})$"][name]'
    if not kws:
        return f'[out:json][timeout:12];{base};out center tags;'
    # 口味在服务器端过滤：名称或菜系里含任一关键词（不区分大小写）；素食/清真另查 diet:* 标签，取并集
    parts = [f'{base}[~"^(name|cuisine)$"~"{_kw_pattern(kws)}",i];']
    parts += [f'{base}["diet:{d}"~"^(yes|only)$"];' for d in DIET_KW if d in kws]
    return f'[out:json][timeout:12];({"".join(parts)});out center tags;'


def tag_text(tags: Dict[str, str]) -> str:
    """程序端匹配口味用的文本：店名 + 菜系 + 饮食标签。"""
    return f"{tags.get('name', '')} {tags.get('cuisine', '')} {_diet_words(tags)}"


# 熔断：某台服务器超时或报错后，接下来 CIRCUIT_SECONDS 秒内直接跳过它。
# 实测 mail.ru 会间歇性地连续几分钟没响应，不熔断的话，这段时间里每个请求都要白等 10–15 秒才转快照。
CIRCUIT_SECONDS = 300
_DOWN_UNTIL: Dict[str, float] = {}


def _fetch_overpass(query: str, http: Callable[..., Any], budget: Optional[float] = None) -> Tuple[Dict[str, Any], str]:
    budget = NEARBY_BUDGET if budget is None else budget
    faults = _faults()
    if "overpass_down" in faults:
        raise ToolError("MAP_UNAVAILABLE", "人为故障：overpass_down", {"tried": [u for u, _ in OVERPASS_SERVERS]})
    errors: List[str] = []
    deadline = time.monotonic() + budget
    for i, (url, timeout) in enumerate(OVERPASS_SERVERS):
        left = deadline - time.monotonic()
        if time.time() < _DOWN_UNTIL.get(url, 0):
            errors.append(f"{url} → SKIPPED（{CIRCUIT_SECONDS // 60} 分钟内失败过，暂时跳过）")
            continue
        if left < 2.0:
            errors.append(f"{url} → SKIPPED（总时限 {budget:.0f} 秒已用完）")
            continue
        limit = min(timeout, left)
        if i == 0 and "overpass_slow" in faults:
            errors.append(f"{url} → TIMEOUT（人为故障：overpass_slow）")  # 人为故障不触发熔断，免得影响后面的用例
            continue
        try:
            data = _with_deadline(lambda: http(url, data={"data": query}, timeout=limit), limit)
            if not isinstance(data, dict) or "elements" not in data:
                raise ToolError("BAD_RESPONSE", f"{url} 缺少 elements")
            _DOWN_UNTIL.pop(url, None)
            return data, url
        except ToolError as exc:
            errors.append(f"{url} → {exc.code}")
            if exc.code in ("TIMEOUT", "RATE_LIMITED", "HTTP_ERROR", "BAD_RESPONSE"):
                _DOWN_UNTIL[url] = time.time() + CIRCUIT_SECONDS
    raise ToolError("MAP_UNAVAILABLE", "所有 Overpass 服务器都失败：" + "；".join(errors), {"tried": errors})


_SNAP_CACHE: Dict[str, Tuple[float, Any]] = {}
PLANNED_SNAPSHOT_RADIUS = 1500  # 不限口味、半径 ≥ 这个值：直接用当天的快照（实测这类查询在线要 15 秒以上）
PLANNED_SNAPSHOT_MAX_AGE = 2    # 快照超过几天就不再优先使用


def load_snapshot(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """离线门店快照（scripts/build_poi_snapshot.py 生成）：大范围查询直接用，在线服务器都失败时兜底。

    按文件修改时间缓存，重建快照后自动重新读取。path 留空时用 SNAPSHOT_PATH（测试里可以改成不存在的路径）。
    """
    path = path or SNAPSHOT_PATH
    try:
        mtime = os.path.getmtime(path)
        hit = _SNAP_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        with open(path, encoding="utf-8") as fh:
            snap = json.load(fh)
        _SNAP_CACHE[path] = (mtime, snap)
        return snap
    except (OSError, ValueError):
        return None


def _from_snapshot(lat: float, lon: float, radius: int, amenities: Tuple[str, ...],
                   snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not snap:
        return None
    clat, clon = snap["center"]
    if distance_m(lat, lon, clat, clon) + radius > snap["radius"]:
        return None  # 查询范围超出快照覆盖范围，快照不完整，不能用
    els = [e for e in snap["elements"]
           if e["tags"].get("amenity") in amenities and distance_m(lat, lon, e["lat"], e["lon"]) <= radius]
    return {"elements": els, "server": "snapshot", "data_time": snap["data_time"]}


def nearby(lat: float, lon: float, *, radius: int = 800, keywords: Optional[List[str]] = None,
           amenities: Tuple[str, ...] = FOOD_AMENITIES, now: Optional[datetime] = None,
           http: Callable[..., Any] = _http_json, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """查周边门店，返回 {shops, server, data_time, radius, cached, ...}。

    keywords（如 indian / curry / pizza，由上游把用户的说法整理成英文关键词）在 Overpass 查询里过滤，
    程序端再按名称和菜系过滤一遍（快照兜底时只有程序端过滤）。
    """
    kws = clean_keywords(keywords)
    key = f"{lat:.3f},{lon:.3f},{radius},{'|'.join(amenities)},{'|'.join(kws)}"
    hit = _CACHE.get(key)
    cached = bool(hit and time.time() - hit[0] < CACHE_TTL)
    planned = None
    if not cached and not kws and radius >= PLANNED_SNAPSHOT_RADIUS:
        snap = _from_snapshot(lat, lon, radius, amenities, snapshot if snapshot is not None else load_snapshot())
        age = _data_age_days(snap["data_time"]) if snap else None
        if snap and age is not None and age <= PLANNED_SNAPSHOT_MAX_AGE:
            planned = dict(snap, fallback_reason="", planned=True)
    if cached:
        payload = hit[1]
    elif planned:
        payload = planned
    else:
        try:
            data, server = _fetch_overpass(_overpass_query(lat, lon, radius, amenities, kws), http)
            payload = {"elements": data.get("elements", []), "server": server,
                       "data_time": (data.get("osm3s") or {}).get("timestamp_osm_base", "")}
            _CACHE[key] = (time.time(), payload)
        except ToolError as exc:
            snap = _from_snapshot(lat, lon, radius, amenities, snapshot if snapshot is not None else load_snapshot())
            if snap is None:
                raise
            payload = dict(snap, fallback_reason=exc.message)
    elements = [] if "overpass_empty" in _faults() else payload["elements"]

    kw_re = re.compile(_kw_pattern(kws), re.I) if kws else None
    shops: List[Shop] = []
    seen = set()
    for el in elements:
        t = el.get("tags") or {}
        name = t.get("name", "").strip()
        if not name:
            continue
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None or plon is None:
            continue
        if kw_re and not kw_re.search(tag_text(t)):
            continue
        dedup = (name.lower(), round(plat, 4), round(plon, 4))
        if dedup in seen:
            continue
        seen.add(dedup)
        d = distance_m(lat, lon, plat, plon)
        addr = " ".join(x for x in (t.get("addr:housenumber", ""), t.get("addr:street", "")) if x)
        shops.append(Shop(
            name=name, lat=plat, lon=plon, amenity=t.get("amenity", ""), cuisine=t.get("cuisine", ""),
            opening_hours=t.get("opening_hours", ""), address=addr, postcode=t.get("addr:postcode", ""),
            website=t.get("website") or t.get("contact:website", ""), phone=t.get("phone") or t.get("contact:phone", ""),
            osm_id=f"{el.get('type', 'node')}/{el.get('id')}", distance_m=round(d), walk_min=walk_minutes(d),
            open_now=open_status(t.get("opening_hours", ""), now)))
    shops.sort(key=lambda s: s.distance_m)
    stale = _data_age_days(payload["data_time"])
    return {"shops": [s.to_dict() for s in shops], "server": payload["server"], "data_time": payload["data_time"],
            "data_age_days": stale, "radius": radius, "cached": cached, "total_in_radius": len(elements),
            "from_snapshot": payload["server"] == "snapshot", "fallback_reason": payload.get("fallback_reason", ""),
            "snapshot_planned": bool(payload.get("planned"))}


def _data_age_days(ts: str) -> Optional[int]:
    try:
        return (datetime.now(LONDON) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return None


def _snapshot_only(url: str, **_: Any) -> Any:
    raise ToolError("MAP_UNAVAILABLE", "本次请求前面已转快照，不再等在线服务器")


def nearby_expanding(lat: float, lon: float, *, keywords: Optional[List[str]] = None,
                     radii: Tuple[int, ...] = (800, 1500, 3000), min_results: int = 3,
                     **kw: Any) -> Dict[str, Any]:
    """结果太少就逐级扩大半径；返回值多一个 tried_radii。

    - 第一次就转了快照，后面扩大半径时直接用快照（否则一次请求可能连等三轮在线超时）；
    - 扩大半径那一步失败（例如超出快照范围），返回上一步已经拿到的结果，不让整次请求失败。
    """
    tried: List[int] = []
    last: Optional[Dict[str, Any]] = None
    for r in radii:
        try:
            cur = nearby(lat, lon, radius=r, keywords=keywords, **kw)
        except ToolError:
            if last is None:
                raise
            break
        if last is not None and last.get("from_snapshot"):
            cur["fallback_reason"] = last["fallback_reason"]
        last = cur
        tried.append(r)
        if last.get("from_snapshot"):
            kw["http"] = _snapshot_only
        if len(last["shops"]) >= min_results:
            break
    assert last is not None
    last["tried_radii"] = tried
    return last


# ---------------------------------------------------------------- 热门榜单 / 优惠（联网搜索）

_INJECTION_RE = re.compile(r"(ignore (all|previous)|忽略(以上|之前|前面)|system prompt|你现在是)", re.I)


SEARCH_BUDGET = 12.0  # 一次联网搜索的时限，秒
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


_FOREIGN = {"it", "fr", "es", "de", "pt", "nl", "pl", "ja", "ko", "ru", "tr", "sv", "da"}


def _item_lang(title: str, url: str) -> str:
    """zh / en / other。实测会搜到意大利语的 Wanderlog 页面（/it/list/...），按网址里的语言段识别。"""
    if _CJK_RE.search(title):
        return "zh"
    parsed = urllib.parse.urlparse(url)
    host_first = parsed.netloc.split(".")[0].lower()
    path_first = parsed.path.strip("/").split("/")[0].lower().split("-")[0] if parsed.path.strip("/") else ""
    return "other" if host_first in _FOREIGN or path_first in _FOREIGN else "en"


def _age_days(publish_time: str, now: datetime) -> Optional[int]:
    if not publish_time:
        return None
    try:
        t = datetime.fromisoformat(publish_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=LONDON)
    return (now - t).days


def search_hot(query: str, *, count: int = 8, max_age_days: int = 365, city: Optional[str] = None,
               client: Any = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """联网搜索热门榜单 / 优惠，返回 {items, fresh, stale}。

    每条保留标题、站点、链接、摘要和发布时间；超过 max_age_days 或没有发布时间的标为过时（stale）。
    另外标出：
      relevant   标题/摘要/链接里提到了 city（没给 city 时一律 True）——实测会混进别的城市的榜单；
      tiktok     内容与 TikTok 相关（TikTok 没有开放接口，这是「TikTok 热度」唯一能拿到的信号）；
      lang       zh / en；
      suspicious 摘要里有像指令的句子（「忽略以上…」），工作流直接丢弃，不送给模型。
    """
    faults = _faults()
    if "search_down" in faults:
        raise ToolError("SEARCH_FAILED", "人为故障：search_down")
    if client is None:
        from coze_coding_dev_sdk import SearchClient  # 只在扣子环境里有
        client = SearchClient()
    try:
        resp = _with_deadline(lambda: client.search(query, count=count, need_summary=False), SEARCH_BUDGET)
    except ToolError as exc:
        raise ToolError("SEARCH_FAILED", f"{exc.code}: {exc.message}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError("SEARCH_FAILED", f"{type(exc).__name__}: {exc}") from exc
    now = (now or datetime.now(LONDON)).astimezone(LONDON)
    city_l = (city or "").lower()
    items = []
    for it in ([] if "search_empty" in faults else (getattr(resp, "web_items", None) or [])):
        age = _age_days(it.publish_time or "", now)
        if "search_stale" in faults:
            age = max_age_days + 100
        title = it.title or ""
        snippet = (it.snippet or "")[:300]
        text = f"{title} {snippet} {it.url or ''}".lower()
        items.append({"title": title, "site": it.site_name or "", "url": it.url or "",
                      "snippet": snippet, "publish_time": it.publish_time or "", "age_days": age,
                      "stale": age is None or age > max_age_days,
                      "relevant": (not city_l) or city_l in text or (city_l == "birmingham" and "brum" in text),
                      "tiktok": "tiktok" in text,
                      "lang": _item_lang(title, it.url or ""),
                      "suspicious": bool(_INJECTION_RE.search(f"{title} {snippet}"))})
    fresh = [i for i in items if not i["stale"]]
    return {"query": query, "items": items, "fresh": len(fresh), "stale": len(items) - len(fresh)}


_GENERIC_NAME_WORDS = {"the", "restaurant", "cafe", "café", "bar", "kitchen", "grill", "pub", "and", "co", "birmingham"}


def _norm_name(text: str) -> str:
    # 撇号直接去掉：地图上是「Purnell's」，网页标题常写「Purnells」
    text = text.lower().replace("’", "").replace("'", "").replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def mentions(shop_name: str, items: List[Dict[str, Any]]) -> List[int]:
    """返回提到这家店的搜索结果下标（按店名整词匹配标题和摘要）。

    去掉 the / restaurant 等通用词后少于 4 个字母的店名不匹配（「Bar」「KFC」这类容易误配）。
    """
    core = " ".join(w for w in _norm_name(shop_name).split() if w not in _GENERIC_NAME_WORDS)
    if len(core.replace(" ", "")) < 4:
        return []
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(core) + r"(?![a-z0-9])")
    return [i for i, it in enumerate(items) if pat.search(_norm_name(f"{it.get('title', '')} {it.get('snippet', '')}"))]


def london_now() -> datetime:
    """英国当前时间。扣子服务器是北京时间，所有「现在」都要用这个。"""
    return datetime.now(LONDON)
