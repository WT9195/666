#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import json
import base64
import gzip
import zlib
import ssl
import uuid
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from urllib.parse import quote, unquote, urlparse

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        def getCache(self, key): return None
        def setCache(self, key, value): return "fail"
        def delCache(self, key): return "fail"


def format_remarks(brand, meta):
    clean_meta = str(meta or "").strip()
    clean_meta = re.sub(r"[\r\n\t]+", " ", clean_meta).strip()
    if clean_meta:
        return "%s | %s" % (brand, clean_meta)
    return brand


_SBOX = []
_INV_SBOX = [0] * 256


def _build_sbox():
    global _SBOX, _INV_SBOX
    if _SBOX:
        return
    sbox = [0] * 256

    def rotl(x, n):
        return ((x << n) | (x >> (8 - n))) & 0xff

    p = 1
    q = 1
    while True:
        p = (p ^ ((p << 1) & 0xff) ^ (0x1b if (p & 0x80) else 0)) & 0xff
        q ^= (q << 1) & 0xff
        q ^= (q << 2) & 0xff
        q ^= (q << 4) & 0xff
        q ^= (0x09 if (q & 0x80) else 0) & 0xff
        sbox[p] = (q ^ rotl(q, 1) ^ rotl(q, 2) ^ rotl(q, 3) ^ rotl(q, 4) ^ 0x63) & 0xff
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _SBOX = sbox
    _INV_SBOX = inv


