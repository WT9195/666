# -*- coding: utf-8 -*-
# 懂片帝 - TVBox / FongMi(追剧影视) 通用 Python 源
# 站点：https://dongpian17.com
# 接口：/v1/* REST API（HMAC-SHA256 签名，消息为 方法\n路径+查询\n时间戳\n随机数）
#
# 说明：
#   1. 只用标准库，不依赖 requests / bs4。影视壳（Chaquopy）里经常没有 requests，
#      原版顶部 `import requests` 会直接抛 ImportError，整个源加载失败 —— 表现就是
#      「能搜到片但播放不了」甚至整站空白。
#   2. 详情页会并发实测每条线路（取清单 → 按 BYTERANGE 取首个分片 → 验 TS/AES 密钥），
#      能播的按首包延迟排序，坏线直接剔除，避免点进去黑屏。
#   3. 线路地址形如 /video/ 的 m3u8 多为「伪装成 .png / .image 的分片 + EXT-X-BYTERANGE」，
#      播放器（ExoPlayer / IJK）原生支持，这里不做二次转发。
import sys
import os
import json
import time
import hmac
import gzip
import zlib
import ssl
import threading
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar

# ↓↓↓ TVBox Python 插件必需：base 包在上一级目录，这一行不能删 ↓↓↓
sys.path.append('..')

try:
    from base.spider import Spider as BaseSpider
except Exception:
    BaseSpider = None

if BaseSpider is None:
    class BaseSpider(object):
        def __init__(self):
            self.extend = ''

        def init(self, extend=""):
            pass

        def getName(self):
            return ""

        def isVideoFormat(self, url):
            return False

        def manualVideoCheck(self):
            return False

        def destroy(self):
            pass

        def localProxy(self, param):
            return None


