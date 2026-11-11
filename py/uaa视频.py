# -*- coding: utf-8 -*-
"""March APP — TVBox/CatVod Spider (video + comic, authorized account required)."""
import json
import re
import sys

sys.path.append('..')
try:
    from base.spider import Spider as _BaseSpider
except ImportError:
    class _BaseSpider:
        pass

try:
    import requests
except ImportError:
    requests = None

_API = bytes([104,116,116,112,115,58,47,47,97,112,105,46,109,97,114,99,104,50,52,49,54,56,46,111,110,108,105,110,101]).decode()
_CDN = bytes([104,116,116,112,115,58,47,47,99,100,110,46,117,97,109,101,116,97,46,97,105,47,102,105,108,101,47,98,117,99,107,101,116,45,109,101,100,105,97]).decode()
_UA = "Dart/3.11 (dart:io)"
# 播放器取流用浏览器 UA：部分 CDN 对 Dart UA 会返回 403
_PLAY_UA = ("Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
_PLAY_REFERER = "https://cdn.uameta.ai/"
_MEDIA_EXT = (".m3u8", ".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm", ".m4v",
              ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg")
_LOGIN_NAME = bytes([50,56,53,56,56,54,55,53,49,64,113,113,46,99,111,109]).decode()
_PASSWORD = bytes([113,119,101,114,52,51,50,49]).decode()

# 内容分类（接口 category 参数，实测仅这 5 个，均为全站真实分类）
_VIDEO_CATEGORIES = ("国产片", "日韩片", "欧美片", "无码流出", "H动漫")

# Only the user-selected major studios. The values match the API's `author`
# parameter, confirmed against the live authors list and search responses.
_VIDEO_CATS = (
    ("FC2", "FC2"),
    ("MOODYZ", "MOODYZ(Moody's)"),
    ("S1", "S1 No. 1 Style"),
    ("加勒比", "加勒比"),
    ("一本道", "一本道"),
    ("麻豆传媒", "麻豆传媒"),
)

_COMIC_CATS = (
    ("全彩", "全彩"),
    ("剧情", "剧情"),
    ("巨乳大奶", "巨乳大奶"),
    ("内射中出", "内射中出"),
    ("不伦", "不伦"),
    ("单女", "单女"),
    ("单男", "单男"),
)

_AUDIO_CATS = (
    ("有声小说", "有声小说"),
    ("淫词艳曲", "淫词艳曲"),
    ("激情骚麦", "激情骚麦"),
    ("寸止训练", "寸止训练"),
    ("ASMR", "ASMR"),
)

_NOVEL_CATS = (
    ("校园", "校园"),
    ("都市", "都市"),
    ("古代", "古代"),
    ("NP", "NP"),
    ("剧情", "剧情"),
    ("甜文", "甜文"),
    ("百合", "百合"),
    ("纯爱", "纯爱"),
)


class Spider(_BaseSpider):
    def init(self, extend=""):
        self.session = requests.Session()
        self.session.headers.update({"user-agent": _UA, "accept-encoding": "gzip"})
        self.token = ""
        self.items = {}
        # `extend` may override the embedded authorized account.
        try:
            config = json.loads(extend) if extend else {}
        except Exception:
            config = {}
        self.login_name = config.get("loginName") or _LOGIN_NAME
        self.password = config.get("password") or _PASSWORD

    def getName(self):
        return "March视频+漫画"

    def isVideoFormat(self, url):
        path = (url or "").split("?")[0].split("#")[0].lower()
        return path.endswith(_MEDIA_EXT)

    def manualVideoCheck(self):
        return False

    def _login(self):
        if self.token:
            return True
        if not self.login_name or not self.password:
            return False
        try:
            response = self.session.post(
                _API + "/console/app/login",
                params={"loginName": self.login_name, "password": self.password, "platform": "app"},
                timeout=25,
            )
            data = response.json()
            model = data.get("model") or {}
            self.token = model.get("token", "") if data.get("code") == 0 else ""
            return bool(self.token)
        except Exception as exc:
            print("[March] login:", exc)
            return False

    def _request(self, path, params=None, _retry=True):
        if not self._login():
            return None
        try:
            response = self.session.get(
                _API + path,
                params=params or {},
                headers={"token": self.token},
                timeout=25,
            )
            data = response.json()
            if data.get("code") == 0:
                return data.get("model") or {}
            # token 失效：清空后重登一次
            if response.status_code in (401, 403) or data.get("code") in (401, 403):
                self.token = ""
                if _retry:
                    return self._request(path, params, _retry=False)
            return None
        except Exception as exc:
            print("[March] request:", exc)
            return None

    def _cover(self, item):
        cover = item.get("coverUrl") or item.get("cover") or ""
        if cover.startswith("http"):
            return cover
        return _CDN + cover if cover.startswith("/") else ""

    @staticmethod
    def _dur(seconds):
        """秒 -> 时:分:秒，用于在列表上直接看片长。"""
        try:
            total = int(seconds or 0)
        except (TypeError, ValueError):
            return ""
        if total <= 0:
            return ""
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return "%d:%02d:%02d" % (hours, minutes, secs)
        return "%02d:%02d" % (minutes, secs)

    def _remark(self, item, *extra):
        parts = [p for p in ((item.get("categories") or item.get("tags") or item.get("tag"),)
                             + extra + (self._dur(item.get("duration")),)) if p]
        return " · ".join(parts)

    def _item(self, item):
        return {
            "vod_id": "video:" + str(item.get("id", "")),
            "vod_name": item.get("title") or item.get("number") or "未命名视频",
            "vod_pic": self._cover(item),
            "vod_remarks": self._remark(item),
        }

    def _videoItem(self, item):
        """视频列表项。播放地址直接带进 vod_id，这样收藏/历史在进程重启后也能播。"""
        vod = self._item(item)
        video_id = str(item.get("id", ""))
        url = item.get("url") or ""
        vod["vod_id"] = ("video:%s|%s" % (video_id, url)) if url else vod["vod_id"]
        return vod

    def homeContent(self, filter=False):
        # 一级分类：5 个内容分类 → 6 个片商 → 有声/漫画/小说
        classes = []
        for name in _VIDEO_CATEGORIES:
            classes.append({"type_id": "c:" + name, "type_name": name})
        for value, name in _VIDEO_CATS:
            classes.append({"type_id": "a:" + value, "type_name": name})
        classes.extend([
            {"type_id": "audio", "type_name": "有声"},
            {"type_id": "comic", "type_name": "漫画"},
            {"type_id": "novel", "type_name": "小说"},
        ])

        filters = {
            "audio": [
                {"key": "sub", "name": "分类",
                 "value": [{"n": "全部", "v": ""}] + [{"n": name, "v": value} for value, name in _AUDIO_CATS]}],
            "comic": [
                {"key": "sub", "name": "分类",
                 "value": [{"n": "全部", "v": ""}] + [{"n": name, "v": value} for value, name in _COMIC_CATS]}],
            "novel": [
                {"key": "sub", "name": "分类",
                 "value": [{"n": "全部", "v": ""}] + [{"n": name, "v": value} for value, name in _NOVEL_CATS]}],
        }
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        return self.categoryContent("video", 1)

    def categoryContent(self, tid, pg=1, filter=False, extend=None):
        page = max(1, int(pg))
        tid = str(tid)
        # `filter` 是 bool(是否带筛选)，`extend` 才是用户选择的筛选条件 dict
        if not isinstance(extend, dict):
            extend = {}
        if tid.startswith("c:"):          # 按内容分类（国产片/日韩片/欧美片/无码流出/H动漫）
            return self._videoCategoryContent(page, extend, category=tid[2:])
        if tid.startswith("a:"):          # 按片商（FC2/S1/麻豆传媒…）
            return self._videoCategoryContent(page, extend, author=tid[2:])
        if tid == "video":
            return self._videoCategoryContent(page, extend)
        elif tid == "comic":
            return self._comicCategoryContent(tid, page, extend)
        elif tid == "audio":
            return self._audioCategoryContent(tid, page, extend)
        elif tid == "novel":
            return self._novelCategoryContent(tid, page, extend)
        return {"list": [], "page": page, "pagecount": 1, "limit": 50, "total": 0}

    def _videoCategoryContent(self, page, filter_args=None, category="", author=""):
        params = {"orderType": 2, "page": page, "size": 50}
        if not isinstance(filter_args, dict):
            filter_args = {}
        if not category:
            category = filter_args.get("class", "") or ""
        if not author:
            author = filter_args.get("sub", "") or ""
        if category:
            params["category"] = category
        if author:
            params.update({"searchType": 2, "author": author})
        model = self._request("/video/app/video/search", params)
        if not model:
            return {"list": [], "page": page, "pagecount": 1, "limit": 50, "total": 0}
        data = model.get("data") or []
        for item in data:
            self.items["video:" + str(item.get("id", ""))] = item
        return {
            "list": [self._videoItem(item) for item in data],
            "page": model.get("currentPage", page),
            "pagecount": model.get("totalPage", 1),
            "limit": model.get("pageSize", 50),
            "total": model.get("totalCount", 0),
        }

    def _comicCategoryContent(self, tid, page, filter_args):
        params = {"orderType": 2, "page": page, "size": 50}
        if not isinstance(filter_args, dict):
            filter_args = {}
        sub = filter_args.get("sub", "")
        if sub:
            params["tag"] = sub
        model = self._request("/comic/app/comic/search", params)
        if not model:
            return {"list": [], "page": page, "pagecount": 1, "limit": 50, "total": 0}
        data = model.get("data") or []
        for item in data:
            # store with a prefix so detailContent can distinguish video vs comic
            self.items["comic:" + str(item.get("id", ""))] = item
        return {
            "list": [self._comicItem(item) for item in data],
            "page": model.get("currentPage", page),
            "pagecount": model.get("totalPage", 1),
            "limit": model.get("pageSize", 50),
            "total": model.get("totalCount", 0),
        }

    def _comicItem(self, item):
        return {
            "vod_id": "comic:" + str(item.get("id", "")),
            "vod_name": item.get("title") or item.get("number") or "未命名漫画",
            "vod_pic": self._cover(item),
            "vod_remarks": self._remark(item),
        }

    def _audioCategoryContent(self, tid, page, filter_args):
        params = {"orderType": 1, "page": page, "searchType": 1, "size": 42}
        if not isinstance(filter_args, dict):
            filter_args = {}
        sub = filter_args.get("sub", "")
        if sub:
            params["category"] = sub
        model = self._request("/audio/app/audio/search", params)
        if not model:
            return {"list": [], "page": page, "pagecount": 1, "limit": 42, "total": 0}
        data = model.get("data") or []
        for item in data:
            self.items["audio:" + str(item.get("id", ""))] = item
        return {
            "list": [self._audioItem(item) for item in data],
            "page": model.get("currentPage", page),
            "pagecount": model.get("totalPage", 9999),
            "limit": model.get("pageSize", 42),
            "total": model.get("totalCount", 0),
        }

    def _audioItem(self, item):
        return {
            "vod_id": "audio:" + str(item.get("id", "")),
            "vod_name": item.get("title") or "未命名音频",
            "vod_pic": self._cover(item),
            "vod_remarks": self._remark(item),
        }

    def _novelCategoryContent(self, tid, page, filter_args):
        params = {"orderType": 2, "page": page, "size": 50}
        if not isinstance(filter_args, dict):
            filter_args = {}
        sub = filter_args.get("sub", "")
        if sub:
            params["tag"] = sub
        model = self._request("/novel/app/novel/search", params)
        if not model:
            return {"list": [], "page": page, "pagecount": 1, "limit": 50, "total": 0}
        data = model.get("data") or []
        for item in data:
            self.items["novel:" + str(item.get("id", ""))] = item
        return {
            "list": [self._novelItem(item) for item in data],
            "page": model.get("currentPage", page),
            "pagecount": model.get("totalPage", 1),
            "limit": model.get("pageSize", 50),
            "total": model.get("totalCount", 0),
        }

    def _novelItem(self, item):
        return {
            "vod_id": "novel:" + str(item.get("id", "")),
            "vod_name": item.get("title") or "未命名小说",
            "vod_pic": self._cover(item),
            "vod_remarks": self._remark(item),
        }

    def _po18_chapters(self, source_url):
        """从 po18.tw sourceUrl 解析章节列表（标题+url）。"""
        m = re.search(r'po18\.tw/books/(\d+)', source_url or "")
        if not m:
            return []
        book_id = m.group(1)
        articles_url = "https://www.po18.tw/books/%s/articles" % book_id
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36"
        try:
            r = self.fetch(articles_url, headers={"User-Agent": ua}, timeout=20, verify=False)
            body = r.text or ""
        except Exception as exc:
            print("[March] po18:", exc)
            return []
        chs = re.findall(r'<a[^>]*href="(/books/%s/articles/(\d+))"[^>]*>(.*?)</a>' % book_id, body, re.S)
        result = []
        seen = set()
        for href, aid, title in chs:
            if aid in seen:
                continue
            seen.add(aid)
            t = re.sub(r'<[^>]+>', '', title).strip()
            if t:
                result.append(("https://www.po18.tw" + href, t))
        return result

    def _audioChapterUrl(self, chapter_id):
        if not chapter_id:
            return ""
        model = self._request("/audio/app/audio/chapter", {"id": chapter_id})
        if not model:
            return ""
        return model.get("chapterUrl") or ""

    def _wnacg_images(self, source_url):
        """从 wnacg sourceUrl 解析图集图片列表（先 index 拿 cookie 再 gallery 提 imglist）。"""
        m = re.search(r'photos-index-aid-(\d+)', source_url or "")
        if not m:
            return []
        aid = m.group(1)
        index_url = "https://www.wnacg.com/photos-index-aid-%s.html" % aid
        gallery_url = "https://www.wnacg.com/photos-gallery-aid-%s.html" % aid
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36"
        try:
            r1 = self.fetch(index_url, headers={"User-Agent": ua}, timeout=15, verify=False)
            r2 = self.fetch(gallery_url, headers={"User-Agent": ua, "Referer": index_url},
                            cookies=r1.cookies, timeout=15, verify=False)
            text = r2.text or ""
        except Exception as exc:
            print("[March] wnacg:", exc)
            return []
        urls = re.findall(r'url:\s*fast_img_host\+"([^"]+)"', text)
        if not urls:
            urls = re.findall(r'//img\d+\.qy0\.ru/data/[^"\s]+\.webp', text)
        return [("https:" + u if u.startswith("//") else u) for u in urls]

    def detailContent(self, ids):
        raw_id = str(ids[0]) if ids else ""

        # audio 走独立详情接口（intro + chapter），不依赖内存缓存
        if raw_id.startswith("audio:"):
            audio_id = raw_id[len("audio:"):]
            model = self._request("/audio/app/audio/intro", {"id": audio_id})
            if not model:
                return {"list": []}
            play_list = []
            chapters = model.get("chapters") or []
            for chapter in chapters:
                chapter_id = chapter.get("id", "")
                chapter_title = chapter.get("title") or "第%d集" % (chapter.get("order") or 1)
                chapter_url = self._audioChapterUrl(chapter_id)
                if chapter_url:
                    play_list.append("%s$%s" % (chapter_title, chapter_url))
            if not play_list and model.get("latestReadChapterUrl"):
                play_list.append("第1集$%s" % model.get("latestReadChapterUrl"))
            play_url = "#".join(play_list) if play_list else ""
            vod = {
                "vod_id": raw_id,
                "vod_name": model.get("title") or "未命名音频",
                "vod_pic": self._cover(model),
                "vod_content": model.get("intro") or model.get("brief") or "",
                "vod_actor": model.get("author") or "",
                "vod_play_from": "UAA",
                "vod_play_url": play_url,
            }
            return {"list": [vod]}

        # novel 走独立详情接口（intro + po18 章节列表），不依赖内存缓存
        if raw_id.startswith("novel:"):
            novel_id = raw_id[len("novel:"):]
            model = self._request("/novel/app/novel/intro", {"id": novel_id})
            if not model:
                return {"list": []}
            vod = self._novelItem(model)
            vod.update({
                "vod_content": model.get("brief") or model.get("description") or "",
                "vod_actor": model.get("authors") or "",
                "vod_play_from": "小说",
            })
            chapters = self._po18_chapters(model.get("sourceUrl") or "")
            vod["vod_play_url"] = "#".join("%s$%s" % (t, u) for u, t in chapters) if chapters else ""
            return {"list": [vod]}

        # comic 走独立详情接口（intro + wnacg 图集解析），不依赖内存缓存
        if raw_id.startswith("comic:"):
            comic_id = raw_id[len("comic:"):]
            model = self._request("/comic/app/comic/intro", {"id": comic_id})
            if not model:
                return {"list": []}
            vod = self._comicItem(model)
            vod.update({
                "vod_content": model.get("brief") or model.get("description") or "",
                "vod_actor": model.get("authors") or "",
                "vod_play_from": "图文",
            })
            imgs = self._wnacg_images(model.get("sourceUrl") or "")
            vod["vod_play_url"] = "图片$pics://" + "&&".join(imgs) if imgs else ""
            return {"list": [vod]}

        # video：优先内存缓存，缓存丢失时用 vod_id 里带的地址兜底
        if raw_id.startswith("video:"):
            body = raw_id[len("video:"):]
            video_id, _, inline_url = body.partition("|")
            item = self.items.get("video:" + video_id) or {}
            url = item.get("url") or inline_url or ""
            if not item and not url:
                return {"list": []}
            vod = self._item(item) if item else {
                "vod_id": raw_id, "vod_name": "视频 " + video_id,
                "vod_pic": "", "vod_remarks": "",
            }
            vod["vod_id"] = raw_id
            # 简介前面补上时长/分类，方便判断片子本来就多长（源站不少是 15 分钟短片）
            intro = item.get("brief") or item.get("description") or ""
            meta = []
            if item.get("categories"):
                meta.append("分类：" + str(item["categories"]))
            length = self._dur(item.get("duration"))
            if length:
                meta.append("时长：" + length)
            if item.get("uploader"):
                meta.append("上传：" + str(item["uploader"]))
            if meta:
                intro = "　".join(meta) + ("\n\n" + intro if intro else "")
            vod.update({
                "vod_content": intro,
                "vod_actor": item.get("actress") or item.get("authors") or "",
                "vod_play_from": "官方线路",
                "vod_play_url": "播放$" + url if url else "",
            })
            return {"list": [vod]}

        return {"list": []}

    def playerContent(self, flag, id, vipFlags=None):
        if not id:
            return {"url": ""}
        # 漫画图文: pics://url1&&url2&&... PeekPro 图片阅读器识别
        if id.startswith("pics://"):
            header = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://www.wnacg.com/",
            }
            return {"parse": 0, "url": id, "header": json.dumps(header), "position": "0"}
        # 小说章节: po18.tw 网页，用 webview 加载（需 po18 会员登录）
        if "po18.tw" in id:
            header = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "https://www.po18.tw/",
            }
            return {"parse": 0, "url": id, "header": json.dumps(header), "position": "0"}
        # 关键修复：直链（m3u8/mp4/mp3）必须显式返回 parse=0。
        # OK影视/CatVod 内核在缺省 parse 时按 1 处理，会拿直链去网页嗅探 → 一直转圈或解析失败。
        header = {"User-Agent": _PLAY_UA, "Referer": _PLAY_REFERER}
        return {"parse": 0, "url": id, "header": json.dumps(header), "position": "0"}

    def searchContent(self, key, quick=False, pg=1):
        key = (key or "").strip()
        if not key:
            return {"list": []}
        page = max(1, int(pg))
        result = []
        pagecount, total = 1, 0

        # 视频用 keyword，其余三个分类用 wd（实测参数不同）
        sources = (
            ("/video/app/video/search", "keyword", "video", self._videoItem),
            ("/comic/app/comic/search", "wd", "comic", self._comicItem),
            ("/audio/app/audio/search", "wd", "audio", self._audioItem),
            ("/novel/app/novel/search", "wd", "novel", self._novelItem),
        )
        for path, key_param, prefix, builder in sources:
            # 翻页只做视频，其余三类只出第一页，避免分页错乱
            if prefix != "video" and page > 1:
                continue
            model = self._request(path, {
                key_param: key,
                "page": page,
                "size": 50,
                "searchType": 1,
                "orderType": 2,
            })
            if not model:
                continue
            data = model.get("data") or []
            for item in data:
                self.items[prefix + ":" + str(item.get("id", ""))] = item
            result.extend(builder(item) for item in data)
            if prefix == "video":
                pagecount = model.get("totalPage", 1) or 1
                total = model.get("totalCount", 0) or 0
        total = max(total, len(result))
        return {"list": result, "page": page, "pagecount": pagecount,
                "limit": 50, "total": total}

    def localProxy(self, param):
        return [404, "text/plain", b""]
