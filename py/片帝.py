# -*- coding: utf-8 -*-
"""
@header({
  searchable: 1,
  filterable: 1,
  quickSearch: 1,
  title: 'mock',
  lang: 'hipy',
})
dongpian1.com TVBox Spider
- 签名: HMAC-SHA256, KEY 从 movie-card-runtime-DSGpZH3f.js 提取
- 详情页真实线路名：并发实测每条线路（换票 → 取清单 → 抓首片），
  实测取不到流的直接不铺，其余按「4K → 1080P → 720P → 其它」再按码率排，
  所以站点下发的死线不会再出现在列表里
- 播放时若选中的线路取不到地址，自动按实测顺序换一条（最多试 4 条）
- 官方/4K 直连；普通 m3u8 走本地代理清洗广告分片
"""
import re
import json
import hmac
import hashlib
import time
import secrets
import uuid
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote, urljoin, urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

try:
    from base.spider import Spider as BaseSpider
    RUN_IN_TVBOX = True
except ImportError:
    RUN_IN_TVBOX = False
    class BaseSpider:
        def init(self, extend=""): pass
        def getName(self): return "mock"


# ==================== HLS 清洗 ====================
def clean_m3u8(text, ad_keywords, min_duration=0.5):
    try:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "#EXTM3U":
            return text
        if "#EXT-X-MAP:" in text:
            return text

        header_lines, segments = [], []
        cur = {"tags": [], "uri": "", "dur": 0.0}
        in_seg = False
        orig_seq = 0
        has_endlist = False

        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#EXTINF:"):
                in_seg = True
                m = re.search(r"#EXTINF:([0-9.]+)", s)
                cur["dur"] = float(m.group(1)) if m else 3.0
                cur["tags"].append(line)
            elif s.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", s)
                if m:
                    orig_seq = int(m.group(1))
            elif s == "#EXT-X-ENDLIST":
                has_endlist = True
            elif s.startswith("#"):
                if in_seg:
                    cur["tags"].append(line)
                else:
                    header_lines.append(line)
            else:
                if in_seg:
                    cur["uri"] = s
                    segments.append(cur)
                    cur = {"tags": [], "uri": "", "dur": 0.0}
                    in_seg = False
                else:
                    header_lines.append(line)

        if not segments:
            return text

        cleaned, removed = [], 0
        for seg in segments:
            uri_low = seg["uri"].lower()
            is_ad = any(k in uri_low for k in ad_keywords)
            if 0 < seg["dur"] < min_duration:
                is_ad = True
            if is_ad:
                removed += 1
                continue
            cleaned.extend(seg["tags"])
            cleaned.append(seg["uri"])

        seg_count = sum(1 for x in cleaned if not x.strip().startswith("#"))
        if seg_count < 3:
            return text

        out = ["#EXTM3U"]
        out.extend(l for l in header_lines if not l.strip().startswith("#EXTM3U"))
        if removed > 0:
            out.append(f"#EXT-X-MEDIA-SEQUENCE:{orig_seq + removed}")
        out.extend(cleaned)
        if has_endlist:
            out.append("#EXT-X-ENDLIST")
        return "\n".join(out)
    except Exception:
        return text


def absolutize_m3u8(text, base_url, proxy_port=0):
    try:
        p = urlparse(base_url)
        base_dir = p.path.rsplit("/", 1)[0] + "/"
        base = f"{p.scheme}://{p.netloc}{base_dir}"
        out = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                out.append(line)
                continue
            if s.startswith("#EXT-X-KEY:") or s.startswith("#EXT-X-MAP:"):
                def repl(m):
                    uri = m.group(1)
                    abs_uri = uri if uri.startswith("http") else urljoin(base, uri)
                    if proxy_port > 0:
                        return f'URI="http://127.0.0.1:{proxy_port}/?url={quote(abs_uri, safe="")}"'
                    return f'URI="{abs_uri}"'
                out.append(re.sub(r'URI="([^"]+)"', repl, s))
            elif s.startswith("#"):
                out.append(line)
            else:
                out.append(urljoin(base, s))
        return "\n".join(out)
    except Exception:
        return text