class _Http(object):
    """轻量 HTTP 会话：cookie 持久化 + 忽略证书 + gzip + 有限重试，纯标准库。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        try:
            ctx = ssl._create_unverified_context()
            if ctx is not None:
                handlers.insert(0, urllib.request.HTTPSHandler(context=ctx))
        except Exception:
            pass
        try:
            self.opener = urllib.request.build_opener(*handlers)
        except Exception:
            self.opener = urllib.request.build_opener()

    def req(self, method, url, headers=None, body=None, timeout=12,
            retry=1, max_bytes=None):
        """返回 (status, headers_dict, bytes)；异常时 status 为 -1。"""
        last = None
        for attempt in range(retry + 1):
            try:
                req = urllib.request.Request(url, data=body,
                                             headers=headers or {}, method=method)
                resp = self.opener.open(req, timeout=timeout)
                try:
                    data = resp.read(max_bytes) if max_bytes else resp.read()
                    hdrs = dict(resp.headers.items()) if resp.headers else {}
                    code = resp.getcode()
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                enc = (hdrs.get("Content-Encoding") or "").lower()
                if data[:2] == b"\x1f\x8b" and "gzip" in enc:
                    try:
                        data = gzip.decompress(data)
                    except Exception:
                        pass
                elif "deflate" in enc:
                    try:
                        data = zlib.decompress(data)
                    except Exception:
                        pass
                return code, hdrs, data
            except urllib.error.HTTPError as e:
                try:
                    data = e.read()
                except Exception:
                    data = b""
                hdrs = dict(e.headers.items()) if e.headers else {}
                return int(getattr(e, "code", -1)), hdrs, data
            except Exception as e:
                last = e
                if attempt < retry:
                    time.sleep(0.25 * (attempt + 1))
        return -1, {}, b""


class Spider(BaseSpider):

    # 请求签名密钥（硬编码于前端 movie-card-runtime）
    SIGN_KEY = "8b9a908a05eac640e1ee06f52acaa741bfe4ba9e004eeffdbeb635e532e06666"

    # 分类映射：tab名 → kind 参数
    KINDS = [
        ("all", "全部"),
        ("movie", "电影"),
        ("series", "剧集"),
        ("short_drama", "短剧"),
        ("anime", "动漫"),
        ("variety", "综艺"),
    ]

    # 榜单映射
    WINDOWS = [
        ("day", "日榜"),
        ("week", "周榜"),
        ("month", "月榜"),
        ("total", "总榜"),
    ]

    # 探测参数
    PROBE_LIMIT = 9         # 最多实测多少条线路
    PROBE_BUDGET = 2.0      # 并发探测总预算（秒），超时未返回的按「未知」保留
    PROBE_TIMEOUT = 3.0     # 单次请求超时（秒）
    PROBE_BYTES = 131072    # 测速时下载的分片字节数（128KB）
    SLOW_KBPS = 300         # 低于此吞吐视为「慢线」，沉到后面
    MAX_LINES = 10          # 详情页最多展示多少条线路
    # 置顶线路关键字（首选线路，命中即排第一）
    # 实测 2026-11-10：本站真 4K 线路叫「4K官方C」，实测 HEVC 3840x2160、
    # 码率 10~12Mbps（同片「官方C」只有 2Mbps）。原来只认「专享4K」，
    # 而站点现在不出现这个标签，导致默认播的是 1080p 那条。
    PREFER_LABELS = ("4K", "专享4K")

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self.host = "https://dongpian17.com"
        self.name = "懂片帝"
        self.ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
        self.http = _Http()
        self.account = ""
        self.password = ""
        self.cookie_str = ""
        self._logged_in = False
        self.probe = 1          # extend 传 {"probe":0} 可关闭线路实测（详情更快）
        self._login_lock = threading.Lock()
        self._login_tried = 0.0
        self._lock = threading.Lock()
        # 缓存
        self._cache_lines = {}   # token -> (ts, [line dict])
        self._cache_url = {}     # ticket -> (ts, url)
        self._cache_order = {}   # token -> (ts, [line_id ...])

    def init(self, extend=""):
        try:
            super().init(extend)
        except Exception:
            pass
        try:
            if not extend:
                extend = getattr(self, 'extend', '')
            if isinstance(extend, str):
                try:
                    ext = json.loads(extend) if extend.strip() else {}
                except Exception:
                    ext = {}
            elif isinstance(extend, dict):
                ext = extend
            else:
                ext = {}
            if not isinstance(ext, dict):
                ext = {}
            self.host = str(ext.get("host", self.host)).rstrip("/")
            self.name = ext.get("name", self.name)
            self.ua = ext.get("ua", self.ua)
            # 账号配置：官方线路需登录播放，支持 account/password 或直接给 cookie
            self.account = ext.get("account", self.account)
            self.password = ext.get("password", self.password)
            self.cookie_str = ext.get("cookie", self.cookie_str)
            try:
                self.probe = int(ext.get("probe", self.probe))
            except Exception:
                self.probe = 1
            if self.cookie_str:
                self._apply_cookie(self.cookie_str)
                self._logged_in = True
            print("懂片帝 init: host={}, name={}".format(self.host, self.name))
        except Exception as e:
            print("懂片帝 init error: {}".format(e))

    # ================= 登录会话 =================

    def _apply_cookie(self, cookie_str):
        try:
            for kv in str(cookie_str).split(";"):
                kv = kv.strip()
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    c = http.cookiejar.Cookie(
                        version=0, name=k.strip(), value=v.strip(), port=None,
                        port_specified=False, domain="dongpian17.com",
                        domain_specified=False, domain_initial_dot=False,
                        path="/", path_specified=True, secure=False,
                        expires=None, discard=True, comment=None,
                        comment_url=None, rest={}, rfc2109=False)
                    self.http.jar.set_cookie(c)
        except Exception as e:
            print("懂片帝 apply cookie error: {}".format(e))

    def _login(self):
        """账号密码登录，成功后会话 cookie 保存在 session 中"""
        if not self.account or not self.password:
            return False
        try:
            path = "/v1/users/password/login"
            headers = self._headers("POST", path, "")
            headers["Content-Type"] = "application/json"
            body = json.dumps({"account": self.account,
                               "password": self.password}).encode("utf-8")
            code, _, data = self.http.req("POST", self.host + path, headers, body, timeout=15)
            text = data.decode("utf-8", "ignore")
            info = json.loads(text) if text[:1] in "{[" else {}
            if code == 200 and not info.get("error"):
                self._logged_in = True
                print("懂片帝 login ok: {}".format(self.account))
                return True
            print("懂片帝 login fail: {}".format(text[:200]))
        except Exception as e:
            print("懂片帝 login error: {}".format(e))
        return False

    def _anonymous_login(self):
        """注册匿名(游客)会话，服务端种 cookie。部分官方线路按游客配额放行。"""
        if self._logged_in:
            return True
        try:
            import uuid
            path = "/v1/users/anonymous"
            headers = self._headers("POST", path, "")
            headers["Content-Type"] = "application/json"
            anon_id = "web_{}".format(uuid.uuid4())
            body = json.dumps({"anonymous_id": anon_id}).encode("utf-8")
            code, _, data = self.http.req("POST", self.host + path, headers, body, timeout=15)
            if code in (200, 201):
                self._logged_in = True
                print("懂片帝 anonymous session ok: {}".format(anon_id))
                return True
            print("懂片帝 anonymous fail: {} {}".format(
                code, data.decode("utf-8", "ignore")[:200]))
        except Exception as e:
            print("懂片帝 anonymous error: {}".format(e))
        return False

    def _ensure_login(self):
        if self._logged_in:
            return True
        # 多线程探测时会同时撞上「需要会话」，加锁 + 60s 内只试一次，避免重复注册游客
        self._login_lock.acquire()
        try:
            if self._logged_in:
                return True
            if time.time() - self._login_tried < 60:
                return False
            self._login_tried = time.time()
            # 优先账号密码登录（配额更高），否则注册匿名游客会话
            if self.account and self.password:
                if self._login():
                    return True
            return self._anonymous_login()
        finally:
            self._login_lock.release()

    # ================= 签名请求 =================

    def _headers(self, method, path, search):
        ts = str(int(time.time() * 1000))
        nonce = os.urandom(16).hex()
        msg = "{}\n{}{}\n{}\n{}".format(method, path, search, ts, nonce)
        sign = hmac.new(self.SIGN_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "User-Agent": self.ua,
            "Accept": "application/json",
            "x-ai-movie-client-name": "movie-search-frontend",
            "x-ai-movie-client-version": "1.0.0",
            "x-ai-movie-build-version": "dongpiandi-v2026.10.04.1-episode-resume-efd9a7f2e87e-web",
            "x-ai-movie-protocol-version": "2026-07-05.library-v2.playback-v1",
            "x-ai-movie-timestamp": ts,
            "x-ai-movie-nonce": nonce,
            "x-ai-movie-signature": sign,
        }

    def _get(self, path, search=""):
        try:
            code, _, data = self.http.req(
                "GET", self.host + path + search,
                self._headers("GET", path, search), timeout=15)
            text = data.decode("utf-8", "ignore")
            if not text.strip():
                return {}
            return json.loads(text)
        except Exception as e:
            print("懂片帝 _get error {}: {}".format(path, e))
            return {}

    def _post(self, path, search, obj):
        try:
            headers = self._headers("POST", path, search)
            headers["Content-Type"] = "application/json"
            body = json.dumps(obj).encode("utf-8")
            code, _, data = self.http.req("POST", self.host + path + search,
                                          headers, body, timeout=15)
            text = data.decode("utf-8", "ignore")
            if not text.strip():
                return {}
            return json.loads(text)
        except Exception as e:
            print("懂片帝 _post error {}: {}".format(path, e))
            return {}

    def _resolve_line(self, ticket, retried=False):
        """resolve-line 二次解析。站点播放服务偶发 502（播放服务暂时不可用），
        这里对 5xx / 空结果重试，遇登录要求时自动登录重试一次。"""
        if not ticket:
            return {}
        for attempt in range(3):
            d2 = self._post("/v1/playback/resolve-line", "?view=compact",
                            {"ticket": ticket})
            line = d2.get("line", {}) or {}
            if line.get("url"):
                return line
            code = str(d2.get("code") or d2.get("error") or "")
            status = d2.get("statusCode") or 0
            need = code == "playback_user_session_required"
            if need and not retried:
                retried = True
                if self._ensure_login():
                    continue
            if code == "playback_resolve_upstream_error" or status >= 500:
                time.sleep(0.35 * (attempt + 1))
                continue
            break
        return {}

    def _lines_of(self, token, force=False):
        """取某集的所有线路（带 120s 缓存，避免切线路时重复请求）

        站点的 /v1/playback/resolve 会间歇性返回 502「播放服务暂时不可用」，
        不重试就会得到空线路表 —— 壳里表现就是线路空白、点开黑屏。
        """
        now = time.time()
        if not force:
            hit = self._cache_lines.get(token)
            if hit and now - hit[0] < 120 and hit[1]:
                return hit[1]
        lines = []
        for attempt in range(3):
            data = self._get("/v1/playback/resolve/" + token, "?view=compact")
            lines = data.get("line_options", []) or []
            if lines:
                break
            code = str(data.get("code") or data.get("error") or "")
            status = data.get("statusCode") or 0
            if code == "playback_user_session_required":
                if self._ensure_login():
                    continue
            if code == "playback_resolve_upstream_error" or status >= 500:
                time.sleep(0.35 * (attempt + 1))
                continue
            break
        if lines:
            with self._lock:
                if len(self._cache_lines) > 60:
                    self._cache_lines.clear()
                self._cache_lines[token] = (now, lines)
        return lines

    def _title_lines(self, episodes):
        """枚举整部片可用的线路。

        综艺 / 短剧的不同集挂在不同的上游线路上：只查第一集经常只拿到 1~2 条，
        换成线路最全的那一集去查，才不会「详情页只剩一条线」。
        """
        n = len(episodes)
        idxs = []
        for i in (0, 1, n // 2, n - 1):
            if 0 <= i < n and i not in idxs:
                idxs.append(i)
        best = []
        tried = 0
        for i in idxs:
            ep = episodes[i]
            tok = ep.get("token") if isinstance(ep, dict) else ""
            if not tok:
                continue
            ls = self._lines_of(tok)
            tried += 1
            if len(ls) > len(best):
                best = ls
            if len(best) >= 6 or tried >= 3:
                break
        return best

    def _line_url(self, ln):
        """把线路对象转成可直接播放的地址（需要时走 resolve-line 二次解析）"""
        if not isinstance(ln, dict):
            return ""
        kind = ln.get("url_kind") or ""
        raw = (ln.get("url") or "").replace("\\/", "/")
        if kind == "m3u8" and raw and not raw.startswith("resolve://") \
                and not ln.get("resolve_required"):
            return raw
        ticket = raw.replace("resolve://", "")
        if not ticket:
            return ""
        now = time.time()
        hit = self._cache_url.get(ticket)
        if hit and now - hit[0] < 60:
            return hit[1]
        url = ""
        if len(ticket) > 60:   # resolve:// 票据较长，避免把普通地址误提交
            line = self._resolve_line(ticket)
            url = (line.get("url") or "").replace("\\/", "/")
        if url:
            with self._lock:
                if len(self._cache_url) > 200:
                    self._cache_url.clear()
                self._cache_url[ticket] = (now, url)
        return url

    # ================= 线路探测 =================

    @staticmethod
    def _segments(text):
        """解析 m3u8，返回 [(url, (len, off) 或 None)] 与 key 地址"""
        segs = []
        key = ""
        br = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-KEY:"):
                if "METHOD=NONE" not in line:
                    i = line.find('URI="')
                    if i >= 0:
                        key = line[i + 5:].split('"')[0]
            elif line.startswith("#EXT-X-BYTERANGE:"):
                v = line.split(":", 1)[1].split("@")
                try:
                    br = (int(v[0]), int(v[1]) if len(v) > 1 else 0)
                except Exception:
                    br = None
            elif not line.startswith("#"):
                segs.append((line, br))
                br = None
        return segs, key

    def _probe_line(self, ln):
        """实测一条线路：返回 dict(ok, speed KB/s, ttfb, note)

        只测「能不能连」是不够的 —— 站里有的线路首包很快但吞吐只有 100KB/s 出头，
        点进去就是无限缓冲/黑屏。所以这里真实下载 256KB 分片来算速度。
        """
        url = self._line_url(ln)
        if not url:
            return {"ok": False, "speed": 0.0, "ttfb": 9.9, "note": "resolve-fail"}
        http = _Http()
        base = {"User-Agent": self.ua, "Accept": "*/*"}
        t0 = time.time()
        code, _, data = http.req("GET", url, dict(base, Range="bytes=0-4095"),
                                 timeout=self.PROBE_TIMEOUT, retry=0, max_bytes=65536)
        ttfb = time.time() - t0
        if code < 200 or code >= 400:
            return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "http-%d" % code}
        if not data:
            return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "empty"}

        # 直链 MP4：拉 512KB 测吞吐
        if data[:7] != b"#EXTM3U":
            if not (data[4:8] in (b"ftyp", b"moov", b"mdat", b"free") or data[:2] == b"\x00\x00"):
                return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "not-media"}
            t = time.time()
            _, _, blob = http.req("GET", url, dict(base, Range="bytes=0-524287"),
                                  timeout=self.PROBE_TIMEOUT, retry=0, max_bytes=524288)
            dt = max(time.time() - t, 0.001)
            return {"ok": len(blob or b"") > 0, "speed": len(blob or b"") / dt / 1024.0,
                    "ttfb": ttfb, "note": "mp4"}

        text = data.decode("utf-8", "ignore")
        if not text.startswith("#EXTM3U"):
            return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "not-media"}
        segs, key = self._segments(text)
        if not segs:
            return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "no-seg"}
        if key:
            # 加密流：先确认密钥能取到（取不到必然黑屏）
            kurl = urllib.parse.urljoin(url, key)
            kcode, _, kdata = http.req("GET", kurl, {"User-Agent": self.ua},
                                       timeout=self.PROBE_TIMEOUT, retry=0, max_bytes=64)
            if kcode != 200 or len(kdata or b"") < 16:
                return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "key-%d" % kcode}

        # 取首个分片：既要验内容（明文看 TS 同步字节），也要测吞吐
        su, br = segs[0]
        h = {"User-Agent": self.ua, "Accept": "*/*"}
        if br:
            h["Range"] = "bytes=%d-%d" % (br[1], br[1] + self.PROBE_BYTES - 1)
        t = time.time()
        scode, _, sdata = http.req("GET", urllib.parse.urljoin(url, su), h,
                                   timeout=self.PROBE_TIMEOUT, retry=0,
                                   max_bytes=self.PROBE_BYTES)
        dt = max(time.time() - t, 0.001)
        if scode not in (200, 206) or not sdata:
            return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "seg-%d" % scode}
        if not key:
            good = sdata[0] == 0x47 or sdata[4:8] == b"ftyp" or sdata[:3] == b"ID3"
            if not good:
                return {"ok": False, "speed": 0.0, "ttfb": ttfb, "note": "bad-seg"}
        return {"ok": True, "speed": len(sdata) / dt / 1024.0,
                "ttfb": ttfb, "note": "aes" if key else "hls"}

    def _probe_all(self, lines):
        """并发探测，超预算未返回的按「未知」保留"""
        res = {}
        lock = threading.Lock()

        def work(ln):
            try:
                r = self._probe_line(ln)
            except Exception:
                r = {"ok": False, "speed": 0.0, "ttfb": 9.9, "note": "err"}
            with lock:
                res[ln.get("id")] = r

        works = []
        for ln in lines[:self.PROBE_LIMIT]:
            th = threading.Thread(target=work, args=(ln,))
            th.daemon = True
            th.start()
            works.append(th)
            time.sleep(0.05)   # 轻微错峰，避免一次并发把站点播放服务打出 502
        end = time.time() + self.PROBE_BUDGET
        for th in works:
            left = end - time.time()
            if left <= 0:
                break
            th.join(left)
        return res

    def _order_lines(self, token, lines):
        """按实测吞吐排序：够快 → 偏慢 → 未知 → 死线丢弃"""
        now = time.time()
        hit = self._cache_order.get(token)
        if hit and now - hit[0] < 300:
            by_id = dict((x.get("id"), x) for x in lines)
            ordered = [by_id[i] for i in hit[1] if i in by_id]
            if ordered:
                return ordered

        # 只有一条线路、或用户关掉了实测，直接返回（首选线路照样置顶）
        if len(lines) <= 1 or not self.probe:
            pref = [ln for ln in lines if self._is_preferred(ln)]
            rest = [ln for ln in lines if not self._is_preferred(ln)]
            return (pref + rest)[:self.MAX_LINES]

        metrics = self._probe_all(lines)
        first_ok, fast, slow, unknown, dead = [], [], [], [], []
        for ln in lines:
            m = metrics.get(ln.get("id"))
            if m is None:
                if self._is_preferred(ln):
                    first_ok.append((0.0, 0.0, ln))   # 首选线路即使没测到也排前面
                else:
                    unknown.append(ln)
            elif m.get("ok"):
                item = (m.get("speed", 0), m.get("ttfb", 9.9), ln)
                if self._is_preferred(ln):
                    first_ok.append(item)             # 首选线路可播 → 永远第一
                elif m.get("speed", 0) >= self.SLOW_KBPS:
                    fast.append(item)
                else:
                    slow.append(item)
            else:
                dead.append(ln)
        # 首选线路内部按速度排；其余按速度降序，接近时首包延迟低的优先
        first_ok.sort(key=lambda x: -x[0])
        fast.sort(key=lambda x: (-round(x[0] / 400.0), x[1]))
        slow.sort(key=lambda x: -x[0])
        # 注意：死线不删，只沉到最后 —— 站点会间歇性 502 / 探测超时，
        # 删掉就等于「网一抖，线路只剩一条」，用户反而没得切。
        ordered = ([ln for _, _, ln in first_ok] + [ln for _, _, ln in fast]
                   + [ln for _, _, ln in slow] + unknown + dead)
        ordered = ordered[:self.MAX_LINES]

        with self._lock:
            if len(self._cache_order) > 60:
                self._cache_order.clear()
            self._cache_order[token] = (now, [x.get("id") for x in ordered])
        return ordered

    # ================= TVBox 接口 =================

    def homeContent(self, filter=False):
        classes = []
        for tid, tname in self.KINDS:
            classes.append({"type_id": tid, "type_name": tname})

        filters = {}
        if filter:
            window_values = [{"n": n, "v": v} for v, n in self.WINDOWS]
            for tid, _ in self.KINDS:
                filters[tid] = [{
                    "key": "window",
                    "name": "榜单",
                    "value": window_values,
                }]
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        # 首页推荐：大家都在看（日榜快照）
        data = self._get("/v1/feed/home", "?scope=public&mode=preview&sections=3&cards=12")
        videos = []
        for sec in data.get("sections", []):
            if sec.get("id") == "feed_sec_everyone_watching" or sec.get("type") == "trend":
                for card in sec.get("cards", []):
                    videos.append(self._parse_card(card))
                break
        if not videos:
            for sec in data.get("sections", []):
                for card in sec.get("cards", []):
                    videos.append(self._parse_card(card))
                if videos:
                    break
        return {"list": videos}

    def categoryContent(self, tid, pg="1", filter=None, extend=None):
        window = "day"
        try:
            if isinstance(extend, str):
                extend = json.loads(extend) if extend.strip() else {}
            if isinstance(extend, dict) and extend.get("window"):
                window = extend["window"]
        except Exception:
            pass

        search = "?sort=trending&window={}&page={}&limit=20".format(window, pg)
        if tid and tid != "all":
            search += "&kind=" + str(tid)

        data = self._get("/v1/browse/catalog", search)

        videos = []
        for card in data.get("cards", []):
            videos.append(self._parse_card(card))

        pagination = data.get("pagination", {}) or {}
        has_more = bool(pagination.get("has_more"))
        try:
            page = int(pg)
        except Exception:
            page = 1
        pagecount = page + 1 if has_more else page

        return {
            "page": page,
            "pagecount": pagecount,
            "limit": 20,
            "total": pagecount * 20,
            "list": videos,
        }

    def detailContent(self, ids):
        vid = ids[0] if isinstance(ids, list) else ids
        data = self._get("/v1/catalog/" + str(vid))
        card = data.get("card", data) if isinstance(data, dict) else {}
        if not isinstance(card, dict):
            card = {}

        episodes = card.get("episodes") or []
        # 详情内嵌剧集不全时，走剧集分页接口补齐
        episode_count = card.get("episode_count") or len(episodes)
        if episode_count > len(episodes):
            episodes = self._fetch_all_episodes(vid, episodes)

        play_urls = []
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            title = ep.get("title") or ep.get("display_name") or \
                "第{}集".format(ep.get("number", ""))
            token = ep.get("token", "")
            if token:
                play_urls.append("{}${}".format(title, token))

        vod = self._parse_card(card)
        vod["vod_id"] = str(vid)

        # 用第1集 token 拉线路列表；官方线路实测后按速度排序
        lines = []
        first_token = ""
        if episodes and isinstance(episodes[0], dict):
            first_token = episodes[0].get("token", "") or ""
        if episodes:
            all_lines = self._title_lines(episodes)
            lines = [ln for ln in all_lines if self._is_official_line(ln)]
            if not lines:
                lines = [ln for ln in all_lines if ln.get("url_kind") == "m3u8"
                         and not ln.get("resolve_required")]
            lines = self._order_lines(first_token or str(vid), lines)

        if lines and play_urls:
            froms = []
            urls_per_line = []
            used_names = {}
            for ln in lines:
                line_id = ln.get("id", "")
                if not line_id:
                    continue
                label = ln.get("label") or ln.get("play_from") or ln.get("provider_id") or "线路"
                label = self._short_line_name(label)
                # 同名线路去重（加序号）
                if label in used_names:
                    used_names[label] += 1
                    label = "{}{}".format(label, used_names[label])
                else:
                    used_names[label] = 1
                froms.append(label)
                eps_urls = []
                for pu in play_urls:
                    title, token = pu.split("$", 1)
                    eps_urls.append("{}${}@@{}".format(title, token, line_id))
                urls_per_line.append("#".join(eps_urls))
            if froms:
                vod["vod_play_from"] = "$$$".join(froms)
                vod["vod_play_url"] = "$$$".join(urls_per_line)
            else:
                vod["vod_play_from"] = self.name
                vod["vod_play_url"] = "#".join(play_urls)
        else:
            vod["vod_play_from"] = self.name
            vod["vod_play_url"] = "#".join(play_urls)
        return {"list": [vod]}

    def searchContent(self, key, quick=False, pg="1"):
        # 注意：影视壳是按 searchContent(key, quick, page) 三个参数调的，少一个参数
        # 就会抛 TypeError 且被壳静默吞掉（表现为「搜不到」），所以这里必须收 pg。
        try:
            page = max(1, int(pg))
        except Exception:
            page = 1
        q = urllib.parse.quote(str(key))
        data = self._get("/v1/browse/catalog",
                         "?q={}&page={}&limit=20".format(q, page))
        videos = []
        for card in data.get("cards", []):
            videos.append(self._parse_card(card))
        pagination = data.get("pagination", {}) or {}
        has_more = bool(pagination.get("has_more"))
        return {
            "page": page,
            "pagecount": page + 1 if has_more else page,
            "limit": 20,
            "total": (page + 1 if has_more else page) * 20,
            "list": videos,
        }

    # 部分壳（FongMi 分支）会调这个别名做分页搜索
    def searchContentPage(self, key, quick=False, pg="1"):
        return self.searchContent(key, quick, pg)

    def playerContent(self, flag, id, vipFlags=""):
        token = id
        line_id = ""
        # 详情页写入的格式：token@@线路ID（用于切换播放来源）
        if "@@" in id:
            token, line_id = id.split("@@", 1)
        # 兜底：万一带上了「第1集$」前缀
        if "$" in token:
            token = token.split("$")[-1]

        lines = self._lines_of(token)
        play_url = self._pick_url(lines, flag, line_id)
        # 线路表为空（站点 502）或全解析失败：强制刷新一次再试
        if not play_url:
            lines = self._lines_of(token, force=True)
            play_url = self._pick_url(lines, flag, line_id)

        # 最后兜底：id 本身就是直链（部分壳/旧版详情会直接写地址）
        if not play_url and token.startswith("http"):
            play_url = token

        return {
            "parse": 0,
            "playUrl": "",
            "url": play_url,
            "header": {"User-Agent": self.ua},
        }

    def _pick_url(self, lines, flag, line_id):
        """在给定线路表里选出可播放地址：精确线路 → flag 同名 → 官方线 → 普通线"""
        # 指定线路：按 line_id 精确选中
        target = None
        if line_id:
            for ln in lines:
                if ln.get("id") == line_id:
                    target = ln
                    break
        # 再按 flag（播放来源名）匹配
        if target is None and flag and flag != self.name:
            for ln in lines:
                if (ln.get("label") or "") == flag or (ln.get("play_from") or "") == flag \
                        or self._short_line_name(ln.get("label") or "") == flag:
                    target = ln
                    break

        url = self._line_url(target) if target is not None else ""

        # 回退：官方线路优先，再普通线路
        if not url:
            official = [ln for ln in lines if self._is_official_line(ln)]
            rest = [ln for ln in lines if not self._is_official_line(ln)]
            candidates = official + rest
            sel = [ln for ln in candidates if ln.get("selected")]
            for ln in (sel + candidates):
                url = self._line_url(ln)
                if url:
                    break
        return url

    # ↓↓↓ TVBox 框架 base.spider 抽象方法必须实现，缺失会导致实例化失败 ↓↓↓
    def getName(self):
        return self.name

    def localProxy(self, param):
        return None

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    # ================= 工具方法 =================

    def _short_line_name(self, label):
        """线路显示名简化：'1080P-官方Z · 普通话' -> '官方Z'；
        含4K的保留4K前缀以区分（'4K海外-官方C' -> '4K官方C'）；其他原样返回"""
        label = str(label or "")
        if "官方" in label:
            base = label.split("·")[0].strip()
            for seg in reversed(base.split("-")):
                if "官方" in seg:
                    name = seg.strip()
                    if "4K" in base.upper() and not name.upper().startswith("4K"):
                        name = "4K" + name
                    return name
        return label

    def _is_preferred(self, ln):
        """首选线路（默认「4K」），命中的线路置顶。
        想换置顶的线路，改 PREFER_LABELS 里的关键字即可。
        大小写不敏感：站点标签有「4K海外-官方C」这种写法。"""
        label = str(ln.get("label") or "").upper()
        for key in self.PREFER_LABELS:
            if key and key.upper() in label:
                return True
        return False

    def _is_official_line(self, ln):
        """官方线路：label含"官方"，或 provider_id 为 official-*/bytevod-*"""
        label = ln.get("label") or ""
        provider = (ln.get("provider_id") or "").lower()
        if "官方" in label:
            return True
        if provider.startswith("official") or provider.startswith("bytevod"):
            return True
        return False

    def _fetch_all_episodes(self, vid, inline_eps):
        episodes = list(inline_eps)
        try:
            offset = len(episodes)
            while offset < 600:
                data = self._get("/v1/catalog/{}/episodes".format(vid),
                                 "?limit=48&offset={}".format(offset))
                eps = data.get("episodes", []) or []
                if not eps:
                    break
                episodes.extend(eps)
                page_info = data.get("episode_pagination", {}) or {}
                if not page_info.get("has_more"):
                    break
                offset += len(eps)
        except Exception as e:
            print("懂片帝 fetch episodes error: {}".format(e))
        return episodes

    def _parse_card(self, card):
        if not isinstance(card, dict):
            card = {}
        remarks = card.get("remarks", "") or ""
        return {
            "vod_id": str(card.get("id", "")),
            "vod_name": card.get("title", "") or card.get("normalized_title", ""),
            "vod_pic": card.get("poster_url", "") or card.get("backdrop_url", ""),
            "vod_remarks": remarks,
            "vod_year": str(card.get("year", "") or ""),
            "vod_area": card.get("area", "") or "",
            "vod_director": "、".join(card.get("directors", []) or []),
            "vod_actor": "、".join(card.get("actors", []) or []),
            "vod_content": card.get("content", "") or card.get("description", "")
            or card.get("blurb", ""),
        }
