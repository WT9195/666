# -*- coding: utf-8 -*-
# 大海影视 (com.dhys88.tv v1.0) — 神马 tvlauncher 族
# OK影视 py 版单宿主源（2026-11-04 沙箱全链实证后交付）
#
# 协议要点（全部实拉验证）：
#  引导：OSS api.json（双重 base64）→ 主控 POST /?app=10000 {key=RC4(md5("No00000"),md5("No00000SmtEk1")), t}
#        配置字段 RC4(Maink) 逐个解密；pgUrl/yryUrl 再走 AES-256-CBC（time 材料 = MT 字段解出值）
#  CMS ：{pgUrl}api.php/localhost/... 每请求带 ?key=RC4(t,t)&tt=t 签名 + Authorization 头
#        响应含 code 直读 JSON，否则整包 hex → RC4(G) 解密（G = 配置 pg 字段值）
#  会员：{yryUrl}/api.php?app=10000&act=user_reg/user_logon
#        信封 data=hex(RC4(明文, Rc4key))、sign=md5(明文 + "&" + G)
#        账号=密码=markcode=设备值（ANDROID_ID 式）；新注册即终身会员 vip=999999999
#  播放：GET {newClient}/?url=<集令牌>&app=10000&account&password&series=<片名-集名>&edition=1.0
#        响应 hex → RC4(G) → {"code":200, "url": 真链, "UserAgent": 播放 UA}
#
# OK 极简红线：单后端、零后台线程、零列表磁盘缓存（仅配置快照/账号档落盘）、
# 每请求 1 次重试、七方法永不抛、字段压平、4K 线置顶。

import base64
import hashlib
import json
import os
import random
import re
import time
from urllib.parse import quote

import requests
from Crypto.Cipher import AES, ARC4
from Crypto.Util.Padding import unpad

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider:
        def log(self, msg):
            pass


# ---------------- 工具 ----------------

def _b64d(s):
    s = str(s)
    return base64.b64decode(s + '=' * (-len(s) % 4))


def _dbl(txt):
    """引导密文：去前 16 字符后双重 base64 解码。"""
    return _b64d(_b64d(str(txt)[16:]).decode('utf-8', 'replace')).decode('utf-8', 'replace')


def _md5(s):
    return hashlib.md5(str(s).encode('utf-8')).hexdigest()


def _rc4_enc(key, text):
    return ARC4.new(str(key).encode()).encrypt(str(text).encode()).hex()


def _rc4_dec(key, hexs):
    return ARC4.new(str(key).encode()).decrypt(bytes.fromhex(str(hexs).strip())).decode('utf-8', 'replace')


def _is_hex(s):
    s = str(s).strip()
    return len(s) > 8 and len(s) % 2 == 0 and all(c in '0123456789abcdefABCDEF' for c in s)


def _unesc(s):
    return str(s).replace('\\/', '/')


# ---------------- 常量（本租户实证值，作引导失败时的快照兜底） ----------------

APP_ID = '10000'
EDITION = '1.0'
_API_JSON = 'https://fkyk.oss-cn-shanghai.aliyuncs.com/jy2024/api.json'
_NUM = _md5('No00000')
_NUMKEY = _md5('No00000SmtEk1')
_MAIN_SNAP = 'http://101.35.222.60:8880'
_CFG_SNAP = {
    'pg': 'http://124.221.104.42:882/',          # CMS host
    'hostkey': 'localhost',                       # CMS 路径段（配置 HOST 字段）
    'yry': 'http://101.35.222.60:8881',           # 会员 host
    'client': 'http://101.35.222.60:8881/Client',  # 解析网关
    'g': 'f744e1b1168a55ae3f871898a54d8f0c',      # CMS/解析响应 RC4 钥 = 会员 sign 的 APPKEY
    'rc4key': 'GN8ZGa4DmaHQrHhSTyQ3FwnhCQt68EXQ',  # 会员信封钥
    'auth': 'Basic Og==',
}
_DIR = os.path.dirname(os.path.abspath(__file__))
_CFG_FILE = os.path.join(_DIR, 'dahai_cfg.json')
_ACCT_FILE = os.path.join(_DIR, 'dahai_acct.json')
_SAMPLE_HINT = ('kwimgs',)