# ==================== HLS 代理 ====================
class HLSProxyServer:
    _server = None
    _port = 0
    _lock = threading.Lock()
    _session = None
    _session_lock = threading.Lock()
    _cache = {}
    _cache_lock = threading.Lock()
    _cache_ttl = 8

    class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    @classmethod
    def _get_session(cls, ua, referer):
        if cls._session is None:
            with cls._session_lock:
                if cls._session is None:
                    s = requests.Session()
                    s.headers.update({
                        "User-Agent": ua,
                        "Accept": "*/*",
                        **({"Referer": referer} if referer else {}),
                    })
                    cls._session = s
        return cls._session

    @classmethod
    def _fetch(cls, url, ua, referer):
        with cls._cache_lock:
            hit = cls._cache.get(url)
            if hit and time.time() - hit[0] < cls._cache_ttl:
                return hit[1]
        try:
            r = cls._get_session(ua, referer).get(url, timeout=6, verify=False)
            text = r.content.decode("utf-8", "replace")
            with cls._cache_lock:
                cls._cache[url] = (time.time(), text)
                if len(cls._cache) > 100:
                    items = sorted(cls._cache.items(), key=lambda x: x[1][0])
                    for k, _ in items[:50]:
                        cls._cache.pop(k, None)
            return text
        except Exception:
            return None

    @classmethod
    def start(cls, config):
        with cls._lock:
            if cls._server is not None:
                return cls._port
            for port in range(9979, 9999):
                try:
                    handler = cls._make_handler(config)
                    srv = cls._ThreadingHTTPServer(("127.0.0.1", port), handler)
                    threading.Thread(target=srv.serve_forever, daemon=True).start()
                    cls._server = srv
                    cls._port = port
                    return port
                except OSError:
                    continue
                except Exception:
                    continue
            return 0

    @classmethod
    def stop(cls):
        with cls._lock:
            if cls._server is not None:
                try:
                    cls._server.shutdown()
                except Exception:
                    pass
                cls._server = None
                cls._port = 0

    @classmethod
    def _make_handler(cls, config):
        ad_keywords = config.get("ad_keywords", [])
        min_duration = config.get("min_duration", 0.5)
        default_ua = config.get("ua", "Mozilla/5.0")
        default_ref = config.get("referer", "")

        class Handler(BaseHTTPRequestHandler):
            def _rewrite_master(self, text, base_url):
                port = HLSProxyServer._port
                if port <= 0:
                    return text
                out = []
                for line in text.splitlines():
                    s = line.strip()
                    if not s or s.startswith("#"):
                        out.append(line)
                        continue
                    absolute = urljoin(base_url, s)
                    if ".m3u8" in absolute.lower():
                        out.append(f"http://127.0.0.1:{port}/?url={quote(absolute, safe='')}")
                    else:
                        out.append(absolute)
                return "\n".join(out)

            def _do_fetch(self):
                target = ""
                try:
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    target = params.get("url", [""])[0]
                    if not target:
                        self.send_response(400)
                        self.end_headers()
                        return

                    is_m3u8 = "m3u8" in target.lower()

                    if not is_m3u8:
                        try:
                            r = requests.get(target, headers={
                                "User-Agent": default_ua,
                                "Referer": default_ref,
                            }, timeout=10, stream=True, verify=False)
                            self.send_response(r.status_code)
                            for k, v in r.headers.items():
                                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                                    self.send_header(k, v)
                            self.end_headers()
                            for chunk in r.iter_content(8192):
                                self.wfile.write(chunk)
                            return
                        except Exception:
                            self.send_response(502)
                            self.end_headers()
                            return

                    text = HLSProxyServer._fetch(target, default_ua, default_ref)
                    if text is None:
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.end_headers()
                        return

                    if "#EXT-X-STREAM-INF" in text:
                        text = self._rewrite_master(text, target)
                    else:
                        text = clean_m3u8(text, ad_keywords, min_duration)
                        text = absolutize_m3u8(text, target, HLSProxyServer._port)

                    body = text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=6")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except Exception:
                    try:
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.end_headers()
                    except Exception:
                        pass

            def do_GET(self):
                self._do_fetch()

            def do_HEAD(self):
                self._do_fetch()

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def log_message(self, *args):
                pass

        return Handler


