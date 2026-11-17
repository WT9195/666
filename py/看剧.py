# -*- coding: utf-8 -*-
"""
看剧AI（kanju26.com）TVBox / 默影视 Python 源

接口说明：
  站点为前端 SPA，数据全部来自同域 /v1/* 接口，请求需带 HMAC 签名头：
    x-ai-movie-timestamp / x-ai-movie-nonce / x-ai-movie-signature
  签名内容： METHOD \\n path?query \\n timestamp \\n nonce
  签名密钥内嵌在前端 JS 中，脚本首次签名被拒时会自动从站点重新抓取。
  播放地址：先 GET /v1/playback/resolve/{episode_token} 取线路与 ticket，
            再 POST /v1/playback/resolve-line 换取直链（m3u8/mp4）。

线路排序（2026-11-17）：
  详情页会用首集把每条线路真跑一遍——读 m3u8 主清单拿 RESOLUTION（媒体清单没有
  分辨率时按「分片字节数 / 时长」估码率再归档），并限时抓首个分片算 KB/s。
  然后按「像素面积 → 速度」排序，实测取不到流的线路直接不铺。实测数据只参与
  排序，线路名保持站点原名，不再拼接分辨率/速度字样。整轮实测限时 4.5 秒，
  超时的线补到列表尾部。结果缓存 10 分钟（画质/速度按线路缓存、直链按
  「剧集 token + 线路」缓存）。
  注意：官方线后几集常返回「需登录」，播放时会自动退到能出流的线路。

extend 支持：
  空字符串            -> 使用 https://kanju26.com
  "https://xxx.com"   -> 自定义站点域名（换域名时无需改文件）
  {"host": "..."}     -> 同上，JSON 形式

挂载示例（gyys11.13.json 的 sites）：
  {"name": "看剧AI26", "type": 3, "api": "./py/看剧AI26.py", "searchable": 1,
   "quickSearch": 0, "filterable": 1, "ext": ""}
"""

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed

# OK影视 / FongMi 的 py 壳会在 init 之前先调 getDependence()，
# 基类里带着一批桥接口；能继承就继承，拿不到就自己补齐（见下方桥接口区）
try:
    from base.spider import Spider as BaseSpider
except Exception:
    BaseSpider = object

DEFAULT_HOST = "https://kanju26.com"

# 站点换域名很勤（bootstrap 里 canonical_host 已经指向 kanju13.com）。
# 主域连不上时自动切到这几个镜像，签名密钥同源通用，无需改配置。
HOST_BACKUPS = ("https://kanju13.com",)

# 前端构建时内置的签名密钥，仅在自动抓取失败时兜底
FALLBACK_SECRET = "557d0e4ae929f438da6bd84412374e6086b8af09b3fed54bf22601d5bf8c54a0"

CLIENT_HEADERS = {
    "x-ai-movie-client-name": "dianyingtiantang-frontend",
    "x-ai-movie-client-version": "1.0.0",
    "x-ai-movie-build-version": "dianyingtiantang-v2026.11.12.3-worker-flag-fix-46f791897b69-web",
    "x-ai-movie-protocol-version": "2026-07-05.library-v2.playback-v1",
}

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")

# 线路实测相关
RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.I)
BITRATE_RE = re.compile(r"#EXT-X-BITRATE:(\d+)", re.I)
EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)")
PROBE_BUDGET = 5.0      # 线路实测总时长上限（秒），超时的线排到列表尾部
PROBE_WORKERS = 16      # 并发实测路数（纯网络等待，不占 CPU；再高站点会限流）
PROBE_LIMIT = 20        # 最多实测多少条（清单已按名字里的画质预排，前 20 条够覆盖）
SHOW_UNTESTED = 8       # 未测速的线最多再补几条到列表尾部
MEASURE_TTL = 600       # 可播线路的实测结果缓存（秒）
DEAD_TTL = 180          # 死线缓存（秒），免得每次开详情都把废域名重打一遍
PENDING_TTL = 300       # 「这轮没测完」也缓存一会儿，避免重复打开详情又等满 3 秒

KIND_ORDER = [
    ("series", "电视剧"),
    ("movie", "电影"),
    ("anime", "动漫"),
    ("variety", "综艺"),
    ("short_drama", "短剧"),
    ("documentary", "纪录片"),
]

# 站点没返回筛选配置时的兜底
FALLBACK_FILTERS = {
    "genre": ["喜剧", "爱情", "动作", "犯罪", "悬疑", "科幻", "奇幻", "古装",
              "历史", "战争", "剧情", "动画", "真人秀", "脱口秀"],
    "area": ["大陆", "香港", "台湾", "日本", "韩国", "欧美", "英国", "泰国"],
    "year": ["今年", "去年", "更早", "90年代", "80年代", "1980年以前"],
    "sort": ["updated_desc", "heat_desc"],
}


def _b64e(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "kj26:" + base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64d(text):
    raw = text[5:] if text.startswith("kj26:") else text
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))