_build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _aes_expand_key(key):
    w = list(key)
    i = 16
    while i < 176:
        t = w[i - 4:i]
        if i % 16 == 0:
            t = [_SBOX[t[1]] ^ _RCON[i // 16 - 1], _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
        for j in range(4):
            w.append(w[i - 16 + j] ^ t[j])
        i += 4
    return w


def _aes_gmul(a, b):
    r = 0
    while b > 0:
        if b & 1:
            r ^= a
        a = ((a << 1) & 0xff) ^ (0x1b if (a & 0x80) else 0)
        b >>= 1
    return r


def _aes_decrypt_block(block, w):
    s = list(block)

    def add_rk(rnd):
        off = rnd * 16
        for k in range(16):
            s[k] ^= w[off + k]

    def inv_shift():
        t = list(s)
        for row in range(4):
            for col in range(4):
                s[row + 4 * col] = t[row + 4 * ((col - row) % 4)]

    def inv_sub():
        for k in range(16):
            s[k] = _INV_SBOX[s[k]]

    def inv_mix():
        for col in range(4):
            a0, a1, a2, a3 = s[4 * col], s[4 * col + 1], s[4 * col + 2], s[4 * col + 3]
            s[4 * col] = _aes_gmul(a0, 14) ^ _aes_gmul(a1, 11) ^ _aes_gmul(a2, 13) ^ _aes_gmul(a3, 9)
            s[4 * col + 1] = _aes_gmul(a0, 9) ^ _aes_gmul(a1, 14) ^ _aes_gmul(a2, 11) ^ _aes_gmul(a3, 13)
            s[4 * col + 2] = _aes_gmul(a0, 13) ^ _aes_gmul(a1, 9) ^ _aes_gmul(a2, 14) ^ _aes_gmul(a3, 11)
            s[4 * col + 3] = _aes_gmul(a0, 11) ^ _aes_gmul(a1, 13) ^ _aes_gmul(a2, 9) ^ _aes_gmul(a3, 14)

    add_rk(10)
    for rnd in range(9, 0, -1):
        inv_shift()
        inv_sub()
        add_rk(rnd)
        inv_mix()
    inv_shift()
    inv_sub()
    add_rk(0)
    return s


class FastAesCbc(object):
    def __init__(self, key, iv):
        self.rk = _aes_expand_key(list(key))
        self.iv = list(iv)

    def decrypt(self, data):
        if not data or len(data) % 16 != 0:
            return b""
        out = bytearray()
        prev = self.iv
        for off in range(0, len(data), 16):
            blk = list(data[off:off + 16])
            dec = _aes_decrypt_block(blk, self.rk)
            for i in range(16):
                out.append(dec[i] ^ prev[i])
            prev = blk
        if out:
            pad = out[-1]
            if 1 <= pad <= 16 and pad <= len(out) and bytes(out[-pad:]) == bytes([pad]) * pad:
                del out[-pad:]
        return bytes(out)


class Spider(BaseSpider):
    def __init__(self):
        super(Spider, self).__init__()
        self.siteName = "草莓视频"
        self.host = "https://scor.t2j8l5ixcm.cc"
        self.staticBase = "https://cmsta.uitxn.cn"
        self.imgFallback = "https://cmvpic.uitxn.cn"
        self.proxyBase = "http://127.0.0.1:9977/proxy?do=py"
        self.platform = "9"
        self._ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        self.brandActor = "🦋 TG群: @tvshare23"
        self.brandDirector = "🦋 蝴蝶影视"
        self.tgGroup = "https://t.me/tvshare23"
        self.options = {}
        self._uuid = str(uuid.uuid4())
        self._aes = FastAesCbc(b"NHboMHZerxFQ401E", b"i7JeCEIMVrj2W9xN")
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=self.ctx)
        )
        self.sortOpts = [
            {"n": "今日更新", "v": "1"},
            {"n": "推荐", "v": "2"},
            {"n": "日榜", "v": "3"},
            {"n": "周榜", "v": "4"},
            {"n": "月榜", "v": "5"}
        ]
        self.tree = [
            (51, "国产", [(156, "乱伦"), (152, "偷情"), (191, "剧情"), (144, "偷拍"), (145, "自拍"), (151, "直播"), (153, "探花"), (154, "强奸"), (155, "迷奸")]),
            (76, "日韩", [(169, "中字"), (171, "无码"), (194, "乱伦"), (193, "人妻"), (183, "群交"), (170, "OL"), (196, "偷情")]),
            (127, "欧美", [(176, "黑白配"), (185, "剧情"), (178, "中字"), (182, "SM"), (186, "自拍"), (177, "男同"), (181, "女同")]),
            (93, "吃瓜", []),
            (60, "传媒", [(146, "麻豆"), (147, "天美"), (148, "91"), (157, "星空"), (158, "精东"), (159, "蜜桃"), (160, "SWAG"), (187, "果冻"), (188, "糖心"), (190, "萝莉社"), (192, "扣扣"), (195, "皇家")]),
            (137, "AI视频", []),
            (83, "动漫", [(172, "中字"), (173, "有码"), (174, "无码"), (189, "3D")]),
            (71, "综艺", []),
            (198, "解说", []),
            (197, "VR", []),
            (199, "伦理", []),
            (200, "猎奇", []),
            (201, "福利姬", [])
        ]

    def init(self, extend=""):
        if isinstance(extend, dict):
            self.options = extend
        elif extend:
            try:
                self.options = json.loads(extend)
            except Exception:
                self.options = {}
        custom = str(self.options.get("host") or "").strip()
        if custom.startswith("http"):
            self.host = custom.rstrip("/")
        return True

    def getName(self):
        return self.siteName

    def _channel_code(self):
        h = re.sub(r"^https?://", "", self.host)
        first = h.split("/")[0].split(".")[0] or ""
        if first == "www":
            return ""
        return first[:4]

    def _fetch(self, url, headers, timeout=5):
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self.opener.open(req, timeout=timeout) as resp:
                    raw = resp.read()
                    enc = ""
                    try:
                        enc = resp.headers.get("Content-Encoding", "") or ""
                    except Exception:
                        enc = ""
                    if raw.startswith(b"\x1f\x8b") or enc == "gzip":
                        raw = gzip.decompress(raw)
                    elif enc == "deflate":
                        try:
                            raw = zlib.decompress(raw)
                        except Exception:
                            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    return resp.getcode(), raw
            except urllib.error.HTTPError as e:
                try:
                    return e.code, e.read() if e.fp else b""
                except Exception:
                    return e.code, b""
            except Exception:
                if attempt == 0:
                    continue
                return 0, b""
        return 0, b""

    def _api_headers(self):
        return {
            "User-Agent": self._ua,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "platform": self.platform,
            "Channel-Code": self._channel_code(),
            "uuid": self._uuid,
            "Referer": self.host + "/",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive"
        }

    def _decrypt_json(self, raw):
        try:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                return None
            plain = self._aes.decrypt(base64.b64decode(text))
            if not plain:
                return None
            return json.loads(plain.decode("utf-8", "replace"))
        except Exception:
            return None

    def _api(self, path):
        code, raw = self._fetch(self.host + path, self._api_headers())
        if code != 200:
            return None
        return self._decrypt_json(raw)

    def _static(self, path):
        code, raw = self._fetch(self.staticBase + path, {
            "User-Agent": self._ua,
            "Referer": self.host + "/",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive"
        })
        if code != 200:
            return None
        return self._decrypt_json(raw)

    def _clean_title(self, t):
        s = str(t or "").replace("﻿", "")
        return s.strip()

    def _fmt_dur(self, sec):
        try:
            n = int(sec)
        except Exception:
            return ""
        if n <= 0:
            return ""
        h = n // 3600
        m = (n % 3600) // 60
        s = n % 60
        if h > 0:
            return "%d:%02d:%02d" % (h, m, s)
        return "%02d:%02d" % (m, s)

    def _pic(self, domain, rel_path):
        if not rel_path:
            return ""
        rel = str(rel_path)
        if rel.startswith("http"):
            full = rel
        else:
            full = (domain or self.imgFallback).rstrip("/") + rel
        return "%s&type=img&url=%s" % (self.proxyBase, quote(full, safe=""))

    def _cards(self, records):
        cards = []
        if not isinstance(records, list):
            return cards
        for r in records:
            if not isinstance(r, dict) or not r.get("viewKey"):
                continue
            cards.append({
                "vod_id": str(r["viewKey"]),
                "vod_name": self._clean_title(r.get("title")),
                "vod_pic": self._pic(self.imgFallback, r.get("poster")),
                "vod_remarks": format_remarks("蝴蝶影视", self._fmt_dur(r.get("duration"))),
                "style": {"type": "rect", "ratio": 1.78}
            })
        return cards

    def _list_result(self, data, page):
        if not isinstance(data, dict):
            return {"list": [], "page": page, "pagecount": page, "limit": 18, "total": 0}
        cards = self._cards(data.get("records"))
        try:
            pc = int(data.get("pageCount") or page)
        except Exception:
            pc = page
        try:
            total = int(data.get("total") or 0)
        except Exception:
            total = 0
        return {"list": cards, "page": page, "pagecount": pc, "limit": 18, "total": total}

    def homeContent(self, filter):
        classes = [{"type_id": "featured", "type_name": "精选"}]
        for cid, name, kids in self.tree:
            classes.append({"type_id": str(cid), "type_name": name})
        classes.append({"type_id": "short", "type_name": "短视频"})
        result = {"class": classes}
        if filter:
            filters = {}
            filters["featured"] = [
                {"key": "sort", "name": "排序", "init": "2", "value": self.sortOpts}
            ]
            for cid, name, kids in self.tree:
                groups = []
                if kids:
                    opts = [{"n": "全部", "v": ""}]
                    for kid, kname in kids:
                        opts.append({"n": kname, "v": str(kid)})
                    groups.append({"key": "sub", "name": "子分类", "init": "", "value": opts})
                groups.append({"key": "sort", "name": "排序", "init": "1", "value": self.sortOpts})
                filters[str(cid)] = groups
            result["filters"] = filters
        return result

    def homeVideoContent(self):
        data = self._static("/static/common/home_data_9.js")
        if isinstance(data, dict) and isinstance(data.get("recommendList"), list):
            return {"list": self._cards(data.get("recommendList"))}
        data = self._api("/app/movie/getList?page=1&pageSize=18&sort=2")
        if isinstance(data, dict):
            return {"list": self._cards(data.get("records"))}
        return {"list": []}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = int(pg)
        except Exception:
            page = 1
        if page < 1:
            page = 1
        ext = extend if isinstance(extend, dict) else {}
        tid = str(tid or "featured")
        if tid == "short":
            data = self._api("/app/movie/getShortVideoList?page=%d&pageSize=18" % page)
            return self._list_result(data, page)
        if tid == "featured":
            sort = str(ext.get("sort") or "2")
            data = self._api("/app/movie/getList?page=%d&pageSize=18&sort=%s" % (page, quote(sort, safe="")))
            return self._list_result(data, page)
        cid = tid
        sub = str(ext.get("sub") or "")
        if sub:
            cid = sub
        sort = str(ext.get("sort") or "1")
        data = self._api("/app/movie/getList?page=%d&pageSize=18&cid=%s&sort=%s" % (page, quote(cid, safe=""), quote(sort, safe="")))
        return self._list_result(data, page)

    def searchContent(self, key, quick, pg="1"):
        try:
            page = int(pg)
        except Exception:
            page = 1
        if page < 1:
            page = 1
        kw = str(key or "").strip()
        if not kw:
            return {"list": [], "page": page, "pagecount": page, "limit": 18, "total": 0}
        data = self._api("/app/movie/getList?keyword=%s&page=%d&pageSize=18" % (quote(kw, safe=""), page))
        return self._list_result(data, page)

    def detailContent(self, ids):
        raw_id = ids[0] if isinstance(ids, (list, tuple)) else str(ids)
        vk = str(raw_id or "").split("@@@")[0].strip()
        if not vk:
            return {"list": []}
        resp = self._api("/app/movie/getDetail?viewKey=%s" % quote(vk, safe=""))
        d = (resp or {}).get("data") if isinstance(resp, dict) else None
        if not isinstance(d, dict):
            return {"list": []}
        title = self._clean_title(d.get("title"))
        tags = []
        if d.get("tags"):
            for t in str(d.get("tags")).split(","):
                t = t.strip()
                if t:
                    tags.append(t)
        img_domain = self.imgFallback
        svo = d.get("sourceInfoVO")
        if isinstance(svo, dict) and svo.get("imgDomain"):
            img_domain = str(svo.get("imgDomain"))
        pic = self._pic(img_domain, d.get("posterOriginal") or d.get("poster"))

        episodes = []
        episodes.append("%s$%s@@@%s" % ("正片", vk, title))
        rel = self._api("/app/movie/getDetailRelationList?viewKey=%s&page=1&pageSize=18" % quote(vk, safe=""))
        if isinstance(rel, dict) and isinstance(rel.get("records"), list):
            for rec in rel.get("records"):
                if not isinstance(rec, dict) or not rec.get("viewKey"):
                    continue
                rvk = str(rec.get("viewKey"))
                if rvk == vk:
                    continue
                rtitle = self._clean_title(rec.get("title")) or "推荐视频"
                ep_name = rtitle.replace("$", "").replace("#", "").replace("@", "")
                episodes.append("%s$%s@@@%s" % (ep_name, rvk, rtitle))

        vod = {
            "vod_id": vk,
            "vod_name": title,
            "vod_pic": pic,
            "vod_bg": pic,
            "vod_background": pic,
            "vod_banner": pic,
            "vod_actor": self.brandActor,
            "vod_director": self.brandDirector,
            "vod_remarks": format_remarks("蝴蝶影视", self._fmt_dur(d.get("duration"))),
            "vod_content": "🦋 蝴蝶影视专线加速播放 | 官方TG交流群：@tvshare23 | 4K高清原生极速画质",
            "vod_play_from": "蝴蝶专线",
            "vod_play_url": "#".join(episodes)
        }
        if tags:
            vod["vod_tag"] = ",".join(tags)
        rd = str(d.get("releaseDate") or "")
        if len(rd) >= 4:
            vod["vod_year"] = rd[:4]
        return {"list": [vod]}

    def playerContent(self, flag, id, vipFlags):
        raw = str(id or "").strip()
        parts = raw.split("@@@")
        vk = parts[0].strip()
        ep_title = parts[1].strip() if len(parts) > 1 else ""
        headers = {
            "User-Agent": self._ua,
            "Referer": self.host + "/",
            "Origin": self.host
        }
        if not vk:
            return {"parse": 0, "jx": 0, "url": "", "header": headers}
        resp = self._api("/app/movie/getPlayUrl?viewKey=%s" % quote(vk, safe=""))
        data = (resp or {}).get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            return {"parse": 0, "jx": 0, "url": "", "header": headers}
        play_url = str(data.get("playUrl") or "")
        if not play_url:
            return {"parse": 0, "jx": 0, "url": "", "header": headers}
        domain = self.imgFallback
        infos = data.get("sourceInfoListVO")
        if isinstance(infos, list) and infos and isinstance(infos[0], dict) and infos[0].get("playDomain"):
            domain = str(infos[0].get("playDomain")).rstrip("/")
        if play_url.startswith("http"):
            full = play_url
        else:
            full = domain + play_url
        result = {"parse": 0, "jx": 0, "url": full, "header": headers}
        if ep_title:
            result["desc"] = ep_title
        return result

    def _sniff_mime(self, data):
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if len(data) >= 4 and data[:4] == b"\x89PNG":
            return "image/png"
        if len(data) >= 4 and data[:4] == b"GIF8":
            return "image/gif"
        return ""

    def localProxy(self, params):
        try:
            p = params if isinstance(params, dict) else {}
            target = str(p.get("url") or "")
            if "%" in target:
                target = unquote(target)
            parsed = urlparse(target)
            if parsed.scheme != "https" or parsed.hostname != "cmvpic.uitxn.cn":
                return [403, "text/plain; charset=utf-8", "host not allowed"]
            code, raw = self._fetch(target, {
                "User-Agent": self._ua,
                "Referer": self.host + "/",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive"
            }, timeout=8)
            if code != 200 or not raw:
                return [502, "text/plain; charset=utf-8", "upstream error"]
            if len(raw) > 20 * 1024 * 1024:
                return [413, "text/plain; charset=utf-8", "too large"]
            mime = self._sniff_mime(raw)
            if mime:
                return [200, mime, raw]
            plain = self._aes.decrypt(raw)
            if plain:
                mime = self._sniff_mime(plain)
                if mime:
                    return [200, mime, plain]
            return [415, "text/plain; charset=utf-8", "unknown image data"]
        except Exception:
            return [500, "text/plain; charset=utf-8", "proxy error"]

    def action(self, action):
        return {"msg": "ok"}

    def liveContent(self):
        return ""

    def isVideoFormat(self, url):
        low = str(url or "").lower()
        return (".m3u8" in low) or (".mp4" in low)

    def manualVideoCheck(self):
        return False

    def destroy(self):
        self.options = {}
