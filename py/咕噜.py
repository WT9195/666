# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import zlib
from urllib.parse import quote, urljoin

CRYPTO_HINT = (
    "缺少 pycryptodome 加密库（Crypto），本脚本无法解密服务器数据。"
    "请在盒子/运行环境执行 pip install pycryptodome 后重启影视；"
    "若客户端不支持安装依赖，请改用零依赖的 咕噜2.py。"
)

# 缺依赖时不能让整个脚本导入失败（原来顶部三行 import 直接崩，壳里只显示"源失效"）
try:
    from Crypto.Cipher import AES
    from Crypto.PublicKey import ECC
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
    CRYPTO_ERROR = ""
except Exception as _crypto_exc:
    AES = None
    ECC = None
    HAS_CRYPTO = False
    CRYPTO_ERROR = "%s: %s" % (type(_crypto_exc).__name__, _crypto_exc)

    def pad(data, block_size, style="pkcs7"):
        raise RuntimeError(CRYPTO_HINT)

    def unpad(data, block_size, style="pkcs7"):
        raise RuntimeError(CRYPTO_HINT)

from base.spider import Spider

# 优先用 requests；如果盒子环境真没有（概率极低，因为旧版依赖它），再兜底
try:
    import requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

API_URL = "http://103.45.132.22:19987/app/bn/v2"
USER_AGENT = "Dart/3.10 (dart:io)"
# 服务器会校验 boot / detail 上报的版本号：2.3.5 / 2.3.6 已被拉入黑名单，
# 命中后列表返回"最新版下载地址：glgl.tv"假片名、详情返回假简介、
# 播放源只给一个 __v99_blacklist_guard__。2.3.10 是 2026-11-11 实测放行版本。
# 服务器日后再次变更版本时，脚本会自动探测（见 _ensure_version）；
# 也可以在扩展参数里用 ver=2.3.11 或 {"ver":"2.3.11"} 手动指定，无需改代码。
APP_VERSION = "2.3.10"
BUILD_NUMBER = "23050"
APP_SIGNATURE = "32E0AB4FF93A29CE0E6F0BFB01F2F1B788E76262731F3F30F509CB822428ED58"
DEVICE_BUILD = "pangu-build-component-system-513739-s9vkd-rlxnj-p3rhm"
# 只保留语义明确的"假数据"标记。原来的 glgl.tv / IP / shu.jpg 会出现在
# 正常封面和简介里，容易把正常影片误判成版本封禁。
VERSION_GUARD_MARKERS = (
    "最新版下载地址",
    "最新版本下载地址",
    "版本已经失效",
    "请下载最新版",
    "__v99_",
)
FALLBACK_PARSERS = {
    28: {
        "name": "咕噜金牌",
        "url": "http://111.170.58.215:5499/api.php?id=",
        "mode": "json",
        "result_key": "url",
        "server": False,
    },
}

# ============ 线路实测测速（决定"谁排前面"）============
# 服务端限流非常紧：实测一个会话连续约 16 次请求后整窗口 429（Retry-After: 60），
# 因此不能对 13 条线路逐一发解析请求。策略是：
#   ① 只测「静态优先级最高的前几条」，拿真实吞吐排序，其余沿用静态权重兜底；
#   ② 每部片的测速结果缓存 SPEED_TTL，回看详情不重复测；
#   ③ 两次探测之间强制间隔 SPEED_PROBE_GAP，连续翻片不会把配额烧光；
#   ④ 探测途中一旦命中 429 立即中止并进入 SPEED_BLOCK_TTL 冷却，绝不影响详情页返回。
SPEED_PROBE_TOPN = 3        # 每条片最多实测几条线（0 = 关闭，退回纯静态顺序）
SPEED_PROBE_WAIT = 1.8      # 「解析地址」阶段的硬预算（秒），超了就不再解析下一条
SPEED_MEDIA_WAIT = 2.0      # 「媒体测速」阶段的硬预算（秒），到点按已得结果排序
SPEED_PLAY_TIMEOUT = 4      # 探测时单次解析请求的超时（秒），避免一条慢线拖死整页
SPEED_URL_TTL = 180         # 探测时拿到的直链顺手缓存，点播放时直接复用（省一次请求）
SPEED_READ_KB = 384         # 单条线路最多读多少 KB 用于算吞吐
SPEED_MIN_KB = 60           # 吞吐低于此值（≈480kbps）不享受"提前"待遇，避免测速抖动把好线踢下去
SPEED_TTL = 600             # 同一部片的测速结果复用时长（秒）
SPEED_PROBE_GAP = 12        # 两次探测启动的最小间隔（秒），防限流
SPEED_BLOCK_TTL = 150       # 命中 429 后暂停探测的时长（秒）
SPEED_FAKE_SEC = 90         # m3u8 总时长低于此值判为预告/花絮（短剧单集常 2~3 分钟，阈值不能太高）
SPEED_TAG = False           # 线路名后是否显示实测速度；排序仍然按实测吞吐，只是不写进名字

# ============ 全局限流（服务端 429 很凶，必须自己排队）============
# 实测：一个会话短时间连发十几次就整窗口 429，Retry-After 60。
# 所以把「所有」请求（含握手、详情、播放解析）都过一遍令牌桶，
# 而不是只给测速加间隔——分类翻页同样会打满配额。
REQ_BURST = 5               # 令牌桶容量：允许开屏时连发几个（握手/启动/探测/详情）
REQ_REFILL = 1.4            # 每个令牌的回填秒数，约 42 次/分钟
REQ_REFILL_MAX = 4.0        # 命中 429 后自动放慢的上限（秒/令牌）
REQ_MIN_GAP = 0.25          # 相邻两次请求的最小间隔，防瞬间连发
REQ_WAIT_MAX = 4.0          # 单次请求最多排队几秒，超了就直接告诉用户"限流中"
REQ_BLOCK_MIN = 20          # 命中 429 且没有 Retry-After 时的最短冷却（秒）
REQ_BLOCK_MAX = 90          # 冷却上限（秒）


class RateLimited(RuntimeError):
    """服务器 429 或本地冷却中。remaining 是建议等待秒数。"""

    def __init__(self, message, remaining=0.0):
        RuntimeError.__init__(self, message)
        self.remaining = max(0.0, float(remaining or 0))


def _is_rate_limited(error):
    if isinstance(error, RateLimited):
        return True
    text = str(error or "")
    return "429" in text or "rate limit" in text.lower()


def _rate_hint(error, default=30):
    left = int(getattr(error, "remaining", 0) or 0)
    if left <= 0:
        left = default
    if "排队" in str(error):
        return "请求排队较慢，请约 %d 秒后再刷新" % left
    return "服务器限流（HTTP 429），请约 %d 秒后再刷新" % left


