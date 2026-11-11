#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘聚合.py — 17 站聚合版（结构参照 短剧聚合.py）

胶囊行 = 玩偶/木偶/多多/欧歌/二小/虎斑/快映/花卷/夸父/123/卡卡/即刻/指南/双星/爱盼/觅盘/趣盘
点胶囊切换站点看分类/海报；搜索一次搜全部站，结果角标带站名；
UC 解析共用一份；UC 播放只走无限免登匿名直链（与 jar 的 ff() 一致），123 云盘免登直链。

JSON 线路写法（配置里只挂这一条聚合线路即可）：
{
  "key": "网盘聚合",
  "name": "网盘",
  "type": 3,
  "api": "./py/网盘聚合.py",
  "searchable": 1,
  "quickSearch": 1,
  "filterable": 1,
  "changeable": 1,
  "ext": {
    "uc_cookie": "",
    "夸父cookie": "bbs_sid=xxx; bbs_token=xxx",
    "123cookie": "bbs_sid=xxx; bbs_token=xxx"
  }
}
说明：UC 无限免登 = jar 的 ff() 链路（pc-api/file/download 匿名直链）。
123 云盘分享直链免登可用。
"""

import sys
import os
import json
import time
import hashlib
import re
from urllib.parse import quote, unquote, urlencode
from concurrent.futures import ThreadPoolExecutor, wait, as_completed

import requests
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    HTTPAdapter = None
    Retry = None

sys.path.append('..')
try:
    from base.spider import Spider
except ImportError:
    class Spider:
        def init(self, extend=""):
            pass

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 11; M2007J3SC Build/RKQ1.200826.002; wv) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/77.0.3865.120 "
             "MQQBrowser/6.2 TBS/045714 Mobile Safari/537.36")
PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
         "Safari/537.36 Channel/pckk_other_ch")
TV_UA = ("Mozilla/5.0 (Linux; U; Android 12; zh-cn; V2238A Build/V417IR) "
         "AppleWebKit/533.1 (KHTML, like Gecko) Mobile Safari/533.1")
DART_UA = "Dart/2.19(dart:io)"

VIDEO_EXT = ('.mp4', '.mkv', '.ts', '.iso', '.rmvb', '.avi', '.mov', '.mpeg', '.mpg',
             '.wmv', '.3gp', '.asf', '.m4v', '.flv', '.vob', '.strm', '.tp', '.f4v',
             '.webm', '.m2ts', '.ogv')

# ---------------------------------------------------------------------------
# 网盘链接正则（详情页通用提取）
# ---------------------------------------------------------------------------
PAN_RES = [
    ("UC",   re.compile(r"https?://drive\.uc\.cn/s/[0-9a-zA-Z]+(?:\?pwd=\w+)?")),
    ("夸克",  re.compile(r"https?://pan\.quark\.cn/s/[0-9a-zA-Z]+(?:\?pwd=\w+)?")),
    ("百度",  re.compile(r"https?://pan\.baidu\.com/s/[0-9a-zA-Z_\-]+(?:\?pwd=\w+)?")),
    ("123",  re.compile(r"https?://(?:www\.)?(?:123pan|123684|123865|123912|123592)\.(?:com|cn)/s/[\w\-]+(?:\?[^\"'<>\s]*)?")),
    ("迅雷",  re.compile(r"https?://pan\.xunlei\.com/s/[\w\-]+")),
    ("天翼",  re.compile(r"https?://cloud\.189\.cn/(?:t/|web/share\?code=)[\w]+")),
    ("阿里",  re.compile(r"https?://(?:www\.)?(?:aliyundrive|alipan)\.com/s/[\w]+")),
    ("移动",  re.compile(r"https?://caiyun\.139\.com/[\w/]+")),
]


def make_session(ua=MOBILE_UA):
    s = requests.Session()
    s.headers.update({"User-Agent": ua})
    if HTTPAdapter is not None and Retry is not None:
        try:
            retry = Retry(total=1, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503])
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=32)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
        except Exception:
            pass
    return s


def _ensure_url(site):
    """懒探测镜像：首次用到该站时才 HEAD 探测（3s 超时），加载期零网络"""
    if site.get("_url_ok"):
        return
    ok = False
    for u in site["site"]:
        try:
            site["session"].head(u, timeout=1.5, allow_redirects=True)
            site["url"] = u
            ok = True
            break
        except Exception:
            continue
    site["_url_ok"] = True
    if not ok:
        # 全部镜像都连不上：熔断 5 分钟，之后每次加载/搜索直接跳过不再白等
        site["_dead_until"] = time.time() + 300


def http_get(session, url, timeout=8, headers=None, binary=False):
    r = session.get(url, timeout=timeout, headers=headers or {})
    r.encoding = 'utf-8'
    return r.content if binary else r.text




# ---------------------------------------------------------------------------
# UC 网盘解析（共用一份，来自原 网盘搜索.py）
# ---------------------------------------------------------------------------
class UCDrive:
    PC_API = "https://pc-api.uc.cn/1/clouddrive"
    OPEN_API = "https://open-api-drive.uc.cn"
    SHARE_RE = re.compile(r"https?://drive\.uc\.cn/s/([0-9a-zA-Z]+)(?:\?pwd=(\w+))?")

    def __init__(self, cookie=""):
        self.cookie = cookie
        self._stoken_cache = {}
        self._machine_code = None

    # ---------- 公共 ----------
    def _headers_common(self):
        return {"Referer": "https://drive.uc.cn", "Cookie": self.cookie or "", "User-Agent": PC_UA}

    def _headers_anon(self):
        return {"User-Agent": PC_UA, "Accept-Encoding": "identity", "Referer": "https://drive.uc.cn"}

    def _post_json(self, url, body, headers=None, timeout=20):
        hdrs = {"User-Agent": PC_UA, "Content-Type": "application/json; charset=utf-8"}
        if headers:
            hdrs.update(headers)
        resp = requests.post(url, json=body, headers=hdrs, timeout=timeout)
        resp.encoding = "utf-8"
        return resp.text

    def share_token(self, pwd_id, passcode=""):
        url = f"{self.PC_API}/share/sharepage/token?__t={int(time.time() * 1000)}"
        body = {"pwd_id": pwd_id, "passcode": passcode or ""}
        text = self._post_json(url, body, self._headers_common())
        data = json.loads(text)
        stoken = (data.get("data") or {}).get("stoken", "")
        if stoken:
            self._stoken_cache[pwd_id] = stoken
        return data, stoken

    def _get_stoken(self, pwd_id, passcode=""):
        if pwd_id in self._stoken_cache:
            return self._stoken_cache[pwd_id]
        _, stoken = self.share_token(pwd_id, passcode)
        return stoken

    def list_videos(self, share_url, passcode="", limit=0):
        """返回 [{'fid','file_name','size','share_fid_token','pwd_id','stoken'}]
        limit>0 时找到 limit 个视频即停（验证场景免整树遍历）"""
        m = self.SHARE_RE.search(share_url or "")
        if not m:
            return []
        pwd_id, url_pwd = m.group(1), m.group(2)
        pc = passcode or url_pwd or ""
        try:
            stoken = self._get_stoken(pwd_id, pc)
        except Exception:
            return []
        if not stoken:
            return []
        out = []
        self._walk(pwd_id, stoken, "0", out, depth=0, limit=limit)
        return out

    def _walk(self, pwd_id, stoken, pdir_fid, out, depth, limit=0):
        if depth > 12 or (limit and len(out) >= limit):
            return
        page = 1
        while True:
            url = (f"{self.PC_API}/share/sharepage/detail?pr=UCBrowser&fr=pc"
                   f"&pwd_id={pwd_id}&stoken={quote(stoken)}&pdir_fid={pdir_fid}"
                   f"&force=0&_page={page}&_size=50&_sort=file_type:asc,file_name:asc")
            r = requests.get(url, headers=self._headers_common(), timeout=10)
            r.encoding = "utf-8"
            data = r.json()
            items = (data.get("data") or {}).get("list") or []
            if not items:
                break
            for it in items:
                if it.get("dir"):
                    self._walk(pwd_id, stoken, it.get("fid", "0"), out, depth + 1, limit)
                elif it.get("file") and it.get("category") in (1, 2):
                    out.append({"fid": it.get("fid", ""),
                                "file_name": it.get("file_name", "未命名"),
                                "size": it.get("size", 0),
                                "share_fid_token": it.get("share_fid_token", ""),
                                "pwd_id": pwd_id, "stoken": stoken})
                if limit and len(out) >= limit:
                    return
            if len(items) < 50:
                break
            page += 1

    # ---------- 播放链 1：匿名直链（无限） ----------
    def download_url_anon(self, pwd_id, fid, fid_token):
        """就是 jar 的 ff()：匿名免登拿 download_url，失败返回空串"""
        try:
            stoken = self._get_stoken(pwd_id) or ""
        except Exception:
            stoken = ""
        body = {"fids": [fid], "fids_token": [fid_token], "pwd_id": pwd_id,
                "stoken": stoken, "speedup_session": ""}
        url = f"{self.PC_API}/file/download?pr=UCBrowser&fr=pc"
        text = self._post_json(url, body, self._headers_anon())
        data = json.loads(text)
        arr = data.get("data") or []
        if not arr:
            return ""
        return arr[0].get("download_url", "") or ""

    def play_anonymous(self, pwd_id, fid, fid_token):
        """jar 的 q() 无限分支：裸直链 + f() 头，关键是 Referer = download_url + '\\'（CDN 回调鉴权认它）"""
        dl = self.download_url_anon(pwd_id, fid, fid_token)
        if not dl:
            return {"parse": 0, "url": "", "header": self._headers_anon()}
        return {"parse": 0, "url": dl,
                "header": {"User-Agent": PC_UA, "Accept-Encoding": "identity", "Referer": dl + "\\"}}

    def stream_alive(self, dl, timeout=6):
        """探测 CDN 直链是否真能流出字节（Range 0-0 头探测，与播放同请求头）。
        脚本就跑在播放设备上：这里连不上/被拒，播放器一样缓冲死 → 判不可播"""
        if not dl:
            return False
        try:
            r = requests.get(dl, headers={"User-Agent": PC_UA, "Accept-Encoding": "identity",
                                          "Referer": dl + "\\", "Range": "bytes=0-0"},
                             stream=True, timeout=timeout)
            ok = r.status_code in (200, 206)
            r.close()
            return ok
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 123 云盘解析（免登直链，来自 jar merge/b/m.java 移植）
# ---------------------------------------------------------------------------
class Pan123:
    SHARE_RE = re.compile(r"https?://(?:www\.)?(?:123pan|123684|123865|123912|123592)\.(?:com|cn)/s/([\w\-]+)")

    def _headers(self):
        return {"referer": "https://www.123pan.com", "origin": "https://www.123pan.com",
                "platform": "open_platform", "user-agent": DART_UA, "app-version": "3"}

    def _parse(self, share_url):
        m = self.SHARE_RE.search(share_url or "")
        if not m:
            return "", ""
        key = m.group(1)
        if key.endswith(".html"):
            key = key[:-5]
        pwd = ""
        pm = re.search(r"[?&](?:pwd|SharePwd)=(\w+)", share_url or "")
        if pm:
            pwd = pm.group(1)
        return key, pwd

    def list_videos(self, share_url):
        """递归拿分享内视频：返回 [{'file_id','file_name','size','etag','s3key','download_url'}]"""
        key, pwd = self._parse(share_url)
        if not key:
            return []
        out = []
        self._walk(key, pwd, 0, out, depth=0)
        return out

    def _walk(self, key, pwd, parent_id, out, depth):
        if depth > 12:
            return
        page = 1
        while True:
            url = (f"https://www.123pan.cn/b/api/share/get?limit=100&Page={page}&next=-1"
                   f"&orderBy=file_name&orderDirection=asc&parentFileId={parent_id}"
                   f"&shareKey={key}&SharePwd={pwd}")
            r = requests.get(url, headers=self._headers(), timeout=15)
            r.encoding = "utf-8"
            try:
                data = r.json()
            except Exception:
                break
            items = ((data.get("data") or {}).get("InfoList")) or []
            if not items:
                break
            for it in items:
                cat = it.get("Category", -1)
                name = it.get("FileName", "")
                if cat == 2 or name.lower().endswith(VIDEO_EXT):
                    out.append({"file_id": it.get("FileId", ""),
                                "file_name": name or "未命名",
                                "size": it.get("Size", 0),
                                "etag": it.get("Etag", ""),
                                "s3key": it.get("S3KeyFlag", ""),
                                "download_url": it.get("DownloadUrl", "")})
                elif it.get("Type") == 1 or it.get("Type") == 0 and not it.get("DownloadUrl"):
                    self._walk(key, pwd, it.get("FileId", 0), out, depth + 1)
            if len(items) < 100:
                break
            page += 1

    @staticmethod
    def play_direct(download_url):
        u = (download_url or "").replace("_24_24", "")
        u = u.replace("&cache_type=1&w=24&h=24&trade_key=123pan-thumbnail&type=video", "")
        return {"parse": 0, "url": u,
                "header": {"referer": "https://www.123pan.com", "origin": "https://www.123pan.com",
                           "platform": "android", "user-agent": DART_UA, "app-version": "3"}}


# ---------------------------------------------------------------------------
# 站点配置（18 站，镜像来自 jar 配置）
# ---------------------------------------------------------------------------
SITES = [
    {"id": "玩偶",  "name": "玩偶", "type": "wogg",
     "site": ["https://www.wogg.net", "https://wogg.xxooo.cf", "https://woggpan.xxooo.cf"]},
    {"id": "木偶",  "name": "木偶", "type": "webshare",
     "site": ["https://123.666291.xyz", "https://666.666291.xyz",
              "https://www.muou.site", "https://www.muou.asia"]},
    {"id": "多多",  "name": "多多", "type": "webshare",
     "site": ["https://tv.yydsys.top", "https://tv.yydsys.cc", "https://tv.214521.xyz"]},
    {"id": "欧歌",  "name": "欧歌", "type": "webshare",
     "site": ["https://woog.nxog.eu.org", "https://woog.nxog.fun", "https://woog.430520.xyz"]},
    {"id": "二小",  "name": "二小", "type": "webshare",
     "site": ["https://www.2xiaopan.top", "https://2xiaopan.top",
              "https://www.erxiaozhan.top", "https://www.2xiaozhan.top", "https://wexwp.cc"]},
    {"id": "虎斑",  "name": "虎斑", "type": "webshare", "site": ["http://38.76.197.172:16969"]},
    {"id": "快映",  "name": "快映", "type": "webshare", "site": ["http://38.76.197.172:12521"]},
    {"id": "花卷",  "name": "花卷", "type": "webshare", "site": ["https://www.hjzhencai.top"]},
    {"id": "夸父",  "name": "夸父", "type": "bbs", "site": ["https://www.kfzy.net"],
     "search_style": "path",
     "uc_cats": {"1": "9", "2": "1", "3": "4", "4": "3"},  # 电影/剧集/综艺/动漫
     "cats": [{"n": "剧集", "v": "1"}, {"n": "短剧", "v": "2"}, {"n": "动漫", "v": "3"},
              {"n": "综艺", "v": "4"}, {"n": "电影", "v": "9"}]},
    {"id": "123",   "name": "123", "type": "bbs",
     "site": ["https://123panfx.com", "https://pan1.me"],
     "search_style": "query",
     "uc_cats": {"1": "2", "2": "48", "3": "52", "4": "37"},  # 电影/剧集/综艺/动漫
     "cats": [{"n": "电影", "v": "2"}, {"n": "剧集", "v": "48"}, {"n": "4K原盘", "v": "56"},
              {"n": "综艺", "v": "52"}, {"n": "动漫", "v": "37"}]},
    {"id": "卡卡",  "name": "卡卡", "type": "kkpan", "site": ["https://www.kkpans.com"]},
    {"id": "即刻",  "name": "即刻", "type": "jike", "site": ["https://api.uuuka.com"]},
    {"id": "指南",  "name": "指南", "type": "zhinan", "site": ["https://4kzn.cc"]},
    {"id": "双星",  "name": "双星", "type": "shuangxing", "site": ["https://1.star2.cn"]},
    {"id": "爱盼",  "name": "爱盼", "type": "aipan", "site": ["https://www.aipan.me"]},
    {"id": "觅盘",  "name": "觅盘", "type": "mipan", "site": ["https://www.mipan.so"]},
    {"id": "趣盘",  "name": "趣盘", "type": "qupan", "site": ["https://b.funletu.com"]},
    {"id": "盘搜",  "name": "盘搜", "type": "pansou", "site": ["https://pansou.noisework.cn"]},
    {"id": "盘搜2", "name": "盘搜2", "type": "pansou", "site": ["https://so.252035.xyz"]},
]

FILTERS_WEBSHARE = {
    "cateId": [{"n": "电影", "v": "1"}, {"n": "剧集", "v": "2"}, {"n": "综艺", "v": "3"},
               {"n": "动漫", "v": "4"}, {"n": "纪录片", "v": "5"}, {"n": "短剧", "v": "26"}],
    "class": [{"n": "全部", "v": ""}, {"n": "Netflix", "v": "Netflix"},
              {"n": "惊悚", "v": "惊悚"}, {"n": "悬疑", "v": "悬疑"}, {"n": "剧情", "v": "剧情"},
              {"n": "灾难", "v": "灾难"}, {"n": "冒险", "v": "冒险"}, {"n": "动作", "v": "动作"},
              {"n": "科幻", "v": "科幻"}, {"n": "喜剧", "v": "喜剧"}, {"n": "爱情", "v": "爱情"},
              {"n": "奇幻", "v": "奇幻"}, {"n": "恐怖", "v": "恐怖"}, {"n": "战争", "v": "战争"},
              {"n": "犯罪", "v": "犯罪"}, {"n": "动画", "v": "动画"}, {"n": "古装", "v": "古装"},
              {"n": "武侠", "v": "武侠"}, {"n": "权谋", "v": "权谋"}, {"n": "家庭", "v": "家庭"},
              {"n": "历史", "v": "历史"}, {"n": "传记", "v": "传记"}, {"n": "警匪", "v": "警匪"},
              {"n": "江湖", "v": "江湖"}, {"n": "职场", "v": "职场"}, {"n": "音乐", "v": "音乐"},
              {"n": "歌舞", "v": "歌舞"}, {"n": "运动", "v": "运动"}, {"n": "励志", "v": "励志"},
              {"n": "同xing", "v": "同xing"}, {"n": "换身", "v": "换身"}],
    "area": [{"n": "全部", "v": ""}, {"n": "大陆", "v": "大陆"}, {"n": "香港", "v": "香港"},
             {"n": "台湾", "v": "台湾"}, {"n": "美国", "v": "美国"}, {"n": "韩国", "v": "韩国"},
             {"n": "日本", "v": "日本"}, {"n": "英国", "v": "英国"}, {"n": "泰国", "v": "泰国"},
             {"n": "法国", "v": "法国"}, {"n": "印度", "v": "印度"}, {"n": "西班牙", "v": "西班牙"},
             {"n": "俄罗斯", "v": "俄罗斯"}, {"n": "加拿大", "v": "加拿大"},
             {"n": "澳大利亚", "v": "澳大利亚"}, {"n": "意大利", "v": "意大利"},
             {"n": "德国", "v": "德国"}, {"n": "其他", "v": "其他"}],
    "year": [{"n": "全部", "v": ""}] + [{"n": str(y), "v": str(y)} for y in range(2026, 1999, -1)],
    "by": [{"n": "最新", "v": "time"}, {"n": "最热", "v": "hits"}, {"n": "高分好评", "v": "score"}],
}

FILTERS_WOGG = {
    "cateId": [{"n": "热门电影", "v": "dianying"}, {"n": "剧集", "v": "juji"},
               {"n": "综艺", "v": "zongyi"}, {"n": "动漫", "v": "dongman"},
               {"n": "纪录片", "v": "jilupian"}, {"n": "短剧", "v": "duanjv"},
               {"n": "演唱会", "v": "yanchanghui"}],
    "class": [{"n": "全部", "v": ""}, {"n": "动作", "v": "动作"}, {"n": "喜剧", "v": "喜剧"},
              {"n": "爱情", "v": "爱情"}, {"n": "科幻", "v": "科幻"}, {"n": "恐怖", "v": "恐怖"},
              {"n": "剧情", "v": "剧情"}, {"n": "战争", "v": "战争"}, {"n": "警匪", "v": "警匪"},
              {"n": "犯罪", "v": "犯罪"}, {"n": "动画", "v": "动画"}, {"n": "奇幻", "v": "奇幻"},
              {"n": "武侠", "v": "武侠"}, {"n": "冒险", "v": "冒险"}, {"n": "枪战", "v": "枪战"},
              {"n": "悬疑", "v": "悬疑"}, {"n": "惊悚", "v": "惊悚"}, {"n": "经典", "v": "经典"},
              {"n": "青春", "v": "青春"}, {"n": "文艺", "v": "文艺"}, {"n": "古装", "v": "古装"},
              {"n": "历史", "v": "历史"}, {"n": "运动", "v": "运动"}, {"n": "纪录片", "v": "纪录片"}],
    "area": [{"n": "全部", "v": ""}, {"n": "大陆", "v": "大陆"}, {"n": "香港", "v": "香港"},
             {"n": "台湾", "v": "台湾"}, {"n": "美国", "v": "美国"}, {"n": "法国", "v": "法国"},
             {"n": "英国", "v": "英国"}, {"n": "日本", "v": "日本"}, {"n": "韩国", "v": "韩国"},
             {"n": "德国", "v": "德国"}, {"n": "泰国", "v": "泰国"}, {"n": "印度", "v": "印度"},
             {"n": "意大利", "v": "意大利"}, {"n": "西班牙", "v": "西班牙"},
             {"n": "加拿大", "v": "加拿大"}, {"n": "其他", "v": "其他"}],
    "year": [{"n": "全部", "v": ""}] + [{"n": str(y), "v": str(y)} for y in range(2026, 1999, -1)],
    "by": [{"n": "最新", "v": "time"}, {"n": "最热", "v": "hits"}, {"n": "高分好评", "v": "score"}],
}


# ---------------------------------------------------------------------------
# 各站点适配器（输入 site 配置 + session，输出统一结构）
# ---------------------------------------------------------------------------
def _abs(base, href):
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    if href.startswith("http"):
        return href
    return base + "/" + href


PLAY2DETAIL_RE = re.compile(r"/(?:index\.php/)?vod/play/id/(\d+)[^\"']*")


def _to_detail_url(url):
    """苹果CMS 播放页链接 → 详情页链接"""
    m = PLAY2DETAIL_RE.search(url or "")
    if not m:
        return url
    base = url.split("/index.php")[0].split("/vod/")[0]
    return f"{base}/index.php/vod/detail/id/{m.group(1)}.html"


def _extract_pans(text):
    """从任意 HTML/JSON 文本里提取网盘链接 → {pan名: [url,...]}"""
    found = {}
    for name, rx in PAN_RES:
        for m in rx.finditer(text or ""):
            u = m.group(0).rstrip("\\\"'.,;)")
            if u not in found.setdefault(name, []):
                found[name].append(u)
    return found


# ----- 苹果CMS 模板（webshare / wogg） -----
def maccms_categories(site):
    if site["type"] == "wogg":
        return FILTERS_WOGG["cateId"]
    return FILTERS_WEBSHARE["cateId"]


# 标准分类 id → 名称别名（用于匹配各站导航里的真实分类名）
CAT_ALIASES = {
    "1":  ("电影", "影片"),
    "2":  ("剧集", "电视剧", "连续剧"),
    "3":  ("综艺", "娱乐"),
    "4":  ("动漫", "动画", "卡通"),
    "5":  ("纪录片", "纪录"),
    "26": ("短剧", "微短剧"),
}
_CAT_SKIP_PREFIX = ("更多", "全部", "查看", "首页")


def webshare_type_map(site):
    """懒探测 webshare 站真实分类表 {标准cateId: 真实id}。
    各苹果CMS站分类 id 是后台自配的，全不一样（如木偶 综艺=29/短剧无，多多 短剧=5/纪录=20）。
    返回 None=探测失败（退回静态表）；dict=探测成功（缺的标准分类=该站没有）。"""
    if "_typemap" in site:
        return site["_typemap"]
    tmap = None
    try:
        _ensure_url(site)
        pairs = []
        for path in ("/", "/index.php/vod/show/id/1.html"):
            try:
                html = http_get(site["session"], site["url"] + path, timeout=8)
            except Exception:
                continue
            if BeautifulSoup is not None:
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.select("a[href*='/vod/type/id/'], a[href*='/vod/show/id/']"):
                    m = re.search(r"/id/(\d+)", a.get("href", ""))
                    name = (a.get("title") or a.get_text(strip=True) or "").strip()
                    if m and name:
                        pairs.append((m.group(1), name))
            else:
                for m in re.finditer(r'<a[^>]*href="[^"]*/vod/(?:type|show)/id/(\d+)[^"]*"[^>]*>(.*?)</a>',
                                     html, re.S):
                    name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                    if name:
                        pairs.append((m.group(1), name))
            if pairs:
                break
        clean, seen = [], set()
        for rid, name in pairs:
            name = name.replace("\xa0", "").replace("&nbsp;", "").strip()
            if not name or name.startswith(_CAT_SKIP_PREFIX) or (rid, name) in seen:
                continue
            seen.add((rid, name))
            clean.append((rid, name))
        if clean:
            tmap = {}
            for std, aliases in CAT_ALIASES.items():
                for rid, name in clean:  # 页面顺序，首个命中优先
                    if any(a in name for a in aliases):
                        tmap.setdefault(std, rid)
                        break
            if not tmap:
                # 分类名被混淆（虎斑的风花雪月 / 快映的梅兰竹菊）：按惯例 1/2/3/4=电影/剧集/综艺/动漫
                ids = {rid for rid, _ in clean}
                if {"1", "2", "3", "4"} <= ids:
                    tmap = {"1": "1", "2": "2", "3": "3", "4": "4"}
    except Exception:
        tmap = None
    site["_typemap"] = tmap
    return tmap


def resolve_webshare_cat(site, std_id):
    """标准分类 id → 该站真实 id；None 表示该站无此分类（宁缺毋错，不再错显）"""
    tmap = webshare_type_map(site)
    if tmap is None:
        return str(std_id)  # 探测失败，退回旧行为
    return tmap.get(str(std_id))


def maccms_category_url(site, cat_id, pg, extend):
    extend = extend or {}
    cls = quote(extend.get("class", "") or "")
    area = quote(extend.get("area", "") or "")
    year = extend.get("year", "") or ""
    by = extend.get("by", "") or ""
    if site["type"] == "wogg":
        # 标准 vodshow 位序：id-area-by-class-lang-letter-level-page-year
        return f"{site['url']}/vodshow/{cat_id}-{area}-{by}-{cls}-----{pg}---{year}.html"
    if cls or area or year or by:
        # 苹果CMS 命名段路由，空段省略（实测有效），规范顺序 area/by/class/id/page/year
        u = f"{site['url']}/index.php/vod/show"
        if area:
            u += f"/area/{area}"
        if by:
            u += f"/by/{by}"
        if cls:
            u += f"/class/{cls}"
        u += f"/id/{cat_id}/page/{pg}"
        if year:
            u += f"/year/{year}"
        return u + ".html"
    return f"{site['url']}/index.php/vod/show/id/{cat_id}/page/{pg}.html"


def maccms_parse_list(site, html):
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vods = []
    for item in soup.select(".module-item"):
        link = item.select_one(".module-item-pic a") or item.select_one("a")
        if link is None and item.name == "a" and item.get("href"):
            link = item  # 花卷等模板：.module-item 直接挂在 <a> 上
        title = item.select_one(".module-item-title") or item.select_one(".module-poster-item-title")
        pic = item.select_one(".module-item-pic img") or item.select_one("img")
        remark = item.select_one(".module-item-text") or item.select_one(".module-item-note")
        if link and (title or link.get("title")):
            vods.append({
                "vod_id": _to_detail_url(_abs(site["url"], link.get("href", ""))),
                "vod_name": (title.get_text(strip=True) if title else "") or link.get("title", ""),
                "vod_pic": (pic.get("data-src") or pic.get("data-original") or pic.get("src")) if pic else "",
                "vod_remarks": remark.get_text(strip=True) if remark else "",
            })
    if not vods:  # wogg 布局差异兜底
        for item in soup.select(".module-card-item, .module-search-item, .module-items .module-item"):
            link = item.select_one("a")
            pic = item.select_one("img")
            if link and link.get("href"):
                name = link.get("title") or (pic.get("alt") if pic else "") or link.get_text(strip=True)
                vods.append({"vod_id": _to_detail_url(_abs(site["url"], link["href"])),
                             "vod_name": name,
                             "vod_pic": (pic.get("data-src") or pic.get("src")) if pic else "",
                             "vod_remarks": ""})
    return vods


def maccms_category(site, tid, pg, extend):
    cat_id = (extend or {}).get("cateId") or maccms_categories(site)[0]["v"]
    if site["type"] == "webshare":
        cat_id = resolve_webshare_cat(site, cat_id)
        if cat_id is None:
            return []  # 该站没有这个分类（如木偶无短剧），不再错显其他内容
    html = http_get(site["session"], maccms_category_url(site, cat_id, pg, extend or {}))
    return maccms_parse_list(site, html)


def maccms_search(site, key, pg):
    if site["type"] == "wogg":
        url = f"{site['url']}/vodsearch/-------------.html?wd={quote(key)}&submit="
        html = http_get(site["session"], url)
        return maccms_parse_list(site, html)
    url = f"{site['url']}/index.php/vod/search/page/{pg}/wd/{quote(key)}.html"
    html = http_get(site["session"], url)
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vods = []
    for card in soup.select(".module-search-item"):
        link = card.select_one("a")
        title = card.select_one("h3") or card.select_one(".module-item-title")
        pic = card.select_one("img")
        remark = card.select_one(".video-info-aux") or card.select_one(".module-item-text")
        if link and title:
            vods.append({
                "vod_id": _to_detail_url(_abs(site["url"], link.get("href", ""))),
                "vod_name": title.get_text(strip=True),
                "vod_pic": (pic.get("data-src") or pic.get("src")) if pic else "",
                "vod_remarks": remark.get_text(strip=True) if remark else "",
            })
    if not vods:
        vods = maccms_parse_list(site, html)
    return vods


def maccms_detail_pans(site, detail_url):
    """从详情页提取各网盘分享链接"""
    html = http_get(site["session"], _to_detail_url(detail_url), timeout=10)
    found = {}
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select(".module-row-text"):
            link_elem = row.select_one("[data-clipboard-text]")
            if link_elem:
                url = link_elem.get("data-clipboard-text", "")
                for name, rx in PAN_RES:
                    if rx.search(url):
                        found.setdefault(name, [])
                        if url not in found[name]:
                            found[name].append(url)
    if not found:  # 兜底：整页正则
        found = _extract_pans(html)
    return found, html


# ----- 卡卡（kkpans API） -----
def kkpan_search(site, key, pg):
    url = f"{site['url']}/api/resources/public?page={pg}&limit=24&search={quote(key)}"
    data = json.loads(http_get(site["session"], url))
    return [{"vod_id": f"kk:{it.get('id')}",
             "vod_name": it.get("file_name", ""),
             "vod_pic": "",
             "vod_remarks": str(round((it.get("file_size") or 0) / 1073741824, 1)) + "G"}
            for it in (data.get("data") or [])]


def kkpan_category(site, tid, pg, extend):
    url = f"{site['url']}/api/resources/public?page={pg}&limit=24"
    data = json.loads(http_get(site["session"], url))
    return [{"vod_id": f"kk:{it.get('id')}",
             "vod_name": it.get("file_name", ""),
             "vod_pic": "",
             "vod_remarks": (it.get("created_at") or "")[:10]}
            for it in (data.get("data") or [])]


def kkpan_detail(site, real_id):
    rid = real_id.split(":", 1)[-1]
    data = json.loads(http_get(site["session"], f"{site['url']}/api/resources/public/{rid}"))
    d = data.get("data") or {}
    pans = {}
    link = d.get("share_link", "")
    if link:
        for name, rx in PAN_RES:
            if rx.search(link):
                pans[name] = [link]
    if not pans:
        pans = _extract_pans(json.dumps(d, ensure_ascii=False))
    return pans, d.get("description", "") or d.get("file_name", "")


# ----- 即刻（uuuka API） -----
def jike_search(site, key, pg):
    data = json.loads(http_get(site["session"], f"{site['url']}/api/search?keyword={quote(key)}"))
    items = ((data.get("data") or {}).get("items")) or []
    return [{"vod_id": it.get("source_link", ""),
             "vod_name": re.sub(r"</?em>", "", it.get("title", "")),
             "vod_pic": "",
             "vod_remarks": it.get("update_time", "")[:16]}
            for it in items if it.get("source_link")]


def pansou_search(site, key, pg):
    data = json.loads(http_get(site["session"], f"{site['url']}/api/search?kw={quote(key)}"))
    mbt = ((data.get("data") or {}).get("merged_by_type")) or {}
    vods = []
    for it in (mbt.get("uc") or [])[:30]:
        if it.get("password"):
            continue  # 带提取码的免登解不了，跳过
        u = (it.get("url") or "").strip()
        if "drive.uc.cn/s/" not in u:
            continue
        vods.append({"vod_id": u, "vod_name": (it.get("note") or u).strip(),
                     "vod_pic": "", "vod_remarks": "UC免登"})
    return vods


def jike_category(site, tid, pg, extend):
    data = json.loads(http_get(site["session"], f"{site['url']}/api/contents/?page={pg}"))
    items = ((data.get("data") or {}).get("items")) or []
    return [{"vod_id": it.get("source_link", ""),
             "vod_name": re.sub(r"</?em>", "", it.get("title", "")),
             "vod_pic": "",
             "vod_remarks": it.get("update_time", "")[:16]}
            for it in items if it.get("source_link")]


# ----- 趣盘（funletu API） -----
def qupan_search(site, key, pg):
    body = {"keyword": key, "categoryid": 0, "filetypeid": 1, "courseid": 1,
            "page": pg, "pageSize": 15, "sortBy": "sort", "order": "desc", "offset": 0}
    r = site["session"].post(f"{site['url']}/search", json=body, timeout=8,
                             headers={"Origin": "https://b.funletu.com",
                                      "Referer": "https://b.funletu.com/search"})
    data = r.json()
    if data.get("code") != 200:
        return []
    vods = []
    for it in ((data.get("data") or {}).get("list") or []):
        u = it.get("url", "")
        if not u:
            continue
        vods.append({"vod_id": u,
                     "vod_name": re.sub(r"</?em>", "", it.get("title", "")),
                     "vod_pic": "",
                     "vod_remarks": it.get("updatetime", "")})
    return vods


# ----- 爱盼（aipan API） -----
def aipan_search(site, key, pg):
    data = json.loads(http_get(site["session"], f"{site['url']}/api/movies/search?q={quote(key)}"))
    return [{"vod_id": f"ap:{m.get('id')}",
             "vod_name": m.get("title", ""),
             "vod_pic": m.get("cover", ""),
             "vod_remarks": m.get("subtitle", "") or str(m.get("rate", ""))}
            for m in (data.get("movies") or [])]


def aipan_category(site, tid, pg, extend):
    data = json.loads(http_get(site["session"],
                               f"{site['url']}/api/movies/list?page={pg}&pageSize=30"))
    items = data.get("movies") or (data.get("data") or {}).get("movies") or []
    return [{"vod_id": f"ap:{m.get('id')}",
             "vod_name": m.get("title", ""),
             "vod_pic": m.get("cover", ""),
             "vod_remarks": str(m.get("rate", ""))}
            for m in items]


def aipan_detail(site, real_id):
    rid = real_id.split(":", 1)[-1]
    data = json.loads(http_get(site["session"], f"{site['url']}/api/movies/detail/{rid}"))
    pans = {}
    for pl in (data.get("panLinks") or []):
        u = pl.get("url") or pl.get("link") or ""
        if u:
            for name, rx in PAN_RES:
                if rx.search(u):
                    pans.setdefault(name, [])
                    if u not in pans[name]:
                        pans[name].append(u)
    if not pans:
        pans = _extract_pans(json.dumps(data, ensure_ascii=False))
    return pans, data.get("synopsis", "")


# ----- 觅盘（mipan，已换 PanSou 系后端，需 token） -----
def _mipan_token(site):
    if site.get("_token"):
        return site["_token"]
    try:
        r = site["session"].get(f"{site['url']}/api/token", timeout=5)
        d = r.json()
        site["_token"] = d.get("token") or (d.get("data") or {}).get("token", "")
    except Exception:
        site["_token"] = ""
    return site["_token"]


def mipan_search(site, key, pg):
    token = _mipan_token(site)
    if not token:
        return []
    # 新后端关键字参数是 kw（旧的 keyword 被忽略会返回全站 dump）；
    # 结果是 {"data": {"total": N, "merged_by_type": {uc: [...], "123": [...], ...}}}
    r = site["session"].post(f"{site['url']}/api/search",
                             json={"kw": key, "page": pg},
                             headers={"x-mipan-token": token}, timeout=8)
    data = r.json()
    d = data.get("data") or {}
    if isinstance(d, list):  # 旧版扁平列表兜底
        pairs = [(None, it) for it in d]
    else:
        mbt = d.get("merged_by_type") or {}
        pairs = [(typ, it) for typ, arr in mbt.items() for it in (arr or [])]
    vods, seen = [], set()
    for typ, it in pairs:
        if not isinstance(it, dict):
            continue
        raw = it.get("url") or it.get("link") or ""
        pwd = it.get("password") or ""
        u, pan = "", ""
        for name, rx in PAN_RES:
            m = rx.search(raw)
            if m:
                u, pan = m.group(0), name
                break
        if pan not in ("UC", "123"):
            continue  # 其余网盘需登录转存，免登播不了，不出
        if pan == "UC" and pwd and "?pwd=" not in u:
            u += f"?pwd={pwd}"  # 提取码并进链接，下游 UC 解析/验播都认
        if u in seen:
            continue
        seen.add(u)
        title = re.sub(r"</?em>", "", it.get("note") or it.get("title") or it.get("name") or "")
        vods.append({"vod_id": u, "vod_name": title.strip() or u, "vod_pic": "",
                     "vod_remarks": (it.get("datetime") or it.get("time") or "")[:16]})
        if len(vods) >= 30:
            break
    return vods


# ----- 指南（wordpress） -----
ZHINAN_CATS = [{"n": "最新", "v": "zuixin"}, {"n": "电影", "v": "dianying"},
               {"n": "剧集", "v": "juji"}, {"n": "TOP250", "v": "top250"}]


def zhinan_category(site, tid, pg, extend):
    cat = (extend or {}).get("cateId") or "zuixin"
    url = f"{site['url']}/books/{cat}" + (f"/page/{pg}" if pg > 1 else "")
    html = http_get(site["session"], url)
    return _wp_parse(site, html)


def zhinan_search(site, key, pg):
    html = http_get(site["session"], f"{site['url']}/?s={quote(key)}" + (f"&paged={pg}" if pg > 1 else ""))
    return _wp_parse(site, html)


def _wp_parse(site, html):
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vods = []
    for a in soup.select("a[href*='/book']"):
        href = a.get("href", "")
        if not re.search(r"/book[^/]*/\d+[^/]*\.html$|/books?/\d+", href):
            continue
        img = a.select_one("img")
        title = a.get("title") or (img.get("alt") if img else "") or a.get_text(strip=True)
        if not title or len(title) < 2:
            continue
        vods.append({"vod_id": _abs(site["url"], href), "vod_name": title,
                     "vod_pic": (img.get("src") or img.get("data-src")) if img else "",
                     "vod_remarks": ""})
    seen, out = set(), []
    for v in vods:
        if v["vod_id"] not in seen:
            seen.add(v["vod_id"])
            out.append(v)
    return out


# ----- 双星（star2.cn） -----
def shuangxing_search(site, key, pg):
    html = http_get(site["session"], f"{site['url']}/search/?keyword={quote(key)}&page={pg}")
    return _html_link_parse(site, html)


def shuangxing_category(site, tid, pg, extend):
    html = http_get(site["session"], f"{site['url']}/?page={pg}")
    return _html_link_parse(site, html)


def _html_link_parse(site, html):
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vods, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("javascript", "#", "mailto")):
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            img = a.find("img")
            title = (img.get("alt") or "") if img else ""
        if not title or len(title) < 4:
            continue
        full = _abs(site["url"], href)
        if full in seen or full.rstrip("/") == site["url"]:
            continue
        seen.add(full)
        img = a.find("img")
        vods.append({"vod_id": full, "vod_name": title[:80],
                     "vod_pic": (img.get("src") or img.get("data-src")) if img else "",
                     "vod_remarks": ""})
    return vods[:60]


# ----- Xiuno BBS 论坛站（夸父 / 123分享，移植自 PanWebShareKF/PanWebShare123） -----
BBS_REPLY_TEXTS = ["感谢楼主分享！", "这个资源太棒了！", "已收藏，谢谢！",
                   "不错的资源，支持一下", "楼主辛苦了！", "内容很有用，感谢分享"]
ALERT_SEL = ("div.message > div > div.alert, div.message > div.alert, "
             "div.message > section > div.alert, div.message > h5 > div.alert")


def _bbs_headers(site):
    h = {"Referer": site["url"], "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(site.get("extra_headers") or {})
    return h


def bbs_category(site, tid, pg, extend):
    cat = (extend or {}).get("cateId") or site["cats"][0]["v"]
    html = http_get(site["session"], f"{site['url']}/forum-{cat}-{pg}.htm",
                    headers=_bbs_headers(site))
    return _bbs_parse(site, html)


def bbs_search(site, key, pg):
    if site.get("search_style") == "path":
        url = f"{site['url']}/search-{quote(key)}-{pg}.htm"
    else:
        url = f"{site['url']}/search.htm?keyword={quote(key)}&page={pg}"
    html = http_get(site["session"], url, headers=_bbs_headers(site))
    return _bbs_parse(site, html)


def _bbs_parse(site, html):
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    vods = []
    for li in soup.select("ul.threadlist > li"):
        if (li.select_one("i") or {}).get("data-placement", "") == "top" if li.select_one("i") else False:
            continue
        a = li.select_one("div > div.subject > a, div > div.style3_subject > a")
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or "福利" in title:
            continue
        img = li.select_one("a > img")
        pic = img.get("src", "") if img else ""
        if pic and not pic.startswith("http"):
            pic = site["url"] + "/" + pic.lstrip("/")
        vods.append({"vod_id": _abs(site["url"], a.get("href", "")),
                     "vod_name": title, "vod_pic": pic, "vod_remarks": ""})
    return vods[:60]


def _bbs_unlock_and_fetch(site, page_url):
    """回复解锁隐藏内容后返回页面 HTML（无 cookie 时直接返回原页）"""
    html = http_get(site["session"], page_url, timeout=15, headers=_bbs_headers(site))
    if not site.get("extra_headers") or "<立即回复>" not in html:
        return html
    m = re.search(r"thread-(\d+)", page_url)
    if not m:
        return html
    import random
    body = {"doctype": "1", "return_html": "0", "quotepid": "",
            "message": random.choice(BBS_REPLY_TEXTS), "quick_reply_message": "4"}
    h = _bbs_headers(site)
    h["X-Requested-With"] = "XMLHttpRequest"
    try:
        site["session"].post(f"{site['url']}/post-create-{m.group(1)}-1.htm",
                             data=body, headers=h, timeout=15)
        html = http_get(site["session"], page_url, timeout=15, headers=_bbs_headers(site))
    except Exception:
        pass
    return html


def page_detail_pans(site, page_url):
    html = _bbs_unlock_and_fetch(site, page_url)
    text = html
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        alerts = soup.select(ALERT_SEL)
        if alerts:
            parts = []
            for al in alerts:
                for a in al.select("a[href]"):
                    href = a.get("href", "")
                    if "/s/" in href or "/t/" in href:
                        parts.append(href + " " + al.get_text(" ", strip=True))
                parts.append(al.get_text(" ", strip=True))
            text = "\n".join(parts) or html
    # 链接+提取码合并为 ?pwd=
    text = re.sub(r"(https?://[^\s\"'<>]+)[\s\S]{0,30}?提取码[\s:：]*([a-zA-Z0-9]{4})",
                  lambda mm: mm.group(1) + "?pwd=" + mm.group(2), text)
    return _extract_pans(text), html


# ---------------------------------------------------------------------------
# 聚合 Spider
# ---------------------------------------------------------------------------
class Spider(Spider):
    def getDependence(self):
        return []

    def init(self, extend=""):
        try:
            ext = json.loads(extend) if isinstance(extend, str) and extend else (extend or {})
        except Exception:
            ext = {}
        self.uc = UCDrive(ext.get("uc_cookie", "") or ext.get("cookie", ""))
        self.pan123 = Pan123()
        self.uc_only = str(ext.get("mode", "")).lower() == "uc"  # 配置 ext 传 {"mode":"uc"} 变 UC免登专站
        self.fast_filter = str(ext.get("fast", "1")) == "1"  # 仅聚合模式"仅UC免登"筛选用；UC专站搜索/推荐恒为强检测
        self._play_cache = {}  # UC链接有效性缓存 {url: (可播, 时间戳, TTL)}
        self._link_cache = {}  # 详情页UC链接缓存 {real_id: ([url,...], 时间戳)}
        self.sites = {}
        for s in SITES:
            inst = dict(s)
            inst["session"] = make_session()
            ck = ext.get(f"{s['id']}cookie", "")
            if s["type"] == "bbs" and ck:
                inst["extra_headers"] = {"Cookie": ck}
            inst["url"] = s["site"][0]  # 先用首个镜像，真正请求时才懒探测
            self.sites[s["id"]] = inst

    def getName(self):
        return "UC免登" if self.uc_only else "网盘"

    # ---------- UC免登专站：有效性验证 ----------
    UC_SRC = ["木偶", "玩偶", "多多", "欧歌", "二小", "花卷", "虎斑", "快映", "夸父", "123", "卡卡"]
    SEARCH_TIMEOUT = 10  # 搜索/分类取候选总时限（秒），慢站直接放弃不再拖全局
    VERIFY_TIMEOUT = 12  # 统一验播时限（秒），时限内验出多少算多少

    @staticmethod
    def _site_dead(site):
        """熔断：探测/请求失败过的站，5 分钟内直接跳过（防止死站每次都拖满超时）"""
        return time.time() < site.get("_dead_until", 0)

    @staticmethod
    def _mark_dead(site, secs=300):
        site["_dead_until"] = time.time() + secs

    def _uc_playable(self, url):
        """强检测：分享未过期 + 能匿名展开出视频 + 匿名直链可取 + CDN 头探测可连。
        可播缓存 6h；不可播只缓存 10 分钟（防网络抖动误杀后长期消失）"""
        ent = self._play_cache.get(url)
        if ent and time.time() - ent[1] < (ent[2] if len(ent) > 2 else 6 * 3600):
            return ent[0]
        ok = False
        try:
            items = self.uc.list_videos(url, limit=1)
            if items:
                it = items[0]
                dl = self.uc.download_url_anon(it["pwd_id"], it["fid"], it["share_fid_token"])
                ok = bool(dl) and self.uc.stream_alive(dl)
        except Exception:
            ok = False
        self._play_cache[url] = (ok, time.time(), 6 * 3600 if ok else 600)
        return ok

    def _vod_uc_links(self, site, real_id):
        """取某条目的全部 UC 分享链接（30 分钟缓存）"""
        ent = self._link_cache.get(real_id)
        if ent and time.time() - ent[1] < 1800:
            return ent[0]
        try:
            t = site["type"]
            if t in ("pansou", "mipan", "qupan", "jike"):
                # 链接型站点：vod_id 本身就是分享链接，直接判 UC 即可，别再当网页去抓
                links = [real_id] if PAN_RES[0][1].search(real_id) else []
            elif t == "kkpan":
                pans, _ = kkpan_detail(site, real_id)
                links = pans.get("UC") or []
            elif t in ("webshare", "wogg"):
                pans, _ = maccms_detail_pans(site, real_id)
                links = pans.get("UC") or []
            elif t == "aipan":
                pans, _ = aipan_detail(site, real_id)
                links = pans.get("UC") or []
            else:
                pans, _ = page_detail_pans(site, real_id)
                links = pans.get("UC") or []
        except Exception:
            links = []
        self._link_cache[real_id] = (links, time.time())
        return links

    def _verify_uc(self, site, vods, check_play=True):
        """只留带 UC 链接的条目；check_play 时进一步验证分享未过期能播"""
        def work(v):
            links = self._vod_uc_links(site, v["vod_id"])
            if not links:
                return False
            if not check_play:
                return True
            return any(self._uc_playable(u) for u in links[:2])
        with ThreadPoolExecutor(max_workers=16) as ex:
            keep = list(ex.map(work, vods))
        return [v for v, k in zip(vods, keep) if k]

    def _verify_mixed(self, vods, check_play=True, timeout=VERIFY_TIMEOUT):
        """跨站统一验 UC（vods 需带 _sid0 标记站源）。
        旧做法是每个站各自起一个线程池顺序验，慢站会把整轮拖满；
        这里全候选共享一个池 + 全局时限，as_completed 先验完先出，超时直接收尾"""
        out = []
        def work(v):
            site = self.sites.get(v.get("_sid0", ""))
            if not site:
                return None
            try:
                links = self._vod_uc_links(site, v["vod_id"])
                if not links:
                    return None
                if not check_play:
                    return v
                return v if any(self._uc_playable(u) for u in links[:2]) else None
            except Exception:
                return None
        ex = ThreadPoolExecutor(max_workers=16)
        try:
            futs = [ex.submit(work, v) for v in vods]
            for f in as_completed(futs, timeout=timeout):
                try:
                    r = f.result()
                    if r is not None:
                        out.append(r)
                except Exception:
                    pass
        except Exception:
            pass  # as_completed 超时抛 TimeoutError，已验出的照出
        finally:
            ex.shutdown(wait=False)
        return out

    def isVideoFormat(self, url):
        return url.lower().endswith(VIDEO_EXT)

    def manualVideoCheck(self):
        return False

    def localProxy(self, params):
        return []

    # ---------- 主页：UC专站=内容分类；聚合=18站胶囊，maccms系带完整筛选 ----------
    UC_CLASSES = [{"type_id": "UC首页", "type_name": "推荐"},
                  {"type_id": "1", "type_name": "电影"}, {"type_id": "2", "type_name": "剧集"},
                  {"type_id": "3", "type_name": "综艺"}, {"type_id": "4", "type_name": "动漫"},
                  {"type_id": "5", "type_name": "纪录片"}, {"type_id": "26", "type_name": "短剧"}]
    # UC 专站分类 → 玩偶(wogg) 的 slug 映射
    WOGG_CAT_MAP = {"1": "dianying", "2": "juji", "3": "zongyi",
                    "4": "dongman", "5": "jilupian", "26": "duanjv"}

    def _maccms_filter_rows(self, site=None):
        f = FILTERS_WOGG if (site and site["type"] == "wogg") else FILTERS_WEBSHARE
        return [{"key": "cateId", "name": "分类", "value": f["cateId"]},
                {"key": "by", "name": "排序", "value": f["by"]},
                {"key": "class", "name": "类型", "value": f["class"]},
                {"key": "area", "name": "地区", "value": f["area"]},
                {"key": "year", "name": "年份", "value": f["year"]}]

    def homeContent(self, filter):
        if self.uc_only:
            res = {"class": self.UC_CLASSES}
            if filter:
                rows = [r for r in self._maccms_filter_rows() if r["key"] != "cateId"]
                res["filters"] = {c["type_id"]: rows for c in self.UC_CLASSES if c["type_id"] != "UC首页"}
            return res
        classes = []
        for s in SITES:
            classes.append({"type_id": s["id"], "type_name": s["name"]})
        res = {"class": classes}
        if filter:
            filters = {}
            for s in SITES:
                if s["type"] in ("webshare", "wogg"):
                    rows = self._maccms_filter_rows(s)
                    rows.append({"key": "uc", "name": "资源",
                                 "value": [{"n": "全部", "v": "0"}, {"n": "仅UC免登", "v": "1"}]})
                    filters[s["id"]] = rows
                elif s["type"] == "bbs" and s.get("cats"):
                    filters[s["id"]] = [{"key": "cateId", "name": "分类", "value": s["cats"]}]
                elif s["type"] == "zhinan":
                    filters[s["id"]] = [{"key": "cateId", "name": "分类", "value": ZHINAN_CATS}]
            res["filters"] = filters
        return res

    def homeVideoContent(self):
        return {"list": []}

    # ---------- 分类：UC专站按内容分类并行聚合；聚合模式按站点分发 ----------
    def _uc_fetch_site(self, site, tid, pg, extend):
        """单站取一页分类（UC专站用），wogg 走 slug 映射；UC首页 不设 cateId 用站点默认"""
        if self._site_dead(site):
            return site, []
        _ensure_url(site)
        if self._site_dead(site):
            return site, []
        try:
            t = site["type"]
            if t in ("webshare", "wogg"):
                ext = dict(extend or {})
                if str(tid) in self.WOGG_CAT_MAP:
                    ext["cateId"] = self.WOGG_CAT_MAP[str(tid)] if t == "wogg" else str(tid)
                else:
                    ext.pop("cateId", None)
                vods = maccms_category(site, tid, pg, ext)
            elif t == "kkpan":
                vods = kkpan_category(site, tid, pg, extend or {})
            else:
                return site, []
        except Exception:
            self._mark_dead(site)
            vods = []
        return site, vods[:12]

    def categoryContent(self, tid, pg, filter, extend):
        if self.uc_only:
            pool = [self.sites[i] for i in self.UC_SRC if i in self.sites]
            if tid == "UC首页":
                # 推荐：轮流取各 UC 源站的最新一页，验证后返回
                p = max(1, int(pg or 1))
                site = pool[(p - 1) % len(pool)]
                spg = (p - 1) // len(pool) + 1
                site, vods = self._uc_fetch_site(site, tid, spg, {})
                for v in vods:
                    v["_sid0"] = site["id"]
            else:
                # 内容分类：全部源站并行拉取，总时限内收不齐就放弃慢站
                ex = ThreadPoolExecutor(max_workers=len(pool))
                futs = [ex.submit(self._uc_fetch_site, s, tid, pg, extend) for s in pool]
                vods = []
                try:
                    done, _ = wait(futs, timeout=self.SEARCH_TIMEOUT)
                    for f in done:
                        try:
                            site, vs = f.result()
                            for v in vs:
                                v["_sid0"] = site["id"]
                            vods.extend(vs)
                        except Exception:
                            pass
                finally:
                    ex.shutdown(wait=False)
            if not vods:
                return {"list": [], "page": int(pg or 1), "pagecount": 1, "limit": 90, "total": 0}
            # 跨站统一验播：共享线程池 + 全局时限，先验完先出，慢站不再拖满整轮
            ok_vods = self._verify_mixed(vods, check_play=True, timeout=self.VERIFY_TIMEOUT)
            for v in ok_vods:
                sid = v.pop("_sid0", "")
                v["vod_id"] = f"{sid}@{v['vod_id']}"
                v["vod_remarks"] = f"{sid} | {v.get('vod_remarks', '')}".strip(" |")
            return {"list": ok_vods, "page": int(pg or 1), "pagecount": 999, "limit": 90, "total": 999999}
        site = self.sites.get(tid)
        if not site:
            return {"list": []}
        _ensure_url(site)
        try:
            t = site["type"]
            if t in ("webshare", "wogg"):
                vods = maccms_category(site, tid, pg, extend or {})
            elif t == "kkpan":
                vods = kkpan_category(site, tid, pg, extend or {})
            elif t == "jike":
                vods = jike_category(site, tid, pg, extend or {})
            elif t == "aipan":
                vods = aipan_category(site, tid, pg, extend or {})
            elif t == "zhinan":
                vods = zhinan_category(site, tid, pg, extend or {})
            elif t == "shuangxing":
                vods = shuangxing_category(site, tid, pg, extend or {})
            elif t == "bbs":
                vods = bbs_category(site, tid, pg, extend or {})
            else:
                vods = []
        except Exception:
            vods = []
        # "仅UC免登"筛选：默认只查有无 UC 链接（快）；fast=0 时逐条试播（准但慢）
        if (extend or {}).get("uc") == "1" and site["type"] in ("webshare", "wogg", "kkpan") and vods:
            vods = self._verify_uc(site, vods, check_play=not self.fast_filter)
        for v in vods:
            v["vod_id"] = f"{site['id']}@{v['vod_id']}"
        return {"list": vods, "page": pg, "pagecount": 999, "limit": 90, "total": 999999}

    # ---------- 搜索：一次搜全部站，带总时限，慢站直接放弃 ----------
    def _site_search(self, site, key, pg):
        """按站点类型分发搜索（不验证、不标记）"""
        t = site["type"]
        if t in ("webshare", "wogg"):
            return maccms_search(site, key, pg)
        if t == "kkpan":
            return kkpan_search(site, key, pg)
        if t == "jike":
            return jike_search(site, key, pg)
        if t == "aipan":
            return aipan_search(site, key, pg)
        if t == "qupan":
            return qupan_search(site, key, pg)
        if t == "pansou":
            return pansou_search(site, key, pg)
        if t == "mipan":
            return mipan_search(site, key, pg)
        if t == "zhinan":
            return zhinan_search(site, key, pg)
        if t == "shuangxing":
            return shuangxing_search(site, key, pg)
        if t == "bbs":
            return bbs_search(site, key, pg)
        return []

    def searchContent(self, key, quick, pg="1"):
        if self.uc_only:
            # UC专站搜索：全部站点都搜，候选统一过 UC 强检测（检测本身不变），结果不去重。
            # 先并行只取候选，再共享线程池限时验播：慢站不再把整轮拖满还被整体丢弃
            def fetch(site):
                if self._site_dead(site):
                    return []
                _ensure_url(site)
                try:
                    vods = self._site_search(site, key, pg)
                except Exception:
                    self._mark_dead(site)
                    vods = []
                for v in vods[:12]:
                    v["_sid0"] = site["id"]
                return vods[:12]
            cands = self._run_parallel(list(self.sites.values()), fetch, self.SEARCH_TIMEOUT)
            ok = self._verify_mixed(cands, check_play=True, timeout=self.VERIFY_TIMEOUT)  # 强检测：只留真能播的
            for v in ok:
                sid = v.pop("_sid0", "")
                v["vod_id"] = f"{sid}@{v['vod_id']}"
                v["vod_remarks"] = f"{sid} | {v.get('vod_remarks', '')}".strip(" |")
            return {"list": ok}
        def do(site):
            if self._site_dead(site):
                return []
            _ensure_url(site)
            try:
                vods = self._site_search(site, key, pg)
                for v in vods:
                    v["vod_id"] = f"{site['id']}@{v['vod_id']}"
                    v["vod_remarks"] = f"{site['id']} | {v.get('vod_remarks', '')}".strip(" |")
                return vods
            except Exception:
                self._mark_dead(site)
                return []
        return {"list": self._run_parallel(list(self.sites.values()), do, self.SEARCH_TIMEOUT)}

    @staticmethod
    def _run_parallel(items, fn, timeout):
        """并行执行 fn(item)，timeout 秒内没收完的慢任务直接丢弃"""
        merged = []
        ex = ThreadPoolExecutor(max_workers=max(1, len(items)))
        try:
            futs = [ex.submit(fn, it) for it in items]
            done, _ = wait(futs, timeout=timeout)
            for f in done:
                try:
                    merged.extend(f.result())
                except Exception:
                    pass
        finally:
            ex.shutdown(wait=False)
        return merged

    # ---------- 详情：通用网盘链接提取 + UC/123 展开 ----------
    def detailContent(self, array):
        if not array:
            return {"list": []}
        vod_id = array[0]
        if "@" not in vod_id:
            # 推送的原始分享链接：直接当网盘链接展开（UC/123 免登直链）
            pans = {}
            for name, rx in PAN_RES:
                if rx.search(vod_id):
                    pans[name] = [vod_id]
            if not pans:
                # 非网盘直链：照 jar 的 Push 站，给 直连/嗅探/解析 三条线路
                return {"list": [{"vod_id": vod_id, "vod_name": vod_id, "vod_pic": "",
                                  "vod_play_from": "直连$$$嗅探$$$解析",
                                  "vod_play_url": f"播放${vod_id}$$$播放${vod_id}$$$播放${vod_id}"}]}
            return {"list": [self._build_vod(vod_id, vod_id, "", "", pans)]}
        sid, real_id = vod_id.split("@", 1)
        site = self.sites.get(sid)
        if not site:
            return {"list": []}
        _ensure_url(site)
        try:
            t = site["type"]
            if t in ("webshare", "wogg"):
                pans, raw_html = maccms_detail_pans(site, real_id)
                soup = BeautifulSoup(raw_html, "html.parser") if BeautifulSoup else None
                title = (soup.select_one("h1") or soup.select_one("title")).get_text(strip=True) if soup else ""
                pic = ""
                content = ""
                if soup:
                    img = soup.select_one(".module-item-pic img") or soup.select_one(".video-cover img")
                    if img:
                        pic = img.get("data-src") or img.get("src", "")
                    info = soup.select_one(".module-info-introduction-content") or soup.select_one(".vod_content")
                    content = info.get_text(strip=True) if info else ""
            elif t == "kkpan":
                pans, content = kkpan_detail(site, real_id)
                title, pic = content, ""
            elif t == "aipan":
                pans, content = aipan_detail(site, real_id)
                title, pic = "", ""
            elif t in ("jike", "qupan", "mipan", "pansou"):
                pans = {}
                for name, rx in PAN_RES:
                    if rx.search(real_id):
                        pans[name] = [real_id]
                title, pic, content = real_id, "", ""
            else:  # bbs / zhinan / shuangxing：详情页通用正则
                pans, raw_html = page_detail_pans(site, real_id)
                title, pic, content = "", "", ""
                if BeautifulSoup is not None:
                    soup = BeautifulSoup(raw_html, "html.parser")
                    h = soup.select_one("h4") or soup.select_one("h1") or soup.select_one("title")
                    title = h.get_text(strip=True) if h else ""
                    if "权限不足" in raw_html or "立即登录" in raw_html:
                        content = f"该站点需要登录账号，请在 ext 中配置 {sid}cookie"
            if not pans:
                return {"list": [{"vod_id": vod_id, "vod_name": title or sid,
                                  "vod_play_from": "提示",
                                  "vod_play_url": "未找到网盘链接$none"}]}
            return {"list": [self._build_vod(vod_id, title, pic, content, pans)]}
        except Exception:
            return {"list": []}

    # ---------- 选集显示名：跟随剧名 + 集数 + 清晰度 + 文件大小 ----------
    @staticmethod
    def _fmt_size(sz):
        try:
            sz = int(sz or 0)
        except Exception:
            sz = 0
        if sz >= 1073741824:
            return f"{sz / 1073741824:.1f}G"
        if sz > 0:
            return f"{sz / 1048576:.0f}M"
        return ""

    _EP_NUM_RES = [
        re.compile(r"[Ss]\d{1,2}[Ee](\d{1,3})"),      # S01E06
        re.compile(r"\b[Ee][Pp]?(\d{1,3})\b"),        # E06 / EP06
        re.compile(r"第\s*(\d{1,3})\s*[集期话回]"),     # 第6集
        re.compile(r"^(\d{1,3})[ ._\-]"),             # 01 4K / 06.xxx
        re.compile(r"\[(\d{1,3})\]"),                 # [06]
    ]

    def _ep_display(self, title, fname, idx, size=0):
        """长英文发布名不直接上屏：能识别集数就显示"剧名 第N集"；中文短名保留去扩展名；末尾附大小"""
        base = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", fname or "").strip() or (fname or "")
        if (title or "").startswith("http"):
            title = ""
        num = None
        for rx in self._EP_NUM_RES:
            m = rx.search(base)
            if m:
                num = int(m.group(1))
                break
        mq = re.search(r"4320p|2160p|1080p|720p|[48][Kk]", base)
        q = ""
        if mq:
            q = {"2160P": "4K", "4320P": "8K"}.get(mq.group(0).upper(), mq.group(0))
        if re.search(r"[\u4e00-\u9fff]", base) and len(base) <= 24:
            name = base
        elif num is not None:
            name = f"{title} 第{num:02d}集" if title else f"第{num:02d}集"
        elif title:
            name = title if idx == 1 else f"{title} {idx:02d}"
        else:
            name = base[:20]
        if q and q not in name:
            name += f" {q}"
        sz = self._fmt_size(size)
        if sz:
            name += f" · {sz}"
        return name.replace("$", " ").replace("#", " ").strip()

    def _build_vod(self, vod_id, title, pic, content, pans):
        uc_lines, p123_lines = [], []
        # UC 展开（全部链接，后面按集数排序，剧情更全的排最前）
        for link in pans.get("UC", []):
            try:
                items = self.uc.list_videos(link, limit=500)
            except Exception:
                items = []
            if items:
                eps = [f"{self._ep_display(title, it['file_name'], i + 1, it.get('size'))}$"
                       f"{it['file_name']}__{it['fid']}+{it['pwd_id']}+{it['share_fid_token']}"
                       for i, it in enumerate(items)]
                uc_lines.append((len(eps), "#".join(eps)))
        # 123 展开（免登直链）
        for link in pans.get("123", []):
            try:
                items = self.pan123.list_videos(link)
            except Exception:
                items = []
            if items:
                eps = []
                for i, it in enumerate(items):
                    pid = (f"{it['file_name']}__{it['file_id']}+{it['etag']}+{it['s3key']}"
                           f"+{it['size']}+{quote(it['download_url'], safe='')}")
                    eps.append(f"{self._ep_display(title, it['file_name'], i + 1, it.get('size'))}${pid}")
                p123_lines.append((len(eps), "#".join(eps)))
        play_from, play_url = [], []
        # 集数多的线路排前面（剧情更全），线路名带集数
        for i, (cnt, urls) in enumerate(sorted(uc_lines, key=lambda x: -x[0])):
            play_from.append(f"UC无限{cnt}集#{i + 1}")
            play_url.append(urls)
        for i, (cnt, urls) in enumerate(sorted(p123_lines, key=lambda x: -x[0])):
            play_from.append(f"123云盘{cnt}集#{i + 1}")
            play_url.append(urls)
        # 夸克/百度/迅雷/天翼/阿里/移动 均需登录转存，不支持免登直链，直接不出线路
        if not play_from:
            play_from, play_url = ["提示"], ["UC/123分享已失效（或仅有夸克/百度等需登录网盘），请返回换一条$none"]
        return {"vod_id": vod_id, "vod_name": title, "vod_pic": pic, "vod_content": content,
                "vod_play_from": "$$$".join(play_from), "vod_play_url": "$$$".join(play_url)}

    # ---------- 播放：UC 三级回退 + 123 直链 ----------
    def playerContent(self, flag, play_id, vipFlags):
        # 推送直链的三条线路（对齐 jar Push 站）
        if flag == "直连":
            return {"parse": 0, "url": play_id}
        if flag == "嗅探":
            return {"parse": 1, "url": play_id}
        if flag == "解析":
            return {"parse": 2, "jx": 1, "url": play_id}
        if play_id.startswith("http"):
            # 推送的裸分享链接：UC/123 自己展开直接播，其余提示需登录
            if re.search(r"https?://drive\.uc\.cn/s/", play_id):
                try:
                    items = self.uc.list_videos(play_id)
                except Exception:
                    items = []
                if items:
                    it = items[0]
                    return self.uc.play_anonymous(it["pwd_id"], it["fid"], it["share_fid_token"])
                return {"parse": 0, "msg": "UC分享解析失败，请重试"}
            if Pan123.SHARE_RE.search(play_id):
                try:
                    items = self.pan123.list_videos(play_id)
                    if items and items[0].get("download_url"):
                        return self.pan123.play_direct(items[0]["download_url"])
                except Exception:
                    pass
                return {"parse": 0, "msg": "123分享解析失败，请重试"}
            return {"parse": 0, "msg": "该网盘需登录，暂不支持免登播放"}
        if play_id.endswith(".htm") or "分享$" in flag + play_id or play_id == "none":
            return {"parse": 0, "msg": "该分享解析失败或为需登录网盘，请重试或换线路"}
        parts = play_id.rsplit("+", 4)
        try:
            if len(parts) == 5:  # 123 云盘：name__fileId+etag+s3key+size+download_url
                dl = unquote(parts[4])
                rst = self.pan123.play_direct(dl)
                if rst.get("url"):
                    return rst
                return {"parse": 0, "msg": "123直链获取失败"}
            if len(parts) >= 3:  # UC：name__fid+pwd_id+fid_token —— 无限免登裸直链（同 jar 的 q() 无限分支）
                head, pwd_id, fid_token = parts[0], parts[1], parts[2]
                fid = head.split("__", 1)[-1] if "__" in head else head
                rst = self.uc.play_anonymous(pwd_id, fid, fid_token)
                if rst.get("url"):
                    return rst
                return {"parse": 0, "msg": "UC无限线路获取失败，请重试或换集"}
        except Exception as e:
            return {"parse": 0, "msg": f"解析失败: {e}"}
        return {"parse": 0, "msg": "无法识别的播放地址"}


if __name__ == "__main__":
    sp = Spider()
    sp.init("{}")
    home = sp.homeContent(True)
    print("胶囊站点数:", len(home["class"]))
    for c in home["class"]:
        print(" ", c["type_id"], c["type_name"])

    print("\n--- 并发搜索：凡人修仙传 ---")
    rst = sp.searchContent("凡人修仙传", True, "1")
    by_site = {}
    for v in rst["list"]:
        by_site.setdefault(v["vod_id"].split("@")[0], 0)
        by_site[v["vod_id"].split("@")[0]] += 1
    print("结果数:", len(rst["list"]), "按站:", by_site)
    if rst["list"]:
        first = rst["list"][0]
        print("第一条:", first["vod_name"], "|", first["vod_remarks"], "|", first["vod_id"][:80])
