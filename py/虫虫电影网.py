import re, json, base64

# 图片是否附加 @Referer/@User-Agent 后缀（本站无防盗链，默认关闭）
PIC_HEADER_SUFFIX = False

try:
    import requests as _requests
except Exception:
    _requests = None


class Spider:
    def __init__(self):
        self.site = "https://xianyanchu.com"
        self.name = "xianyanchu"
        self.header = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S9080) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            "Referer": self.site,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        self.s = self.session = self.sess = _requests.Session() if _requests else None
        self._extend = {}
        self._home = None
        self._pcache = {}
        # ---- 站点结构（生成时按真实源码探测）----
        # 本站为 div 卡片布局（非 li），分类/列表改用下方专用解析
        self.cat_scope = None
        self.cat_href_re = '<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        self.list_scope = None
        self.detail_tpl = '/voddetail/{id}.html'
        self.play_tpl = '/vodplay/{id}-1-1.html'
        self.m3u8_var = 'player_aaaa'
        self.search_tpl = '/vodsearch/{key}----------{pg}---.html'
        self.page_mode = ('none', 1)

    def getDependence(self):
        return []

    def manualVideoCheck(self):
        return False

    def isVideoFormat(self, url):
        if not url:
            return False
        u = str(url).lower()
        return u.endswith((".m3u8", ".mp4", ".flv", ".mkv", ".ts", ".avi")) or "m3u8" in u

    def destroy(self):
        pass

    def progressVideo(self, speed=0, time=0, end=0):
        # 壳播放进度回调；无弹幕/进度联动需求时直接 false
        return False

    def setVideoFlags(self, siteKey="", flags=None):
        # 壳视频标记回调（海阔/影视仓等扩展壳会调）；记录即可
        try:
            self._siteKey = siteKey or getattr(self, "_siteKey", "")
            self._vflags = flags if isinstance(flags, dict) else dict()
        except Exception:
            pass
        return None

    def action(self, action):
        return {}

    def _get(self, url, referer=None):
        h = dict(self.header)
        if referer:
            h["Referer"] = referer
        if self.s is not None:
            try:
                r = self.s.get(url, headers=h, timeout=12, allow_redirects=True)
                if r.status_code < 400:
                    return r.text
            except Exception:
                pass
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception:
            return ""

    def _u(self, u):
        if not u:
            return u
        u = u.strip()
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("http"):
            return u
        if u.startswith("/"):
            m = re.match(r"(https?://[^/]+)", self.site)
            return (m.group(1) if m else self.site) + u
        return self.site.rstrip("/") + "/" + u.lstrip("/")

    def _cats(self):
        if self._home is None:
            self._home = self._get(self.site)
        html = self._home
        out = []
        seen = set()
        # 一级导航：<nav class="top-bar-navigation ..."><li class="...menu-item..."><a href="/vodtype/1.html">电影&nbsp;<i ...></i></a>
        nav = html
        nm = re.search(r'<nav[^>]*top-bar-navigation[^>]*>([\s\S]*?)</nav>', html)
        if nm:
            nav = nm.group(1)
        for hm in re.finditer(
                r'<li[^>]*class="[^"]*menu-item[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>',
                nav, re.S):
            href = hm.group(1).strip()
            name = _html_unescape(re.sub(r"<[^>]+>", "", hm.group(2)))
            name = re.sub(r"\s+", "", name.replace("&nbsp;", "")).strip()
            if not name or name == "首页" or len(name) > 12 or name in seen:
                continue
            seen.add(name)
            out.append({"type_id": self._u(href), "type_name": name})
        # 追加二级分类（下拉菜单 a[title]），让壳里可直接进子类
        for hm in re.finditer(
                r'<a[^>]+href="(/vodtype/\d+\.html)"[^>]*title="([^"]{2,12})"', nav):
            name = _html_unescape(hm.group(2)).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"type_id": self._u(hm.group(1)), "type_name": name})
        return out

    def _vid(self, href):
        # 详情链接 → 纯数字 id（TVBox 惯例），异常时回退完整 URL
        m = re.search(r"/voddetail/(\d+)", href or "")
        return m.group(1) if m else self._u(href)

    def _items(self, html, base=None):
        out = []
        seen = set()
        base = base or self.site
        # 结构 A：搜索页 <article class="post-list contt blockimg">
        #   <a class="block-fea lazyload" data-original="图" href="/voddetail/id.html" title="片名">
        #   <span class="entry-title"><a href="...">片名</a></span>
        if "video-content-item" not in html:
            for bm in re.finditer(r'<article class="post-list contt[^"]*">([\s\S]*?)</article>', html):
                block = bm.group(1)
                hm = re.search(r'href="(/voddetail/\d+\.html)"', block)
                if not hm:
                    continue
                vid = hm.group(1)
                if vid in seen:
                    continue
                seen.add(vid)
                im = re.search(r'data-original="([^"]+)"', block)
                tm = re.search(r'<span class="entry-title"><a[^>]*>([^<]+)</a>', block) \
                     or re.search(r'title="([^"]+)"', block)
                rm = re.search(r'<em class="voddate[^"]*">([^<]+)</em>', block)
                out.append({
                    "vod_id": self._vid(vid),
                    "vod_name": _html_unescape(tm.group(1)).strip() if tm else "",
                    "vod_pic": self._pic(im.group(1)) if im else "",
                    "vod_remarks": _html_unescape(rm.group(1)).strip() if rm else "",
                })
            return out
        # 结构 B：首页/分类页 <div class="video-content-item">
        #   <a class="video-img lazyload" href="/voddetail/98955.html" data-original="...jpg">
        #   <span class="video-title ...">HD国语</span>
        #   <p class="video-name"><a class="text-overflow" href="..." title="片名">片名</a></p>
        # 以 video-content-item 为锚，截到下个卡片之前，避开嵌套 div 的非贪婪截断
        for bm in re.finditer(
                r'<div class="video-content-item">([\s\S]*?)(?=<div class="video-content-item">|</main>|$)',
                html):
            block = bm.group(1)
            hm = re.search(r'href="(/voddetail/\d+\.html)"', block)
            if not hm:
                continue
            vid = hm.group(1)
            if vid in seen:
                continue
            seen.add(vid)
            im = re.search(r'data-original="([^"]+)"', block)
            tm = re.search(r'<a class="text-overflow"[^>]*title="([^"]+)"', block)
            if not tm:
                tm = re.search(r'<img[^>]+title="([^"]+)"', block)
            rm = re.search(r'<span class="video-title[^"]*">([^<]+)</span>', block)
            out.append({
                "vod_id": self._vid(vid),
                "vod_name": _html_unescape(tm.group(1)).strip() if tm else "",
                "vod_pic": self._pic(im.group(1)) if im else "",
                "vod_remarks": _html_unescape(rm.group(1)).strip() if rm else "",
            })
        return out

    def init(self, extend=""):
        self._extend = {}
        if isinstance(extend, dict):
            self._extend = extend
        elif isinstance(extend, str) and extend.strip():
            try:
                e = json.loads(extend)
                if isinstance(e, dict):
                    self._extend = e
            except Exception:
                pass

    def homeContent(self, filter=None):
        cats = self._cats()
        html = self._home if self._home is not None else self._get(self.site)
        vod_list = self._items(html)
        return {"class": cats, "list": vod_list}

    def homeVideoContent(self):
        if self._home is None:
            self._home = self._get(self.site)
        return {"list": self._items(self._home)}

    def categoryContent(self, tid, pg=1, filter=None, extend=None):
        try:
            pg = int(pg)
        except Exception:
            pg = 1
        url = self._cat_url(tid, pg)
        html = self._get(url)
        if not html:
            return {"list": [], "page": pg, "pagecount": 1, "limit": 24, "total": 0}
        vod_list = self._items(html)
        pagecount = 1
        total = len(vod_list)
        m = re.search(r"共\s*(\d+)\s*页", html)
        if m:
            pagecount = int(m.group(1))
        return {"list": vod_list, "page": pg, "pagecount": pagecount, "limit": 24, "total": total}

    def _cat_url(self, tid, pg):
        t = str(tid).strip()
        if t.startswith("http"):
            return t
        if t.startswith("/"):
            return self.site.rstrip("/") + t
        return self.site.rstrip("/") + "/vodtype/%s.html" % t

    def detailContent(self, ids):
        vid = ids
        if isinstance(ids, (list, tuple)):
            vid = ids[0] if ids else ""
        vid = str(vid).strip()
        url = self._u(vid) if vid.startswith(("/", "http")) else None
        if url is None and self.detail_tpl:
            url = self.site.rstrip("/") + self.detail_tpl.format(id=vid)
        if url is None:
            return {"list": []}
        html = self._get(url)
        if not html:
            return {"list": []}
        title = ""
        tm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S) or re.search(r"<title[^>]*>([^<]{2,60})</title>", html)
        if tm:
            title = _html_unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        # 详情页封面在 <div class="block-fea lazyload" data-original="...">（非 img 标签）
        im = re.search(r'<div[^>]*class="[^"]*block-fea[^"]*"[^>]*data-original="([^"]+)"', html) \
             or re.search(r'<img[^>]+(?:data-original|data-src|src)="([^"]+)"', html) \
             or re.search(r'property="og:image" content="([^"]+)"', html)
        pic = self._pic(im.group(1)) if im else ""
        # 简介在 <div class="entry-content clearfix"><p>...</p></div>
        dm = re.search(r'<div[^>]*class="[^"]*entry-content[^"]*"[^>]*>([\s\S]{10,3000}?)</div>', html) \
             or re.search(r'<p[^>]*class="[^"]*(?:desc|content|intro)[^"]*"[^>]*>([\s\S]{10,2000}?)</p>', html) \
             or re.search(r'property="og:description" content="([^"]+)"', html)
        desc = _html_unescape(re.sub(r"<[^>]+>", "", dm.group(1))).strip() if dm else ""
        # 详情字段（MacCMS 模板：<p><span>年份：</span><a>2026</a></p>）
        def _fld(label, limit):
            m = re.search(r'<p[^>]*>\s*<span>' + label + r'：</span>([\s\S]{0,%d}?)</p>' % limit, html)
            if m:
                txt = _html_unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
                return re.sub(r"\s+", " ", txt.replace("&nbsp;", " ")).strip()
            m = re.search(label + r"：</span>\s*(?:<a[^>]*>)?([^<]{1,%d})" % min(limit, 200), html)
            return _html_unescape(m.group(1)).replace("&nbsp;", " ").strip() if m else ""
        year = _fld("年份", 300)
        area = _fld("地区", 300)
        actors = _fld("演员", 4000)[:200]
        director = _fld("导演", 1500)[:120]
        # 播放源/集数（按线路容器 ewave-playlist-N 分组，多线路用 $$$ 分隔）
        play_from, play_url = "", ""
        if self.play_tpl:
            groups = []
            for gm in re.finditer(r'<ul[^>]*id="ewave-playlist-(\d+)"[^>]*>([\s\S]*?)</ul>', html):
                seg = gm.group(2)
                eps, seen_h = [], set()
                for pm in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{1,30}?)\s*</a>', seg):
                    h, n = pm.group(1), _html_unescape(pm.group(2).strip())
                    if not re.search(r"vodplay", h, re.I) or h in seen_h:
                        continue
                    seen_h.add(h)
                    eps.append("%s$%s" % (n or "播放", self._u(h)))
                if eps:
                    groups.append(eps)
            if not groups:
                # 回退：无线路容器时全文抓播放链接（单线路）
                eps, seen_h = [], set()
                for pm in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{1,20}?)\s*</a>', html):
                    h, n = pm.group(1), _html_unescape(pm.group(2).strip())
                    if not re.search(r"(play|vodplay|/p/|-0-0\.html)", h, re.I) or h in seen_h:
                        continue
                    seen_h.add(h)
                    eps.append("%s$%s" % (n or "播放", self._u(h)))
                if eps:
                    groups.append(eps)
            if groups:
                # 线路名取 tab 文本，重名时追加序号
                names = [_html_unescape(x).strip() for x in re.findall(
                    r'<li[^>]*class="[^"]*ewave-tab[^"]*"[^>]*data-target="#ewave-playlist-\d+"[^>]*>\s*<a[^>]*>([^<]+)</a>',
                    html)]
                # 线路名用站点自己的 tab 文案（本站为「高清云播」），而不是域名；
                # TVBox 播放页顶部显示的就是这个名字，用域名会让人以为是外站接口。
                if len(groups) == 1:
                    play_from = (names[0] if names and names[0] else "") or "高清云播"
                else:
                    play_from = "$$$".join(
                        "%s%d" % (names[i] if i < len(names) and names[i] else "线路", i + 1)
                        for i in range(len(groups)))
                play_url = "$$$".join("#".join(g) for g in groups)
        return {"list": [{"vod_id": vid, "vod_name": title, "vod_pic": pic,
                           "vod_content": desc, "vod_year": year, "vod_area": area,
                           "vod_actor": actors, "vod_director": director,
                           "vod_play_from": play_from, "vod_play_url": play_url}]}

    def searchContent(self, key, quick=False, pg="1"):
        if not self.search_tpl:
            return {"list": []}
        try:
            from urllib.parse import quote
            url = self.search_tpl.replace("{key}", quote(str(key)))
            if "{pg}" in url:
                url = url.replace("{pg}", str(pg))
            url = self._u(url)
        except Exception:
            return {"list": []}
        html = self._get(url)
        if not html:
            return {"list": []}
        return {"list": self._items(html)}

    def playerContent(self, flag, ids, vipFlags=None):
        url = ids
        if isinstance(ids, (list, tuple)):
            url = ids[0] if ids else ""
        url = str(url or "").strip()
        if not url:
            return {"parse": 0, "url": "", "header": dict(self.header)}
        if not url.startswith("http"):
            url = self._u(url)
        # 同一集常被壳重复请求（起播/切线路/重试），缓存直链省一次站点往返
        hit = self._pcache.get(url)
        if hit:
            return {"parse": 0, "url": hit, "header": dict(self.header)}
        u = url.lower()
        if u.endswith((".m3u8", ".mp4", ".flv", ".mkv", ".ts", ".avi")) or "m3u8" in u:
            return {"parse": 0, "url": url, "header": dict(self.header)}
        html = self._get(url, referer=self.site)
        if not html:
            return {"parse": 0, "url": "", "header": dict(self.header)}
        found = ""
        # MacCMS 播放页：var player_aaaa={"flag":"play",...,"url":"https:\/\/xxx\/index.m3u8",...}
        # 注意是 JSON 对象而非字符串赋值，且斜杠被转义成 \/
        if self.m3u8_var and self.m3u8_var != "regex":
            m = re.search(r'var\s+%s\s*=\s*([\s\S]{0,3000})' % re.escape(self.m3u8_var), html)
            if m:
                seg = m.group(1)
                um = re.search(r'"url"\s*:\s*"([^"]+)"', seg)
                if um:
                    found = um.group(1)
                if not found:
                    sm = re.search(r'^\s*["\']([^"\']+)', seg)
                    if sm:
                        found = sm.group(1)
        if not found:
            flat = html.replace("\\/", "/")
            m = re.search(r'"url"\s*:\s*"(https?://[^"]+\.m3u8[^"]*)"', flat) or \
                re.search(r'var\s+now\s*=\s*["\']([^"\']+)', flat) or \
                re.search(r'<source[^>]+src=["\']([^"\']+)', flat) or \
                re.search(r'(https?://[^\s"\']+\.m3u8[^\s"\']*)', flat)
            if m:
                found = m.group(1)
        if found:
            found = found.replace("\\/", "/")
        if not found:
            return {"parse": 0, "url": "", "header": dict(self.header)}
        found = found.strip()
        if found.startswith("//"):
            found = "https:" + found
        elif not found.startswith("http"):
            found = self._u(found)
        if len(self._pcache) > 300:
            self._pcache.clear()
        self._pcache[url] = found
        return {"parse": 0, "url": found, "header": dict(self.header)}

    def _pic(self, src):
        """图片地址：剥历史代理前缀 → 补全绝对 URL → 注入 @Referer/@User-Agent 后缀。

        ★图片 UA 墙正解（2026-10-12 悟空影视实证）：TVBox 生态图片请求头走
        URL 后缀约定，壳侧拆分后自动转成请求头：<URL>@Referer=<ref>@User-Agent=<ua>
        ★图片不走 localProxy：proxy:// / proxy?url= 前缀对图片无效（图片加载器
        只认 data: 与 @ 后缀，proxy 前缀会被当普通 URL 请求 → 404 空白）。
        ★幂等：重复解析不得叠加后缀；已带 proxy 前缀的（旧版遗留）先剥离
        （必须在 _u() 之前剥，否则 proxy?url= 会被当相对路径拼到域名后面）。
        """
        u = (src or "").strip()
        if not u:
            return u
        if u.startswith("proxy?url="):
            u = u[len("proxy?url="):]
            if "%" in u:
                import urllib.parse as _up
                u = _up.unquote(u)
        elif u.startswith("proxy://"):
            u = u[len("proxy://"):]
        u = self._u(u)
        if not u or not u.startswith("http"):
            return u
        # 本站图床 feifan.dz002.com 无防盗链（okhttp / 空 UA 均实测 200），
        # 直连即可；加 @ 后缀反而依赖壳的解析能力，能省则省。
        if not PIC_HEADER_SUFFIX:
            return u
        if ("@User-Agent=" in u or "@Referer=" in u
                or "@Cookie=" in u or "@Headers=" in u):
            return u
        ua = dict(self.header).get("User-Agent", "")
        return u + "@Referer=" + self.site + "/@User-Agent=" + ua

    def localProxy(self, param):
        """本地代理兜底（m3u8 分片/KEY、图片代取）。

        ★返回必须是 dict 契约 {'code','content','headers'}（71模板/FongMi 实证）；
        list 格式 [status, ctype, body, None] 壳不认，是资源空白的经典原因。
        ★参数兼容 dict(url/u/key 键) 与裸串；统一剥离 proxy?url=/proxy:// 前缀并 unquote。
        """
        import urllib.parse as _up
        u = ""
        if isinstance(param, dict):
            u = str(param.get("url") or param.get("u") or param.get("key") or "")
        else:
            u = str(param or "")
        if u.startswith("proxy?url="):
            u = u[len("proxy?url="):]
        elif "proxy?url=" in u:
            u = u.split("proxy?url=", 1)[-1]
        if u.startswith("proxy://"):
            u = u[len("proxy://"):]
        if "%" in u:
            u = _up.unquote(u)
        if not u or not u.startswith("http"):
            return {"code": 403, "content": b"", "headers": {}}
        h = dict(self.header)
        try:
            if self.s is not None:
                r = self.s.get(u, headers=h, timeout=15)
                if r.status_code == 200 and r.content:
                    return {"code": 200, "content": r.content,
                            "headers": {"Content-Type": r.headers.get("Content-Type", "application/octet-stream")}}
                return {"code": r.status_code, "content": b"", "headers": {}}
        except Exception:
            pass
        try:
            import urllib.request
            req = urllib.request.Request(u, headers=h)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                if body:
                    return {"code": 200, "content": body,
                            "headers": {"Content-Type": resp.headers.get("Content-Type", "application/octet-stream")}}
        except Exception:
            pass
        return {"code": 404, "content": b"", "headers": {}}


def _html_unescape(s):
    import html as _h
    return _h.unescape(s) if s else s