def _varint(value):
    if value < 0:
        raise ValueError("negative protobuf varint")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field_bytes(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_varint(number, value):
    return _varint(number << 3) + _varint(value)


def _read_varint(data, position):
    value = 0
    shift = 0
    while position < len(data) and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid protobuf varint")


def _parse_fields(data):
    fields = []
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field zero")
        if wire == 0:
            value, position = _read_varint(data, position)
        elif wire == 1:
            value = data[position:position + 8]
            position += 8
        elif wire == 2:
            size, position = _read_varint(data, position)
            value = data[position:position + size]
            position += size
        elif wire == 5:
            value = data[position:position + 4]
            position += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if position > len(data):
            raise ValueError("truncated protobuf field")
        fields.append((number, wire, value))
    return fields


def _field_values(data, number, wire=None):
    return [
        value
        for field_number, field_wire, value in _parse_fields(data)
        if field_number == number and (wire is None or field_wire == wire)
    ]


def _field_value(data, number, default=None, wire=None):
    values = _field_values(data, number, wire)
    return values[-1] if values else default


def _text(value, default=""):
    if not isinstance(value, bytes):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return default


def _packed_varints(data):
    values = []
    position = 0
    while position < len(data):
        value, position = _read_varint(data, position)
        values.append(value)
    return values


def _raw_deflate(data):
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
        memLevel=8,
        strategy=zlib.Z_RLE,
    )
    return compressor.compress(data) + compressor.flush()


def _derive_key(session_id, shared_x):
    prk = hmac.new(session_id.encode("ascii"), shared_x, hashlib.sha256).digest()
    return hmac.new(prk, b"v2-session\x01", hashlib.sha256).digest()


_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B


def _sec1_export_public(key):
    point = key.public_key().pointQ
    x = int(point.x).to_bytes(32, "big")
    y = int(point.y).to_bytes(32, "big")
    return b"\x04" + x + y


