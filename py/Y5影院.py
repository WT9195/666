# coding=utf-8
# @tvbox-source
"""
目标站：Y5影院 (m.jysct15.com / www.jysct15.com)
模板：影视聚合 + 体育直播
说明：
  1. 网关接口全部需要动态签名（sign），算法见 _sign()；
  2. 播放提供蓝光 1080P / 高清 720P / 标清 480P，当前清晰度缺失时自动回退；
  3. 体育直播从斗球直播页（sujijwp.com/live/{anchorId}）提取真实 m3u8；
  4. 首页板块：最新电影 / 最新剧集 / 综艺 / 动漫 / 短剧 / 预告 / 片单 全部合并进精选，
     分类 Tab 覆盖 电影、电视剧、综艺、动漫、短剧、体育直播。
"""
import hashlib
import json
import re
import sys
import time
import urllib.parse
import uuid

sys.path.append('..')
from base.spider import Spider


class Spider(Spider):
    host = 'https://www.jysct15.com'
    api = host + '/api/mw-movie'
    sign_key = 'cb808529bae6b6be45ecfab29a4889bc'
    live_site = 'https://www.sujijwp.com'
    ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    default_pic = 'https://obs.3688baihuo.com/upload/movie/20241221/vod27040807.webp'
    play_resolutions = (
        ('1080', '蓝光1080P'),
        ('720', '高清720P'),
        ('480', '标清480P'),
    )

    def init(self, extend=""):
        self.device_id = str(uuid.uuid4())
        self.headers = {
            'User-Agent': self.ua,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': self.host + '/',
            'client': 'h5',
        }
        self._filter_cache = None

    def getName(self):
        return 'Y5影院'

    def isVideoFormat(self, url):
        u = (url or '').lower()
        return '.m3u8' in u or '.mp4' in u

    def manualVideoCheck(self):
        return False

    def destroy(self):
        pass

    # ------------------------------------------------------------------
    # 签名 / 请求
    # ------------------------------------------------------------------
    def _sign(self, url):
        """sign = sha1(md5(排序后的 query + key=xxx&t=毫秒))"""
        query = url.split('?', 1)[1] if '?' in url else ''
        parts = []
        for kv in query.split('&'):
            if '=' not in kv:
                continue
            k, v = kv.split('=', 1)
            v = urllib.parse.unquote(v)
            if v in ('', 'undefined', 'null'):
                continue
            parts.append((k, v))
        raw = '&'.join('%s=%s' % (k, v) for k, v in sorted(parts))
        ts = int(time.time() * 1000)
        payload = (raw + '&' if raw else '') + 'key=%s&t=%d' % (self.sign_key, ts)
        sign = hashlib.sha1(hashlib.md5(payload.encode('utf-8')).hexdigest().encode('utf-8')).hexdigest()
        return {
            't': str(ts),
            'sign': sign,
            'deviceId': self.device_id,
            'authorization': '',
        }

    def _get_json(self, path, params=None):
        query = ''
        if params:
            clean = [(k, v) for k, v in params.items() if v not in (None, '')]
            query = urllib.parse.urlencode(clean, quote_via=urllib.parse.quote)
        url = self.api + path + ('?' + query if query else '')
        headers = dict(self.headers)
        headers.update(self._sign(url))
        try:
            resp = self.fetch(url, headers=headers)
            return json.loads(resp.text)
        except Exception as e:
            self.log('Y5 request error: %s %s' % (path, e))
            return {}

    def _data(self, path, params=None):
        j = self._get_json(path, params)
        return j.get('data') if isinstance(j, dict) else None

    # ------------------------------------------------------------------
    # 数据转换
    # ------------------------------------------------------------------
    def _vod(self, v):
        remark = v.get('vodRemarks') or ''
        if not remark:
            remark = v.get('vodVersion') or ''
        year = str(v.get('vodYear') or '')
        if not year and v.get('vodPubdate'):
            year = str(v.get('vodPubdate'))[:4]
        content = v.get('vodContent') or v.get('vodBlurb') or ''
        content = re.sub(r'<[^>]+>', '', content).strip()
        return {
            'vod_id': str(v.get('vodId') or ''),
            'vod_name': v.get('vodName') or '',
            'vod_pic': v.get('vodPic') or self.default_pic,
            'vod_remarks': remark,
            'vod_year': year,
            'vod_area': v.get('vodArea') or '',
            'vod_actor': v.get('vodActor') or '',
            'vod_director': v.get('vodDirector') or '',
            'vod_content': content,
            'vod_score': v.get('vodScore') or v.get('vodDoubanScore') or '',
        }

    def _live_vod(self, m):
        """把一场比赛映射成可展示的「直播卡片」条目。"""
        status = m.get('statusLabel') or m.get('matchStatusName') or ''
        score = ''
        try:
            if int(m.get('matchStatus') or 0) >= 2:
                score = '%s : %s' % (m.get('hostTeamScore'), m.get('guestTeamScore'))
        except Exception:
            score = ''
        league = m.get('leagueName') or ''
        return {
            'vod_id': 'live|%s|%s' % (m.get('anchorId') or '', m.get('matchId') or ''),
            'vod_name': '%s vs %s' % (m.get('hostTeamName') or '', m.get('guestTeamName') or ''),
            'vod_pic': m.get('hostTeamLogo') or m.get('leagueLogo') or self.default_pic,
            'vod_remarks': ('%s %s' % (league, status)).strip(),
            'vod_type': 'live',
            'vod_live_league': league,
            'vod_live_time': m.get('matchTimeStr') or '',
            'vod_live_status': status,
            'vod_live_score': score,
            'vod_live_home': m.get('hostTeamName') or '',
            'vod_live_guest': m.get('guestTeamName') or '',
            'vod_live_home_logo': m.get('hostTeamLogo') or '',
            'vod_live_guest_logo': m.get('guestTeamLogo') or '',
            'vod_live_anchor': m.get('anchorNickname') or '',
            'vod_live_hot': m.get('anchorHot') or 0,
            'vod_live_living': int(m.get('matchStatus') or 0) >= 2,
        }

    # ------------------------------------------------------------------
    # 首页
    # ------------------------------------------------------------------
    def homeContent(self, filter="", page=1):
        try:
            page = int(page)
        except Exception:
            page = 1
        classes = [
            {'type_id': '1', 'type_name': '电影'},
            {'type_id': '2', 'type_name': '电视剧'},
            {'type_id': '3', 'type_name': '综艺'},
            {'type_id': '4', 'type_name': '动漫'},
            {'type_id': '88', 'type_name': '短剧'},
            {'type_id': 'live', 'type_name': '体育直播'},
        ]
        modules = [
            'homeNewMoviePageData',
            'homeBroadcastPageData',
            'newestTvPageData',
            'newestVarietyPageData',
            'newestCartoonPageData',
            'newestShortTvPageData',
            'homeManagerPageData',
            'homeTrailerPageData',
        ]
        data = self._data('/anonymous/v1/home/all/list', {
            'clientType': '1',
            'pageNum': str(page),
        }) or {}

        buckets = []
        pagecount = 1
        for key in modules:
            block = data.get(key) or {}
            items = block.get('list') or []
            if items:
                buckets.append(items)
            total = block.get('totalPage') or block.get('totalCount') or 0
            if isinstance(total, (int, float)) and total > pagecount:
                pagecount = int(total)

        # 各模块轮流取，避免精选被同一板块霸屏。
        merged, seen = [], set()
        cursor = 0
        while len(buckets) > 0:
            alive = False
            for items in buckets:
                if cursor < len(items):
                    alive = True
                    item = items[cursor]
                    vid = str(item.get('vodId') or '')
                    if vid and vid not in seen:
                        seen.add(vid)
                        merged.append(self._vod(item))
            if not alive:
                break
            cursor += 1

        return {
            'class': classes,
            'list': merged,
            'filters': self._build_filters(),
            'pagecount': pagecount,
        }

    def _build_filters(self):
        if self._filter_cache is not None:
            return self._filter_cache
        data = self._data('/anonymous/v1/get/filer/list', {'clientType': '1'}) or {}
        mapping = [
            ('typeList', 'class', '类型'),
            ('plotList', 'plot', '剧情'),
            ('districtList', 'area', '地区'),
            ('yearList', 'year', '年份'),
            ('languageList', 'lang', '语言'),
        ]
        result = {}
        for tid, groups in data.items():
            groups = groups or {}
            rows = []
            for src_key, out_key, name in mapping:
                items = groups.get(src_key) or []
                values = [
                    {'n': str(i.get('itemText')), 'v': str(i.get('itemText'))}
                    for i in items if i.get('itemText')
                ]
                if values:
                    rows.append({'key': out_key, 'name': name, 'value': values})
            if rows:
                result[str(tid)] = rows
        # 体育直播分类
        result['live'] = [
            {
                'key': 'sport',
                'name': '分类',
                'value': [
                    {'n': '足球', 'v': '1'},
                    {'n': '篮球', 'v': '2'},
                ],
            },
            {
                'key': 'state',
                'name': '状态',
                'value': [
                    {'n': '直播中', 'v': 'living'},
                    {'n': '赛程', 'v': 'schedule'},
                ],
            },
        ]
        self._filter_cache = result
        return result

    # ------------------------------------------------------------------
    # 分类
    # ------------------------------------------------------------------
    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = int(pg)
        except Exception:
            page = 1
        if str(tid) == 'live':
            return self._live_content(page, filter or extend or {})

        selected = {}
        for src in (filter, extend):
            if isinstance(src, dict):
                selected.update({k: v for k, v in src.items() if v not in (None, '')})
        params = {
            'pageNum': str(page),
            'pageSize': '24',
            'type1': str(tid),
        }
        if selected.get('class'):
            params['v_class'] = selected['class']
        if selected.get('plot'):
            params['v_class'] = selected['plot']
        if selected.get('area'):
            params['area'] = selected['area']
        if selected.get('year'):
            params['year'] = selected['year']
        if selected.get('lang'):
            params['lang'] = selected['lang']

        data = self._data('/anonymous/video/list', params) or {}
        items = [self._vod(v) for v in (data.get('list') or [])]
        total_page = data.get('totalPage') or 0
        try:
            total_page = int(total_page)
        except Exception:
            total_page = 0
        if total_page < 1:
            total_page = page + 1 if items else page
        return {
            'list': items,
            'page': page,
            'pagecount': total_page,
            'limit': 24,
            'total': data.get('totalCount') or total_page * 24,
        }

    def _live_content(self, page, selected):
        sport = str(selected.get('sport') or '')
        state = str(selected.get('state') or '')
        raw = []
        if state != 'schedule':
            raw.extend(self._data('/anonymous/dqSports/getMatchLiving/h5') or [])
        if state != 'living':
            raw.extend(self._data('/anonymous/dqSports/getMatchSchedule/h5') or [])

        seen, items = set(), []
        for m in raw:
            if not isinstance(m, dict):
                continue
            if sport and str(m.get('sportId') or '') != sport:
                continue
            key = str(m.get('matchId') or '')
            if key and key in seen:
                continue
            seen.add(key)
            items.append(self._live_vod(m))
        return {
            'list': items,
            'page': page,
            'pagecount': 1,
            'limit': len(items) or 30,
            'total': len(items),
        }

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def searchContent(self, key, quick, pg="1"):
        try:
            page = int(pg)
        except Exception:
            page = 1
        data = self._data('/anonymous/video/searchByWord', {
            'keyword': key,
            'pageNum': str(page),
            'pageSize': '24',
        }) or {}
        result = data.get('result') or {}
        items = [self._vod(v) for v in (result.get('list') or [])]
        total_page = result.get('totalPage') or 0
        try:
            total_page = int(total_page)
        except Exception:
            total_page = 0
        if total_page < 1:
            total_page = page if items else 0
        return {
            'list': items,
            'page': page,
            'pagecount': total_page,
            'limit': 24,
            'total': result.get('totalCount') or len(items),
        }

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) and ids else ids)
        if vid.startswith('live|'):
            return {'list': [self._live_detail(vid)]}

        data = self._data('/anonymous/video/detail', {'id': vid}) or {}
        if not data:
            return {'list': []}

        name = data.get('vodName') or ''
        episodes = data.get('episodeList') or []
        play = []
        for ep in episodes:
            nid = str(ep.get('nid') or '')
            if not nid:
                continue
            ep_name = str(ep.get('name') or '')
            if not ep_name:
                ep_name = '第%d集' % (len(play) + 1)
            play.append('%s$%s|%s' % (ep_name, vid, nid))

        play_url = '#'.join(play)
        play_from = ''
        if play_url:
            play_from = '$$$'.join(label for _, label in self.play_resolutions)
            play_url = '$$$'.join([play_url] * len(self.play_resolutions))
        vod = {
            'vod_id': vid,
            'vod_name': name,
            'vod_pic': data.get('vodPic') or self.default_pic,
            'vod_remarks': data.get('vodRemarks') or data.get('vodVersion') or '',
            'vod_year': str(data.get('vodYear') or ''),
            'vod_area': data.get('vodArea') or '',
            'vod_lang': data.get('vodLang') or '',
            'vod_actor': data.get('vodActor') or '',
            'vod_director': data.get('vodDirector') or '',
            'vod_content': re.sub(r'<[^>]+>', '', data.get('vodContent') or '').strip(),
            'vod_score': data.get('vodScore') or data.get('vodDoubanScore') or '',
            'type_name': data.get('typeName') or '',
            'vod_play_from': play_from,
            'vod_play_url': play_url,
        }
        return {'list': [vod]}

    def _live_detail(self, vid):
        _, anchor, match_id = (vid.split('|') + ['', ''])[:3]
        name, league, status, time_str, anchor_name = '体育直播', '', '', '', ''
        pic = self.default_pic
        for api in ('/anonymous/dqSports/getMatchLiving/h5',
                    '/anonymous/dqSports/getMatchSchedule/h5'):
            for m in (self._data(api) or []):
                if str(m.get('matchId') or '') == str(match_id):
                    name = '%s vs %s' % (m.get('hostTeamName') or '', m.get('guestTeamName') or '')
                    league = m.get('leagueName') or ''
                    status = m.get('statusLabel') or ''
                    time_str = m.get('matchTimeStr') or ''
                    anchor_name = m.get('anchorNickname') or ''
                    pic = m.get('hostTeamLogo') or m.get('leagueLogo') or pic
                    break
            if name != '体育直播':
                break
        content = '赛事：%s\n时间：%s\n状态：%s\n主播：%s' % (league, time_str, status, anchor_name)
        return {
            'vod_id': vid,
            'vod_name': name,
            'vod_pic': pic,
            'vod_content': content,
            'vod_play_from': '直播间',
            'vod_play_url': '进入直播间$live|%s' % anchor,
        }

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------
    def playerContent(self, flag, id, vipFlags=None):
        raw = str(id or '')
        if '$' in raw:
            raw = raw.split('$')[-1]

        if raw.startswith('live|'):
            return self._live_play(raw.split('|')[1])

        if raw.startswith('http') and ('.m3u8' in raw or '.mp4' in raw):
            return {'parse': 0, 'url': raw, 'header': self._media_headers()}

        vid, nid = (raw.split('|', 1) + [''])[:2] if '|' in raw else (raw, '')
        data = self._data('/anonymous/v2/video/episode/url', {
            'clientType': '1',
            'id': vid,
            'nid': nid,
        }) or {}
        lst = data.get('list') or []
        if not lst:
            return {'parse': 0, 'url': '', 'header': self._media_headers()}
        # 按所选清晰度取流；该集没有对应清晰度时回退到可用的最高清晰度。
        try:
            lst = sorted(lst, key=lambda x: int(x.get('resolution') or 0), reverse=True)
        except Exception:
            pass
        wanted = next((value for value, label in self.play_resolutions
                       if label == str(flag or '').strip()), '')
        selected = next((item for item in lst
                         if wanted and str(item.get('resolution') or '') == wanted and item.get('url')), None)
        if selected is None:
            selected = next((item for item in lst if item.get('url')), {})
        url = selected.get('url') or ''
        return {'parse': 0, 'url': url, 'header': self._media_headers()}

    def _live_play(self, anchor):
        url = '%s/live/%s' % (self.live_site, anchor)
        try:
            resp = self.fetch(url, headers={
                'User-Agent': self.ua,
                'Referer': self.host + '/',
            })
            html = resp.text
        except Exception as e:
            self.log('live page error: %s' % e)
            return {'parse': 1, 'url': url, 'header': {'User-Agent': self.ua}}
        urls = re.findall(r'https?://[^"\'\s\\]+?\.m3u8[^"\'\s\\]*', html)
        if not urls:
            return {'parse': 1, 'url': url, 'header': {'User-Agent': self.ua}}
        # 优先原画线路（不带 _lsd/_lsd 低码率后缀）。
        target = next((u for u in urls if '_lsd' not in u), urls[0])
        target = target.replace('\\u0026', '&').replace('\\/', '/')
        return {
            'parse': 0,
            'url': target,
            'header': {
                'User-Agent': self.ua,
                'Referer': self.live_site + '/',
            },
        }

    def _media_headers(self):
        return {
            'User-Agent': self.ua,
            'Referer': self.host + '/',
        }