def _text(value, default=""):
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def _join(value):
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join([_text(v) for v in value if _text(v)])
    return _text(value)


class Spider(BaseSpider):
    def __init__(self):
        self.host = DEFAULT_HOST
        self._custom_host = False
        self.secret = FALLBACK_SECRET
        self._secret_refreshed_at = 0.0
        self._bootstrap = None
        self._ssl_ctx = None
        self._loose_ctx = None
        self._opt_cache = {}
        self._measure_cache = {}
        self._quality_cache = {}   # 线路 id -> 画质/速度（各集通用，用来排序）

    # ------------------------------------------------------ 壳要求的基础接口
    # 缺任何一个，OK影视 在 init 之前就会抛 AttributeError，直接中断加载，
    # 表现就是「站点能选中但一个分类都拉不到」。这里全部显式实现，
    # 不依赖 base.spider 的具体版本。

    def getDependence(self):
        """本脚本只用标准库，无第三方依赖。"""
        return []

    def getName(self):
        return "看剧"

    def isVideoFormat(self, url):
        low = _text(url).lower().split("?")[0]
        return low.endswith((".m3u8", ".mp4", ".mkv", ".flv", ".ts", ".mpd"))

    def manualVideoCheck(self):
        return False

    def liveContent(self, url):
        return {"class": [], "list": []}

    def localProxy(self, param):
        return None

    def action(self, action):
        return None

    def destroy(self):
        return None

    def log(self, msg):
        return None

    # ------------------------------------------------------------------ 基础

    def init(self, extend=""):
        ext = _text(extend)
        if ext.startswith("http"):
            self.host = ext.rstrip("/")
            self._custom_host = True
        elif ext.startswith("{"):
            try:
                cfg = json.loads(ext)
                if isinstance(cfg, dict) and _text(cfg.get("host")):
                    self.host = _text(cfg.get("host")).rstrip("/")
                    self._custom_host = True
            except Exception:
                pass
        return None

    def _ctx(self):
        if self._ssl_ctx is None:
            try:
                self._ssl_ctx = ssl.create_default_context()
            except Exception:
                self._ssl_ctx = ssl._create_unverified_context()
        return self._ssl_ctx

    def _raw(self, url, timeout=20, headers=None):
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx()) as resp:
                return resp.read()
        except Exception:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()

    def _sign_headers(self, method, path_query):
        ts = str(int(time.time() * 1000))
        nonce = os.urandom(16).hex()
        msg = "%s\n%s\n%s\n%s" % (method, path_query, ts, nonce)
        sig = hmac.new(self.secret.encode("utf-8"), msg.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        headers = dict(CLIENT_HEADERS)
        headers.update({
            "x-ai-movie-timestamp": ts,
            "x-ai-movie-nonce": nonce,
            "x-ai-movie-signature": sig,
            "Accept": "application/json",
            "Origin": self.host,
            "Referer": self.host + "/",
            "User-Agent": UA,
        })
        return headers

    def _open(self, method, path, params=None, body=None, timeout=20, auth=True):
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            path_query = path + "?" + query
        else:
            path_query = path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = self._sign_headers(method, path_query) if auth else {
            "Accept": "application/json", "User-Agent": UA,
            "Origin": self.host, "Referer": self.host + "/",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.host + path_query, data=payload,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx()) as resp:
                return resp.getcode(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:
                return exc.code, ""
        except Exception:
            return 0, ""

    def _refresh_secret(self):
        """签名被拒时，从站点前端 bundle 里重新提取签名密钥。"""
        if time.time() - self._secret_refreshed_at < 60:
            return False
        self._secret_refreshed_at = time.time()
        try:
            html = self._raw(self.host + "/", timeout=15).decode("utf-8", "replace")
        except Exception:
            return False
        assets = re.findall(r'(?:src|href)="(/assets/[A-Za-z0-9_.\-]+\.js)"', html)
        seen = set()
        for path in assets:
            if path in seen:
                continue
            seen.add(path)
            try:
                js = self._raw(self.host + path, timeout=20).decode("utf-8", "replace")
            except Exception:
                continue
            match = re.search(r'["\']([0-9a-f]{64})["\']\s*\.trim\(\)', js)
            if not match:
                match = re.search(r'([0-9a-f]{64})', js)
            if match:
                self.secret = match.group(1)
                return True
        return False

    def _api(self, path, params=None, method="GET", body=None, timeout=20, retry=True):
        code, text = self._open(method, path, params=params, body=body, timeout=timeout)
        if code == 0 or code >= 500:
            # 网络抖动或网关 5xx：原地重试一次
            time.sleep(0.3)
            code, text = self._open(method, path, params=params, body=body, timeout=timeout)
        if retry and code in (401, 403) and "signature" in text.lower():
            if self._refresh_secret():
                code, text = self._open(method, path, params=params, body=body, timeout=timeout)
        if code >= 400 or code == 0:
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 数据映射

    def _video(self, card):
        adults = bool(card.get("is_adult")) or card.get("content_kind") == "adult"
        if adults:
            return None
        name = _text(card.get("title")) or _text(card.get("normalized_title"))
        if not name:
            return None
        return {
            "vod_id": _text(card.get("id")) or _text(card.get("selected_variant_id")),
            "vod_name": name,
            "vod_pic": _text(card.get("poster_url")) or _text(card.get("carousel_url")),
            "vod_remarks": _text(card.get("remarks")),
            "vod_year": _text(card.get("year")),
            "vod_area": _text(card.get("area")),
            "vod_lang": _text(card.get("language")),
            "vod_actor": _join(card.get("actors")),
            "vod_director": _join(card.get("directors")),
            "vod_content": _text(card.get("description") or card.get("overview")),
            "type_name": ",".join([g for g in (card.get("genres") or []) if _text(g)][:4]),
        }

    def _video_list(self, cards):
        out = []
        for card in cards or []:
            if not isinstance(card, dict):
                continue
            item = self._video(card)
            if item and item["vod_id"]:
                out.append(item)
        return out

    def _filters(self):
        """从站点 bootstrap 里取分类筛选项。"""
        data = self._bootstrap_data()
        filters = {}
        catalogs = (data.get("catalog_ui") or {}).get("catalogs") or []
        for catalog in catalogs:
            kind = _text(catalog.get("kind"))
            if not kind:
                continue
            rows = []
            for row in catalog.get("rows") or []:
                key = _text(row.get("key"))
                options = row.get("options") or []
                if not key or not options:
                    continue
                values = [{"n": "全部", "v": ""}]
                for opt in options:
                    label = _text(opt.get("label"))
                    params = opt.get("params") or {}
                    if isinstance(params, dict) and params:
                        value = "&".join("%s=%s" % (k, v) for k, v in params.items()
                                         if _text(v))
                    else:
                        value = _text(opt.get("key"))
                    if label and value:
                        values.append({"n": label, "v": value})
                rows.append({"key": key, "name": _text(row.get("label")) or key, "value": values})
            if rows:
                filters[kind] = rows
        if not filters:
            for kind, _label in KIND_ORDER:
                filters[kind] = [
                    {"key": key, "name": {"genre": "分类", "area": "地区", "year": "年份",
                                          "sort": "排序"}[key],
                     "value": [{"n": "全部", "v": ""}] + [{"n": v, "v": v} for v in values]}
                    for key, values in FALLBACK_FILTERS.items()
                ]
        return filters

    def _bootstrap_data(self):
        """bootstrap 顺带充当「域名探活」：主域打不通就换镜像，之后全程用可用的那个。"""
        if self._bootstrap is None:
            self._bootstrap = {}
            hosts = [self.host]
            if not self._custom_host:
                hosts += [h for h in HOST_BACKUPS if h != self.host]
            for host in hosts:
                origin = self.host
                self.host = host
                data = self._api("/v1/runtime/bootstrap", timeout=12)
                if data:
                    self._bootstrap = data
                    return self._bootstrap
                self.host = origin
        return self._bootstrap

    def _feed_sections(self, sections=10, cards=20):
        data = self._api("/v1/feed/home", {
            "scope": "public", "mode": "page",
            "sections": str(sections), "cards": str(cards),
        }, timeout=25)
        return data.get("sections") or []

    # ------------------------------------------------------------------ 线路

    @staticmethod
    def _line_key(line):
        return (_text(line.get("playback_source_id")) or _text(line.get("id")),
                _text(line.get("provider_id")),
                _text(line.get("play_from")),
                _text(line.get("source_vod_id")))

    @staticmethod
    def _line_score(line):
        # 1080P 优先排前面：4K 线路基本是 HEVC，部分手机/盒子解码不了
        label = (_text(line.get("display_label")) or _text(line.get("label"))).upper()
        score = 0
        if "1080" in label:
            score += 60
        elif "高清" in label or "720" in label:
            score += 50
        elif "4K" in label or "2160" in label:
            score += 40
        if "蓝光" in label:
            score += 5
        if _text(line.get("url")).startswith("http"):
            score += 3
        if _text(line.get("minimum_user_level")) not in ("", "0"):
            score -= 20
        return score

    @staticmethod
    def _auto_rank(line):
        """自动线路的尝试优先级：1080P > 高清 > 4K > 其它。"""
        label = (_text(line.get("display_label")) or _text(line.get("label"))).upper()
        if "1080" in label:
            return 3
        if "高清" in label or "720" in label:
            return 2
        if "4K" in label or "2160" in label:
            return 1
        return 0

    @staticmethod
    def _probe_url(url):
        """在设备本机网络下探测直链是否可取到数据（过期域名常返回 403 或 HTML）。"""
        if not url:
            return False
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=3) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read(2048)
            if resp.getcode() != 200 or not data:
                return False
            if "text/html" in ctype or data[:1] == b"<":
                return False
            if "mpegurl" in ctype and not data.lstrip().startswith(b"#EXTM3U"):
                return False
            return True
        except Exception:
            return False

    def _line_meta(self, line):
        return {
            "l": _text(line.get("playback_source_id")) or _text(line.get("id")),
            "p": _text(line.get("provider_id")),
            "f": _text(line.get("play_from")),
            "s": _text(line.get("source_vod_id")),
        }

    def _line_options(self, token):
        """取一集的所有线路（带 5 分钟缓存）。带 line 提示的请求最快。"""
        now = time.time()
        cached = self._opt_cache.get(token)
        if cached and now - cached[0] < 300:
            return cached[1]
        options = []
        for hint in ("yjplayer", "yjapi", "yjm3u8", ""):
            probe = self._api("/v1/playback/resolve/" + urllib.parse.quote(token),
                              {"line": hint} if hint else None, timeout=8)
            options = probe.get("line_options") or []
            if options:
                break
        self._opt_cache[token] = (now, options)
        return options

    def _resolve_direct(self, token, line_meta, extra, timeout=25):
        """GET 取线路，必要时 POST resolve-line 换直链。"""
        if not token:
            return ""
        params = {"line": line_meta.get("l"), "provider_id": line_meta.get("p"),
                  "play_from": line_meta.get("f"), "source_vod_id": line_meta.get("s")}
        params.update(extra or {})
        params = {k: v for k, v in params.items() if v not in (None, "")}
        data = self._api("/v1/playback/resolve/" + urllib.parse.quote(token), params,
                         timeout=timeout)
        options = data.get("line_options") or []
        target = None
        for opt in options:
            if line_meta.get("l") and _text(opt.get("playback_source_id")) == line_meta.get("l"):
                target = opt
                break
            if (not line_meta.get("l") and line_meta.get("p")
                    and _text(opt.get("provider_id")) == line_meta.get("p")
                    and _text(opt.get("play_from")) == line_meta.get("f")):
                target = opt
                break
        if target is None and options:
            target = options[0]
        if not target:
            return ""
        url = _text(target.get("url"))
        if url.startswith("http"):
            return url
        ticket = _text(target.get("resolve_ticket"))
        if not ticket and url.startswith("resolve://"):
            ticket = url[10:].strip()
        return self._post_ticket(ticket, timeout)

    def _post_ticket(self, ticket, timeout=20):
        """把 resolve:// ticket 换成真正可播的直链。"""
        if not ticket:
            return ""
        code, text = self._open("POST", "/v1/playback/resolve-line",
                                params={"view": "compact"}, body={"ticket": ticket},
                                timeout=timeout)
        if code >= 400:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            return ""
        line = payload.get("line") or {}
        url = _text(line.get("url"))
        return url if url.startswith("http") else ""

    # ------------------------------------------------------- 线路实测（画质/速度）

    def _line_url(self, token, line, timeout=4):
        """线路 -> 直链。本身是 http 的直接用，省一次接口往返。"""
        raw = _text(line.get("url"))
        if raw.startswith("http"):
            return raw
        ticket = _text(line.get("resolve_ticket"))
        if not ticket and raw.startswith("resolve://"):
            ticket = raw[10:].strip()
        if ticket:
            url = self._post_ticket(ticket, timeout)
            if url:
                return url
        return self._resolve_direct(token, self._line_meta(line), {}, timeout)

    @staticmethod
    def _read_capped(resp, cap, deadline):
        """限时读：到 deadline 就收手。慢速 CDN 上 read() 会一直等，线程变僵尸拖慢下一次打开。"""
        chunks = []
        got = 0
        while got < cap:
            try:
                block = resp.read(min(16384, cap - got))
            except Exception:
                break
            if not block:
                break
            chunks.append(block)
            got += len(block)
            if time.time() > deadline:
                break
        return b"".join(chunks)

    def _get_text(self, url, budget=1.8, cap=65536):
        """读文本（m3u8 清单），返回 (正文, 状态码)。

        CDN 分片的证书经常配不齐，这里直接用不校验证书的 ctx 发一次请求就算，
        不再「先验证再重试」——那等于把每条死线的最长等待翻倍。
        """
        if self._loose_ctx is None:
            try:
                self._loose_ctx = ssl._create_unverified_context()
            except Exception:
                self._loose_ctx = self._ctx()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=budget, context=self._loose_ctx) as resp:
                data = self._read_capped(resp, cap, time.time() + max(budget * 2, 2.0))
                return data.decode("utf-8", "replace"), resp.getcode()
        except Exception:
            return "", 0

    def _probe_segment(self, url, budget=1.2, cap=131072):
        """取首个分片：返回 (KB/s, 该分片完整字节数)。"""
        if self._loose_ctx is None:
            try:
                self._loose_ctx = ssl._create_unverified_context()
            except Exception:
                self._loose_ctx = self._ctx()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": "bytes=0-%d" % (cap - 1)})
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=budget, context=self._loose_ctx) as resp:
                data = self._read_capped(resp, cap, started + max(budget * 1.5, 1.8))
                if not data:
                    return 0.0, 0
                total = 0
                content_range = resp.headers.get("Content-Range") or ""
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except Exception:
                        total = 0
                if not total:
                    try:
                        total = int(resp.headers.get("Content-Length") or 0)
                    except Exception:
                        total = 0
                span = max(time.time() - started, 0.001)
                return len(data) / 1024.0 / span, total
        except Exception:
            return 0.0, 0

    @staticmethod
    def _bitrate_height(kbps):
        """媒体清单没写 RESOLUTION 时，用码率（kbit/s）估个档位。"""
        if kbps >= 3000:
            return 1080
        if kbps >= 1500:
            return 720
        if kbps >= 700:
            return 480
        if kbps > 0:
            return 360
        return 0

    @staticmethod
    def _label_height(line):
        """线路名里自带的画质信息（1080P / 720P / 4K / 高清），比码率估算靠谱。"""
        label = (_text(line.get("display_label")) or _text(line.get("label"))).upper()
        if "4K" in label or "2160" in label:
            return 2160
        if "1080" in label or "蓝光" in label:
            return 1080
        if "720" in label or "高清" in label or "HD" in label:
            return 720
        if "540" in label:
            return 540
        return 0

    def _measure_line(self, token, line, speed=True, url=None):
        """实测一条线路：返回 {url, w, h, kbs, est, dead, seg, dur}。

        dead=True 表示这条线这次确实取不到流（解析失败 / 清单不是 m3u8 / 分片为空），
        detailContent 会把它们从线路列表里去掉——点了必黑屏的线不留着骗人。
        speed=False 时只读清单拿分辨率，分片地址留在 seg 里，交给第二阶段测速。
        url 已由线路清单给出时直接用，省掉一次 resolve 请求（站点对并发 resolve 会限流）。
        """
        out = {"url": "", "w": 0, "h": 0, "kbs": 0.0, "est": False,
               "dead": False, "seg": "", "dur": 0.0}
        label_h = self._label_height(line)
        url = _text(url)
        if url.startswith("resolve://"):
            url = self._post_ticket(url[10:].strip(), 4)
        elif not url.startswith("http"):
            url = self._line_url(token, line, timeout=3)
        if not url:
            out["dead"] = True
            return out
        out["url"] = url

        if ".m3u8" not in url.lower().split("?")[0] and ".mpd" not in url.lower():
            # mp4 等直链：拿不到分辨率，只有速度可测
            out["h"] = label_h
            if speed:
                kbs, total = self._probe_segment(url)
                out["kbs"] = kbs
                out["dead"] = not total
            else:
                out["seg"] = url
            return out

        text, code = self._get_text(url)
        if code != 200 or "#EXTM3U" not in text:
            out["dead"] = True
            return out

        width = height = 0
        for m in RES_RE.finditer(text):
            w, h = int(m.group(1)), int(m.group(2))
            if w * h > width * height:
                width, height = w, h

        base, body = url, text
        if "#EXT-X-STREAM-INF" in text:
            # 多档主清单：直接进像素面积最大的那一档，避免把主清单当媒体清单测
            best_area, best_uri = -1, ""
            for chunk in text.split("#EXT-X-STREAM-INF:")[1:]:
                head, _, rest = chunk.partition("\n")
                mr = RES_RE.search(head)
                area = int(mr.group(1)) * int(mr.group(2)) if mr else 0
                uri = ""
                for ln in rest.splitlines():
                    s = ln.strip()
                    if s and not s.startswith("#"):
                        uri = s
                        break
                if uri and area > best_area:
                    best_area, best_uri = area, uri
            if best_uri:
                sub = urllib.parse.urljoin(url, best_uri)
                sub_text, sub_code = self._get_text(sub)
                if sub_code == 200 and "#EXTM3U" in sub_text:
                    base, body = sub, sub_text

        duration = 0.0
        seg = ""
        for ln in body.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("#EXTINF:"):
                m = EXTINF_RE.search(s)
                if m:
                    try:
                        duration = float(m.group(1))
                    except Exception:
                        duration = 0.0
            elif not s.startswith("#"):
                seg = s
                break
        if not seg:
            out["dead"] = True
            return out

        out["seg"] = urllib.parse.urljoin(base, seg)
        out["dur"] = duration
        out["w"], out["h"] = width, height

        total = 0
        if speed:
            total = self._speed_fill(out)
            if not total:
                out["dead"] = True
                return out

        if not height:
            if label_h:
                # 线路名自带画质就采信它：码率估算对加密分片/短分片经常失真
                out["h"] = label_h
            else:
                # 媒体清单没有 RESOLUTION：分片「字节数 / 时长」就是真实码率，
                # 再映射到档位。估出来的值加 ~ 前缀，不冒充实测分辨率。
                bitrate = 0.0
                if total and duration > 0:
                    bitrate = total * 8 / duration / 1000.0
                if not bitrate:
                    m = BITRATE_RE.search(body)
                    if m:
                        try:
                            bitrate = float(m.group(1))
                        except Exception:
                            bitrate = 0.0
                est = self._bitrate_height(bitrate)
                if est:
                    out["h"], out["est"] = est, True
        return out

    def _speed_fill(self, res):
        """按 res['seg'] 测一次首片速度，写回 kbs；返回分片总字节数（0 = 取不到）。"""
        if not res or not res.get("seg"):
            return 0
        kbs, total = self._probe_segment(res["seg"])
        if total:
            res["kbs"] = kbs
        return total

    @staticmethod
    def _line_id(line):
        return _text(line.get("playback_source_id")) or _text(line.get("id"))

    def _probe_lines(self, token, lines, speed=True, urls=None):
        """并发实测，到点即收；返回 {线路 id: 实测结果}。

        缓存键必须带 token：同一个 playback_source_id 在各集都会出现，
        只按线路 id 缓存会让第 5 集拿到第 1 集的直链。
        urls 是「线路 id -> 清单里自带的直链」，能给就不再多打 resolve。
        """
        result = {}
        now = time.time()
        urls = urls or {}
        todo = []
        for line in lines:
            lid = self._line_id(line)
            cached = self._measure_cache.get("%s|%s" % (token, lid))
            if cached and cached[0] > now:
                result[lid] = cached[1]
                if not cached[1].get("dead") and (cached[1].get("h") or cached[1].get("kbs")):
                    self._quality_cache[lid] = cached[1]
            else:
                todo.append(line)
        if not todo:
            return result
        todo = todo[:PROBE_LIMIT]
        futures = {}
        pool = None
        try:
            pool = ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(todo)))
            for line in todo:
                futures[pool.submit(self._measure_line, token, line, speed,
                                    urls.get(self._line_id(line), ""))] = line
            try:
                for fut in as_completed(futures, timeout=PROBE_BUDGET):
                    line = futures[fut]
                    lid = self._line_id(line)
                    try:
                        res = fut.result()
                    except Exception:
                        continue
                    result[lid] = res
                    ttl = DEAD_TTL if res.get("dead") else MEASURE_TTL
                    self._measure_cache["%s|%s" % (token, lid)] = (time.time() + ttl, res)
                    if not res.get("dead") and (res.get("h") or res.get("kbs")):
                        self._quality_cache[lid] = res
            except Exception:
                # 到点了还有线没回来：已完成的照用，剩下的补到详情页列表尾部
                for fut in futures:
                    fut.cancel()
            # 没测完的记一个短期占位，免得重新打开详情又把同样的线重打一遍
            pending = {"url": "", "w": 0, "h": 0, "kbs": 0.0,
                       "est": False, "dead": False, "skip": True}
            for fut, line in futures.items():
                lid = self._line_id(line)
                if lid not in result:
                    self._measure_cache["%s|%s" % (token, lid)] = (
                        time.time() + PENDING_TTL, pending)
        except Exception:
            pass
        finally:
            if pool is not None:
                # 不能 wait：没测完的线各自有 2~3 秒超时，等它们会把详情页拖到十几秒
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
        return result

    @staticmethod
    def _line_area(line, res):
        """排序用的画质分：优先像素面积；只有估出来的档位就打个折。"""
        if not res:
            label = (_text(line.get("display_label")) or _text(line.get("label"))).upper()
            if "4K" in label or "2160" in label:
                h, est = 2160, True
            elif "1080" in label:
                h, est = 1080, True
            elif "720" in label or "高清" in label:
                h, est = 720, True
            else:
                h, est = 0, True
            area = h * h * 16 // 9 if h else 0
            return area // 3
        if res.get("w") and res.get("h"):
            return res["w"] * res["h"]
        h = res.get("h") or 0
        area = h * h * 16 // 9
        return area * 4 // 5 if res.get("est") else area

    # 线路名保持站点原名，不再拼「分辨率 / 速度」后缀（实测值只用于排序）。


    # ------------------------------------------------------------------ 首页

    def homeContent(self, filter=False):
        classes = [{"type_id": "home", "type_name": "推荐"}]
        for kind, label in KIND_ORDER:
            classes.append({"type_id": kind, "type_name": label})
        try:
            filters = self._filters()
        except Exception:
            filters = {}
        # 分类列表是壳渲染左侧栏的唯一依据，任何异常都不能让它变空
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        sections = self._feed_sections(sections=4, cards=20)
        cards = []
        seen = set()
        for section in sections:
            for card in section.get("cards") or []:
                vid = _text(card.get("id"))
                if vid and vid not in seen:
                    seen.add(vid)
                    cards.append(card)
        return {"list": self._video_list(cards)}

    # ------------------------------------------------------------------ 分类

    def categoryContent(self, tid, pg, filter=False, extend=None):
        if isinstance(extend, str):
            try:
                extend = json.loads(extend) if extend.strip().startswith("{") else {}
            except Exception:
                extend = {}
        extend = extend or {}
        page = int(pg) if str(pg).isdigit() else 1
        if tid == "home":
            cards = self.homeVideoContent().get("list") or []
            return {"list": cards, "page": page, "pagecount": 1,
                    "limit": len(cards), "total": len(cards)}
        params = {"kind": tid, "page": str(page), "limit": "20"}
        for key in ("genre", "area", "language", "year", "sort", "status", "orientation"):
            value = _text(extend.get(key))
            if not value:
                continue
            if "=" in value:
                # 筛选项里存的是接口真实参数，如 area=中国大陆、year_lte=2024
                for pair in value.split("&"):
                    field, _, val = pair.partition("=")
                    if _text(field) and _text(val):
                        params[field.strip()] = val.strip()
            else:
                params[key] = value
        data = self._api("/v1/browse/catalog", params, timeout=25)
        cards = data.get("cards") or []
        pagination = data.get("pagination") or {}
        total = pagination.get("total") or len(cards)
        has_more = bool(pagination.get("has_more"))
        return {
            "list": self._video_list(cards),
            "page": page,
            "pagecount": page + 1 if has_more else page,
            "limit": len(cards),
            "total": total,
        }

    # ------------------------------------------------------------------ 搜索

    def searchContent(self, key, quick=False, pg="1"):
        page = int(pg) if str(pg).isdigit() else 1
        data = self._api("/v1/browse/catalog",
                         {"q": key, "page": str(page), "limit": "20"}, timeout=30)
        cards = data.get("cards") or []
        pagination = data.get("pagination") or {}
        result = self._video_list(cards)
        if page > 1:
            return {"list": result, "page": page, "pagecount": page + 1,
                    "limit": len(result), "total": pagination.get("total") or len(result)}
        return {"list": result}

    # ------------------------------------------------------------------ 详情

    def _episodes(self, cid, detail):
        episodes = list(detail.get("episodes") or [])
        pagination = detail.get("episode_pagination") or {}
        offset = len(episodes)
        guard = 0
        while pagination.get("has_more") and guard < 20:
            guard += 1
            data = self._api("/v1/catalog/%s/episodes" % urllib.parse.quote(cid),
                             {"offset": str(offset), "limit": "48"}, timeout=25)
            batch = data.get("episodes") or []
            if not batch:
                break
            episodes.extend(batch)
            offset += len(batch)
            pagination = data.get("episode_pagination") or {}
        return [e for e in episodes if _text(e.get("token"))]

    def detailContent(self, ids):
        vid = ids[0] if isinstance(ids, (list, tuple)) else ids
        vid = _text(vid)
        if not vid:
            return {"list": []}
        detail = self._api("/v1/catalog/" + urllib.parse.quote(vid), timeout=25)
        if not detail:
            return {"list": []}
        episodes = self._episodes(vid, detail)
        if not episodes:
            return {"list": []}

        # 用首集探测线路清单。带一个轻量 line 提示可让服务端跳过全量探测，
        # 无提示时部分影片要等 20 秒以上。清单里已带各线路直链，实测不必再逐条 resolve。
        token0 = _text(episodes[0].get("token"))
        raw_lines = self._line_options(token0) if token0 else []

        lines = []
        seen = set()
        for line in raw_lines:
            key = self._line_key(line)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            lines.append(line)
        lines.sort(key=self._line_score, reverse=True)
        # 超长剧集只保留画质最高的一批线路，避免播放列表过大拖慢客户端
        if len(lines) > 6 and len(episodes) > 600:
            lines = lines[:5]
        elif len(lines) > 8 and len(episodes) > 200:
            lines = lines[:8]

        # 逐条实测：能出流的留下并按「像素面积 → 速度」排序，实测确认取不到流的剔掉。
        # 站点下发的线路里常年混着一批域名已废的，点了就是黑屏。
        urls = {}
        for line in lines:
            url = _text(line.get("url"))
            if url.startswith("http") or url.startswith("resolve://"):
                urls[self._line_id(line)] = url
        measured = self._probe_lines(token0, lines[:PROBE_LIMIT], urls=urls) if lines else {}
        alive, unmeasured = [], []
        for line in lines:
            key = _text(line.get("playback_source_id")) or _text(line.get("id"))
            res = measured.get(key)
            if res is None or res.get("skip"):
                unmeasured.append(line)          # 超时没测到：排最后，但不删
            elif res.get("dead"):
                continue                         # 实测死线：直接不铺
            elif not res.get("h") and not res.get("kbs"):
                unmeasured.append(line)          # 画质和速度都没测出来：也算没测
            else:
                alive.append((line, res))
        alive.sort(key=lambda pair: (self._line_area(pair[0], pair[1]),
                                     pair[1].get("kbs") or 0), reverse=True)
        unmeasured = unmeasured[:SHOW_UNTESTED]
        if alive or unmeasured:
            ordered = [pair[0] for pair in alive] + unmeasured
        else:
            # 一条都没测通（多半是接口/网络抖动而非全站瘫）：原样铺出来，别让线路区空着
            ordered = lines

        episode_count = len(episodes)
        names = []
        for idx, episode in enumerate(episodes):
            name = _text(episode.get("title")) or _text(episode.get("display_name"))
            if not name:
                number = episode.get("number")
                name = ("第%s集" % number) if number else ("第%d集" % (idx + 1))
            names.append(name)

        play_from = []
        play_url = []

        auto_eps = []
        for idx, episode in enumerate(episodes):
            auto_eps.append("%s$%s" % (names[idx], _b64e({
                "t": _text(episode.get("token")),
                "n": episode.get("number") or (idx + 1),
            })))
        play_from.append("自动")
        play_url.append("#".join(auto_eps))

        for line in ordered:
            meta = self._line_meta(line)
            label = _text(line.get("display_label")) or _text(line.get("label")) or "线路"
            if label in play_from:
                label = "%s-%d" % (label, len(play_from))
            entries = []
            for idx, episode in enumerate(episodes):
                entries.append("%s$%s" % (names[idx], _b64e({
                    "t": _text(episode.get("token")),
                    "n": episode.get("number") or (idx + 1),
                    "l": meta.get("l"),
                })))
            play_from.append(label)
            play_url.append("#".join(entries))

        item = self._video(detail) or {}
        item.update({
            "vod_id": vid,
            "vod_name": _text(detail.get("title")) or item.get("vod_name", ""),
            "vod_pic": _text(detail.get("poster_url")) or item.get("vod_pic", ""),
            "vod_year": _text(detail.get("year")),
            "vod_area": _text(detail.get("area")),
            "vod_lang": _text(detail.get("language")),
            "vod_remarks": _text(detail.get("episode_progress_text")) or _text(detail.get("remarks")),
            "vod_actor": _join(detail.get("actors")),
            "vod_director": _join(detail.get("directors")),
            "vod_content": _text(detail.get("description")) or item.get("vod_content", ""),
            "type_name": ",".join([g for g in (detail.get("genres") or []) if _text(g)][:4]),
            "vod_play_from": "$$$".join(play_from),
            "vod_play_url": "$$$".join(play_url),
        })
        return {"list": [item]}

    # ------------------------------------------------------------------ 播放

    def playerContent(self, flag, id, vipFlags=None):
        payload = {}
        try:
            payload = _b64d(_text(id))
        except Exception:
            return {"parse": 0, "url": "", "header": {"User-Agent": UA}}
        token = _text(payload.get("t"))
        if not token:
            return {"parse": 0, "url": "", "header": {"User-Agent": UA}}
        header = {"User-Agent": UA}
        options = self._line_options(token)

        line_id = _text(payload.get("l"))
        if line_id:
            for opt in options:
                if _text(opt.get("playback_source_id")) != line_id:
                    continue
                url = self._line_url(token, opt, timeout=8)
                if url:
                    return {"parse": 0, "url": url, "header": header}
                break
            # 选中的线路取不到地址（官方线后几集常要登录）：退到自动，别让用户黑屏
        return self._auto_play(token, options, header)

    def _auto_play(self, token, options, header):
        """自动选线：按实测画质/速度排序后逐条取址，取不到就换下一条。"""
        candidates = [o for o in options
                      if _text(o.get("url")).startswith("http")
                      or _text(o.get("url")).startswith("resolve://")
                      or _text(o.get("url_kind")) == "resolve_ticket"]
        if not candidates:
            return {"parse": 0, "url": "", "header": header}

        def _rank(opt):
            quality = self._quality_cache.get(self._line_id(opt))
            if quality:
                return (self._line_area(opt, quality), quality.get("kbs") or 0)
            return (self._line_area(opt, None), 0)

        # 实测过的线路按画质→速度排前面；没测过的按标签猜（1080>高清>4K>其它）
        order = sorted(candidates, key=lambda o: (_rank(o), self._auto_rank(o),
                                                  self._line_score(o)), reverse=True)
        now = time.time()
        for line in order[:3]:
            # 本集刚实测过：地址就是这一集的，直接播
            hit = self._measure_cache.get("%s|%s" % (token, self._line_id(line)))
            if hit and hit[0] > now and hit[1].get("url"):
                return {"parse": 0, "url": hit[1]["url"], "header": header}

        tried = {}
        for line in order[:6]:
            lid = self._line_id(line)
            url = self._line_url(token, line, timeout=6)
            tried[lid] = url
            if url and self._probe_url(url):
                return {"parse": 0, "url": url, "header": header}
        # 探测全失败：至少给一条能取到地址的，交给播放器碰运气
        for line in order[:6]:
            url = tried.get(self._line_id(line))
            if url:
                return {"parse": 0, "url": url, "header": header}
        return {"parse": 0, "url": "", "header": header}