def _sec1_import_public(data):
    raw = bytes(data or b"")
    if len(raw) == 65 and raw[0] == 4:
        x = int.from_bytes(raw[1:33], "big")
        y = int.from_bytes(raw[33:65], "big")
    elif len(raw) == 33 and raw[0] in (2, 3):
        x = int.from_bytes(raw[1:], "big")
        y2 = (pow(x, 3, _P256_P) - 3 * x + _P256_B) % _P256_P
        y = pow(y2, (_P256_P + 1) // 4, _P256_P)
        if (y & 1) != (raw[0] & 1):
            y = _P256_P - y
    else:
        raise ValueError("invalid P-256 SEC1 public key")
    return ECC.construct(curve="P-256", point_x=x, point_y=y)


class _GuluProtocol:
    def __init__(self, app_version=None, timeout=15):
        self.timeout = timeout
        # 版本号做成实例属性，方便运行时被自动探测/扩展参数覆盖
        self.app_version = APP_VERSION if app_version is None else str(app_version)
        self.session_id = ""
        self.session_key = b""
        self.device_id = secrets.token_hex(8)
        self.request_id = 0
        self.players = {}
        self.parsers = {}
        self.last_detail_error = ""
        self.lock = threading.RLock()
        # ---- 全局限流状态（只在 self.lock 内读写）----
        self.blocked_until = 0.0        # 429 冷却截止时间戳
        self.last_429_at = 0.0          # 最近一次 429 的时间戳，用于"别把限流当封版"
        self._tokens = float(REQ_BURST)
        self._token_at = time.time()
        self._refill = float(REQ_REFILL)
        self._last_req_at = 0.0
        self._last_ok_at = 0.0

    def _pace(self):
        """令牌桶排队。容量用完后，按 1/refill 秒补一个，避免把会话打成 429。"""
        now = time.time()
        if now < self.blocked_until:
            raise RateLimited("服务器限流冷却中", self.blocked_until - now)
        if self._refill > REQ_REFILL and now - self._last_429_at > 90:
            # 90 秒没再被限流，慢慢恢复到正常速率
            self._refill = max(float(REQ_REFILL), self._refill - 0.05)
        elapsed = now - self._token_at
        if elapsed > 0:
            self._tokens = min(float(REQ_BURST), self._tokens + elapsed / self._refill)
            self._token_at = now
        if self._tokens < 1.0:
            need = (1.0 - self._tokens) * self._refill
            if need > REQ_WAIT_MAX:
                raise RateLimited("本地请求排队过久", need)
            time.sleep(need)
            self._tokens = 0.0
            self._token_at = time.time()
            now = self._token_at
        else:
            self._tokens -= 1.0
        gap = self._last_req_at + REQ_MIN_GAP - now
        if gap > 0:
            time.sleep(min(gap, 0.5))
            now = time.time()
        self._last_req_at = now

    def _note_success(self):
        self._last_ok_at = time.time()

    def _post(self, body, header_name, header_value, extra_headers=None):
        """
        盒子专用：使用 requests 而非 http.client。
        Chaquopy 下 http.client 的 putrequest/endheaders 在 32位 Android 上极不稳定。
        """
        if not HAS_REQUESTS:
            raise RuntimeError("当前环境缺少 requests 模块，无法运行新版咕噜咕噜")
        if not HAS_CRYPTO:
            raise RuntimeError(CRYPTO_HINT)
        # 所有 HTTP 请求都在这里排队：握手、列表、详情、播放解析一视同仁
        self._pace()
        headers = {
            "user-agent": USER_AGENT,
            header_name: header_value,
            "content-type": "application/x-protobuf",
            "accept-encoding": "gzip",
            "content-length": str(len(body)),
            "host": "103.45.132.22:19987",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = requests.post(
                API_URL,
                data=body,
                headers=headers,
                timeout=self.timeout,
                stream=False,
            )
            if resp.status_code == 429:
                retry = 0
                try:
                    retry = int(float(resp.headers.get("retry-after", "0") or 0))
                except (TypeError, ValueError):
                    retry = 0
                retry = min(REQ_BLOCK_MAX, max(REQ_BLOCK_MIN, retry))
                now = time.time()
                self.last_429_at = now
                self.blocked_until = now + retry
                self._tokens = 0.0
                self._token_at = now
                self._refill = min(float(REQ_REFILL_MAX), self._refill * 1.8)
                raise RateLimited("服务器限流（HTTP 429）", retry)
            if resp.status_code < 200 or resp.status_code >= 300:
                raise RuntimeError("HTTP %s: %s" % (resp.status_code, resp.text[:200]))
            self._note_success()
            return resp.content
        except requests.exceptions.Timeout:
            raise RuntimeError("请求超时，请检查网络")
        except requests.exceptions.ConnectionError:
            raise RuntimeError("连接失败，服务器可能不可达")

    def handshake(self):
        if not HAS_CRYPTO:
            raise RuntimeError(CRYPTO_HINT)
        private_key = ECC.generate(curve="P-256")
        public_key = _sec1_export_public(private_key)
        capabilities = _field_varint(1, 1) + _field_varint(2, 0) + _field_varint(3, 1)
        request = (
            _field_bytes(1, public_key)
            + _field_bytes(2, b"1.0.0")
            + _field_bytes(3, capabilities)
        )
        handshake_key = secrets.token_hex(16)
        iv = secrets.token_bytes(16)
        cipher = AES.new(handshake_key.encode("ascii"), AES.MODE_CBC, iv)
        encrypted = base64.b64encode(
            iv + cipher.encrypt(pad(zlib.compress(request, 4), AES.block_size))
        )
        content = self._post(encrypted, "x-handshake-key", handshake_key)
        raw = base64.b64decode(content)
        response_cipher = AES.new(handshake_key.encode("ascii"), AES.MODE_CBC, raw[:16])
        plain = zlib.decompress(unpad(response_cipher.decrypt(raw[16:]), AES.block_size))
        session_id = _field_value(plain, 1, wire=2)
        server_public = _field_value(plain, 2, wire=2)
        if not session_id or not server_public:
            raise RuntimeError("invalid handshake response")
        self.session_id = session_id.decode("ascii")
        server_key = _sec1_import_public(server_public)
        shared_point = server_key.pointQ * int(private_key.d)
        shared_x = int(shared_point.x).to_bytes(32, "big")
        self.session_key = _derive_key(self.session_id, shared_x)
        self.request_id = 0

    def _encrypt(self, data):
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self.session_key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        ciphertext, tag = cipher.encrypt_and_digest(_raw_deflate(data))
        return nonce + ciphertext + tag

    def _decrypt(self, data):
        if len(data) < 28:
            raise ValueError("invalid GCM payload")
        nonce, ciphertext, tag = data[:12], data[12:-16], data[-16:]
        compressed = AES.new(
            self.session_key, AES.MODE_GCM, nonce=nonce, mac_len=16
        ).decrypt_and_verify(ciphertext, tag)
        return zlib.decompress(compressed, -15)

    def request(self, method, payload=b"", scope=3, extra_headers=None):
        with self.lock:
            if not self.session_id:
                self.handshake()
            self.request_id += 1
            request_id = self.request_id
            core = (
                _field_varint(1, request_id)
                + _field_varint(2, scope)
                + _field_varint(3, method)
                + _field_bytes(4, self.device_id)
                + _field_bytes(5, b"")
                + _field_bytes(6, payload)
                + _field_varint(7, int(time.time() * 1000))
            )
            body = _field_varint(1, request_id) + _field_bytes(2, self._encrypt(core))
            content = self._post(
                body,
                "x-session-id",
                self.session_id,
                extra_headers=extra_headers,
            )
            encrypted = _field_value(content, 2, wire=2)
            if encrypted is None:
                error = _text(_field_value(content, 3, b"", wire=2))
                raise RuntimeError(error or "server rejected request")
            plain = self._decrypt(encrypted)
            status = _text(_field_value(plain, 3, b"", wire=2))
            if status and status not in ("ok", "success"):
                raise RuntimeError(status)
            return plain

    def boot(self):
        now_us = int(time.time() * 1_000_000)
        app = (
            _field_bytes(1, "咕噜咕噜")
            + _field_bytes(2, self.app_version)
            + _field_bytes(3, b"com.himrsc.viz")
            + _field_bytes(4, APP_SIGNATURE)
            + _field_bytes(5, BUILD_NUMBER)
            + _field_varint(6, now_us)
            + _field_varint(7, now_us)
        )
        device = (
            _field_bytes(1, self.device_id)
            + _field_varint(2, 1)
            + _field_bytes(3, b"15")
            + _field_bytes(4, b"Redmi K50 Ultra")
            + _field_bytes(5, b"Redmi/diting/diting:15/AQ3A.240912.001/OS2.0.215.0.VOACNXM:user/release-keys")
            + _field_bytes(6, b"Redmi")
            + _field_bytes(7, b"qcom")
            + _field_varint(8, 0)
            + _field_bytes(9, b"unknown")
            + _field_bytes(10, DEVICE_BUILD)
            + _field_varint(11, 0)
            + _field_varint(12, 35)
        )
        payload = (
            _field_bytes(1, b"v2")
            + _field_bytes(2, b"android")
            + _field_bytes(3, b"gulu")
            + _field_bytes(4, app)
            + _field_bytes(5, device)
        )
        response = self.request(0, payload, scope=1)
        config = _field_value(response, 4, b"", wire=2)
        players = {}
        for item in _field_values(config, 4, wire=2):
            code = _text(_field_value(item, 3, b"", wire=2))
            if not code:
                continue
            parser_ids = []
            for packed in _field_values(item, 8, wire=2):
                parser_ids.extend(_packed_varints(packed))
            players[code] = {
                "id": _field_value(item, 1, 0, wire=0),
                "name": _text(_field_value(item, 4, b"", wire=2)),
                "parser_ids": parser_ids,
            }
        parsers = {}
        for item in _field_values(config, 7, wire=2):
            parser_id = _field_value(item, 1, 0, wire=0)
            if parser_id:
                parsers[parser_id] = {
                    "name": _text(_field_value(item, 2, b"", wire=2)),
                    "url": _text(_field_value(item, 3, b"", wire=2)),
                    "mode": _text(_field_value(item, 4, b"", wire=2)),
                    "result_key": _text(_field_value(item, 10, b"url", wire=2)),
                    "server": bool(_field_value(item, 20, 0, wire=0)),
                }
        self.players = players
        self.parsers = parsers

    def search(self, keyword, page=1, limit=21, category_id=""):
        filters = _field_bytes(18, b"vod_hits_month") + _field_varint(19, 1)
        category_id = str(category_id or "").strip()
        if category_id:
            filters += _field_bytes(3, category_id) + _field_bytes(4, category_id)
        payload = (
            _field_bytes(1, keyword)
            + _field_varint(2, int(page))
            + _field_varint(3, int(limit))
            + _field_bytes(5, filters)
        )
        response = self.request(61, payload)
        data = _field_value(response, 4, b"", wire=2)
        videos = []
        for message in _field_values(data, 1, wire=2):
            classes = [_text(v) for v in _field_values(message, 17, wire=2)]
            videos.append({
                "vod_id": str(_field_value(message, 1, 0, wire=0)),
                "vod_name": _text(_field_value(message, 3, b"", wire=2)),
                "vod_pic": _text(_field_value(message, 6, b"", wire=2)),
                "vod_remarks": _text(_field_value(message, 11, b"", wire=2)),
                "vod_year": str(_field_value(message, 2, 0, wire=0)),
                "vod_content": _text(_field_value(message, 20, b"", wire=2)),
                "type_name": ",".join(classes),
                "vod_area": _text(_field_value(message, 16, b"", wire=2)),
                "vod_actor": ",".join(_text(v) for v in _field_values(message, 14, wire=2)),
                "vod_director": ",".join(_text(v) for v in _field_values(message, 15, wire=2)),
                "_type": _field_value(message, 4, 0, wire=0),
            })
        page_info = _field_value(data, 2, b"", wire=2)
        return {
            "videos": videos,
            "page": _field_value(page_info, 1, int(page), wire=0),
            "pagecount": _field_value(page_info, 3, 1, wire=0),
            "limit": _field_value(page_info, 2, int(limit), wire=0),
            "total": _field_value(page_info, 4, len(videos), wire=0),
        }

    def detail(self, vod_id):
        self.last_detail_error = ""
        payload = (
            _field_varint(1, int(vod_id))
            + _field_bytes(3, self.app_version)
            + _field_bytes(4, b"1")
            + _field_varint(5, 1)
        )
        response = self.request(
            62,
            payload,
            extra_headers={"x-player-page-protection": "1"},
        )
        data = _field_value(response, 4, b"", wire=2)
        name = _text(_field_value(data, 5, b"", wire=2))
        # 只检查片名、简介和线路 code。封面(field 13)常带官网域名/IP，
        # 拿它做判定会把正常影片误杀，所以不参与。
        guard_text = " ".join(
            [_text(_field_value(data, field, b"", wire=2)) for field in (5, 21)]
            + [
                _text(_field_value(source, 1, b"", wire=2))
                for source in _field_values(data, 75, wire=2)
            ]
        ).lower()
        if any(marker.lower() in guard_text for marker in VERSION_GUARD_MARKERS):
            self.last_detail_error = "server_version_guard"
            return None
        if not name or _field_value(data, 1, 0, wire=0) == 0:
            self.last_detail_error = "invalid_detail"
            return None
        sources = []
        for source in _field_values(data, 75, wire=2):
            code = _text(_field_value(source, 1, b"", wire=2))
            if not code or any(m.lower() in code.lower() for m in VERSION_GUARD_MARKERS):
                continue
            config = self.players.get(code, {})
            episodes = []
            for episode in _field_values(source, 2, wire=2):
                episode_id = _text(_field_value(episode, 3, b"", wire=2))
                if not episode_id:
                    continue
                episodes.append({
                    "index": _field_value(episode, 1, len(episodes) + 1, wire=0),
                    "id": episode_id,
                    "name": _text(_field_value(episode, 4, b"", wire=2)) or f"第{len(episodes) + 1}集",
                })
            if episodes:
                sources.append({
                    "code": code,
                    "name": config.get("name", code),
                    "parser_id": (config.get("parser_ids") or [0])[0],
                    "episodes": episodes,
                })
        return {
            "id": str(_field_value(data, 1, vod_id, wire=0)),
            "name": name,
            "pic": _text(_field_value(data, 13, b"", wire=2)),
            "remarks": _text(_field_value(data, 22, b"", wire=2)),
            "content": _text(_field_value(data, 21, b"", wire=2)),
            "classes": [
                _text(value) for value in _field_values(data, 12, wire=2)
            ],
            "area": _text(_field_value(data, 28, b"", wire=2)),
            "year": _text(_field_value(data, 30, b"", wire=2)),
            "sources": sources,
        }

    def play(self, parser_id, play_id):
        payload = _field_varint(1, int(parser_id)) + _field_bytes(2, play_id)
        response = self.request(69, payload)
        data = _field_value(response, 4, b"", wire=2)
        return _text(_field_value(data, 2, b"", wire=2))


class Spider(Spider):
    def __init__(self):
        self.ext = ""
        self.name = "咕噜咕噜"
        self.app_version = APP_VERSION
        self.forced_version = ""          # 扩展参数里手动指定的版本，优先级最高
        self.version_name = APP_VERSION
        self.build_number = BUILD_NUMBER
        self.package = "com.jymqfh.xee"
        self.api_version = "v2"
        self.ua = USER_AGENT
        # 手动排序：优先线路 → 4K → 2K → 自建(☆) → 采集
        # 2026-11 服务器新增了 新咕噜4K / 极速蓝光 / VIP4K / VIP蓝光 / 菲乐2K /
        # iyf4k / ☆哔狸☆ 等线路，补进名单，否则会被兜底规则甩到最后。
        self.play_order = [
            # === 优先线路（你指定的前6个）===
            "咕噜4K",
            "新咕噜4K",
            "菲乐4K",
            "鲸宝4K",
            "极速蓝光",
            "VIP4K",
            "VIP蓝光",
            "神话",
            "臻影4K",
            "臻影",
            "精品2K",
            "鲸宝2K",
            "短剧2K",
            "菲乐2K",
            "iyf4k",
            "天堂",
            # === 自建线路（带☆）===
            "☆讯飞☆",
            "☆奇趣☆",
            "☆果汁☆",
            "☆酷萌☆",
            "☆哔哩☆",
            "☆哔狸☆",
            # === 采集类 ===
            "咖啡",
            "量子",
            "非凡",
            "暴风",
            "蚂蚁",
            "小熊",
            "海外",
            "花旗",
            "letv",
        ]
        self.categories = [
            {"id": "1", "name": "电影"},
            {"id": "2", "name": "电视剧"},
            {"id": "3", "name": "综艺"},
            {"id": "4", "name": "动漫"},
            {"id": "5", "name": "短剧"},
            {"id": "60", "name": "直播"},
        ]
        self.player_config = {}
        self.parser_apis = dict(FALLBACK_PARSERS)
        self.protocol = _GuluProtocol(app_version=self.app_version)
        self.last_error = ""
        # 测速：vod_id -> (时间戳, {线路code: (KB/s, 时长秒, 失败原因)})
        self._speed_cache = {}
        self._last_probe_at = 0.0          # 上次启动探测的时间，用于强制间隔
        self._probe_block_until = 0.0      # 命中 429 后的冷却截止时间
        self._probe_cost = 0.0             # 上次探测实际耗时（排查用）
        self._url_cache = {}               # (parser_id|分集id) -> (时间戳, 直链)，省掉重复解析
        self.speed_topn = SPEED_PROBE_TOPN
        self._fallback_players()
        self.protocol.players = dict(self.player_config)
        self.protocol.parsers = dict(self.parser_apis)

    def getName(self):
        return "咕噜咕噜"

    def getDependence(self):
        # 缺 pycryptodome 时把依赖告诉客户端（支持自动装依赖的壳会去装）
        return [] if HAS_CRYPTO else ["pycryptodome"]

    @staticmethod
    def _parse_extend(extend):
        """扩展参数既可能是 JSON，也可能是 key=value&key=value。"""
        text = str(extend or "").strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k).strip(): str(v).strip() for k, v in obj.items()}
        except (TypeError, ValueError):
            pass
        out = {}
        for part in re.split(r"[&,\n]", text):
            if "=" in part:
                key, value = part.split("=", 1)
                out[key.strip()] = value.strip()
        return out

    def _apply_extend(self, extend):
        self.ext = str(extend or "")
        conf = self._parse_extend(self.ext)
        ver = conf.get("ver", conf.get("version", conf.get("appVersion")))
        if ver is not None:
            # 允许 ver= 显式指定空版本（服务器不校验版本的兜底方案）
            ver = str(ver)
            self.forced_version = ver
            self.app_version = ver
            self.version_name = ver or APP_VERSION
        elif getattr(self, "forced_version", ""):
            self.app_version = self.forced_version
            self.version_name = self.forced_version
        # probe=0 关闭测速（纯静态顺序），probe=6 提高实测条数
        probe = conf.get("probe", conf.get("speedprobe"))
        if probe is not None:
            try:
                self.speed_topn = max(0, int(probe))
            except (TypeError, ValueError):
                pass
        protocol = getattr(self, "protocol", None)
        if protocol is not None:
            protocol.app_version = self.app_version

    def setExtendInfo(self, extend):
        # 允许在订阅扩展参数里写 ver=2.3.7 或 {"ver":"2.3.7"}，
        # 服务器哪天再改版本时不用动代码。
        self._apply_extend(extend)
        return None

    def homeLayout(self):
        return 0

    def init(self, extend=""):
        self._apply_extend(getattr(self, "ext", "") or extend or "")
        if not hasattr(self, "protocol") or self.protocol is None:
            self.__init__()
            self._apply_extend(self.ext)
        if not HAS_CRYPTO:
            # 依赖缺失时说人话，不要抛 ImportError 让壳显示"源失效"
            self.last_error = CRYPTO_HINT
            self.log("[咕噜] " + CRYPTO_HINT)
            return
        try:
            self.protocol.app_version = self.app_version
            self.protocol.handshake()
            self.protocol.boot()
            self._merge_config()
            self.last_error = ""
        except RateLimited as error:
            self.last_error = _rate_hint(error)
            self.log("[咕噜] " + self.last_error)
        except Exception as error:
            self.last_error = "init:%s:%s" % (type(error).__name__, error)
            self.log("[咕噜] " + self.last_error)
        # 服务器换版本 / 拉黑当前版本时自动找到可用版本
        try:
            self._ensure_version()
        except Exception as error:
            self.log("[咕噜] version probe: %s" % error)

    def _merge_config(self):
        """服务器配置优先，但保留本地兜底映射，避免线路显示成英文 code。"""
        players = dict(getattr(self, "player_config", {}) or {})
        players.update(self.protocol.players or {})
        self.player_config = players
        self.protocol.players = dict(players)
        parsers = dict(FALLBACK_PARSERS)
        parsers.update(getattr(self, "parser_apis", {}) or {})
        parsers.update(self.protocol.parsers or {})
        self.parser_apis = parsers
        self.protocol.parsers = dict(parsers)

    def _reset_session(self):
        self.protocol.session_id = ""
        self.protocol.session_key = b""
        self.protocol.request_id = 0

    @staticmethod
    def _is_poisoned(names):
        markers = ("最新版下载地址", "最新版本下载地址", "版本已经失效", "请下载最新版")
        return any(any(m in (name or "") for m in markers) for name in names)

    def _version_candidates(self):
        """内置放行版本 → 后续小版本 → 空版本(服务器不校验) → 派生版本。"""
        candidates = []

        def add(value):
            if value not in candidates:
                candidates.append(value)

        add(APP_VERSION)
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", self.app_version or "")
        if match:
            major, minor, patch = (int(x) for x in match.groups())
            for step in range(1, 7):
                add("%d.%d.%d" % (major, minor, patch + step))
            for step in range(1, 3):
                add("%d.%d.0" % (major, minor + step))
        add("")  # 空版本实测可绕过校验，作为最后的兜底
        return candidates

    def _ensure_version(self):
        """检测服务器是否仍在返回假数据；是则逐个换版本重握手，找到可用版本后固定。"""
        if not HAS_CRYPTO:
            return
        protocol = getattr(self, "protocol", None)
        # 正在 429 冷却：这时候换版本只会再烧一串请求，必然全失败
        if protocol is not None and time.time() < getattr(protocol, "blocked_until", 0.0):
            return
        try:
            probe = self.protocol.search("", 1, 3)
            names = [v.get("vod_name", "") for v in probe.get("videos", [])[:3]]
        except RateLimited:
            return
        except Exception:
            return
        if not names or not self._is_poisoned(names):
            return
        # 刚被限流过，服务器可能返回"假数据"；180 秒内一律按限流处理，不判封版
        if time.time() - getattr(protocol, "last_429_at", 0.0) < 180:
            self.log("[咕噜] 近期有 429，暂不判定版本封禁")
            return
        original = self.app_version
        old_players = dict(self.protocol.players)
        old_parsers = dict(self.protocol.parsers)
        aborted = False
        for version in self._version_candidates():
            try:
                self._reset_session()
                self.protocol.app_version = version
                self.protocol.handshake()
                self.protocol.boot()
                result = self.protocol.search("", 1, 3)
                fresh = [v.get("vod_name", "") for v in result.get("videos", [])[:3]]
                if fresh and not self._is_poisoned(fresh):
                    self.app_version = version
                    self.version_name = version or APP_VERSION
                    self._merge_config()
                    self.last_error = ""
                    self.log("[咕噜] 版本已自动切换为 %s" % (version or "(空)"))
                    return
            except RateLimited:
                aborted = True
                break
            except Exception:
                continue
        # 全部失败：恢复原会话配置，给出可操作的提示
        self.protocol.app_version = original
        self.protocol.players = old_players
        self.protocol.parsers = old_parsers
        if aborted:
            # 429 不是"版本被封"，别把用户引到改版本号的错路上
            self.last_error = "服务器限流（HTTP 429），暂不判断版本，请稍后重试"
            self.log("[咕噜] " + self.last_error)
            return
        self.last_error = "version_guard: 当前版本被服务器拦截，请在扩展参数中指定 ver=新版本号"
        self.log("[咕噜] " + self.last_error)

    def _probe_detail_version(self, vod_id):
        """详情接口的版本号在 payload 里，可以不必重握手就逐个试版本。"""
        original = self.protocol.app_version
        for version in self._version_candidates():
            try:
                self.protocol.app_version = version
                detail = self.protocol.detail(vod_id)
                if detail:
                    self.app_version = version
                    self.version_name = version or APP_VERSION
                    self.last_error = ""
                    self.log("[咕噜] 详情版本已切换为 %s" % (version or "(空)"))
                    # 会话也切到新版本，保证后面的播放解析不被旧会话拖累
                    try:
                        self._reset_session()
                        self.protocol.app_version = version
                        self.protocol.handshake()
                        self.protocol.boot()
                        self._merge_config()
                    except Exception:
                        pass
                    return detail
            except RateLimited:
                # 429 时继续换版本没有意义，立刻收手
                self.protocol.app_version = original
                return None
            except Exception:
                continue
        self.protocol.app_version = original
        return None

    def _fallback_players(self):
        # 这个字典只负责"服务器code → 中文显示名"的映射，顺序由 play_order 控制
        fallback = {
            "JD4K": ("鲸宝4K", 32), "CO4K": ("菲乐4K", 23), "rose": ("咖啡", 22),
            "NBY": ("蚂蚁", 25), "lzm3u8": ("量子", 33), "bfzym3u8": ("暴风", 34),
            "ffm3u8": ("非凡", 35), "dyttm3u8": ("天堂", 13), "jplink": ("精品2K", 28),
            "xfyun": ("咕噜4K", 27), "zydj": ("短剧2K", 29), "hqdj": ("花旗", 21),
            "bilibili": ("☆哔哩☆", 22), "qq": ("☆讯飞☆", 22), "qiyi": ("☆奇趣☆", 22),
            "mgtv": ("☆果汁☆", 22), "youku": ("☆酷萌☆", 22), "IMDB": ("臻影", 26),
            "qingshan": ("小熊", 26), "rrmj": ("海外", 21), "qsvip": ("神话", 26),
        }
        for code, (name, parser_id) in fallback.items():
            self.player_config.setdefault(code, {"name": name, "parser_ids": [parser_id]})

    def _search(self, keyword, page=1, limit=21, category_id=""):
        self.last_error = ""
        try:
            result = self.protocol.search(keyword, page, limit, category_id)
            names = [v.get("vod_name", "") for v in result.get("videos", [])[:3]]
            if names and self._is_poisoned(names):
                # 部分盒子不调用 init()，这里也补一次版本自愈
                self._ensure_version()
                result = self.protocol.search(keyword, page, limit, category_id)
                if getattr(self, "last_error", ""):
                    result["error"] = self.last_error
            return result
        except Exception as first_error:
            if _is_rate_limited(first_error):
                # 429 绝对不能"重置会话再试一次"：那只会再烧 3 个请求，冷却拖得更久
                self.last_error = _rate_hint(first_error)
                self.log("[咕噜] " + self.last_error)
                return {
                    "videos": [], "page": page, "pagecount": 1,
                    "limit": limit, "total": 0, "error": self.last_error,
                }
            try:
                self._reset_session()
                result = self.protocol.search(keyword, page, limit, category_id)
                return result
            except Exception as second_error:
                if _is_rate_limited(second_error):
                    self.last_error = _rate_hint(second_error)
                    self.log("[咕噜] " + self.last_error)
                    return {
                        "videos": [], "page": page, "pagecount": 1,
                        "limit": limit, "total": 0, "error": self.last_error,
                    }
                self.last_error = "query:%s:%s | retry:%s:%s" % (
                    type(first_error).__name__, first_error,
                    type(second_error).__name__, second_error,
                )
                self.log("[咕噜] " + self.last_error)
                return {
                    "videos": [], "page": page, "pagecount": 1,
                    "limit": limit, "total": 0, "error": self.last_error,
                }

    def homeContent(self, filter=False):
        result = {
            "class": [
                {"type_id": item["id"], "type_name": item["name"]}
                for item in self.categories
            ],
            "filters": {},
        }
        if not HAS_CRYPTO:
            result["msg"] = CRYPTO_HINT
        elif getattr(self, "last_error", ""):
            msg = self.last_error
            # 限流提示本身已经是给用户看的人话，不要再套"初始化异常"
            result["msg"] = msg if msg.startswith("服务器限流") else "初始化异常: " + msg
        return result

    def homeVideoContent(self):
        result = self._search("", 1, 21)
        videos = result.get("videos", [])
        for item in videos:
            item.pop("_type", None)
        response = {"list": videos[:20]}
        if result.get("error"):
            response["msg"] = result["error"]
        return response

    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = max(1, int(pg))
        except (TypeError, ValueError):
            page = 1
        category_id = str(tid or "").strip()
        result = self._search("", page, 21, category_id)
        videos = result.get("videos", [])
        for item in videos:
            item.pop("_type", None)
        response = {
            "page": int(result.get("page", page) or page),
            "pagecount": int(result.get("pagecount", 1) or 1),
            "limit": int(result.get("limit", 21) or 21),
            "total": int(result.get("total", len(videos)) or 0),
            "list": videos,
        }
        if result.get("error"):
            response["msg"] = result["error"]
        return response

    def searchContent(self, key, quick, pg="1"):
        try:
            page = max(1, int(pg))
        except (TypeError, ValueError):
            page = 1
        result = self._search(key, page, 21)
        for item in result["videos"]:
            item.pop("_type", None)
        response = {
            "list": result["videos"],
            "page": page,
            "pagecount": result.get("pagecount", 1),
        }
        if result.get("error"):
            response["msg"] = result["error"]
        return response

    def _detail_id(self, ids):
        pending = [ids]
        while pending:
            value = pending.pop(0)
            if isinstance(value, (list, tuple, set)):
                pending[0:0] = list(value)
                continue
            if isinstance(value, dict):
                for key in ("vod_id", "vodId", "id", "key"):
                    if key in value:
                        pending.insert(0, value[key])
                        break
                continue
            if isinstance(value, bytes):
                value = value.decode("utf-8", "ignore")
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if text[:1] in ("[", "{"):
                try:
                    pending.insert(0, json.loads(text))
                    continue
                except (TypeError, ValueError):
                    pass
            if text.isdigit():
                return text
            match = re.search(r"(?:^|[^0-9])(\d{1,12})(?:$|[^0-9])", text)
            if match:
                return match.group(1)
        return ""

    def detailContent(self, ids):
        vod_id = self._detail_id(ids)
        if not vod_id:
            return {"list": [], "msg": "无效的视频ID"}
        try:
            if not hasattr(self, "protocol") or self.protocol is None:
                self.init("")
            try:
                detail = self.protocol.detail(vod_id)
            except Exception as error:
                if _is_rate_limited(error):
                    return {"list": [], "msg": _rate_hint(error)}
                self._reset_session()
                try:
                    self.protocol.handshake()
                    detail = self.protocol.detail(vod_id)
                except Exception as retry_error:
                    if _is_rate_limited(retry_error):
                        return {"list": [], "msg": _rate_hint(retry_error)}
                    raise
            if not detail and getattr(self.protocol, "last_detail_error", "") == "server_version_guard":
                # 详情接口单独校验版本：逐个试候选版本，而不是直接放弃
                detail = self._probe_detail_version(vod_id)
            if not detail:
                result = {"list": []}
                if getattr(self.protocol, "last_detail_error", "") == "server_version_guard":
                    result["msg"] = "服务器版本校验未通过，请在扩展参数中指定 ver=新版本号"
                else:
                    result["msg"] = "获取详情失败，视频可能已下架"
                return result
            # 组装线路（先不排序），随后按实测吞吐排序
            lines = []
            for source in detail["sources"]:
                episodes = []
                for episode in source["episodes"]:
                    index = episode["index"] or len(episodes) + 1
                    encoded = "{}@{}@{}@{}".format(
                        episode["id"], source["parser_id"], detail["name"], index
                    )
                    episodes.append(f"{episode['name']}${encoded}")
                if episodes:
                    lines.append({
                        "code": source["code"],
                        # 清洗显示名称，界面不再显示【看公告】等后缀
                        "name": self._clean_display_name(source["name"]),
                        "parser_id": source["parser_id"],
                        "episodes": episodes,
                        # 测速要的是原始分集 id，不是拼好的播放串
                        "first_id": source["episodes"][0]["id"],
                    })

            speed = {}
            try:
                speed = self._probe_speeds(lines, detail["id"])
            except Exception as error:      # 测速出问题也绝不能挡住详情
                self.log("[咕噜] 测速跳过: %s" % error)

            def order_key(line):
                static = (1, float(self._play_order_key(line["name"])))
                star = speed.get(line["code"])
                if not star:
                    return static                 # 没测：按静态权重
                kbps, duration, why = star
                if kbps >= SPEED_MIN_KB:
                    if duration and duration < SPEED_FAKE_SEC:
                        return (3, 0.0)           # 预告/花絮：能播但不是正片，沉底
                    return (0, -kbps)             # 实测够快：吞吐越高越靠前
                if why == "解析失败":
                    return (2, 0.0)               # 确认解析不出来，排在未测线之后
                # 拿到了地址但没测出速度（超时/极慢）：不奖励也不惩罚，留在原位
                return static

            lines.sort(key=order_key)

            used = set()
            play_from, play_blocks = [], []
            for line in lines:
                name = line["name"]
                star = speed.get(line["code"])
                if SPEED_TAG and star and star[0] >= 1:
                    name = "%s·%s" % (name, self._fmt_speed(star[0]))
                if name in used:              # 线路名去重，避免壳里两条同名线
                    name = "%s(%s)" % (name, line["code"])
                used.add(name)
                play_from.append(name)
                play_blocks.append("#".join(line["episodes"]))
            vod = {
                "vod_id": detail["id"],
                "vod_name": detail["name"],
                "vod_pic": detail["pic"],
                "vod_remarks": detail["remarks"],
                "vod_year": detail["year"],
                "vod_content": detail["content"],
                "type_name": ",".join(detail["classes"]),
                "vod_area": detail["area"],
                "vod_actor": "",
                "vod_director": "",
                "vod_play_from": "$$$".join(play_from),
                "vod_play_url": "$$$".join(play_blocks),
            }
            return {"list": [vod]}
        except Exception as error:
            self.log("[咕噜] detailContent error: %s" % error)
            return {"list": [], "msg": "获取详情失败: %s" % error}

    def _clean_display_name(self, name):
        """
        统一清洗线路显示名称。
        去掉 【看公告】、[VIP]、（备用）、HD 等装饰性前后缀，
        让界面只保留干净的线路名。
        """
        if not isinstance(name, str) or not name:
            return name or ""
        # 1. 去除各类括号及其内部内容：【...】 [...] （...） (...)
        clean = re.sub(r'[【\[\(（].*?[】\]）)]', '', name)
        # 2. 去除尾部常见独立标记（不区分大小写）
        clean = re.sub(
            r'\s*(?:HD|VIP|备用|推荐|极速|高清|标清|蓝光|超清|试看|抢先|预告|新版|旧版)\s*$',
            '', clean, flags=re.I
        )
        # 3. 去除多余空格
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    def _play_order_key(self, source_name):
        """
        计算播放源排序权重。
        三层策略：
        1. play_order 精确/前缀/包含匹配（权重 0~999，最高优先级）
        2. 基于线路特征的智能分类（权重 1000+，兜底防改名）
           - 4K线路 → 1000
           - 2K线路 → 2000
           - 自建(☆) → 3000
           - 其他采集 → 4000
        这样即使服务器后期改了线路名称（如"咕噜4K"→"咕噜超清4K"），
        只要包含"4K"/"2K"/"☆"等特征，大类顺序就不会乱。
        """
        if not source_name:
            return 9999

        # 清洗会砍掉尾部「蓝光/超清/VIP」等修饰词，导致「极速蓝光→极速」对不上
        # play_order 里的原名而掉进兜底 4000 分。这里原名和清洗名都参与比对。
        clean = self._clean_display_name(source_name)
        candidates = [c for c in (source_name, clean) if c]

        # === 第一层：精确匹配（原名优先，长度不受清洗影响）===
        for cand in candidates:
            low = cand.lower()
            for i, name in enumerate(self.play_order):
                if low == name.lower():
                    return i

        # === 第二层：包含匹配（双向：清洗后变短也能命中）===
        for cand in candidates:
            low = cand.lower()
            for i, name in enumerate(self.play_order):
                name_lower = name.lower()
                if low.startswith(name_lower) or name_lower in low:
                    return i
        for cand in candidates:
            low = cand.lower()
            if len(low) < 2:
                continue
            for i, name in enumerate(self.play_order):
                if low in name.lower():
                    return i

        # === 第三层：基于特征的智能分类（防改名兜底）===
        upper = clean.upper()
        if '4K' in upper:
            return 1000
        if '2K' in upper:
            return 2000
        if '☆' in clean:
            return 3000
        # 纯中文采集类默认排最后
        return 4000

    # ---------------- 线路实测测速 ----------------
    @staticmethod
    def _fmt_speed(kbps):
        if kbps >= 1024:
            return "%.1fM" % (kbps / 1024.0)
        return "%dK" % int(kbps)

    @staticmethod
    def _url_key(parser_id, episode_id):
        return "%s|%s" % (parser_id, episode_id)

    def _url_put(self, parser_id, episode_id, url):
        """探测时顺手把直链记下来，用户点同一条线时复用，省一次解析请求。"""
        if not url or not episode_id:
            return
        if len(self._url_cache) > 120:
            for key, _value in sorted(self._url_cache.items(),
                                      key=lambda item: item[1][0])[:40]:
                self._url_cache.pop(key, None)
        self._url_cache[self._url_key(parser_id, episode_id)] = (time.time(), url)

    def _url_get(self, parser_id, episode_id):
        hit = self._url_cache.get(self._url_key(parser_id, episode_id))
        if not hit:
            return ""
        if time.time() - hit[0] > SPEED_URL_TTL:      # 直链有签名时效，过期就重新解析
            self._url_cache.pop(self._url_key(parser_id, episode_id), None)
            return ""
        return hit[1]

    def _speed_gate_open(self):
        """探测闸门：429 冷却期内、或离上次探测太近，都不再发解析请求。"""
        now = time.time()
        if now < self._probe_block_until:
            return False
        if now < getattr(self.protocol, "blocked_until", 0.0):
            return False
        if now - self._last_probe_at < SPEED_PROBE_GAP:
            return False
        return True

    def _measure(self, url):
        """量一条真实直链的吞吐与时长。返回 (KB/s, 内容时长秒, 失败原因)。
        m3u8 会先取播放列表拿总时长（顺便识别预告片），再量首个分片的下载速度；
        MP4 之类直接量前 SPEED_READ_KB。CDN 直连不计入服务端请求配额。"""
        if not url:
            return 0.0, 0.0, "无地址"
        if not HAS_REQUESTS:
            return 0.0, 0.0, "缺 requests"
        dur = 0.0
        try:
            head = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=(3.5, 5.0), verify=False, stream=True)
            if ".m3u8" in url.lower():
                text = head.content.decode("utf-8", "replace")
                head.close()
                base = url
                if "#EXT-X-STREAM-INF" in text:
                    nxt = ""
                    for line in text.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            nxt = line
                            break
                    if nxt:
                        base = urljoin(base, nxt)
                        text = requests.get(base, headers={"User-Agent": USER_AGENT},
                                            timeout=(3.5, 5.0), verify=False).text
                dur = sum(float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", text))
                segment = ""
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        segment = urljoin(base, line)
                        break
                if not segment:
                    return 0.0, dur, "无分片"
                target = segment
            else:
                head.close()
                target = url

            t0 = time.time()
            stream = requests.get(target, headers={"User-Agent": USER_AGENT},
                                  timeout=(3.5, 5.0), verify=False, stream=True)
            got = 0
            limit = SPEED_READ_KB * 1024
            for chunk in stream.iter_content(65536):
                got += len(chunk)
                if got >= limit or time.time() - t0 > 1.5:
                    break
            try:
                stream.close()
            except Exception:
                pass
            cost = max(time.time() - t0, 0.05)
            if got <= 0:
                return 0.0, dur, "无数据"
            return got / 1024.0 / cost, dur, ""
        except Exception as error:
            return 0.0, dur, type(error).__name__

    def _probe_speeds(self, lines, vod_id):
        """实测前 speed_topn 条线路的吞吐，返回 {线路code: (KB/s, 时长, 失败原因)}。
        任何异常都不允许冒泡到详情页：测不到就退回静态顺序。"""
        topn = int(getattr(self, "speed_topn", SPEED_PROBE_TOPN) or 0)
        if topn <= 0 or len(lines) < 2:
            return {}
        hit = self._speed_cache.get(vod_id)
        if hit and time.time() - hit[0] < SPEED_TTL:
            return hit[1]
        if not self._speed_gate_open():
            return {}

        self._last_probe_at = time.time()
        started = time.time()
        picked = sorted(lines, key=lambda item: self._play_order_key(item["name"]))[:topn]

        # ① 串行解析地址：协议层有全局锁，并发没有收益，还会更容易触发限流
        urls = {}
        tried = set()
        old_timeout = getattr(self.protocol, "timeout", 15)
        try:
            # 探测期间缩短单次超时，并且把「剩余预算」作为本次超时上限：
            # 服务器偶尔对某条线路反应很慢，不能让它把详情页拖到十几秒。
            for line in picked:
                left = SPEED_PROBE_WAIT - (time.time() - started)
                if left <= 0.2:
                    break
                self.protocol.timeout = max(1.0, min(old_timeout, SPEED_PLAY_TIMEOUT, left))
                tried.add(line["code"])
                try:
                    url = self.protocol.play(line["parser_id"], line["first_id"])
                except Exception as error:
                    msg = str(error)
                    if _is_rate_limited(error):
                        # 配额已被打满：立刻收手，进冷却，本次详情按静态顺序返回
                        left_block = max(SPEED_BLOCK_TTL, float(getattr(error, "remaining", 0) or 0))
                        self._probe_block_until = time.time() + left_block
                        break
                    # 超时/连不上是临时故障，不能当"这条线坏了"处理，否则一次抖动
                    # 就会把本来很快的线路踢到最后（实测过：极速 4MB/s 被误沉底）
                    if any(mark in msg for mark in ("超时", "连接失败", "HTTP ",
                                                    "Timeout", "timed out")):
                        tried.discard(line["code"])
                    continue
                if url:
                    urls[line["code"]] = url
                    # 顺手缓存直链：用户点这条线时不用再发一次解析请求
                    self._url_put(line["parser_id"], line["first_id"], url)
        finally:
            self.protocol.timeout = old_timeout

        # 只有真正试过的线路才记账：没轮到、没测完的保持"未测"，继续按静态权重排，
        # 不能因为预算用尽就把它们当成坏线沉底。
        result = {}
        for code in tried:
            result[code] = (0.0, 0.0, "解析失败")
        for code in urls:
            result[code] = (0.0, 0.0, "测速超时")

        # ② 并行量媒体速度（直连 CDN，不占服务端配额）
        if urls:
            def worker(code, url):
                result[code] = self._measure(url)
            threads = []
            for code, url in urls.items():
                thread = threading.Thread(target=worker, args=(code, url))
                thread.daemon = True
                threads.append(thread)
                thread.start()
            end = time.time() + SPEED_MEDIA_WAIT
            for thread in threads:
                thread.join(max(0.0, end - time.time()))

        # 只留最近的测速结果，长期运行不会无限涨内存
        if len(self._speed_cache) > 60:
            oldest = sorted(self._speed_cache.items(), key=lambda item: item[1][0])[:20]
            for key, _value in oldest:
                self._speed_cache.pop(key, None)
        self._probe_cost = time.time() - started
        self._speed_cache[vod_id] = (time.time(), dict(result))
        return result

    def _is_playable_url(self, value, strict=True):
        if not isinstance(value, str):
            return False
        url = value.strip()
        if not re.match(r"^https?://", url, re.I):
            return False
        if not strict:
            # 不少解析接口返回不带扩展名的直链，解析结果只要是个 http 地址就收下
            return True
        return any(mark in url.lower() for mark in (".m3u8", ".mp4", ".mkv", ".flv", ".ts", "/m.php", "?data="))

    def _extract_play_url(self, value, preferred_key="url"):
        if isinstance(value, str):
            value = value.strip()
            return value if self._is_playable_url(value, strict=False) else ""
        if isinstance(value, dict):
            keys = [preferred_key, "url", "play_url", "playUrl", "m3u8", "data"]
            for key in dict.fromkeys(keys):
                if key in value:
                    found = self._extract_play_url(value[key], preferred_key)
                    if found:
                        return found
            for item in value.values():
                found = self._extract_play_url(item, preferred_key)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = self._extract_play_url(item, preferred_key)
                if found:
                    return found
        return ""

    def _external_play(self, parser_id, play_id):
        parser = self.protocol.parsers.get(parser_id, {})
        api_url = str(parser.get("url") or "").strip()
        if not api_url:
            return ""
        encoded = quote(str(play_id), safe="")
        target = api_url.replace("{url}", encoded) if "{url}" in api_url else api_url + encoded
        # 兼容不同版本的 base.spider：新版的 fetch 支持 timeout，旧版不支持该关键字参数
        try:
            response = self.fetch(target, headers={"User-Agent": USER_AGENT}, timeout=15)
        except TypeError:
            response = self.fetch(target, headers={"User-Agent": USER_AGENT})
        if response is None:
            return ""
        if isinstance(response, (dict, list, str)):
            payload = response
        else:
            try:
                payload = response.json()
            except Exception:
                payload = getattr(response, "text", "")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                pass
        return self._extract_play_url(payload, parser.get("result_key") or "url")

    def playerContent(self, flag, id, vipFlags):
        # 播放串格式：raw_id@parser_id@片名@集数。片名/播放ID 里可能带 @，
        # 因此先看正序能否拆出数字 parser_id，不行再从右侧拆一次。
        play_id = str(id or "")
        parts = play_id.split("@", 3)
        raw_id = parts[0] if len(parts) > 1 else play_id
        parser_id, vod_name, episode_index = 0, "", "1"
        if len(parts) == 4 and parts[1].strip().isdigit():
            parser_id, vod_name, episode_index = int(parts[1]), parts[2], parts[3]
        else:
            right = play_id.rsplit("@", 3)
            if len(right) == 4 and right[1].strip().isdigit():
                raw_id = right[0]
                parser_id, vod_name, episode_index = int(right[1]), right[2], right[3]
        url = raw_id if self._is_playable_url(raw_id) else ""
        if not url and parser_id:
            # 详情页测速时已经解析过这条线，直接复用，省一次服务端请求
            url = self._url_get(parser_id, raw_id)
        if not url and parser_id:
            parser = self.protocol.parsers.get(parser_id, {})
            if parser and not parser.get("server", True):
                try:
                    url = self._external_play(parser_id, raw_id)
                except Exception:
                    url = ""
            if not url:
                try:
                    url = self.protocol.play(parser_id, raw_id)
                except Exception as error:
                    if _is_rate_limited(error):
                        # 429 时重握手只会再烧配额，直接告诉用户等几秒
                        return {
                            "parse": 0,
                            "url": "",
                            "header": json.dumps({"User-Agent": USER_AGENT}),
                            "msg": _rate_hint(error),
                        }
                    try:
                        self.protocol.session_id = ""
                        self.protocol.session_key = b""
                        self.protocol.request_id = 0
                        self.protocol.handshake()
                        url = self.protocol.play(parser_id, raw_id)
                    except Exception as retry_error:
                        if _is_rate_limited(retry_error):
                            return {
                                "parse": 0,
                                "url": "",
                                "header": json.dumps({"User-Agent": USER_AGENT}),
                                "msg": _rate_hint(retry_error),
                            }
                        url = ""
            if url:
                self._url_put(parser_id, raw_id, url)
        if not url:
            return {
                "parse": 0,
                "url": "",
                "header": json.dumps({"User-Agent": USER_AGENT}),
                "msg": "解析失败，请尝试更换播放源",
            }
        danmaku = (
            "http://127.0.0.1:9978/proxy?do=appdanmu&vodName="
            + quote(vod_name, safe="")
            + "&vodIndex="
            + quote(str(episode_index), safe="")
            + "&vodUrl="
        )
        return {
            "parse": 0,
            "url": url,
            "header": json.dumps({"User-Agent": USER_AGENT}),
            "danmaku": danmaku,
        }

    def isVideoFormat(self, url):
        return self._is_playable_url(url)

    def manualVideoCheck(self):
        return False

    def proxy(self, params):
        pass

    def localProxy(self, params):
        pass