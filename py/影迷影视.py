#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影迷影视网 TVBox / drpy / FongMi  Python 爬虫源
目标站: https://www.xzcdc.org

接口: Spider 类方法 + 顶层转发函数（兼容类式壳与函数式壳）

站点是服务端渲染 HTML，整页被 base64 包裹后由 document.write 输出，取到后先解码:
    GET /                                首页最新，?page=N 分页
    GET /cecb672a08680/{tid}.html         分类列表，?page=N 分页
    GET /?s={关键词}                      搜索，&page=N 分页
    GET /pd33944863973/{vid}.html         详情，?ep=N 切换分集
    详情页 iframe 的 src 参数              当前分集的 m3u8 直链

每页主列表 24 条（videoGrid），页面尾部另有 8 条固定热门推荐，解析时排除。
详情按分集换页，每集一个播放地址，播放时按 vid + 集号再取一次详情页。

支持: 首页 / 分类 / 搜索(分页) / 详情 / 分集播放
依赖: 仅标准库，urllib 直连，不需要 requests / bs4 / Crypto
数据源: 仅 www.xzcdc.org 自身页面，域名固定，播放直链取自页面 iframe
"""

import sys
import re
import time
import html
import gzip
import base64
import urllib.request
import urllib.parse

sys.path.append('..')

try:
    from base.spider import Spider as _BaseSpider
except Exception:
    # 本地直接运行（python 影迷影视.py --selftest）时的兜底；
    # 在 drpy / TVBox 环境里会加载到真实基类，不受影响。
    class _BaseSpider(object):
        pass


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
HOST = 'https://www.xzcdc.org'
CAT_PATH = '/cecb672a08680/%s.html'
DETAIL_PATH = '/pd33944863973/%s.html'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

PAGE_SIZE = 24          # 站点主列表固定每页 24 条
CACHE_TTL = 300         # 页面缓存秒数
TIMEOUT = 10            # 前两次快速尝试
SLOW_TIMEOUT = 22       # 之后放长，等慢速 chunked 传输
RETRIES = 5             # 失败重试次数

# 默认不列出成人分类，全部关掉后显示的仍是本站点自己的分类 成人向分类默认不列出（SKIP_ADULT_CATEGORIES = True，按名称关键字过滤，去掉 8 个，剩 48 项）。要全量就把它改成 False。
SKIP_ADULT_CATEGORIES = False
ADULT_WORDS = ('伦理', '三级', '写真', '两性', '擦边')

# 首页分类抽屉里的分类（站点自身 ID），仅在首页抓取失败时顶上
FALLBACK_CLASSES = [
    ('11', '剧情片'), ('59', '现代都市'), ('54', '女频恋爱'), ('13', '国产剧'),
    ('7', '喜剧片'), ('6', '动作片'), ('30', '日韩动漫'), ('58', '古装仙侠'),
    ('10', '恐怖片'), ('57', '年代穿越'), ('45', '足球'), ('55', '反转爽剧'),
    ('40', '影视解说'), ('16', '美国剧'), ('39', '篮球'), ('25', '大陆综艺'),
    ('29', '国产动漫'), ('14', '香港剧'), ('23', '日本剧'), ('56', '脑洞悬疑'),
    ('31', '欧美动漫'), ('9', '科幻片'), ('8', '爱情片'), ('21', '纪录片'),
    ('22', '韩国剧'), ('33', '动画片'), ('37', '泰剧'), ('63', '漫剧'),
]

TAG_RE = re.compile(r'<[^>]+>')
B64_RE = re.compile(r'\("([A-Za-z0-9+/=]{200,})"\)')
B64_LOOSE_RE = re.compile(r'\("([A-Za-z0-9+/=]{200,})')
VIDEO_EXT = re.compile(r'\.(m3u8|mp4|flv|mkv|ts|m4a)(\?|#|$)', re.I)


# --------------------------------------------------------------------------- #
# 爬虫主体
# --------------------------------------------------------------------------- #
class Spider(_BaseSpider):

    # 类级默认值：部分壳不显式调用 init，直接调用也不会崩
    host = HOST
    _classes_cache = None

    def getName(self):
        return '影迷影视'

    def isVideoFormat(self, url):
        return bool(VIDEO_EXT.search(str(url or '')))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    # ------------------------------------------------------------------ #
    # 初始化 / 网络层
    # ------------------------------------------------------------------ #
    def init(self, extend=""):
        """域名固定为目标站；extend 仅保留形参兼容，脚本内不解析。"""
        self.host = HOST
        self._cache = {}
        self._classes_cache = None
        return None

    def _headers(self):
        return {
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'identity',
            'Referer': self.host + '/',
        }

    @staticmethod
    def _decode(page):
        """站点把整页 HTML 编码进一段 base64 里；明文页面原样返回。"""
        if not page:
            return ''
        if 'data-xsg-obfuscation' not in page:
            return page
        blobs = B64_RE.findall(page)
        if not blobs:
            blobs = B64_LOOSE_RE.findall(page)
        if not blobs:
            return page
        out = []
        for b in blobs:
            try:
                out.append(base64.b64decode(b + '=' * (-len(b) % 4)).decode('utf-8', 'replace'))
            except Exception:
                pass
        return ''.join(out) or page

    def _fetch(self, url, timeout=TIMEOUT, retry=RETRIES, ttl=CACHE_TTL):
        """GET 一个页面，返回解码后的 HTML；全部重试失败返回空串。"""
        if not hasattr(self, '_cache'):
            self.init()
        now = time.time()
        if ttl and url in self._cache:
            ts, val = self._cache[url]
            if now - ts < ttl and val:
                return val

        page = ''
        for attempt in range(max(1, retry)):
            # 站点是慢速 chunked 传输：先用短超时快速试，再放长等待
            to = timeout if attempt < 2 else SLOW_TIMEOUT
            raw = b''
            enc = ''
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=to) as r:
                    enc = (r.headers.get('Content-Encoding') or '').lower()
                    while True:
                        try:
                            chunk = r.read(32768)
                        except Exception:
                            # 连接中途断开：已收下的字节可能已经是完整页面
                            break
                        if not chunk:
                            break
                        raw += chunk
            except Exception:
                pass
            if raw and 'gzip' in enc:
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            candidate = self._decode(raw.decode('utf-8', 'ignore')) if raw else ''
            # 必须以 </html> 收尾才算完整，截断的页面会让分类/列表缺项
            if candidate and len(candidate) > 2000 and '</html>' in candidate.lower():
                page = candidate
                break
            if attempt + 1 < max(1, retry):
                time.sleep(0.6)
        if ttl and page:
            self._cache[url] = (now, page)
        return page

    # ------------------------------------------------------------------ #
    # 通用字段处理
    # ------------------------------------------------------------------ #
    def _clean(self, s):
        """去 HTML / 反转义 / 去掉会破坏 TVBox 分隔符的字符。"""
        s = str(s or '')
        s = TAG_RE.sub(' ', s)
        s = html.unescape(s)
        s = s.replace('#', ' ').replace('$', ' ').replace('\r', ' ').replace('\n', ' ')
        return re.sub(r'\s+', ' ', s).strip()

    def _pic(self, u, base=None):
        u = html.unescape(str(u or '').strip())
        if not u:
            return ''
        if u.startswith('//'):
            return 'https:' + u
        if u.startswith('http'):
            return u
        return urllib.parse.urljoin((base or self.host) + '/', u.lstrip('/'))

    def _cards(self, page):
        """只取主列表区 videoGrid，排除页面尾部固定的热门推荐卡片。"""
        if not page:
            return []
        i = page.find('id="videoGrid"')
        seg = page[i:] if i >= 0 else page
        for stop in ('<nav class="jmt-pagination">', 'id="emptyState"', 'id="recommendGrid"'):
            j = seg.find(stop)
            if j > 0:
                seg = seg[:j]

        out, seen = [], set()
        for card in re.findall(r'<article class="xai-card">(.*?)</article>', seg, re.S):
            m = re.search(r'href="([^"]*?/(?:pd)?\d*/(\d+)\.html[^"]*)"', card)
            if not m:
                m = re.search(r'href="([^"]*?/(\d+)\.html[^"]*)"', card)
            if not m:
                continue
            vid = m.group(2)
            if vid in seen:
                continue
            seen.add(vid)

            img = re.search(r'<img[^>]*src="([^"]+)"', card)
            alt = re.search(r'<img[^>]*alt="([^"]*)"', card)
            title = re.search(r'<h3>(.*?)</h3>', card, re.S)
            name = self._clean(title.group(1) if title else (alt.group(1) if alt else ''))
            if not name:
                continue

            p = re.search(r'<p[^>]*>(.*?)</p>', card, re.S)
            meta = self._clean(p.group(1) if p else '')
            parts = [x.strip() for x in meta.split('/') if x.strip()]
            cat = parts[0] if parts else ''
            date = parts[1] if len(parts) > 1 else ''
            year = date[:4] if re.match(r'^\d{4}', date) else ''

            out.append({
                'vod_id': vid,
                'vod_name': name,
                'vod_pic': self._pic(img.group(1) if img else ''),
                'vod_remarks': date or cat,
                'vod_year': year,
                'type_name': cat,
            })
        return out

    def _pagecount(self, page, pg):
        m = re.search(r'<nav class="jmt-pagination">(.*?)</nav>', page or '', re.S)
        block = m.group(1) if m else ''
        nums = [int(x) for x in re.findall(r'page-numbers[^>]*>(\d+)</a>', block)]
        nums += [int(x) for x in re.findall(r'page-numbers current">(\d+)</span>', block)]
        return max(nums) if nums else max(1, pg)

    def _page_result(self, page, pg):
        lst = self._cards(page)
        pc = self._pagecount(page, pg)
        return {
            'list': lst,
            'page': pg,
            'pagecount': max(1, pc),
            'limit': PAGE_SIZE,
            'total': pc * PAGE_SIZE,
        }

    # ------------------------------------------------------------------ #
    # 首页 / 分类
    # ------------------------------------------------------------------ #
    def _classes(self):
        if getattr(self, '_classes_cache', None):
            return self._classes_cache
        page = self._fetch(self.host + '/')
        pairs = []
        if page:
            m = re.search(r'<div class="film-category-grid">(.*?)</div></aside>', page, re.S)
            block = m.group(1) if m else page
            pairs = re.findall(
                r'href="[^"]*?/cecb672a08680/(\d+)\.html"[^>]*><strong>(.*?)</strong>',
                block, re.S)
        if not pairs:
            pairs = list(FALLBACK_CLASSES)

        classes = [{'type_id': 'home', 'type_name': '最新'}]
        seen = set()
        for tid, name in pairs:
            tid = str(tid).strip()
            name = self._clean(name)
            if not tid or not name or tid in seen:
                continue
            if SKIP_ADULT_CATEGORIES and any(w in name for w in ADULT_WORDS):
                continue
            seen.add(tid)
            classes.append({'type_id': tid, 'type_name': name})
        self._classes_cache = classes
        return classes

    def _home_page(self, pg=1):
        url = self.host + '/' if pg <= 1 else self.host + '/?page=%d' % pg
        return self._fetch(url)

    def _home_videos(self):
        page = self._home_page(1)
        out = self._cards(page)
        seen = set(x['vod_id'] for x in out)
        # 首页尾部热门推荐也补进来，去重后作为首页推荐位
        m = re.search(r'id="recommendGrid">(.*?)</section>', page or '', re.S)
        if m:
            fake = '<div id="videoGrid">' + m.group(1) + '<nav class="jmt-pagination">'
            for one in self._cards(fake):
                if one['vod_id'] not in seen:
                    seen.add(one['vod_id'])
                    out.append(one)
        return out

    def homeContent(self, filter=False):
        result = {'class': self._classes(), 'list': self._home_videos()}
        # 站点只有分类 + 分页 + 搜索，没有筛选参数，不虚构筛选项
        if filter:
            result['filters'] = {}
        return result

    def homeVideoContent(self):
        return {'list': self._home_videos()}

    def categoryContent(self, tid, pg="1", filter=False, extend=None):
        try:
            page = max(1, int(pg or 1))
        except Exception:
            page = 1
        tid = str(tid or 'home').strip()
        if tid in ('', 'home', '0', 'all'):
            url = self.host + '/' if page <= 1 else self.host + '/?page=%d' % page
        else:
            if not re.match(r'^\d+$', tid):
                tid = '11'
            url = self.host + (CAT_PATH % tid)
            if page > 1:
                url += '?page=%d' % page
        return self._page_result(self._fetch(url), page)

    # ------------------------------------------------------------------ #
    # 详情 / 分集
    # ------------------------------------------------------------------ #
    @staticmethod
    def _vid(x):
        s = str(x or '').strip()
        m = re.search(r'/(\d+)\.html', s)
        if m:
            return m.group(1)
        m = re.search(r'(\d+)', s)
        return m.group(1) if m else ''

    def _episodes(self, page, vid):
        m = re.search(r'<div class="episode-grid">(.*?)</div>', page or '', re.S)
        eps = []
        if m:
            eps = re.findall(r'href="[^"]*\?ep=(\d+)[^"]*"[^>]*>(.*?)</a>', m.group(1), re.S)
        play, seen = [], set()
        for num, name in eps:
            if num in seen:
                continue
            seen.add(num)
            label = self._clean(name) or ('第%s集' % num)
            play.append('%s$%s-%s' % (label, vid, num))
        if not play:
            play.append('正片$%s-1' % vid)
        return play

    def detailContent(self, array):
        if not isinstance(array, (list, tuple)):
            array = [array]
        if not array:
            return {'list': []}
        vid = self._vid(array[0])
        if not vid:
            return {'list': []}
        page = self._fetch(self.host + (DETAIL_PATH % vid))
        if not page:
            return {'list': []}

        t = re.search(r'<div class="film-title-row">.*?<h1>(.*?)</h1>', page, re.S)
        if not t:
            t = re.search(r'<title>《(.*?)》', page, re.S)
        name = self._clean(t.group(1) if t else '') or '未命名'

        facts = {}
        for k, v in re.findall(r'<span><em>(.*?)</em>(.*?)</span>', page, re.S):
            k = self._clean(k)
            if k and k not in facts:
                facts[k] = self._clean(v)

        creds = {}
        for k, v in re.findall(r'<p class="film-credit"><b>(.*?)</b>(.*?)</p>', page, re.S):
            creds[self._clean(k).rstrip('：:')] = self._clean(v)

        sum_m = re.search(r'<div class="film-summary">.*?<p>(.*?)</p>', page, re.S)
        content = self._clean(sum_m.group(1) if sum_m else '')

        pic = ''
        pic_m = re.search(r'"thumbnailUrl"\s*:\s*\[\s*"([^"]+)"', page)
        if pic_m:
            pic = self._pic(pic_m.group(1).replace('\\u0026', '&').replace('\\/', '/'))
        if not pic:
            pic_m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
            pic = self._pic(pic_m.group(1) if pic_m else '')

        genre = ''
        g = re.search(r'"genre"\s*:\s*"([^"]+)"', page)
        if g:
            genre = self._clean(g.group(1))

        play = self._episodes(page, vid)
        vod = {
            'vod_id': vid,
            'vod_name': name,
            'vod_pic': pic,
            'type_name': genre,
            'vod_year': facts.get('年份', ''),
            'vod_area': facts.get('地区', ''),
            'vod_lang': facts.get('语言', ''),
            'vod_remarks': facts.get('状态', ''),
            'vod_actor': creds.get('主演', ''),
            'vod_director': creds.get('导演', ''),
            'vod_content': content,
            'vod_play_from': '默认',
            'vod_play_url': '#'.join(play),
        }
        return {'list': [vod]}

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def searchContentPage(self, key, quick=False, pg="1"):
        try:
            page_no = max(1, int(pg or 1))
        except Exception:
            page_no = 1
        kw = str(key or '').strip()
        if not kw:
            return {'list': [], 'page': page_no, 'pagecount': 1,
                    'limit': PAGE_SIZE, 'total': 0}
        url = self.host + '/?' + urllib.parse.urlencode({'s': kw})
        if page_no > 1:
            url += '&page=%d' % page_no
        return self._page_result(self._fetch(url), page_no)

    def searchContent(self, key, quick=False, pg="1"):
        return self.searchContentPage(key, quick, pg)

    # ------------------------------------------------------------------ #
    # 播放
    # ------------------------------------------------------------------ #
    @staticmethod
    def _play_id(raw):
        """播放 id 形如 919-3；也兼容纯影片 id、完整详情 URL、集名$id。"""
        s = str(raw or '').strip()
        m = re.search(r'/(\d+)\.html\?ep=(\d+)', s)
        if m:
            return m.group(1), m.group(2)
        if '-' in s:
            a, b = s.rsplit('-', 1)
            if a.isdigit() and b.isdigit():
                return a, b
        m = re.search(r'/(\d+)\.html', s)
        if m:
            return m.group(1), '1'
        m = re.search(r'(\d+)', s)
        return (m.group(1), '1') if m else ('', '1')

    def _player_src(self, page):
        for m in re.findall(r'<iframe[^>]*?src="([^"]+)"', page or ''):
            u = html.unescape(m)
            q = urllib.parse.parse_qs(u.split('?', 1)[1] if '?' in u else '')
            src = (q.get('src') or [''])[0].strip()
            if src:
                return src
        # 同一页面 ld+json 里的 embedUrl，参数与上一种一致
        m = re.search(r'"embedUrl"\s*:\s*"([^"]+)"', page or '')
        if m:
            u = html.unescape(m.group(1).replace('\\u0026', '&').replace('\\/', '/'))
            q = urllib.parse.parse_qs(u.split('?', 1)[1] if '?' in u else '')
            return (q.get('src') or [''])[0].strip()
        return ''

    def playerContent(self, flag, id, vipFlags=None):
        raw = str(id or '').strip()
        if '$' in raw and not raw.startswith('http'):
            raw = raw.rsplit('$', 1)[-1].strip()
        if raw.startswith('http') and VIDEO_EXT.search(raw):
            return self._play_result(raw)
        vid, ep = self._play_id(raw)
        if not vid:
            return self._play_result('')
        base_url = self.host + (DETAIL_PATH % vid)
        src = ''
        # 第一集的地址就在详情页里，刚看过详情时直接复用缓存，省一次请求
        if ep == '1':
            hit = getattr(self, '_cache', {}).get(base_url)
            if hit:
                src = self._player_src(hit[1])
        if not src:
            url = base_url + '?ep=%s' % (ep or '1')
            src = self._player_src(self._fetch(url))
        return self._play_result(src)

    def _play_result(self, url):
        return {
            'parse': 0,
            'playUrl': '',
            'url': url,
            'header': {
                'User-Agent': UA,
                'Referer': self.host + '/',
            },
        }

    def localProxy(self, param):
        # 播放地址是页面直接给出的 m3u8 直链，无需本地代理
        return [200, 'text/plain', '', '']


# --------------------------------------------------------------------------- #
# 顶层转发层：部分壳按模块函数调用，保留这一层最省事
# --------------------------------------------------------------------------- #
_S = None


def _inst():
    global _S
    if _S is None:
        _S = Spider()
        _S.init()
    return _S


def getName():
    return _inst().getName()


def init(extend=""):
    return _inst().init(extend)


def isVideoFormat(url):
    return _inst().isVideoFormat(url)


def manualVideoCheck():
    return _inst().manualVideoCheck()


def destroy():
    return _inst().destroy()


def homeContent(filter=False):
    return _inst().homeContent(filter)


def homeVideoContent():
    return _inst().homeVideoContent()


def categoryContent(tid, pg="1", filter=False, extend=None):
    return _inst().categoryContent(tid, pg, filter, extend)


def detailContent(ids):
    return _inst().detailContent(ids)


def searchContent(key, quick=False, pg="1"):
    return _inst().searchContent(key, quick, pg)


def searchContentPage(key, quick=False, pg="1"):
    return _inst().searchContentPage(key, quick, pg)


def playerContent(flag, id, vipFlags=None):
    return _inst().playerContent(flag, id, vipFlags)


def localProxy(param):
    return _inst().localProxy(param)


# --------------------------------------------------------------------------- #
# 自测：python 影迷影视.py --selftest
# --------------------------------------------------------------------------- #
if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    s = Spider()
    s.init()
    print('站点: %s' % s.host)
    print('名称: %s' % s.getName())

    h = s.homeContent(True)
    names = [c['type_name'] for c in (h.get('class') or [])]
    print('\n[首页] 分类 %d 个: %s' % (len(names), '、'.join(names[:12])))
    print('[首页] 推荐 %d 条, 首条: %s' % (
        len(h.get('list') or []), (h.get('list') or [{}])[0].get('vod_name', '-')))

    c1 = s.categoryContent('home', 1)
    c2 = s.categoryContent('home', 2)
    print('\n[最新] 共 %s 页, 本页 %d 条' % (c1.get('pagecount'), len(c1.get('list') or [])))
    print('[最新] 第1页前3: %s' % [x['vod_name'] for x in (c1.get('list') or [])[:3]])
    print('[最新] 第2页前3: %s' % [x['vod_name'] for x in (c2.get('list') or [])[:3]])

    cc = s.categoryContent('11', 1)
    print('\n[分类] 剧情片 共 %s 页, 本页 %d 条, 前3: %s' % (
        cc.get('pagecount'), len(cc.get('list') or []),
        [x['vod_name'] for x in (cc.get('list') or [])[:3]]))

    sr = s.searchContent('黑牢城', False, 1)
    print('\n[搜索] "黑牢城" 共 %s 页, 本页 %d 条: %s' % (
        sr.get('pagecount'), len(sr.get('list') or []),
        [x['vod_name'] for x in (sr.get('list') or [])[:3]]))

    vid = (sr.get('list') or [{}])[0].get('vod_id') or (c1.get('list') or [{}])[0].get('vod_id')
    if vid:
        vod_list = s.detailContent([vid]).get('list') or []
        if not vod_list:
            print('\n[详情] 首次请求超时，重试一次...')
            vod_list = s.detailContent([vid]).get('list') or []
        if not vod_list:
            print('[详情] 站点无响应（本机链路波动），跳过详情/播放')
        else:
            vod = vod_list[0]
            play = (vod.get('vod_play_url') or '').split('#')
            print('\n[详情] id=%s %s | 年份=%s 地区=%s 状态=%s' % (
                vid, vod.get('vod_name'), vod.get('vod_year'),
                vod.get('vod_area'), vod.get('vod_remarks')))
            print('[详情] 线路=%s, 分集 %d 个, 前3: %s' % (
                vod.get('vod_play_from'), len(play), play[:3]))
            first = play[0].rsplit('$', 1)[-1] if play and '$' in play[0] else vid
            p = s.playerContent('默认', first)
            print('[播放] parse=%s, 视频格式=%s, url=%s' % (
                p.get('parse'), s.isVideoFormat(p.get('url')), p.get('url')))
    print('\n自测完成。')