# ==================== Spider ====================
class Spider(BaseSpider):
    host = "https://dongpian1.com"
    SIGN_KEY = "8b9a908a05eac640e1ee06f52acaa741bfe4ba9e004eeffdbeb635e532e06666"
    PAGE_SIZE = 20

    ENABLE_HLS_CLEAN = True

    # ---- 线路实测参数 ----
    PROBE_WORKERS = 24      # 并发路数（纯网络等待，不占 CPU）
    PROBE_BUDGET = 4.0      # 整轮实测限时（秒），到点就先出结果
    PROBE_LIMIT = 22        # 最多实测多少条（站点常见 20~35 条，前 22 条按站点权重取）
    SHOW_UNMEASURED = 6     # 到点还没测完的线路，最多再补几条进列表
    LINE_CAP = 20           # 线路列表上限（站点常下发 30+ 条，铺满反而难挑）
    LINE_TTL = 240          # 直链缓存（签名地址有时效，别存太久）
    META_TTL = 300          # 详情 / 线路清单缓存
    DEAD_TTL = 300          # 死线记忆时长（同片子不再重复试）

    KIND_MAP = {
        "movie": "movie", "series": "series",
        "anime": "anime", "variety": "variety", "short": "short",
    }

    PLAY_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
        "Origin": "https://dongpian1.com",
        "Referer": "https://dongpian1.com/",
    }

    def init(self, extend=""):
        self.proxy_config = {
            "ad_keywords": [
                "advert", "preroll", "片头", "广告",
                "/gg/", "banner", "promo", "casino", "博彩",
                "充值", "sponsor", "/ad/", "/ads/", "ad.mp4", "ad.m3u8",
            ],
            "min_duration": 0.5,
            "ua": self.PLAY_HEADERS["User-Agent"],
            "referer": self.PLAY_HEADERS["Referer"],
        }
        # ---- 缓存（壳可能并发调用，统一加锁）----
        self._lock = threading.Lock()
        self._meta = {}          # vod_id -> (ts, catalog json)
        self._lines = {}         # token -> (ts, line_options)
        self._urls = {}          # (token, line_id) -> (ts, 直链)
        self._probe = {}         # (token, line_id) -> (ts, 实测结果)
        self._dead = {}          # (vod_id, line_id) -> ts，播放时踩过雷的线路
        self._skip = {}          # (token, line_id) -> 上次没测完，两分钟内不再重复测
        self._miss = {}          # (token, line_id) -> 连不上的次数，两次以上当死线
        self._order = {}         # vod_id -> (ts, [line_id...]) 实测后的线路顺序
        self._tls = threading.local()

    # ---- 桥接口（OK影视/影视仓缺一个就 init 中断，分类会全空）----
    def getName(self):
        return "懂片帝4K"

    def getDependence(self):
        return []

    def isVideoFormat(self, url):
        u = str(url or "").lower()
        return any(x in u for x in (".m3u8", ".mp4", ".ts", ".mkv", ".flv"))

    def manualVideoCheck(self):
        return False

    def liveContent(self, url=None):
        return {"class": [], "list": []}

    def action(self, action):
        return None

    def localProxy(self, param):
        return None

    def destroy(self):
        try:
            HLSProxyServer.stop()
        except Exception:
            pass

    # ========== 缓存小工具 ==========
    def _cache_get(self, store, key, ttl):
        with self._lock:
            hit = store.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def _cache_put(self, store, key, value, keep=200):
        with self._lock:
            store[key] = (time.time(), value)
            if len(store) > keep:
                items = sorted(store.items(), key=lambda x: x[1][0])
                for k, _ in items[:keep // 2]:
                    store.pop(k, None)

    def _catalog(self, vod_id):
        data = self._cache_get(self._meta, vod_id, self.META_TTL)
        if data is None:
            data = self._get_json("/v1/catalog/%s" % vod_id, tag="catalog")
            if data:
                self._cache_put(self._meta, vod_id, data)
        return data or {}

    def _line_options(self, token, tag="resolve"):
        opts = self._cache_get(self._lines, token, self.META_TTL)
        if opts is None:
            data = self._get_json("/v1/playback/resolve/%s" % token, tag=tag)
            opts = (data or {}).get("line_options") or []
            if opts:
                self._cache_put(self._lines, token, opts, keep=120)
        return opts or []

    def _resolve_ticket(self, ticket, retries=0):
        data = self._post_json("/v1/playback/resolve-line", "?view=compact",
                              {"ticket": ticket}, tag="resolve-line",
                              timeout=6, retries=retries)
        return ((data or {}).get("line") or {}).get("url", "") or ""

    def _sess(self):
        s = getattr(self._tls, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self.PLAY_HEADERS)
            self._tls.s = s
        return s

    def _get_head(self, url, budget=2.2, cap=196608):
        """读前 cap 字节：返回 (data, status, 耗时)。CDN 证书常配不齐，一律不校验。

        连接超时压到 1.2 秒：域名已废的线路多半是「连不上」，让它快点判死，
        比在详情页里干等 3 秒划算。域名解析失败（DNS 都没了）用 -1 标记，
        这种是确定死线，不用等第二次。
        """
        t0 = time.time()
        try:
            r = self._sess().get(url, timeout=(1.2, budget), stream=True, verify=False)
            data = b""
            for chunk in r.iter_content(32768):
                data += chunk
                if len(data) >= cap:
                    break
            try:
                r.close()
            except Exception:
                pass
            return data, r.status_code, time.time() - t0
        except Exception as e:
            txt = str(e)
            dns = ("NameResolution" in txt or "getaddrinfo" in txt
                   or "Name or service not known" in txt
                   or "nodename nor servname" in txt)
            return b"", (-1 if dns else 0), time.time() - t0

    # ========== 签名 ==========
    def _sign(self, method, api, search_or_data=""):
        ts = str(int(time.time() * 1000))
        n = secrets.token_hex(16)
        msg = f"{method.upper()}\n{api}{search_or_data}\n{ts}\n{n}"
        sig = hmac.new(self.SIGN_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "x-ai-movie-timestamp": ts,
            "x-ai-movie-nonce": n,
            "x-ai-movie-signature": sig,
        }

    def _get_json(self, path, params=None, tag="", timeout=5, retries=1):
        query_string = ""
        if params:
            query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        url = self.host + path + query_string
        last = ""
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.3)
            headers = self._sign("GET", path, query_string)   # 重试必须重签，时间戳会过期
            tmo = timeout if attempt == 0 else 3.0
            try:
                r = requests.get(url, headers=headers, timeout=tmo, verify=False)
            except Exception as e:
                last = "EXC %s" % type(e).__name__
                continue
            if r.status_code != 200:
                last = "%s: %s" % (r.status_code, r.text[:120])
                break                     # 4xx/5xx 重试没意义
            try:
                return r.json()
            except Exception:
                last = "非 JSON"
                continue
        if tag:
            print(f"[{tag}] GET {path} -> {last}")
        return None

    def _post_json(self, path, query_string, body_dict, tag="", timeout=6, retries=1):
        body_str = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":"))
        url = self.host + path + query_string
        last = ""
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.4)
            headers = self._sign("POST", path, query_string)
            headers["Content-Type"] = "application/json"
            try:
                r = requests.post(url, headers=headers,
                                  data=body_str.encode("utf-8"),
                                  timeout=timeout, verify=False)
            except Exception as e:
                last = "EXC %s" % type(e).__name__
                continue
            if r.status_code not in (200, 201):
                last = "%s: %s" % (r.status_code, r.text[:120])
                break
            try:
                return r.json()
            except Exception:
                last = "非 JSON"
                continue
        if tag:
            print(f"[{tag}] POST {path} -> {last}")
        return None

    # ========== 筛选 ==========
    @staticmethod
    def _sort_options():
        return [
            {"n": "热度", "v": "trending"},
            {"n": "最近更新", "v": "new"},
            {"n": "评分", "v": "score"},
            {"n": "上映时间", "v": "time"},
        ]

    @staticmethod
    def _window_options():
        return [
            {"n": "日榜", "v": "day"},
            {"n": "周榜", "v": "week"},
            {"n": "月榜", "v": "month"},
            {"n": "全部", "v": "all"},
        ]

    def _build_filter_objects(self, tid):
        return [
            {"key": "sort", "name": "排序", "value": self._sort_options()},
            {"key": "window", "name": "榜单", "value": self._window_options()},
        ]

    def _build_filters_dict(self, tid):
        return {
            "sort": self._sort_options(),
            "window": self._window_options(),
        }

    # ========== 首页 ==========
    def homeContent(self, filter=False):
        classes = [
            {"type_id": "movie", "type_name": "电影"},
            {"type_id": "series", "type_name": "电视剧"},
            {"type_id": "anime", "type_name": "动漫"},
            {"type_id": "variety", "type_name": "综艺"},
            {"type_id": "short", "type_name": "短剧"},
        ]
        filters = {c["type_id"]: self._build_filter_objects(c["type_id"]) for c in classes}
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        data = self._get_json("/v1/feed/home", {
            "scope": "public", "mode": "preview",
            "sections": 3, "cards": 10,
        }, tag="homeVideoContent")
        if not data:
            return {"list": []}
        videos = []
        for sec in data.get("sections", []):
            for card in sec.get("cards", []):
                videos.append(self._card_to_vod(card))
        return {"list": videos[:50]}

    # ========== 分类 ==========
    def categoryContent(self, tid="movie", pg="1", filter=False, extend=None):
        kind = self.KIND_MAP.get(str(tid), "movie")
        try:
            pg_num = int(pg or 1)
        except Exception:
            pg_num = 1
        ext = self._safe_dict(extend)

        sort = str(ext.get("sort") or "trending")
        window = str(ext.get("window") or "day")

        data = self._get_json("/v1/browse/catalog", {
            "sort": sort, "window": window,
            "page": pg_num, "limit": 20, "kind": kind,
        }, tag="categoryContent")
        if not data:
            return {"list": [], "page": pg_num, "pagecount": 1,
                    "limit": 20, "total": 0,
                    "filters": self._build_filters_dict(tid)}

        videos = [self._card_to_vod(c) for c in data.get("cards", [])]
        pagination = data.get("pagination") or {}
        has_more = pagination.get("has_more", False)
        pagecount = min(pg_num + 1, 999) if has_more else pg_num

        return {
            "list": videos, "page": pg_num, "pagecount": pagecount,
            "limit": 20, "total": pagecount * 20,
            "filters": self._build_filters_dict(tid),
        }

    # ========== 搜索 ==========
    def searchContent(self, key="", quick=False, pg="1"):
        if not key:
            return {"list": []}

        thread_body = {
            "title": key,
            "metadata": {
                "search_fields": "all",
                "search_scope_label": "综合",
                "search_mode": "fast",
                "source": "movie_composer_route",
                "submission_id": str(uuid.uuid4()),
            }
        }
        thread = self._post_json("/v1/threads", "", thread_body,
                                 tag="searchContent/thread")
        thread_id = (thread or {}).get("id", "")
        if not thread_id:
            return {"list": []}

        try:
            pg_num = int(pg or 1)
        except Exception:
            pg_num = 1

        # 站点的搜索是「先建会话、再查索引」，建会话后要等后端把索引做好。
        # 0.7 秒够用（实测与 1.5 秒结果一致），查空再补等 0.9 秒重查一次。
        raw = []
        for wait in (0.7, 0.9):
            time.sleep(wait)
            data = self._get_json("/v1/browse/catalog", {
                "page": pg_num, "limit": 20,
                "query_mode": "fast_v3",
                "thread_id": thread_id,
                "search_fields": "all",
                "q": quote(key),
            }, tag="searchContent/query")
            if not data:
                continue
            raw = data.get("cards") or data.get("results") or data.get("items") or []
            if raw:
                break
        if not raw:
            return {"list": []}
        videos = [self._card_to_vod(c) for c in raw]
        return {"list": videos, "page": pg_num}

    # ========== 线路实测 ==========
    @staticmethod
    def _pick_variant(text, base):
        """master 清单里挑像素面积（其次码率）最高的一路，返回 (地址, 宽, 高, 码率KB/s)。"""
        rows = text.splitlines()
        best = None
        for i, ln in enumerate(rows):
            if not ln.startswith("#EXT-X-STREAM-INF"):
                continue
            uri = ""
            for nxt in rows[i + 1:]:
                s = nxt.strip()
                if s and not s.startswith("#"):
                    uri = urljoin(base, s)
                    break
            if not uri:
                continue
            mm = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            bw = re.search(r"BANDWIDTH=(\d+)", ln)
            w, h = (int(mm.group(1)), int(mm.group(2))) if mm else (0, 0)
            kbp = int(bw.group(1)) / 8192.0 if bw else 0.0
            key = (w * h, kbp)
            if best is None or key > best[0]:
                best = (key, uri, w, h, kbp)
        if not best:
            return "", 0, 0, 0.0
        return best[1], best[2], best[3], best[4]

    def _probe_line(self, opt):
        """实测一条线路：换票 → 取清单 →（必要时）抓首片。

        只用 1~2 个请求：master 清单里已经写了 BANDWIDTH/RESOLUTION 就直接采信，
        不再去抓子清单和分片——33 条线路全量抓分片会把详情页拖到十几秒。

        返回 {url, w, h, kbs, alive, certain, mp4, est}；certain=True 表示这次是
        「确定取不到」（换票被拒 / HTTP 非 200 / 拿到的不是视频），
        不确定的（超时、连不上）不当死线处理，免得一次网络抖动把好线路全删了。
        """
        res = {"url": "", "w": 0, "h": 0, "kbs": 0.0, "est": False,
               "alive": False, "certain": False, "mp4": False}
        url = opt.get("url") or ""
        kind = opt.get("url_kind") or ""

        if kind == "resolve_ticket" or url.startswith("resolve://"):
            url = self._resolve_ticket(url.replace("resolve://", ""))
            if not url:
                res["certain"] = True         # 401/login_required 之类，换票直接被拒
                return res
        if not url.startswith("http"):
            res["certain"] = True
            return res
        res["url"] = url

        data, code, cost = self._get_head(url, 2.2, 65536)
        if code != 200 or not data:
            res["certain"] = code not in (0, 200)
            return res

        if b"#EXTM3U" in data[:400]:
            text = data.decode("utf-8", "replace")
            if "#EXT-X-STREAM-INF" in text:
                sub, w, h, kbp = self._pick_variant(text, url)
                res["w"], res["h"] = w, h
                if not sub:
                    res["certain"] = True
                    return res
                # 子清单必须真的能取到：有些 CDN 的 m3u8 被服务端转码搞坏了
                # （内容全是 U+FFFD），master 正常、子清单是垃圾，播放器必黑屏。
                data2, code2, _ = self._get_head(sub, 2.0, 262144)
                if code2 != 200 or b"#EXTM3U" not in data2[:400]:
                    res["certain"] = code2 not in (0, 200)
                    return res
                subtext = data2.decode("utf-8", "replace")
                if not [l for l in subtext.splitlines()
                        if l.strip() and not l.strip().startswith("#")]:
                    res["certain"] = True
                    return res
                res["url"] = sub          # 直接用子清单，播放时少一次跳转
                res["kbs"] = kbp
                res["est"] = kbp > 0
                res["alive"] = True
                return res
            segs = [l.strip() for l in text.splitlines()
                    if l.strip() and not l.strip().startswith("#")]
            if not segs:
                res["certain"] = True
                return res
            seg = urljoin(url, segs[0])
            sdata, scode, scost = self._get_head(seg, 2.3, 131072)
            if scode not in (200, 206) or not sdata:
                res["certain"] = scode not in (0, 200, 206)
                return res
            res["kbs"] = len(sdata) / 1024.0 / max(scost, 0.01)
            res["alive"] = True
            return res

        # 不是 m3u8：官方线常直接给 mp4（byteimg / volc）
        if b"ftyp" in data[4:16] or b"ftyp" in data[:64]:
            res["mp4"] = True
            res["kbs"] = len(data) / 1024.0 / max(cost, 0.01)
            res["alive"] = True
            return res

        res["certain"] = True             # 拿回来是网页/报错页
        return res

    def _probe_lines(self, vod_id, token, opts):
        """并发实测，到点即收；返回 {线路 id: 实测结果}。"""
        out = {}
        todo = []
        now = time.time()
        for opt in opts[:self.PROBE_LIMIT]:
            sid = opt.get("playback_source_id") or opt.get("id") or ""
            if not sid:
                continue
            # 播放时踩过雷的线路（登录墙 / 解析票过期）优先级最高，先把缓存结果盖掉
            if self._cache_get(self._dead, (vod_id, sid), self.DEAD_TTL):
                out[sid] = {"url": "", "w": 0, "h": 0, "kbs": 0.0,
                            "alive": False, "certain": True, "mp4": False}
                continue
            hit = self._cache_get(self._probe, (token, sid), self.META_TTL)
            if hit is not None:
                out[sid] = hit
                continue
            if self._cache_get(self._skip, (token, sid), 120):
                continue                  # 上次到点没测完，短时间别反复磨
            todo.append((sid, opt))

        if not todo:
            return out

        pool = ThreadPoolExecutor(max_workers=self.PROBE_WORKERS)
        futs = {}
        try:
            for sid, opt in todo:
                futs[pool.submit(self._probe_line, opt)] = sid
            deadline = time.time() + self.PROBE_BUDGET
            for f in as_completed(list(futs), timeout=self.PROBE_BUDGET):
                sid = futs[f]
                try:
                    res = f.result()
                except Exception:
                    continue
                out[sid] = res
                self._cache_put(self._probe, (token, sid), res, keep=600)
                if res["alive"] and res["url"]:
                    self._cache_put(self._urls, (token, sid), res["url"], keep=400)
                elif res["certain"]:
                    self._cache_put(self._dead, (vod_id, sid), True, keep=400)
                else:
                    # 连不上（DNS/拒绝连接/超时）不能一次就判死，两次以上才当死线
                    miss = (self._cache_get(self._miss, (token, sid), 900) or 0) + 1
                    self._cache_put(self._miss, (token, sid), miss, keep=400)
                    if miss >= 2:
                        self._cache_put(self._dead, (vod_id, sid), True, keep=400)
                if time.time() > deadline:
                    break
        except Exception:
            pass
        finally:
            pool.shutdown(wait=False)      # 别用 wait=True，否则会把 4s 拖成 15s
        for sid, _opt in todo:
            if sid not in out:
                self._cache_put(self._skip, (token, sid), True, keep=600)
        return out

    def _tier(self, label, res):
        """画质档位：0=4K 1=1080P 2=720P 3=540P 4=更低 5=未知。实测像素优先，其次看线路名。"""
        w, h = res.get("w") or 0, res.get("h") or 0
        if w and h:
            if w >= 3800 or h >= 2000:
                return 0
            if w >= 1900 or h >= 1000:
                return 1
            if w >= 1200 or h >= 700:
                return 2
            if w >= 850 or h >= 500:
                return 3
            return 4
        lab = "%s %s" % (label or "", res.get("label") or "")
        up = lab.upper()
        if "4K" in up or "2160" in lab:
            return 0
        if "1080" in lab:
            return 1
        if "720" in lab or "高清" in lab:
            return 2
        return 5

    def _rank(self, pairs):
        """按「画质档位 → 是否实测通过 → 实测码率 → 站点权重」排。

        没测完的线路（res 为空）照样参与排序，只是同档位里排在实测通过之后：
        4K 线路换票常常要两三秒，不能因为没测完就被塞到 540P 后面。
        """
        def key(pair):
            opt, res = pair
            label = opt.get("label") or opt.get("provider_name") or ""
            return (self._tier(label, res),
                    0 if res.get("alive") else 1,
                    -min(res.get("kbs") or 0.0, 4096.0),
                    -(opt.get("preference_weight") or 0))
        return [p[0] for p in sorted(pairs, key=key)]

    # ========== 详情：实测过滤死线，4K/高码率排前面 ==========
    def detailContent(self, ids=None):
        ids = self._safe_list(ids)
        if not ids:
            return {"list": []}
        vod_id = str(ids[0])

        data = self._catalog(vod_id)
        if not data:
            return {"list": []}

        episodes = data.get("episodes") or []
        if not episodes:
            return {"list": []}

        first_token = episodes[0].get("token", "")
        lines = self._line_options(first_token) if first_token else []

        # 逐条实测：确认取不到流的直接不铺（站点下发的线路里常年混着
        # 一批域名已废的，点了就是黑屏），其余按画质/码率排。
        probed = self._probe_lines(vod_id, first_token, lines) if lines else {}
        pairs, pending = [], []
        for opt in lines:
            sid = opt.get("playback_source_id") or opt.get("id") or ""
            res = probed.get(sid)
            if res is None:
                pending.append(opt)            # 到点还没测完
            elif res["alive"]:
                pairs.append((opt, res))
            # 确认死的丢掉
        # 没测完的线路只在「实测通过的不够挑」时才补进来：否则尾部全是没验过的，
        # 点开才发现是死线（虽然会自动换线，但列表看着乱）
        if pending and len(pairs) < 10:
            pending.sort(key=lambda o: (self._tier(o.get("label") or "", {}),
                                        -(o.get("preference_weight") or 0)))
            room = min(self.LINE_CAP - len(pairs), 8)
            pairs += [(o, {}) for o in pending[:max(room, 0)]]
        ordered = self._rank(pairs)[:self.LINE_CAP]

        # 同名线路只留一条（暴风/魔都这类常同时挂 m3u8 和 unknown 两份）
        final, seen_name, seen_url = [], set(), set()
        for opt in ordered:
            name = opt.get("label") or opt.get("provider_name") or ""
            sid = opt.get("playback_source_id") or opt.get("id") or ""
            if not sid:
                continue
            got = probed.get(sid) or {}
            key_url = got.get("url") or ""
            if name and name in seen_name:
                continue
            if key_url and key_url in seen_url:
                continue                     # 新浪/速播这类同源不同名的重复地址
            if name:
                seen_name.add(name)
            if key_url:
                seen_url.add(key_url)
            final.append(opt)

        if final:
            self._cache_put(self._order, vod_id,
                            [o.get("playback_source_id") or o.get("id") for o in final],
                            keep=120)

        play_from_list = []
        play_url_list = []
        for opt in final:
            line_id = opt.get("playback_source_id") or opt.get("id") or ""
            name = opt.get("label") or opt.get("provider_name") or line_id
            play_from_list.append(name)

            ep_strs = []
            for i, ep in enumerate(episodes):
                ep_title = ep.get("title") or "第%s集" % (ep.get("number") or (i + 1))
                # 集号一律用下标：综艺/花絮这类没有 number 字段，用 number 会取不到
                ep_strs.append("%s$%s|%s|%d" % (ep_title, vod_id, line_id, i))
            play_url_list.append("#".join(ep_strs))

        if not play_from_list:
            ep_strs = []
            for i, ep in enumerate(episodes):
                ep_title = ep.get("title") or "第%s集" % (ep.get("number") or (i + 1))
                ep_strs.append("%s$%s||%d" % (ep_title, vod_id, i))
            play_from_list = ["默认线路"]
            play_url_list = ["#".join(ep_strs)]

        return {"list": [{
            "vod_id": vod_id,
            "vod_name": data.get("title") or "",
            "vod_pic": data.get("poster_url") or "",
            "vod_content": data.get("description") or "",
            "vod_year": str(data.get("year") or ""),
            "vod_actor": "、".join(data.get("actors") or []),
            "vod_director": "、".join(data.get("directors") or []),
            "vod_play_from": "$$$".join(play_from_list),
            "vod_play_url": "$$$".join(play_url_list),
        }]}

    # ========== 播放 ==========
    def _find_line(self, opts, line_id):
        for opt in opts:
            oid = opt.get("playback_source_id") or opt.get("id") or ""
            if oid == line_id:
                return opt
        for opt in opts:
            name = opt.get("label") or opt.get("provider_name") or ""
            if name == line_id:
                return opt
        return None

    def _make_play(self, url):
        """直链 → 播放结果。m3u8 走本地代理洗掉广告分片，mp4 直连。"""
        if not url:
            return None
        if ".m3u8" in url.lower() and self.ENABLE_HLS_CLEAN:
            port = HLSProxyServer.start(self.proxy_config)
            if port > 0:
                return {"parse": 0, "header": self.PLAY_HEADERS,
                        "url": "http://127.0.0.1:%d/?url=%s" % (port, quote(url, safe=""))}
        return {"parse": 0, "url": url, "header": self.PLAY_HEADERS}

    def _line_url(self, token, vod_id, line_id, opt):
        """取某条线路的播放地址：先看缓存，没有就现解析一次。"""
        hit = self._cache_get(self._urls, (token, line_id), self.LINE_TTL)
        if hit:
            return hit
        if not opt:
            return ""
        url = opt.get("url") or ""
        kind = opt.get("url_kind") or ""
        if kind == "resolve_ticket" or url.startswith("resolve://"):
            url = self._resolve_ticket(url.replace("resolve://", ""))
        if not url.startswith("http"):
            return ""
        self._cache_put(self._urls, (token, line_id), url, keep=400)
        return url

    def playerContent(self, flag="", id="", vipFlags=None):
        id = str(id or "")
        if id.startswith("http") and self.isVideoFormat(id):
            return {"parse": 0, "url": id, "header": self.PLAY_HEADERS}

        parts = id.split("|")
        if len(parts) != 3:
            return {"parse": 1, "url": "", "msg": "id 格式错误"}
        vod_id, line_id, ep_num = parts

        detail = self._catalog(vod_id)
        episodes = detail.get("episodes") or []
        if not episodes:
            return {"parse": 1, "url": "", "msg": "详情加载失败"}

        token = ""
        try:
            idx = int(ep_num)
        except Exception:
            idx = -1
        if 0 <= idx < len(episodes):
            token = episodes[idx].get("token") or ""
        if not token:                       # 兼容旧格式（按集号匹配）
            for ep in episodes:
                if str(ep.get("number")) == str(ep_num):
                    token = ep.get("token") or ""
                    break
        if not token:
            return {"parse": 1, "url": "", "msg": "找不到第%s集" % ep_num}

        opts = self._line_options(token, tag="player/resolve")
        if not opts:                       # 详情页那次可能赶上接口抖动，重取一次
            opts = self._line_options(token, tag="player/retry")
        opt = self._find_line(opts, line_id)
        url = self._line_url(token, vod_id, line_id, opt)
        play = self._make_play(url)
        if play:
            return play

        # 选中的线路取不到地址（登录墙 / 解析票过期）：按实测顺序自动换一条
        name = (opt or {}).get("label") or line_id
        fail_msg = "「%s」取不到播放地址" % name
        self._cache_put(self._dead, (vod_id, line_id), True, keep=400)
        alt = self._auto_play(vod_id, token, opts, line_id)
        if alt:
            play, alt_name = alt
            play["msg"] = fail_msg + "，已自动切到「%s」" % alt_name
            return play
        return {"parse": 1, "url": "", "msg": fail_msg}

    def _auto_play(self, vod_id, token, opts, skip_id):
        """按详情页实测顺序换线，最多试 4 条。"""
        order = self._cache_get(self._order, vod_id, self.META_TTL) or []
        if not order:                      # 详情没走实测（或上次失败）：按站点原顺序试
            order = [o.get("playback_source_id") or o.get("id") for o in opts]
        by_id = {}
        for opt in opts:
            oid = opt.get("playback_source_id") or opt.get("id") or ""
            if oid:
                by_id[oid] = opt
        cand = [x for x in order if x and x != skip_id and x in by_id]
        for oid in order:
            if oid and oid != skip_id and oid not in cand and oid in by_id:
                cand.append(oid)
        deadline = time.time() + 8.0
        for oid in cand[:4]:
            if time.time() > deadline:
                break
            opt = by_id.get(oid)
            if not opt:
                continue
            url = self._line_url(token, vod_id, oid, opt)
            play = self._make_play(url)
            if play:
                return play, (opt.get("label") or opt.get("provider_name") or oid)
        return None

    # ========== 辅助 ==========
    def _card_to_vod(self, card):
        return {
            "vod_id": str(card.get("id") or ""),
            "vod_name": card.get("title") or "",
            "vod_pic": card.get("poster_url") or "",
            "vod_remarks": card.get("remarks") or "",
        }

    @staticmethod
    def _safe_list(v):
        if v is None:
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, (str, int)):
            return [str(v)]
        return []

    @staticmethod
    def _safe_dict(v):
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return {}
            try:
                d = json.loads(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
        return {}


# ==================== 测试入口 ====================
if __name__ == "__main__":
    spider = Spider()
    spider.init()
    try:
        home = spider.homeVideoContent().get("list", [])
        print("0. homeVideoContent:", len(home))

        hc = spider.homeContent(True)
        print("1. filters dict:", isinstance(hc.get("filters"), dict))

        if home:
            vid = home[0]["vod_id"]
            res = spider.detailContent([vid])
            lst = res.get("list", [])
            print("2. detailContent:", len(lst), "条")
            if lst:
                play_from = lst[0].get("vod_play_from") or ""
                lines = play_from.split("$$$")
                print(f"   线路数: {len(lines)}")
                for i, name in enumerate(lines):
                    print(f"     [{i}] {name}")

                play_url = lst[0].get("vod_play_url", "")
                groups = play_url.split("$$$")
                print(f"\n3. 测试前 2 条线路播放")
                for i in range(min(2, len(lines))):
                    if i >= len(groups) or not groups[i]:
                        continue
                    first_ep = groups[i].split("#")[0]
                    ep_id = first_ep.split("$")[1]
                    play_res = spider.playerContent("", ep_id)
                    is_proxy = str(play_res.get("url", "")).startswith("http://127.0.0.1")
                    print(f"   [{lines[i]}] parse={play_res.get('parse')} "
                          f"proxy={is_proxy} url={str(play_res.get('url'))[:70]}")
    finally:
        spider.destroy()
