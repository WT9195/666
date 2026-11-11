# -*- coding: utf-8 -*-
"""
豆瓣导航 Spider（CatVod / TVBox）
==================================================
相对旧版的优化点：
1. [BUG] 分页推进 offset += count 改为按实际返回条数推进，修复漏数据
2. [BUG] 空筛选拼出 ",," / ",,," 脏 tags，现过滤空值
3. [BUG] episodes_info 从 recent_hot 榜单按 start 取、命中率极低 -> 改为缓存式按需补全
4. [BUG] hot_anime 双接口共用 start 导致翻页错位 -> 改为电影优先、剧集补足
5. [BUG] anime 缺少翻页循环 -> 统一走公共分页取数
6. [BUG] hot_anime 默认值与筛选 UI 首项不一致
7. [性能] 缓存加容量上限 + TTL 清理 + 线程锁；请求加重试
8. [功能] 补全 detailContent / searchContent / 首页推荐
9. [健壮] 路径导入、基类遮蔽、异常兜底、筛选 key 中英文兼容
"""
import os
import re
import sys
import json
import time
import threading
from collections import OrderedDict
from urllib.parse import quote, urlencode

import requests

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    HTTPAdapter = None
    Retry = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.abspath(os.path.join(BASE_DIR, '..')) not in sys.path:
    sys.path.append(os.path.abspath(os.path.join(BASE_DIR, '..')))

try:
    from base.spider import Spider as BaseSpider
except Exception:  # 便于脱离框架单独做语法/逻辑测试
    class BaseSpider(object):
        pass


# ================= 常量 =================
DOUBAN_HOST = "https://frodo.douban.com/api/v2"
DOUBAN_API_KEY = "0ac44ae016490db2204ce0a042db2916"
DOUBAN_UA = ("Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/53.0.2785.143 Safari/537.36 "
             "MicroMessenger/7.0.9.501 NetType/WIFI MiniProgramEnv/Windows WindowsWechat")
DOUBAN_REFERER = "https://servicewechat.com/wx2f9b06c1de1ccfca/84/page-frame.html"

PIC_SUFFIX = ('@Referer=https://api.douban.com/'
              '@User-Agent=Mozilla/5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20'
              'AppleWebKit/537.36%20(KHTML,%20like%20Gecko)%20Chrome/122.0.0.0%20Safari/537.36')

M_DOUBAN_HOST = "https://m.douban.com/rexxar/api/v2"
M_DOUBAN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
M_DOUBAN_REFERER_TV = "https://movie.douban.com/tv/"
M_DOUBAN_REFERER_MOVIE = "https://movie.douban.com/explore"

PAGE_SIZE = 40          # 对外每页条数
API_PAGE_LIMIT = 20     # 豆瓣单页安全上限，超过可能被截断
MAX_LOOP = 8            # 单次取数最多翻几页


# ================= 筛选配置 =================
def _opts(items, label="全部"):
    return [{"n": label, "v": ""}] + [{"n": i, "v": i} for i in items]


_MOVIE_GENRES = ["喜剧", "爱情", "动作", "科幻", "动画", "悬疑", "犯罪", "惊悚",
                 "冒险", "音乐", "历史", "奇幻", "恐怖", "战争", "传记", "歌舞",
                 "武侠", "情色", "灾难", "西部", "纪录片", "短片"]
_MOVIE_REGIONS = ["华语", "欧美", "韩国", "日本", "中国大陆", "美国", "中国香港",
                  "中国台湾", "英国", "法国", "德国", "意大利", "西班牙", "印度",
                  "泰国", "俄罗斯", "加拿大", "澳大利亚", "爱尔兰", "瑞典", "巴西", "丹麦"]
_YEARS = ["2026", "2025", "2024", "2023", "2022", "2021", "2020", "2019",
          "2020年代", "2010年代", "2000年代", "90年代", "80年代", "70年代", "60年代", "更早"]
_TV_GENRES = ["喜剧", "爱情", "悬疑", "动画", "武侠", "古装", "家庭", "犯罪",
              "科幻", "恐怖", "历史", "战争", "动作", "冒险", "传记", "剧情",
              "奇幻", "惊悚", "灾难", "歌舞", "音乐"]
_TV_PLATFORMS = ["腾讯视频", "爱奇艺", "优酷", "湖南卫视", "Netflix", "HBO",
                 "BBC", "NHK", "CBS", "NBC", "tvN"]
_SHOW_GENRES = ["真人秀", "脱口秀", "音乐", "喜剧", "纪实"]
_SHOW_REGIONS = ["中国大陆", "韩国", "港台", "欧美"]
_ANIME_GENRES = ["动画", "日本动画", "国产动画", "欧美动画", "剧场版", "番剧"]
_ANIME_REGIONS = ["日本", "中国大陆", "美国", "欧美"]