class Spider(BaseSpider):
    def getName(self):
        return 'dahai'

    # ---------------- 初始化 ----------------

    def init(self, extend=''):
        self.cfg = dict(_CFG_SNAP)
        self.acct = {}
        self._filters = None
        self._classes = None
        try:
            if os.path.exists(_CFG_FILE):
                saved = json.load(open(_CFG_FILE, encoding='utf-8'))
                if isinstance(saved, dict) and saved.get('pg'):
                    self.cfg.update(saved)
        except Exception:
            pass
        try:
            if os.path.exists(_ACCT_FILE):
                self.acct = json.load(open(_ACCT_FILE, encoding='utf-8')) or {}
        except Exception:
            self.acct = {}
        self._bootstrap()

    # ---------------- 引导链 ----------------

    def _http(self, method, url, timeout=15, **kw):
        last = None
        for _ in range(2):
            try:
                return requests.request(method, url, timeout=timeout, **kw)
            except Exception as e:
                last = e
        if last:
            raise last

    def _bootstrap(self):
        """OSS api.json → 主控配置 POST → 字段解密。失败保留快照，不抛。"""
        try:
            r = self._http('GET', _API_JSON, timeout=10,
                           headers={'User-Agent': 'okhttp/4.9.0'})
            notice = ((r.json().get('msg') or {}).get('notice')) or {}
            main = _dbl(notice['content'])
            auth = _dbl(notice['type'])
            t = str(int(time.time()))
            r2 = self._http('POST', main.rstrip('/') + '/?app=' + APP_ID, timeout=15,
                            data={'key': _rc4_enc(_NUM, _NUMKEY), 't': t},
                            headers={'Authorization': auth, 'User-Agent': 'okhttp/4.9.0'})
            data = (r2.json() or {}).get('data') or {}
            maink = _rc4_dec(_NUMKEY, data['Maink'])

            def dec(field):
                v = data.get(field)
                return _rc4_dec(maink, v) if v else ''

            cfg = dict(self.cfg)
            cfg['auth'] = auth
            cfg['g'] = dec('pg') or cfg['g']
            cfg['rc4key'] = dec('yry') or cfg['rc4key']
            cfg['hostkey'] = dec('HOST') or cfg['hostkey']
            client = dec('Client') or ''
            newc = dec('newClient') or ''
            base = newc or client
            for suf in ('/?url=', '?url='):
                if base.endswith(suf):
                    base = base[:-len(suf)]
                    break
            cfg['client'] = base.rstrip('/') if base else cfg['client']
            mt = dec('MT')
            pg = self._aes_url(data.get('pgUrl'), mt, 14, 2, 32)
            yry = self._aes_url(data.get('yryUrl'), mt, 14, 2, 16)
            if pg:
                cfg['pg'] = pg if pg.endswith('/') else pg + '/'
            if yry:
                cfg['yry'] = yry.rstrip('/')
            self.cfg = cfg
            try:
                json.dump(cfg, open(_CFG_FILE, 'w', encoding='utf-8'))
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _aes_url(raw, mt, klen1, ilen2, off):
        """pgUrl/yryUrl：双 b64 → 前段为 AES 材料（key=b64(mt+raw[:14]) 32 字符，
        iv=b64(mt+raw[:2]) 16 字符），密文自 off 起。"""
        try:
            s = _b64d(_b64d(str(raw))).decode('utf-8', 'replace')
            key = base64.b64encode((mt + s[:klen1]).encode())
            iv = base64.b64encode((mt + s[:ilen2]).encode())
            out = AES.new(key[:32], AES.MODE_CBC, iv[:16]).decrypt(_b64d(s[off:]))
            try:
                out = unpad(out, 16)
            except Exception:
                out = out.rstrip(b'\x00-\x10')
            return out.decode('utf-8', 'replace').strip('\x00')
        except Exception:
            return ''

    # ---------------- CMS ----------------

    def _cms(self, path, amp=True):
        t = str(int(time.time()))
        url = (self.cfg['pg'] + 'api.php/' + self.cfg['hostkey'] + path +
               ('&' if amp else '?') + 'key=' + _rc4_enc(t, t) + '&tt=' + t)
        r = self._http('GET', url, timeout=20,
                       headers={'Authorization': self.cfg['auth'],
                                'User-Agent': 'okhttp/4.9.0'})
        return self._decode_body(r.text)

    def _decode_body(self, text):
        t = (text or '').strip()
        if not t:
            return None
        if t[0] in '{[':
            return json.loads(t)
        if _is_hex(t):
            return json.loads(_rc4_dec(self.cfg['g'], t))
        return json.loads(t)

    def _classes_filters(self):
        if self._classes is not None:
            return self._classes, self._filters or {}
        classes, filters = [], {}
        try:
            arr = self._cms('/Category', amp=False)
            if isinstance(arr, list):
                for it in arr:
                    slug = str(it.get('url') or '').strip()
                    name = str(it.get('name') or '').strip()
                    if slug and name:
                        classes.append({'type_id': slug, 'type_name': name})
        except Exception:
            pass
        try:
            fl = self._cms('/vod/?ac=flitter')
            if isinstance(fl, dict):
                for slug, groups in fl.items():
                    fs = []
                    for g in groups or []:
                        field = str(g.get('field') or '')
                        vals = [str(v) for v in (g.get('values') or []) if str(v)]
                        if field and vals:
                            fs.append({'key': field, 'name': str(g.get('name') or field),
                                       'value': [{'n': '全部', 'v': ''}] +
                                                [{'n': v, 'v': v} for v in vals]})
                    if fs:
                        filters[slug] = fs
        except Exception:
            pass
        if not classes:
            classes = [{'type_id': s, 'type_name': n} for s, n in (
                ('tvplay', '电视剧'), ('movie', '电影'), ('tvshow', '综艺'), ('comic', '动漫'),
                ('oumeiju', '美剧'), ('hanguoju', '韩剧'), ('movie_4k', '4K'), ('movie_ZB', '少儿'))]
        self._classes, self._filters = classes, filters
        return classes, filters

    @staticmethod
    def _vod_item(it):
        nl = str(it.get('nextlink') or '')
        m = re.search(r'ids=(\d+)', nl)
        return {
            'vod_id': m.group(1) if m else nl,
            'vod_name': str(it.get('title') or ''),
            'vod_pic': _unesc(it.get('pic') or ''),
            'vod_remarks': str(it.get('state') or it.get('score') or ''),
        }

    def _list(self, qs):
        try:
            d = self._cms('/vod/?' + qs)
            items = (d or {}).get('data') or []
            return [self._vod_item(x) for x in items if isinstance(x, dict)]
        except Exception:
            return []

    # ---------------- 七方法 ----------------

    def homeContent(self, filter):
        try:
            classes, filters = self._classes_filters()
            return {'class': classes, 'filters': filters}
        except Exception:
            return {'class': [], 'filters': {}}

    def homeVideoContent(self):
        try:
            return {'list': self._list('ac=list&page=1')}
        except Exception:
            return {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            qs = 'ac=list&class=' + quote(str(tid)) + '&page=' + str(pg or '1')
            if isinstance(extend, dict):
                for k, v in extend.items():
                    if v:
                        qs += '&' + quote(str(k)) + '=' + quote(str(v))
            return {'list': self._list(qs), 'page': int(pg or 1)}
        except Exception:
            return {'list': [], 'page': int(pg or 1)}

    def searchContent(self, key, quick, pg='1'):
        try:
            qs = 'ac=list&wd=' + quote(str(key)) + '&page=' + str(pg or '1')
            return {'list': self._list(qs), 'page': int(pg or 1)}
        except Exception:
            return {'list': [], 'page': 1}

    def detailContent(self, ids):
        try:
            vid = str(ids[0]) if isinstance(ids, (list, tuple)) else str(ids)
            if vid.isdigit():
                d = self._cms('/vod/?ac=detail&ids=' + vid)
            else:
                d = self._cms('/vod/' + vid.lstrip('?'))
            if not isinstance(d, dict):
                return {'list': []}
            shows = {}
            try:
                raw_player = d.get('player')
                if raw_player and _is_hex(str(raw_player)):
                    for p in json.loads(_rc4_dec(self.cfg['g'], str(raw_player))) or []:
                        shows[str(p.get('from') or '')] = (str(p.get('show') or ''),
                                                           int(p.get('sort') or 0))
            except Exception:
                shows = {}
            lines = []  # (from, show, sort, eps)
            vl = d.get('videolist')
            if isinstance(vl, dict):
                for frm, eps in vl.items():
                    show, srt = shows.get(str(frm), (str(frm), 0))
                    lines.append((str(frm), show, srt, eps or []))
            elif isinstance(d.get('video_list'), list):
                for ln in d['video_list']:
                    lines.append((str(ln.get('type') or ''), str(ln.get('name') or ''),
                                  0, ln.get('list') or []))
            # 4K 线置顶，其余按运营方 sort 降序
            lines.sort(key=lambda x: (0 if ('4k' in x[0].lower() or '4k' in x[1].lower()) else 1, -x[2]))
            title = str(d.get('title') or '')
            flags, urls = [], []
            for frm, show, _s, eps in lines:
                ep_strs = []
                for e in eps:
                    en = str(e.get('title') or '')
                    payload = 'DH1.' + base64.urlsafe_b64encode(json.dumps(
                        {'u': str(e.get('url') or ''), 't': frm,
                         'n': title + '-' + en}, ensure_ascii=False).encode()).decode()
                    ep_strs.append(en + '$' + payload)
                if ep_strs:
                    flags.append(show)
                    urls.append('#'.join(ep_strs))
            trunk = str(d.get('trunk') or '')
            if trunk.isdigit():
                remarks = ('全' + trunk + '集') if str(d.get('is_finish')) == '1' else ('更新至' + trunk + '集')
            else:
                remarks = trunk
            vod = {
                'vod_id': vid,
                'vod_name': title,
                'vod_pic': _unesc(d.get('img_url') or ''),
                'vod_year': str(d.get('pubtime') or ''),
                'vod_area': ' '.join(str(x) for x in (d.get('area') or [])),
                'vod_actor': ' '.join(str(x) for x in (d.get('actor') or [])),
                'vod_director': ' '.join(str(x) for x in (d.get('director') or [])),
                'type_name': ' '.join(str(x) for x in (d.get('type') or [])),
                'vod_remarks': remarks,
                'vod_content': str(d.get('intro') or ''),
                'vod_play_from': '$$$'.join(flags),
                'vod_play_url': '$$$'.join(urls),
            }
            return {'list': [vod]}
        except Exception:
            return {'list': []}

    # ---------------- 会员 ----------------

    def _member(self, act, plain):
        data = _rc4_enc(self.cfg['rc4key'], plain)
        sign = _md5(plain + '&' + self.cfg['g'])
        url = self.cfg['yry'] + '/api.php?app=' + APP_ID + '&act=' + act
        r = self._http('POST', url, timeout=25, data={'data': data, 'sign': sign},
                       headers={'Authorization': self.cfg['auth'],
                                'User-Agent': 'okhttp/4.9.0'})
        return self._decode_member(r.text)

    def _decode_member(self, text):
        t = (text or '').strip()
        outer = json.loads(t)
        msg = outer.get('msg')
        if isinstance(msg, str) and _is_hex(msg):
            try:
                outer['msg'] = json.loads(_rc4_dec(self.cfg['rc4key'], msg))
            except Exception:
                pass
        return outer

    def _save_acct(self):
        try:
            json.dump(self.acct, open(_ACCT_FILE, 'w', encoding='utf-8'))
        except Exception:
            pass

    def _logon(self, dev):
        plain = 'account=%s&password=%s&markcode=%s&t=%d' % (dev, dev, dev, int(time.time()))
        res = self._member('user_logon', plain)
        msg = res.get('msg') if isinstance(res, dict) else None
        if isinstance(msg, dict) and msg.get('token'):
            info = msg.get('info') or {}
            self.acct = {'dev': dev, 'token': msg['token'],
                         'vip': str(info.get('vip') or '0')}
            self._save_acct()
            return True
        return False

    def _register(self, dev):
        plain = 'user=%s&password=%s&markcode=%s&t=%d' % (dev, dev, dev, int(time.time()))
        res = self._member('user_reg', plain)
        return isinstance(res, dict) and res.get('code') == 200

    @staticmethod
    def _new_dev():
        return ''.join(random.choice('0123456789abcdef') for _ in range(16))

    def _vip_ok(self):
        try:
            vip = int(self.acct.get('vip') or 0)
        except Exception:
            vip = 0
        return vip > int(time.time()) or vip >= 999999999

    def _ensure_acct(self, force_new=False):
        """返回可用的设备账号（注册即终身会员）；失败返回 None，不抛。"""
        try:
            if not force_new:
                dev = str(self.acct.get('dev') or '')
                if dev and self.acct.get('token') and self._vip_ok():
                    return dev
                if dev and self._logon(dev):
                    if self._vip_ok():
                        return dev
                    force_new = True
            for _ in range(2):
                dev = self._new_dev() if force_new else (str(self.acct.get('dev') or '') or self._new_dev())
                try:
                    self._register(dev)  # 已存在会回非 200，忽略，靠登录判定
                except Exception:
                    pass
                if self._logon(dev):
                    return dev
                force_new = True
            return None
        except Exception:
            return None

    # ---------------- 播放 ----------------

    def _parse_once(self, tok, dev, series):
        url = (self.cfg['client'] + '/?url=' + tok + '&app=' + APP_ID +
               '&account=' + dev + '&password=' + dev +
               '&series=' + quote(series) + '&edition=' + EDITION)
        r = self._http('GET', url, timeout=35,
                       headers={'User-Agent': 'okhttp/4.9.0'})
        d = self._decode_body(r.text)
        if not isinstance(d, dict):
            return None
        u = _unesc(d.get('url') or '')
        if d.get('code') == 200 and u.startswith('http'):
            ua = str(d.get('UserAgent') or '').strip()
            return u, ua
        return None

    def playerContent(self, flag, id, vipFlags):
        try:
            raw = str(id or '')
            if not raw.startswith('DH1.'):
                return {'parse': 0, 'url': raw, 'header': {}}
            p = json.loads(base64.urlsafe_b64decode(raw[4:] + '=' * (-len(raw[4:]) % 4)))
            tok = str(p.get('u') or '')
            series = str(p.get('n') or '')
            if tok.startswith('http'):
                return {'parse': 0, 'url': tok, 'header': {'User-Agent': 'okhttp/4.9.0'}}
            got = None
            dev = self._ensure_acct()
            if dev:
                got = self._parse_once(tok, dev, series)
            if not got or any(h in got[0] for h in _SAMPLE_HINT):
                dev = self._ensure_acct(force_new=True)
                if dev:
                    got = self._parse_once(tok, dev, series) or got
            if got and not any(h in got[0] for h in _SAMPLE_HINT):
                header = {'User-Agent': got[1]} if got[1] else {}
                return {'parse': 0, 'playUrl': '', 'url': got[0], 'header': header}
            return {'parse': 0, 'url': '', 'header': {}}
        except Exception:
            return {'parse': 0, 'url': '', 'header': {}}

    def liveContent(self, url):
        return ''

    def localProxy(self, param):
        return [404, 'text/plain', '']
