# coding=utf-8
#!/usr/bin/python
# ---------------------------------------------------------------
# 香蕉视频（618013.xyz）—— 2026-11 适配版
# 站点改版要点：
#   1. 首页 / 搜索 已上 JS 反爬，纯 requests 只能拿到 506 字节跳转壳页；
#      分类页 /index.php/vod/type/id/{tid}.html 仍可直取，首页推荐改由分类页补。
#   2. 详情页 /index.php/vod/detail/id/{id}.html 已 500，真实“详情+播放入口”
#      就是列表里的 /html/{code}/{密文标题}.html?m={id}&b={封面} 壳页。
#   3. 取流接口 /api/v2/vod/reqplay/{id} 只在携带“播放页 Referer”时放行，
#      否则直接断连(10054)。实测 h5.xxoo168.org 可直取并返回 httpurl(m3u8)，
#      h5.xxoo475.org 在本机常被重置，故 168 排首位 + 每域名重试。
#      拿到候选流后先探一次 #EXTM3U/分片，可用就 parse=0 直出；
#      全部失败才回落播放页(parse=1)让客户端 WebView 嗅探。
# ---------------------------------------------------------------
import sys
sys.path.append('..')
from base.spider import Spider
import json
import time
import hashlib
import base64
import urllib.parse
import re
import requests
from lxml import etree