_SORT_MOVIE = [{"n": "热度", "v": "U"}, {"n": "评分", "v": "S"}, {"n": "时间", "v": "R"}]
_SORT_SHOW = [{"n": "近期热度", "v": "U"}, {"n": "综合排序", "v": "T"},
              {"n": "首播时间", "v": "R"}, {"n": "高分优先", "v": "S"}]

FILTER_CONFIG = {
    "hot_movie": [
        {"key": "sort", "name": "排序", "value": [
            {"n": "热度", "v": "recommend"},
            {"n": "最新", "v": "time"},
            {"n": "评分", "v": "rank"},
        ]},
        {"key": "area", "name": "地区", "value": [
            {"n": "全部", "v": "全部"},
            {"n": "华语", "v": "华语"},
            {"n": "欧美", "v": "欧美"},
            {"n": "韩国", "v": "韩国"},
            {"n": "日本", "v": "日本"},
        ]},
    ],
    "tv_hot": [
        {"key": "type", "name": "分类", "value": [
            {"n": "综合", "v": "tv_hot"},
            {"n": "国产剧", "v": "tv_domestic"},
            {"n": "欧美剧", "v": "tv_american"},
            {"n": "日剧", "v": "tv_japanese"},
            {"n": "韩剧", "v": "tv_korean"},
            {"n": "动画", "v": "tv_animation"},
        ]},
    ],
    "anime_hot": [
        {"key": "type", "name": "分类", "value": [
            {"n": "全部", "v": "全部"},
            {"n": "国漫", "v": "国漫"},
            {"n": "日漫", "v": "日漫"},
            {"n": "美漫", "v": "美漫"},
            {"n": "韩漫", "v": "韩漫"},
            {"n": "港漫", "v": "港漫"},
            {"n": "台漫", "v": "台漫"},
        ]},
    ],
    "show_hot": [
        {"key": "type", "name": "分类", "value": [
            {"n": "综合", "v": "show_hot"},
            {"n": "国内", "v": "show_domestic"},
            {"n": "国外", "v": "show_foreign"},
        ]},
    ],
    "movie_filter": [
        {"key": "genre", "name": "类型", "value": _opts(_MOVIE_GENRES, "全部类型")},
        {"key": "region", "name": "地区", "value": _opts(_MOVIE_REGIONS, "全部地区")},
        {"key": "year", "name": "年代", "value": _opts(_YEARS, "全部年代")},
        {"key": "sort", "name": "排序", "value": _SORT_MOVIE},
    ],
    "tv_filter": [
        {"key": "genre", "name": "类型", "value": _opts(_TV_GENRES, "全部类型")},
        {"key": "region", "name": "地区", "value": _opts(_MOVIE_REGIONS, "全部地区")},
        {"key": "year", "name": "年代", "value": _opts(_YEARS, "全部年代")},
        {"key": "platform", "name": "平台", "value": _opts(_TV_PLATFORMS, "全部平台")},
        {"key": "sort", "name": "排序", "value": _SORT_MOVIE},
    ],
    "show_filter": [
        {"key": "genre", "name": "类型", "value": _opts(_SHOW_GENRES, "全部类型")},
        {"key": "region", "name": "地区", "value": _opts(_SHOW_REGIONS, "全部地区")},
        {"key": "year", "name": "年代", "value": _opts(_YEARS[:8], "全部年代")},
        {"key": "sort", "name": "排序", "value": _SORT_SHOW},
    ],
    "anime": [
        {"key": "genre", "name": "类型", "value": _opts(_ANIME_GENRES, "全部类型")},
        {"key": "region", "name": "地区", "value": _opts(_ANIME_REGIONS, "全部地区")},
        {"key": "year", "name": "年代", "value": _opts(_YEARS[:8], "全部年代")},
        {"key": "sort", "name": "排序", "value": _SORT_SHOW},
    ],
    "hot_anime": [
        {"key": "slug", "name": "榜单", "value": [
            {"n": "全部动漫", "v": "all"},
            {"n": "最新动漫", "v": "anime_recent"},
            {"n": "近期热门动漫", "v": "anime_hot"},
            {"n": "高分动漫", "v": "anime_best"},
        ]},
    ],
    "top_250": [
        {"key": "genre", "name": "类型", "value": _opts(_MOVIE_GENRES, "全部类型")},
        {"key": "region", "name": "地区", "value": _opts(_MOVIE_REGIONS, "全部地区")},
        {"key": "year", "name": "年代", "value": _opts(_YEARS, "全部年代")},
        {"key": "sort", "name": "排序", "value": [
            {"n": "豆瓣排名", "v": ""}, {"n": "评分", "v": "S"}, {"n": "时间", "v": "R"},
        ]},
    ],
}

# 地区聚合：华语 / 欧美
_WESTERN = {"美国", "英国", "法国", "德国", "意大利", "西班牙", "加拿大",
            "澳大利亚", "爱尔兰", "瑞典", "丹麦", "巴西", "俄罗斯"}
_CHINESE = {"中国大陆", "中国香港", "中国台湾", "华语"}

# 筛选 key 兼容：旧版 show_filter / anime 用的是中文 key
_KEY_ALIAS = {
    'genre': ('genre', '类型'),
    'region': ('region', '地区'),
    'year': ('year', '年代'),
}


class _TTLCache(object):
    """带容量上限 + TTL 的线程安全缓存（避免长期运行内存无限增长）"""

    def __init__(self, max_size=64, ttl=600):
        self.max_size = max_size
        self.ttl = ttl
        self._data = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            if time.time() - hit['time'] > self.ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return hit['data']

    def set(self, key, data):
        with self._lock:
            self._data[key] = {'data': data, 'time': time.time()}
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)


class Spider(BaseSpider):
    def init(self, extend=""):
        self.session = requests.Session()
        if HTTPAdapter and Retry:
            retry = Retry(total=2, backoff_factor=0.3,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=frozenset(['GET']))
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=20)
            self.session.mount('https://', adapter)
            self.session.mount('http://', adapter)

        self.headers = {
            "User-Agent": DOUBAN_UA,
            "Referer": DOUBAN_REFERER,
            "Host": "frodo.douban.com",
            "Accept": "application/json",
            "Connection": "Keep-Alive",
        }
        self.page_cache = _TTLCache(max_size=96, ttl=600)
        self.top250_cache = _TTLCache(max_size=2, ttl=3600)
        self.ep_cache = _TTLCache(max_size=4, ttl=1800)
        self.debug = False

    # ---------------- 基础工具 ----------------
    def _log(self, msg):
        if self.debug:
            print("[豆瓣导航] %s" % msg)

    @staticmethod
    def _pick(filters, *keys):
        """兼容中英文筛选 key"""
        for k in keys:
            v = filters.get(k)
            if v:
                return str(v).strip()
        return ''

    def _f(self, filters, name):
        return self._pick(filters, *_KEY_ALIAS.get(name, (name,)))

    # ---------------- HTTP ----------------
    def _request_douban(self, url, timeout=15):
        sep = '&' if '?' in url else '?'
        try:
            resp = self.session.get("%s%sapikey=%s" % (url, sep, DOUBAN_API_KEY),
                                    headers=self.headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            self._log("frodo %s -> %s" % (resp.status_code, url[:80]))
        except Exception as e:
            self._log("豆瓣请求失败: %s..., %s" % (url[:80], e))
        return None

    def _request_mdouban(self, url, referer=None):
        headers = {
            "User-Agent": M_DOUBAN_UA,
            "Referer": referer or M_DOUBAN_REFERER_TV,
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=12)
            if resp.status_code == 200:
                return resp.json()
            self._log("rexxar %s -> %s" % (resp.status_code, url[:80]))
        except Exception as e:
            self._log("m.douban请求失败: %s, %s" % (url[:80], e))
        return None

    # ---------------- 解析工具 ----------------
    def _extract_full_date(self, s):
        if not s:
            return ''
        m = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', str(s))
        return "%s.%s.%s" % (m[1], m[2].zfill(2), m[3].zfill(2)) if m else ''

    def _get_subject_id(self, item):
        if not isinstance(item, dict):
            return ''
        sub = item.get('subject') or {}
        if not isinstance(sub, dict):
            sub = {}
        for c in (sub.get('id'), item.get('id'),
                  item.get('subject_id'), item.get('target_id'), item.get('sid')):
            if c and re.fullmatch(r'\d+', str(c)):
                return str(c).strip()
        return ''

    def _normalize_mdouban_item(self, raw):
        item = dict(raw)
        pic = raw.get('pic')
        if isinstance(pic, dict):
            item['cover'] = {'url': pic.get('large') or pic.get('normal') or '',
                             'normal': pic.get('normal') or ''}
        elif isinstance(pic, str):
            item['cover'] = {'url': pic, 'normal': pic}
        subtitle = raw.get('card_subtitle') or raw.get('subtitle') or ''
        if subtitle:
            ym = re.search(r'^(\d{4})', str(subtitle))
            if ym:
                item['year'] = ym[1]
        return item

    @staticmethod
    def _fmt_score(rating_val):
        try:
            v = float(rating_val)
            return "%.1f" % v if v > 0 else '0'
        except (TypeError, ValueError):
            return '0'

    def _parse_item(self, item):
        if not isinstance(item, dict):
            return None
        sub = item.get('subject') or {}
        if not isinstance(sub, dict):
            sub = {}
        title = item.get('title') or sub.get('title') or '未知'
        raw_id = self._get_subject_id(item)
        if not raw_id:
            return None

        rating = item.get('rating') or sub.get('rating') or {}
        if not isinstance(rating, dict):
            rating = {}
        rating_val = rating.get('value')

        pic_obj = (item.get('cover') or item.get('pic')
                   or sub.get('cover') or sub.get('pic') or {})
        if isinstance(pic_obj, dict):
            pic_url = pic_obj.get('url') or pic_obj.get('normal') or pic_obj.get('large') or ''
        elif isinstance(pic_obj, str):
            pic_url = pic_obj
        else:
            pic_url = ''
        if pic_url:
            pic_url = re.sub(r'img\d+\.doubanio\.com', 'img1.doubanio.com', pic_url)
            pic = pic_url + PIC_SUFFIX
        else:
            pic = ''

        pubdate = ''
        for k in ('pubdate', 'release_date'):
            val = sub.get(k) or item.get(k) or ''
            if val:
                pubdate = val
                break
        year_str = sub.get('year') or item.get('year') or ''
        if not pubdate and year_str:
            pubdate = year_str
        if not pubdate:
            card = item.get('card_subtitle') or sub.get('card_subtitle') or ''
            cm = re.match(r'^(\d{4})', str(card))
            if cm:
                pubdate = cm.group(1)
        if not pubdate and title:
            ym = re.search(r'\((\d{4})\)|\[(\d{4})\]|(\d{4})年', str(title))
            if ym:
                pubdate = ym.group(1) or ym.group(2) or ym.group(3)

        # 旧版缺陷：只有 card_subtitle 里的年份、没有 year 字段时，年份会被丢弃
        fd = self._extract_full_date(pubdate) or self._extract_full_date(year_str)
        if fd:
            date_label = fd
        else:
            ym = re.match(r'^(\d{4})', str(year_str or pubdate))
            date_label = ym.group(1) if ym else str(year_str or pubdate or '').strip()
        score_str = self._fmt_score(rating_val)
        remarks = "%s / %s分" % (date_label, score_str) if date_label else "%s分" % score_str

        ep_info = item.get('episodes_info') or sub.get('episodes_info') or ''
        if ep_info and isinstance(ep_info, str):
            ep_info = re.sub(r'更新至第(\d+)集', r'更新至\1集', ep_info)
            ep_info = re.sub(r'共(\d+)集', r'全\1集', ep_info)
            ep_info = re.sub(r'(\d+)集全', r'全\1集', ep_info).strip()
        else:
            ep_count = sub.get('episodes_count') or item.get('episodes_count') or 0
            cur_ep = sub.get('current_episode') or item.get('current_episode') or 0
            if cur_ep:
                ep_info = "更新至%s集" % cur_ep
            elif ep_count:
                ep_info = "全%s集" % ep_count
            else:
                ep_info = ''

        return {
            'vod_id': raw_id,
            'vod_name': title,
            'vod_pic': pic,
            'vod_remarks': remarks,
            'vod_year': ep_info,  # 沿用旧约定：列表页 vod_year 位展示集数
        }

    # ---------------- 通用分页取数 ----------------
    def _paginate(self, fetcher, start, count):
        """按实际返回条数推进 offset，避免跳页漏数据"""
        result, seen, offset = [], set(), start
        for _ in range(MAX_LOOP):
            want = min(API_PAGE_LIMIT, count - len(result))
            if want <= 0:
                break
            arr = fetcher(offset, want) or []
            if not arr:
                break
            added = 0
            for it in arr:
                sid = self._get_subject_id(it)
                if sid and sid not in seen:
                    seen.add(sid)
                    result.append(it)
                    added += 1
            if not added:
                break
            offset += len(arr)  # 关键：按接口实际返回条数推进
        return result

    def _fetch_rexxar_items(self, base_url, referer, start, count, selected, tags, sort):
        def fetcher(offset, want):
            url = ("%s?refresh=0&start=%s&count=%s"
                   "&selected_categories=%s"
                   "&uncollect=false&score_range=0,10"
                   "&tags=%s&sort=%s" % (base_url, offset, want,
                                         quote(json.dumps(selected, ensure_ascii=False)),
                                         quote(tags), sort))
            data = self._request_mdouban(url, referer)
            return (data or {}).get('items')

        items = self._paginate(fetcher, start, count)
        return [self._normalize_mdouban_item(it) for it in items]

    def _fetch_frodo_items(self, path, params, start, count, key='items'):
        def fetcher(offset, want):
            p = dict(params)
            p['start'], p['count'] = offset, want
            data = self._request_douban("%s%s?%s" % (DOUBAN_HOST, path, urlencode(p)))
            return (data or {}).get(key)

        return self._paginate(fetcher, start, count)

    @staticmethod
    def _join_tags(*parts):
        """过滤空值后再拼 tags，避免全部未选时产生 ',,' """
        return ','.join([p for p in parts if p])

    # ---------------- 各数据源 ----------------
    def _fetch_movie_filter(self, start, count, filters):
        genre = self._f(filters, 'genre')
        region = self._f(filters, 'region')
        year = self._f(filters, 'year')
        sort = self._pick(filters, 'sort') or 'U'
        selected = {}
        if genre:
            selected["类型"] = genre
        if region:
            selected["地区"] = region
        tags = self._join_tags(genre, region, year)
        return self._fetch_rexxar_items(
            "%s/movie/recommend" % M_DOUBAN_HOST, M_DOUBAN_REFERER_MOVIE,
            start, count, selected, tags, sort)

    def _fetch_tv_filter(self, start, count, filters):
        genre = self._f(filters, 'genre')
        region = self._f(filters, 'region')
        year = self._f(filters, 'year')
        platform = self._pick(filters, 'platform')
        sort = self._pick(filters, 'sort') or 'U'
        selected = {"形式": "电视剧"}
        if genre:
            selected["类型"] = genre
        if region:
            selected["地区"] = region
        tags = self._join_tags(genre, region, year, platform)
        return self._fetch_rexxar_items(
            "%s/tv/recommend" % M_DOUBAN_HOST, M_DOUBAN_REFERER_TV,
            start, count, selected, tags, sort)

    def _fetch_show_filter(self, start, count, filters):
        tags = self._join_tags(self._f(filters, 'genre'),
                               self._f(filters, 'region'),
                               self._f(filters, 'year'), '综艺')
        sort = self._pick(filters, 'sort') or 'U'
        return self._fetch_frodo_items("/tv/recommend",
                                       {'tags': tags, 'sort': sort}, start, count)

    def _fetch_anime(self, start, count, filters):
        tags = self._join_tags(self._f(filters, 'genre'),
                               self._f(filters, 'region'),
                               self._f(filters, 'year'), '动画')
        sort = self._pick(filters, 'sort') or 'U'
        return self._fetch_frodo_items("/tv/recommend",
                                       {'tags': tags, 'sort': sort}, start, count)

    def _fetch_hot_anime(self, start, count, filters):
        """动漫电影：电影源优先，不足时用剧集源补足，避免两源共用 start 造成错位"""
        slug = self._pick(filters, 'slug') or 'all'
        sort = {'all': 'U', 'anime_hot': 'U',
                'anime_best': 'S', 'anime_recent': 'R'}.get(slug, 'U')
        params = {'tags': '动画', 'sort': sort}

        items = self._fetch_frodo_items("/movie/recommend", params, start, count)
        if len(items) < count:
            more = self._fetch_frodo_items("/tv/recommend", params, start, count - len(items))
            seen = {self._get_subject_id(i) for i in items}
            for it in more:
                sid = self._get_subject_id(it)
                if sid and sid not in seen:
                    seen.add(sid)
                    items.append(it)
        return items

    # ---------------- 热播/热门入口 ----------------
    def _fetch_hot_movie(self, start, count, filters):
        sort = self._pick(filters, 'sort') or 'recommend'
        area = self._pick(filters, 'area') or '全部'
        return self._fetch_frodo_items(
            "/movie/hot_gaia", {'sort': sort, 'area': area}, start, count)

    def _fetch_collection(self, col, start, count):
        return self._fetch_frodo_items("/subject_collection/%s/items" % col, {},
                                       start, count, key='subject_collection_items')

    def _fetch_tv_hot(self, start, count, filters):
        col = self._pick(filters, 'type') or 'tv_hot'
        return self._fetch_collection(col, start, count)

    def _fetch_show_hot(self, start, count, filters):
        col = self._pick(filters, 'type') or 'show_hot'
        return self._fetch_collection(col, start, count)

    def _fetch_anime_hot(self, start, count, filters):
        choice = self._pick(filters, 'type') or '全部'
        if choice == '全部':
            return self._fetch_collection('tv_animation', start, count)
        tag_map = {
            '国漫': '大陆,动画', '日漫': '日本,动画', '美漫': '欧美,动画',
            '韩漫': '韩国,动画', '港漫': '香港,动画', '台漫': '台湾,动画',
        }
        tags = tag_map.get(choice, '动画')
        return self._fetch_frodo_items("/tv/recommend",
                                       {'tags': tags, 'sort': 'U'}, start, count)

    # ---------------- Top250：全量 + 本地筛选 ----------------
    def _get_top250_all(self):
        cached = self.top250_cache.get('all')
        if cached:
            return cached
        items, seen = [], set()
        ok = False
        for start in range(0, 250, 50):
            data = self._request_douban(
                "%s/subject_collection/movie_top250/items?start=%s&count=50"
                % (DOUBAN_HOST, start))
            if not data:
                break
            arr = data.get('subject_collection_items') or data.get('items') or []
            if not arr:
                break
            ok = True
            for it in arr:
                sid = self._get_subject_id(it)
                if sid and sid not in seen:
                    seen.add(sid)
                    items.append(it)
        # 抓取不完整时不写入长缓存，避免"残缺榜单"被缓存 1 小时
        if items and (ok and len(items) >= 100):
            self.top250_cache.set('all', items)
        return items

    @staticmethod
    def _split_card(item):
        sub = item.get('subject') or {}
        if not isinstance(sub, dict):
            sub = {}
        card = item.get('card_subtitle') or sub.get('card_subtitle') or ''
        parts = [p.strip() for p in str(card).split('/')]
        year = parts[0] if len(parts) > 0 else ''
        regions = parts[1].split() if len(parts) > 1 else []
        genres = parts[2].split() if len(parts) > 2 else []
        return year, regions, genres

    @staticmethod
    def _year_match(item_year, filter_year):
        m = re.match(r'(\d{4})', str(item_year))
        if not m:
            return False
        y = int(m.group(1))
        fy = str(filter_year)
        if fy.isdigit():
            return y == int(fy)
        m = re.match(r'(\d{4})年代', fy)
        if m:
            base = int(m.group(1))
            return base <= y < base + 10
        m = re.match(r'(\d{2})年代', fy)
        if m:
            d = int(m.group(1))
            base = 1900 + d if d >= 30 else 2000 + d
            return base <= y < base + 10
        if fy == '更早':
            return y < 1960
        return True

    def _top250_match(self, item, genre, region, year):
        item_year, regions, genres = self._split_card(item)
        if not item_year:
            sub = item.get('subject') or {}
            item_year = item.get('year') or (sub.get('year') if isinstance(sub, dict) else '') or ''
        if genre and genre not in genres:
            return False
        if region:
            if region == '华语':
                if not any(r in _CHINESE for r in regions):
                    return False
            elif region == '欧美':
                if not any(r in _WESTERN for r in regions):
                    return False
            elif region not in regions:
                return False
        if year and not self._year_match(item_year, year):
            return False
        return True

    def _sort_top250(self, items, sort):
        if sort == 'S':
            def gv(it):
                r = it.get('rating') or {}
                try:
                    return float(r.get('value') or 0)
                except (TypeError, ValueError):
                    return 0.0
            return sorted(items, key=gv, reverse=True)
        if sort == 'R':
            def gy(it):
                y, _, _ = self._split_card(it)
                m = re.match(r'(\d{4})', str(y))
                return int(m.group(1)) if m else 0
            return sorted(items, key=gy, reverse=True)
        return items

    def _fetch_top250(self, start, count, filters):
        all_items = self._get_top250_all()
        genre = self._f(filters, 'genre')
        region = self._f(filters, 'region')
        year = self._f(filters, 'year')
        sort = self._pick(filters, 'sort')
        if genre or region or year:
            all_items = [it for it in all_items if self._top250_match(it, genre, region, year)]
        all_items = self._sort_top250(all_items, sort)
        return all_items[start:start + count]

    # ---------------- 剧集集数补全（缓存式、按需） ----------------
    def _get_episodes_map(self, category):
        cached = self.ep_cache.get(category)
        if cached is not None:
            return cached
        map_info, seen = {}, set()
        for s in range(0, 120, 20):
            url = ("%s/subject/recent_hot/%s?start=%s&limit=20"
                   "&category=%s&type=%s" % (M_DOUBAN_HOST, category, s, category, category))
            data = self._request_mdouban(url, M_DOUBAN_REFERER_TV)
            arr = (data or {}).get('items') or []
            if not arr:
                break
            for it in arr:
                iid = str(it.get('id', ''))
                if iid and iid not in seen and it.get('episodes_info'):
                    seen.add(iid)
                    map_info[iid] = str(it['episodes_info']).strip()
        self.ep_cache.set(category, map_info)
        return map_info

    def _fill_episodes(self, tid, items):
        """只在确实缺少集数信息时才发请求，且用缓存的热门榜 map 兜底"""
        if not items:
            return
        missing = [it for it in items
                   if not (it.get('episodes_info')
                           or (it.get('subject') or {}).get('episodes_info'))]
        if not missing:
            return
        category = 'show' if tid == 'show_filter' else 'tv'
        ep_map = self._get_episodes_map(category)
        if not ep_map:
            return
        for it in missing:
            sid = self._get_subject_id(it)
            if sid and sid in ep_map:
                it['episodes_info'] = ep_map[sid]

    # ---------------- 分类总入口 ----------------
    FETCHERS = None  # init 时无法引用绑定方法，故在方法内 dispatch

    def _fetch_category_items(self, tid, page, filters):
        count = PAGE_SIZE
        start = (page - 1) * count

        if tid == 'hot_movie':
            items = self._fetch_hot_movie(start, count, filters)
        elif tid == 'tv_hot':
            items = self._fetch_tv_hot(start, count, filters)
        elif tid == 'anime_hot':
            items = self._fetch_anime_hot(start, count, filters)
        elif tid == 'show_hot':
            items = self._fetch_show_hot(start, count, filters)
        elif tid == 'top_250':
            return self._fetch_top250(start, count, filters)  # 内部已去重
        elif tid == 'movie_filter':
            items = self._fetch_movie_filter(start, count, filters)
        elif tid == 'tv_filter':
            items = self._fetch_tv_filter(start, count, filters)
        elif tid == 'show_filter':
            items = self._fetch_show_filter(start, count, filters)
        elif tid == 'anime':
            items = self._fetch_anime(start, count, filters)
        elif tid == 'hot_anime':
            items = self._fetch_hot_anime(start, count, filters)
        else:
            items = []

        if tid in ('tv_filter', 'show_filter', 'anime'):
            self._fill_episodes(tid, items)
        return items

    def _category(self, tid, page, filters):
        cache_key = "%s_%s_%s" % (tid, page, json.dumps(filters or {}, sort_keys=True,
                                                        ensure_ascii=False))
        cached = self.page_cache.get(cache_key)
        if cached is not None:
            return cached

        video_list, seen = [], set()
        for it in self._fetch_category_items(tid, page, filters):
            if len(video_list) >= PAGE_SIZE:
                break
            vid = self._get_subject_id(it)
            if not vid or vid in seen:
                continue
            seen.add(vid)
            vod = self._parse_item(it)
            if vod:
                video_list.append(vod)

        total = len(video_list)
        result = {
            'list': video_list,
            'page': page,
            'pagecount': page if total < PAGE_SIZE else page + 1,
            'limit': PAGE_SIZE,
            'total': total if total < PAGE_SIZE else 999999,
        }
        self.page_cache.set(cache_key, result)
        return result

    # ---------------- 对外接口 ----------------
    def homeContent(self, filter=False):
        classes = [
            {"type_id": "hot_movie", "type_name": "热门电影"},
            {"type_id": "tv_hot", "type_name": "热播剧集"},
            {"type_id": "anime_hot", "type_name": "热门动漫"},
            {"type_id": "show_hot", "type_name": "热播综艺"},
            {"type_id": "movie_filter", "type_name": "豆瓣电影"},
            {"type_id": "tv_filter", "type_name": "豆瓣剧集"},
            {"type_id": "show_filter", "type_name": "豆瓣综艺"},
            {"type_id": "anime", "type_name": "动漫剧集"},
            {"type_id": "hot_anime", "type_name": "动漫电影"},
            {"type_id": "top_250", "type_name": "电影Top250"},
        ]
        return {"class": classes, "filters": FILTER_CONFIG, "list": self._home_recommend()}

    def _home_recommend(self):
        """首页推荐：热门电影 + 热播剧集，任一失败不影响另一个"""
        video_list, seen = [], set()
        for tid in ('hot_movie', 'tv_hot'):
            try:
                items = self._fetch_category_items(tid, 1, {})
            except Exception as e:
                self._log("首页推荐 %s 失败: %s" % (tid, e))
                continue
            for it in items:
                if len(video_list) >= 24:
                    break
                vid = self._get_subject_id(it)
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                vod = self._parse_item(it)
                if vod:
                    video_list.append(vod)
        return video_list

    def homeVideoContent(self):
        return {"list": []}

    def categoryContent(self, tid, pg=1, filter=False, extend=None):
        try:
            page = max(1, int(pg or 1))
        except (TypeError, ValueError):
            page = 1
        filters = {}
        if isinstance(extend, dict):
            filters = extend
        elif isinstance(extend, str) and extend.strip():
            try:
                filters = json.loads(extend)
            except Exception:
                filters = {}
        if not isinstance(filters, dict):
            filters = {}
        return self._category(tid, page, filters)

    # ---------------- 详情 ----------------
    def _fetch_subject_detail(self, sid):
        for path in ('/movie/%s', '/tv/%s'):
            data = self._request_douban(DOUBAN_HOST + (path % sid))
            if data and (data.get('title') or data.get('id')):
                return data
        return None

    @staticmethod
    def _names(seq):
        out = []
        for x in (seq or []):
            if isinstance(x, dict):
                n = x.get('name') or x.get('title')
            else:
                n = x
            if n:
                out.append(str(n))
        return ','.join(out)

    def detailContent(self, ids):
        if isinstance(ids, (list, tuple)):
            sid = ids[0] if ids else ''
        else:
            sid = ids
        sid = str(sid or '').strip()
        if not sid:
            return {"list": []}

        d = self._fetch_subject_detail(sid)
        if not d:
            return {"list": []}

        pic_obj = d.get('pic') or d.get('cover') or {}
        if isinstance(pic_obj, dict):
            pic_url = pic_obj.get('large') or pic_obj.get('normal') or pic_obj.get('url') or ''
        else:
            pic_url = pic_obj if isinstance(pic_obj, str) else ''
        if pic_url:
            pic_url = re.sub(r'img\d+\.doubanio\.com', 'img1.doubanio.com', pic_url)

        rating = d.get('rating') or {}
        score = self._fmt_score(rating.get('value') if isinstance(rating, dict) else None)
        year = str(d.get('year') or '')
        area = self._names(d.get('countries'))
        genres = self._names(d.get('genres'))
        intro = (d.get('intro') or d.get('card_subtitle') or '').strip()

        vod = {
            'vod_id': sid,
            'vod_name': d.get('title') or '未知',
            'vod_pic': (pic_url + PIC_SUFFIX) if pic_url else '',
            'vod_year': year,
            'vod_area': area,
            'vod_lang': '',
            'vod_actor': self._names(d.get('actors')),
            'vod_director': self._names(d.get('directors')),
            'vod_content': intro,
            'type_name': genres,
            'vod_remarks': ("%s分" % score) if score != '0' else '',
            'vod_play_from': '豆瓣',
            'vod_play_url': '豆瓣$%s' % sid,
        }
        return {"list": [vod]}

    # ---------------- 搜索 ----------------
    def _normalize_search_item(self, it):
        if not isinstance(it, dict):
            return None
        t = it.get('target') or it.get('subject') or {}
        if isinstance(t, dict) and (t.get('id') or t.get('title')):
            merged = dict(t)
            merged.setdefault('title', it.get('title') or t.get('title'))
            merged.setdefault('rating', it.get('rating') or t.get('rating'))
            return merged
        if it.get('id') or it.get('title'):
            return it
        return None

    def searchContent(self, key, quick=False, pg=1):
        try:
            page = max(1, int(pg or 1))
        except (TypeError, ValueError):
            page = 1
        kw = str(key or '').strip()
        if not kw:
            return {"list": [], "page": page, "pagecount": 1}

        count = PAGE_SIZE
        start = (page - 1) * count

        def fetcher(offset, want):
            url = ("%s/search?q=%s&count=%s&start=%s"
                   % (M_DOUBAN_HOST, quote(kw), want, offset))
            data = self._request_mdouban(url, M_DOUBAN_REFERER_MOVIE)
            if not data:
                return None
            arr = data.get('items') or data.get('subjects') or []
            # 必须在返回前归一化，否则 target 嵌套结构取不到 id，会被去重逻辑丢掉
            out = []
            for x in arr:
                n = self._normalize_search_item(x)
                if n:
                    out.append(n)
            return out

        video_list, seen = [], set()
        items = self._paginate(fetcher, start, count)
        for it in items:
            vid = self._get_subject_id(it)
            if not vid or vid in seen:
                continue
            seen.add(vid)
            vod = self._parse_item(it)
            if vod:
                video_list.append(vod)

        return {
            "list": video_list,
            "page": page,
            "pagecount": page if len(video_list) < count else page + 1,
        }

    def playerContent(self, flag, id, vipFlags=None):
        return {"parse": 1, "url": "https://movie.douban.com/subject/%s/" % id}

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        pass

    def destroy(self):
        try:
            self.session.close()
        except Exception:
            pass