class Spider(Spider):

    # 站点与接口（都做了多值兜底，改版时只改这里）
    HOST = "https://618013.xyz"
    API_HOSTS = ["https://h5.xxoo168.org", "https://h5.xxoo475.org"]
    API_PATH = "/api/v2/vod/reqplay/%s"
    UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    def getName(self):
        return "香蕉视频"

    def init(self, extend=""):
        self.host = self.HOST
        if isinstance(extend, str) and extend.strip().startswith('http'):
            self.host = extend.strip().rstrip('/')
        self.api_hosts = list(self.API_HOSTS)
        self.headers = {
            'User-Agent': self.UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
            'Referer': self.host + '/'
        }
        self._meta = {}          # vid -> {name, pic, path}
        self._sess = requests.Session()
        self._sess.trust_env = False
        self._sess.headers.update(self.headers)
        self.log("香蕉视频初始化完成，主站: %s" % self.host)

    # ============ 基础工具 ============

    def fetch(self, url, headers=None, timeout=12, retry=1):
        """带重试的 GET；反爬壳页（<1KB 且无 vodbox）视为失败重试。"""
        for _ in range(max(1, retry)):
            try:
                r = self._sess.get(url, headers=headers or self.headers,
                                   timeout=timeout, verify=False)
                r.encoding = 'utf-8'
                if r.status_code == 200 and len(r.text) > 1024:
                    return r
            except Exception:
                pass
            time.sleep(0.4)
        return None

    def html(self, content):
        try:
            return etree.HTML(content)
        except Exception:
            return None

    def regStr(self, pattern, string, index=1):
        try:
            m = re.search(pattern, string, re.I)
            if m and len(m.groups()) >= index:
                return m.group(index)
        except Exception:
            pass
        return ""

    def isVideoFormat(self, url):
        return bool(url) and ('.m3u8' in url or '.mp4' in url)

    def manualVideoCheck(self):
        return False

    # ============ 分类 ============

    CLASSES = [
        ('1', '全部视频'), ('13', '香蕉精品'), ('22', '制服诱惑'), ('6', '国产视频'),
        ('8', '清纯少女'), ('9', '辣妹大奶'), ('10', '女同专属'), ('11', '素人出演'),
        ('12', '角色扮演'), ('20', '人妻熟女'), ('23', '日韩剧情'), ('21', '经典伦理'),
        ('7', '成人动漫'), ('14', '精品二区'), ('32', '国产自拍'),
    ]
    # 站点导航里还有 40 精品三区 / 53 动漫中字 / 52 日本无码 / 33 中文字幕 / 44 国产传媒，
    # 但实测这 5 个 type 页恒返回空列表（第 2 页同样为空），故不放出，免得点进去空白。

    def homeContent(self, filter):
        classes = [{'type_id': '%s_%s' % (self.host.replace('https://', ''), tid),
                    'type_name': name} for tid, name in self.CLASSES]
        result = {'class': classes}
        # 首页壳页已被反爬拦截，直接用「全部视频」分类页充当首页推荐
        videos, _ = self._list_by_type('1', '1')
        if not videos:
            rsp = self.fetch(self.host + '/')
            if rsp:
                videos = self._get_videos(self.html(rsp.text), limit=20)
        result['list'] = videos[:20]
        return result

    def homeVideoContent(self):
        videos, _ = self._list_by_type('1', '1')
        return {'list': videos[:20]}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            domain, type_id = tid.split('_', 1)
            type_id = type_id.rsplit('_', 1)[-1]        # 兼容 "域名_分类_子类"
        except Exception:
            domain, type_id = self.host.replace('https://', ''), str(tid)
        pg = str(pg or '1')
        videos, pagecount = self._list_by_type(type_id, pg, domain)
        return {
            'list': videos,
            'page': int(pg) if pg.isdigit() else 1,
            'pagecount': pagecount,
            'limit': 20,
            'total': pagecount * 20,
        }

    def _list_by_type(self, type_id, pg='1', domain=None):
        """取分类页并解析，返回 (列表, 总页数)。分页路径: /type/id/{tid}/page/{pg}.html"""
        domain = domain or self.host.replace('https://', '')
        base = 'https://%s/index.php/vod/type/id/%s' % (domain, type_id)
        url = base + '.html' if str(pg) == '1' else base + '/page/%s.html' % pg
        rsp = self.fetch(url, retry=2)
        if not rsp:
            return [], 1
        doc = self.html(rsp.text)
        videos = self._get_videos(doc)
        pagecount = 1 if not videos else 999
        m = re.search(r"totalPages\s*=\s*'?(\d+)'?", rsp.text)
        if m:
            try:
                pagecount = max(1, int(m.group(1)))
            except Exception:
                pass
        return videos, pagecount

    # ============ 列表解析 ============

    def _get_videos(self, doc, limit=None):
        """站点有两套列表模板：
           A 首页/多数分类: <a class="vodbox" href="/html/x/{密文}.html?m={id}&b={封面}">
           B 香蕉精品/国产自拍: <a class="thumbnail" href="/html/x/{密文}.html?v={m3u8}&b={token}">
           模板 B 的直链直接写在 v 参数里，不必再调取流接口。
        """
        if doc is None:
            return []
        try:
            elems = doc.xpath('//a[@class="vodbox"]')
            elems += doc.xpath('//a[contains(@class,"thumbnail")]')
            videos, seen = [], set()
            for elem in elems:
                v = self._extract_video(elem)
                if not v or v['vod_id'] in seen:
                    continue
                seen.add(v['vod_id'])
                videos.append(v)
            return videos[:limit] if limit else videos
        except Exception as e:
            self.log("获取影片列表出错: %s" % e)
            return []

    def _extract_video(self, element):
        try:
            href = element.xpath('./@href')
            if not href:
                return None
            href = href[0].replace('&amp;', '&')
            # 关闭播放入口：/html/xxx/{密文}.html?m=123&b=封面  或  ?v=直链&b=token
            path = href if href.startswith('/') else '/' + href.lstrip('./')
            # 路径含非 ASCII 密文，统一做一次百分号编码，避免客户端拼 URL 时抛异常
            path = urllib.parse.quote(path, safe="/:?=&%")

            stream = self._stream_of(href)          # 模板 B 自带 m3u8
            vid = self.regStr(r'[?&]m=(\d+)', href)
            if not vid:
                vid = 'k' + hashlib.md5(path.encode('utf-8')).hexdigest()[:12]

            title = ''
            t = element.xpath('./p[@class="km-script"]/text()')
            if not t:
                t = element.xpath('.//*[contains(@class,"km-script")]/text()') or \
                    element.xpath('.//p/text()') or element.xpath('.//h3/text()')
            if t:
                title = self._decrypt_title(t[0].strip())
            if not title:
                title = self._title_from_path(path) or ('视频%s' % vid)

            pic = ''
            p = element.xpath('.//img/@data-original') or element.xpath('.//img/@src')
            if p:
                pic = p[0].strip()
                if pic.startswith('//'):
                    pic = 'https:' + pic
                elif pic.startswith('/'):
                    pic = self.host + pic
            if not pic.startswith('http'):
                m = re.search(r'[?&]b=(https?://[^&\s]+)', href)
                pic = m.group(1) if m else ''
            pic = '' if not pic.startswith('http') else pic

            self._meta[vid] = {'name': title, 'pic': pic, 'path': path,
                               'stream': stream, 'id': vid}
            return {
                'vod_id': self._pack(vid, path, stream, title),
                'vod_name': title,
                'vod_pic': pic,
                'vod_remarks': '',
                'vod_year': '',
            }
        except Exception as e:
            self.log("提取影片信息出错: %s" % e)
            return None

    @staticmethod
    def _stream_of(href):
        """从 href 的 v 参数里取直链（没有就返回空）。"""
        m = re.search(r'[?&]v=(https?://[^&\s]+)', href)
        if not m:
            return ''
        u = m.group(1)
        if re.search(r'\.(m3u8|mp4)(\?|$)', u, re.I):
            return u
        return u if u.startswith('http') else ''

    def _decrypt_title(self, encrypted_text):
        """站点同款：逐字符与 0x80 异或（页面 UTF-8 编码下有效）。"""
        try:
            return ''.join(chr(ord(c) ^ 0x80) for c in encrypted_text)
        except Exception:
            return encrypted_text

    def _title_from_path(self, path):
        """从播放页文件名反解标题（文件名就是密文标题）。"""
        try:
            fn = path.split('?')[0].rsplit('/', 1)[-1]
            fn = urllib.parse.unquote(fn)
            if fn.endswith('.html'):
                fn = fn[:-5]
            if not fn or len(fn) < 2:
                return ''
            t = self._decrypt_title(fn)
            return t if re.search(r'[\u4e00-\u9fff]', t) else ''
        except Exception:
            return ''

    # ============ vod_id 编解码 ============
    # vod_id = "{vid}|{base64url(json:{p:播放页, s:直链, n:标题})}"
    # 客户端只当字符串透传，重启也能还原；旧格式(纯 path 的 b64)仍兼容

    def _pack(self, vid, path, stream='', title=''):
        info = {'p': path, 's': stream or '', 'n': title or ''}
        raw = json.dumps(info, ensure_ascii=False, separators=(',', ':'))
        b = base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii').rstrip('=')
        return '%s|%s' % (vid, b)

    def _unpack(self, vod_id):
        """返回 (vid, path, stream, name)；旧格式无 stream/name。"""
        s = str(vod_id)
        if '|' in s:
            vid, b = s.split('|', 1)
            pad = '=' * (-len(b) % 4)
            try:
                raw = base64.urlsafe_b64decode(b + pad).decode('utf-8')
            except Exception:
                return vid, '', '', ''
            if raw.startswith('{'):
                try:
                    info = json.loads(raw)
                except Exception:
                    return vid, '', '', ''
                return (vid, info.get('p', '') or '', info.get('s', '') or '',
                        info.get('n', '') or '')
            return vid, raw, '', ''
        return s, '', '', ''

    # ============ 详情 ============

    def detailContent(self, ids):
        try:
            vod_id = ids[0] if isinstance(ids, (list, tuple)) else ids
            if not str(vod_id).strip():
                return {'list': []}
            vid, path, stream, cached_name = self._unpack(vod_id)
            if not path:
                meta = self._meta.get(vid) or {}
                path = meta.get('path', '')
                stream = stream or meta.get('stream', '')
            page_url = (self.host + path) if path.startswith('/') else path

            meta = self._meta.get(vid) or {}
            name = cached_name or meta.get('name') or self._title_from_path(path) or ('视频%s' % vid)
            pic = meta.get('pic', '')
            if page_url:
                rsp = self.fetch(page_url, timeout=10)
                if rsp:
                    if not pic:
                        m = re.search(r'[?&]b=([^&"\']+)', page_url)
                        if m:
                            pic = urllib.parse.unquote(m.group(1))
                    t = re.search(r'<title[^>]*>(.*?)</title>', rsp.text, re.S)
                    if t and t.group(1).strip() and len(t.group(1).strip()) > 1:
                        name = t.group(1).strip()

            vod = {
                'vod_id': vod_id,
                'vod_name': name,
                'vod_pic': pic,
                'type_name': '',
                'vod_year': '',
                'vod_area': '',
                'vod_remarks': '',
                'vod_actor': '',
                'vod_director': '',
                'vod_content': name,
                'vod_play_from': '香蕉视频',
                'vod_play_url': '正片$%s' % vod_id,
            }
            return {'list': [vod]}
        except Exception as e:
            self.log("详情获取出错: %s" % e)
            return {'list': []}

    # ============ 搜索 ============
    # 站点搜索页已被反爬接管（恒返回 506 字节壳页 / 500），这里走分类页近似匹配：
    # 在「全部视频」前几页里按标题包含过滤，能搜到近期内容。

    def searchContent(self, key, quick, pg="1"):
        try:
            key_l = str(key).lower()
            out = []
            for p in range(1, 4):
                videos, _ = self._list_by_type('1', str(p))
                for v in videos:
                    if key_l in str(v.get('vod_name', '')).lower():
                        out.append(v)
                if out:
                    break
            return {'list': out[:40], 'page': 1, 'pagecount': 1,
                    'limit': len(out[:40]), 'total': len(out)}
        except Exception as e:
            self.log("搜索出错: %s" % e)
            return {'list': []}

    # ============ 播放 ============

    def playerContent(self, flag, id, vipFlags=None):
        try:
            vid, path, stream, _name = self._unpack(id)
            page_url = (self.host + path) if path.startswith('/') else path
            if not page_url:
                page_url = '%s/html/play.html?m=%s' % (self.host, vid)

            header = {'User-Agent': self.UA}

            # 0) 模板 B 的列表已带直链，探一下可用就直接播（最快，不依赖接口）
            if stream and self._probe_stream(stream):
                self.log("列表直链可用: %s" % stream)
                return {'parse': 0, 'playUrl': '', 'url': stream, 'header': header}

            # 1) 官方取流接口：拿到 m3u8/mp4 直链即 parse=0
            url = self._req_play(vid, page_url)
            if url:
                return {'parse': 0, 'playUrl': '', 'url': url, 'header': header}

            # 2) 回落播放页：交给客户端 WebView 跑站点 JS 后嗅探直链
            if not path:
                return {'parse': 0, 'playUrl': '', 'url': '',
                        'msg': '取流失败：缺少播放入口，请从分类页重新进入该片'}
            self.log("取流接口未通过校验，回落播放页: %s" % page_url)
            return {'parse': 1, 'playUrl': '', 'url': page_url,
                    'header': {'User-Agent': self.UA, 'Referer': page_url}}
        except Exception as e:
            self.log("播放链接获取出错: %s" % e)
            vid, path, _s, _n = self._unpack(id)
            page_url = (self.host + path) if path.startswith('/') else path
            return {'parse': 1, 'playUrl': '', 'url': page_url,
                    'header': {'User-Agent': self.UA}}

    @staticmethod
    def _pick_urls(j, data):
        """从接口 JSON 里收集所有候选流地址（正片优先，试看次之）。"""
        out = []
        if data.get('httpurl'):
            out.append(data.get('httpurl'))
        for it in (data.get('httpurls') or []):
            if isinstance(it, dict):
                u = it.get('httpurl') or it.get('url')
                if u:
                    out.append(u)
        if j.get('retcode') == 3 and data.get('httpurl_preview'):
            out.append(data.get('httpurl_preview'))
        return out

    @staticmethod
    def _clean_url(u):
        u = str(u or '').strip().replace('\\/', '/').replace('?300', '')
        return u if u.startswith('http') else ''

    def _probe_stream(self, url, timeout=6):
        """轻量探针：读到 #EXTM3U / TS 同步字节 / ftyp 才算可用。"""
        try:
            r = self._sess.get(url, headers={'User-Agent': self.UA}, timeout=timeout,
                               verify=False, stream=True, allow_redirects=True)
            try:
                if r.status_code not in (200, 206):
                    return False
                chunk = b''
                for part in r.iter_content(1024):
                    chunk += part
                    if len(chunk) >= 600:
                        break
            finally:
                r.close()
            head = chunk[:600]
            if b'#EXTM3U' in head.upper():
                return True
            if head[:1] == b'\x47':          # MPEG-TS 同步字节
                return True
            if b'ftyp' in head[:64]:         # MP4
                return True
            u = head.upper()
            return b'#EXT-X' in u or b'#EXTINF' in u
        except Exception:
            return False

    def _req_play(self, vid, page_url):
        """多域名 × 多 Referer × 重试 取流；返回探针校验通过的直链。

        关键：接口只在 Referer 为「真实播放页」时放行，缺 Referer 直接断连。
        """
        vid = str(vid)
        refs = []
        for r in (page_url, self.host + '/'):
            if r and r not in refs:
                refs.append(r)

        first_cand = ''
        for api in self.api_hosts:
            api_url = api + (self.API_PATH % vid)
            got_json = False
            for _ in range(3):
                for ref in refs:
                    h = dict(self.headers)
                    h.update({'Referer': ref, 'Origin': self.host,
                              'Accept': 'application/json, text/plain, */*'})
                    try:
                        r = self._sess.get(api_url, headers=h, timeout=8,
                                           verify=False, allow_redirects=True)
                    except Exception:
                        continue
                    if r.status_code != 200:
                        continue
                    try:
                        j = r.json()
                    except Exception:
                        continue
                    data = j.get('data') or {}
                    got_json = True
                    for c in self._pick_urls(j, data):
                        c = self._clean_url(c)
                        if not c:
                            continue
                        if not first_cand:
                            first_cand = c
                        if self._probe_stream(c):
                            self.log("取流成功(%s): %s" % (api.replace('https://', ''), c))
                            return c
                if got_json:
                    break            # 该域名接口通了但流不可用，换下一个域名
                time.sleep(0.25)
        if first_cand:
            self.log("探针未通过，直接用候选直链: %s" % first_cand)
        return first_cand

    # ============ 兼容壳 ============

    def localProxy(self, param):
        return None
