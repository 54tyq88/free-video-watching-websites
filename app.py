#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreeView · 在线影视站
=====================
搜索片名 → 点开就播。播放器控件直接复用 main.py 里那套（进度条/音量/倍速/
快捷键/全屏/加载态/错误重试），流媒体由本地 static/hls.min.js 驱动。

启动：
    python app.py                  # 默认 http://127.0.0.1:8000
    PORT=8080 python app.py

页面：
    /                     首页（搜索）
    /search?wd=关键词      搜索结果
    /play?vid=28356&ep=5   播放页（剧集列表在右侧）

依赖：requests（1.py 需要）；hls.js 已放在 static/hls.min.js
数据来源：同目录的 1.py（非凡资源网抓取）
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IMG_CACHE_DIR = STATIC_DIR / "imgcache"   # 远程封面下载到这里，之后走本地静态文件

DEFAULT_PORT = 8000
CACHE_TTL = 600          # 详情/播放地址缓存 10 分钟
EP_PAGE_LIMIT = 2000     # 单页最多列出多少集

# 首页展示的快捷词，同时也是服务启动后后台预热的对象
WARM_KEYWORDS = ["凡人修仙传", "斗破苍穹", "完美世界", "仙逆", "遮天", "吞噬星空"]


# ============================================================================
# 复用现有模块：main.py 的播放器与样式，1.py 的抓取能力
# ============================================================================
def _load_module(alias: str, filename: str):
    path = BASE_DIR / filename
    if not path.is_file():
        raise SystemExit(f"[FATAL] 找不到 {filename}，请确认它和 app.py 在同一目录")
    spec = importlib.util.spec_from_file_location(alias, path)
    if not spec or not spec.loader:
        raise SystemExit(f"[FATAL] 无法加载 {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


site = _load_module("freeview_site", "main.py")   # SITE_CSS / PLAYER_CSS / PLAYER_JS
ffzy = _load_module("freeview_ffzy", "1.py")      # search_video / get_video_detail / resolve_play_url

esc = site.html_escape
HLS_JS_FILE = STATIC_DIR / "hls.min.js"


# ============================================================================
# 带缓存的抓取（抓详情页和解析 m3u8 都要走外网，别每次都重来）
# ============================================================================
_lock = threading.Lock()
_detail_cache: dict[str, tuple[float, dict]] = {}
_play_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(store: dict, key: str):
    with _lock:
        item = store.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    return None


def _cache_put(store: dict, key: str, value):
    with _lock:
        store[key] = (time.time(), value)


def get_detail(vid: str) -> dict | None:
    """影片详情（含全部剧集 share 地址）。先走 JSON 接口，失败回退 HTML 详情页。"""
    hit = _cache_get(_detail_cache, vid)
    if hit:
        return hit
    detail = ffzy.detail_api(vid)
    if not (detail and detail.get("episodes")):
        detail = ffzy.get_video_detail(vid)
    if detail and detail.get("episodes"):
        _cache_put(_detail_cache, vid, detail)
    return detail


def get_play(share_url: str) -> dict | None:
    """把 share 页解析成真正的 m3u8，带缓存。"""
    hit = _cache_get(_play_cache, share_url)
    if hit:
        return hit
    info = ffzy.resolve_play_url(share_url)
    if info:
        _cache_put(_play_cache, share_url, info)
    return info


_search_cache: dict = {}
_search_lock = threading.Lock()


def search_cached(keyword: str, page: int = 1) -> list:
    """优先走采集 JSON 接口（实测 0.48s，HTML 搜索页要 4.6s），失败再回退 HTML。"""
    key = (keyword, page)
    with _search_lock:
        item = _search_cache.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]

    hits = ffzy.search_api(keyword, page=page)
    if hits is None:                                   # 接口不可用 → 回退 HTML 抓取
        hits = ffzy.search_video(keyword, page=page)
    else:
        # 搜索结果里已经带着海报和全部剧集地址，顺手填进详情缓存，
        # 用户点进播放页时就不用再抓一次详情了
        for h in hits:
            if h.get("episodes"):
                _cache_put(_detail_cache, h["vid"], h)

    with _search_lock:
        _search_cache[key] = (time.time(), hits)
    return hits


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def cache_remote_image(url: str) -> str:
    """把远程封面抓到本地 static/imgcache 再返回本地地址。

    图片域名（fffgood.com）实测 23KB 要 0.5~1.5 秒，落到本地后：
    浏览器直连本地、静态文件还有 1 天缓存，第二次基本秒出。
    抓取失败就返回原外链，功能不受影响。
    """
    if not url.lower().startswith(("http://", "https://")):
        return ""
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    if ext not in IMG_EXTS:
        ext = ".jpg"
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ext
    target = IMG_CACHE_DIR / name
    if target.is_file() and target.stat().st_size > 0:
        return "/static/imgcache/" + name
    try:
        r = ffzy.session.get(url, timeout=20)
        r.raise_for_status()
        if not r.content:
            return url
        IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(r.content)
        return "/static/imgcache/" + name
    except Exception as e:
        print(f"[warn] 封面缓存失败 {url} -> {e}", file=sys.stderr)
        return url


def pick_episodes(detail: dict, source: str = "") -> tuple[list, list, str]:
    """详情页有多个播放源时集数会重复，默认只用一个源。

    指定了 source 就用它；否则优先挑「直接给 m3u8 直链」的源——
    那种源不用再抓一次 share 页，播放页能省 0.5~3 秒。
    """
    eps = detail["episodes"]
    sources = list(dict.fromkeys(e["source"] for e in eps))

    if source and source in sources:
        chosen = source
    else:
        chosen = sources[0] if sources else ""
        for s in sources:
            first = next((e for e in eps if e["source"] == s), None)
            if first and ".m3u8" in first["share_url"].lower():
                chosen = s
                break

    return [e for e in eps if e["source"] == chosen], sources, chosen


# ============================================================================
# m3u8 清洗：删掉插播广告段
# ============================================================================
# 采集站的片源在约 5 分 57 秒处插了一段广告，结构极其规律：
#   段1（正片，约 357s）→ 段2（广告，5 个分片 / 19.6s）→ 段3（正片继续）
# 相邻段之间用 #EXT-X-DISCONTINUITY 隔开，删掉中间那段即可。
# 但有的集（如第 100 集）被切成 60 个小段，不能用这条规则，
# 所以额外要求「首段足够长」才认定是「正片 + 广告」结构。
_hls_cache: dict = {}
AD_MIN_SEC, AD_MAX_SEC = 8.0, 30.0   # 广告段的合理时长区间
MAIN_MIN_SEC = 60.0                  # 首段至少这么长，后面跟的才可能是广告

# 每集在约 5 分 57 秒处都有一段「5 片 / 19.6 秒」的独立段，时长分秒不差，
# 这是模板化插入的固定内容（正片不可能每集都恰好 19.6 秒），按插播广告处理。
# 注意：它的分片体积和正片相当（1~2MB），说明是动态视频广告而不是静态图，
# 所以不能靠体积来区分广告。
STRIP_AD_BLOCK = True


def _parse_segments(txt: str) -> list[dict]:
    segs, disc, dur = [], False, None
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("#EXT-X-DISCONTINUITY"):
            disc = True
        elif ln.startswith("#EXTINF:"):
            try:
                dur = float(ln[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif not ln.startswith("#"):
            segs.append({"dur": dur or 0.0, "uri": ln, "disc": disc})
            disc, dur = False, None
    return segs


def _strip_ad_block(segs: list[dict]) -> tuple[list[dict], float]:
    """按 DISCONTINUITY 分块，命中「长正片 + 短广告」时删掉广告块。"""
    if not segs:
        return segs, 0.0
    blocks, cur = [], []
    for s in segs:
        if s["disc"] and cur:
            blocks.append(cur)
            cur = []
        cur.append(s)
    if cur:
        blocks.append(cur)

    removed = 0.0
    if len(blocks) >= 3:
        first = sum(x["dur"] for x in blocks[0])
        second = sum(x["dur"] for x in blocks[1])
        if first >= MAIN_MIN_SEC and AD_MIN_SEC <= second <= AD_MAX_SEC:
            removed = second
            blocks.pop(1)
    return [s for b in blocks for s in b], removed


def clean_playlist(master_url: str) -> tuple[str, float] | None:
    """拉播放列表 → 去广告段 → 分片地址转绝对。

    返回 (可直接播放的 m3u8 文本, 去掉的广告秒数)；失败返回 None。
    """
    try:
        r = ffzy.session.get(master_url, timeout=20)
        r.raise_for_status()
        txt = r.text
        if "#EXTM3U" not in txt:
            return None

        sub_url = master_url
        if "#EXT-X-STREAM-INF" in txt:          # 是 master，取第一条子列表
            subs = [ln.strip() for ln in txt.splitlines()
                    if ln.strip() and not ln.startswith("#")]
            if not subs:
                return None
            sub_url = urljoin(master_url, subs[0])
            rs = ffzy.session.get(sub_url, timeout=20)
            rs.raise_for_status()
            txt = rs.text

        segs = _parse_segments(txt)
        if not segs:
            return None
        kept, removed = _strip_ad_block(segs)
        if not kept:
            return None

        top = max(s["dur"] for s in kept) or 4.0
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f"#EXT-X-TARGETDURATION:{int(top) + 1}",
        ]
        for s in kept:
            if s["disc"]:
                # 保留原始断点标记，时间轴跳变交给播放器自己处理
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{s['dur']:.6f},")
            lines.append(urljoin(sub_url, s["uri"]))   # 相对路径 → 绝对地址
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines), removed
    except Exception as e:
        print(f"[warn] 清洗 m3u8 失败: {e}", file=sys.stderr)
        return None


_clean_cache: dict = {}


def clean_playlist_cached(master_url: str) -> tuple[str, float] | None:
    """清洗结果也缓存，重复打开同一集就不用再拉 m3u8 了。"""
    hit = _cache_get(_clean_cache, master_url)
    if hit:
        return hit
    res = clean_playlist(master_url)
    if res:
        _cache_put(_clean_cache, master_url, res)
    return res


# ============================================================================
# 后台预热：把"用户下一步大概率要点"的东西提前抓好
# ============================================================================
_prefetch_lock = threading.Lock()
_prefetched: set[str] = set()


def spawn(fn, *args) -> None:
    """丢到后台线程执行，不挡当前这次请求。"""
    threading.Thread(target=fn, args=args, daemon=True).start()


def prefetch_episode(vid: str) -> None:
    """预热某部影片第一集的 m3u8。

    用户停在搜索结果页时后台就把 share 页解析好，点进播放页几乎不用等
    （否则进播放页要现抓一次 share 页，实测 0.5~3 秒）。
    """
    with _prefetch_lock:
        if vid in _prefetched:
            return
        _prefetched.add(vid)
    try:
        detail = get_detail(vid)
        if not detail:
            return
        eps, _, _ = pick_episodes(detail)
        if eps:
            get_play(eps[0]["share_url"])
    except Exception as e:
        print(f"[warn] 预热 {vid} 第 1 集失败: {e}", file=sys.stderr)


def warm_up() -> None:
    """预热首页那几个快捷词，让第一次点击也是秒开。

    并发 2，别把采集站打疼了。
    """
    def one(kw: str) -> None:
        try:
            search_cached(kw)
        except Exception as e:
            print(f"[warn] 预热「{kw}」失败: {e}", file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(one, WARM_KEYWORDS))


# ============================================================================
# 页面骨架
# ============================================================================
PAGE_EXTRA_CSS = r"""
/* 恢复 hidden 属性语义：.player__error / .player__loading 自带 display:grid，
   会盖掉浏览器给 hidden 准备的 [hidden]{display:none}，导致错误卡片藏不掉 */
[hidden]{display:none !important}
.searchbar{display:flex;gap:8px;margin-left:auto}
.searchbar input{width:240px;padding:8px 12px;border-radius:8px;
  background:var(--bg-2);border:1px solid var(--border);color:var(--text);
  outline:none;font-size:14px}
.searchbar input:focus{border-color:var(--accent)}
.searchbar button{padding:8px 16px;border:0;border-radius:8px;cursor:pointer;
  background:var(--accent);color:#fff;font-size:14px}
.searchbar button:hover{background:var(--accent-2)}
.hero-search{display:flex;gap:10px;margin:22px auto 8px;max-width:560px}
.hero-search input{flex:1;padding:14px 18px;border-radius:12px;font-size:15px;
  background:var(--bg-2);border:1px solid var(--border);color:var(--text);outline:none}
.hero-search input:focus{border-color:var(--accent)}
.hero-search button{padding:0 26px;border:0;border-radius:12px;cursor:pointer;
  background:var(--accent);color:#fff;font-size:15px}
.hero-search button:hover{background:var(--accent-2)}
.chip{display:inline-block;margin:4px 6px 0 0;padding:4px 12px;border-radius:999px;
  background:var(--bg-2);border:1px solid var(--border);color:var(--text-dim);
  font-size:13px;cursor:pointer}
.chip:hover{color:var(--text);border-color:var(--accent)}
.episodes{list-style:none;margin:0;padding:0;max-height:min(72vh,720px);
  overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.episodes li{flex:none}
.episodes a{display:block;padding:7px 12px;border-radius:8px;font-size:14px;
  color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.episodes a:hover{background:var(--bg-2);color:var(--text)}
.episodes a[aria-current="true"]{background:var(--accent);color:#fff;font-weight:600}
.ep-search{width:100%;margin:8px 0 10px;padding:7px 10px;border-radius:8px;
  background:var(--bg-2);border:1px solid var(--border);color:var(--text);
  outline:none;font-size:13px}
.ep-search:focus{border-color:var(--accent)}
.now{color:var(--accent);font-weight:600}
.tag{display:inline-block;padding:2px 8px;margin-right:6px;border-radius:6px;
  background:var(--bg-2);border:1px solid var(--border);
  color:var(--text-dim);font-size:12px}
"""


# ============================================================================
# 科技感主题（叠加在 SITE_CSS / PLAYER_CSS 之后，同名变量与选择器都会被覆盖）
# ============================================================================
TECH_THEME_CSS = r"""
:root{
  --bg:#04060c; --bg-2:#0b1422;
  --card:rgba(13,21,35,.66);
  --text:#dbe7f5; --text-dim:#7d8ea6;
  --accent:#22d3ee; --accent-2:#7c3aed;
  --border:rgba(34,211,238,.14);
  --radius:14px; --shadow:0 20px 50px rgba(0,0,0,.55);
  /* 播放器配色 */
  --p-bg:#04060c; --p-fg:#eaf6ff; --p-dim:rgba(219,231,245,.6);
  --p-accent:#22d3ee; --p-border:rgba(34,211,238,.16);
  /* 主题扩展 */
  --cyan:#22d3ee; --violet:#7c3aed;
  --mono:ui-monospace,"Cascadia Code","SF Mono",Consolas,monospace;
}

html{background:#04060c}
body{
  background:transparent; color:var(--text);
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}

/* ---- 背景：深空光晕 + 透视网格 ---- */
body::before{
  content:""; position:fixed; inset:0; z-index:-2; pointer-events:none;
  background:
    radial-gradient(900px 520px at 12% -8%, rgba(34,211,238,.16), transparent 62%),
    radial-gradient(760px 460px at 88% 2%, rgba(124,58,237,.14), transparent 60%),
    radial-gradient(900px 600px at 50% 108%, rgba(34,211,238,.07), transparent 65%),
    linear-gradient(180deg,#04060c 0%,#060b15 48%,#04060c 100%);
}
body::after{
  content:""; position:fixed; inset:-56px 0 0 0; z-index:-1; pointer-events:none;
  background-image:
    linear-gradient(rgba(34,211,238,.055) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,211,238,.055) 1px, transparent 1px);
  background-size:56px 56px;
  -webkit-mask-image:radial-gradient(ellipse 92% 72% at 50% 0%,#000 8%,transparent 78%);
  mask-image:radial-gradient(ellipse 92% 72% at 50% 0%,#000 8%,transparent 78%);
  animation:gridMove 22s linear infinite;
}
@keyframes gridMove{ to{ transform:translate3d(0,56px,0) } }

/* ---- 鼠标跟随光斑 ---- */
.fx-glow{
  position:fixed; left:0; top:0; width:560px; height:560px; margin:-280px 0 0 -280px;
  border-radius:50%; pointer-events:none; z-index:0; opacity:0;
  background:radial-gradient(circle,rgba(34,211,238,.13),rgba(34,211,238,.05) 42%,transparent 68%);
  transition:opacity .4s ease; will-change:transform;
}
body.fx-on .fx-glow{ opacity:1 }

/* ---- 点击涟漪 ---- */
.fx-ripple{
  position:fixed; width:10px; height:10px; margin:-5px 0 0 -5px; border-radius:50%;
  border:1px solid rgba(34,211,238,.9); pointer-events:none; z-index:9999;
  animation:rippleOut .62s cubic-bezier(.2,.7,.3,1) forwards;
}
@keyframes rippleOut{
  0%{ width:10px; height:10px; margin:-5px 0 0 -5px; opacity:.9 }
  100%{ width:150px; height:150px; margin:-75px 0 0 -75px; opacity:0 }
}

/* ---- 顶栏 ---- */
.site-header{
  background:rgba(4,6,12,.72); gap:18px;
  border-bottom:1px solid rgba(34,211,238,.12);
  backdrop-filter:blur(16px) saturate(150%);
  -webkit-backdrop-filter:blur(16px) saturate(150%);
}
.site-header .logo{
  font-family:var(--mono); letter-spacing:2.5px;
  text-shadow:0 0 20px rgba(34,211,238,.75);
}
.site-header .logo::after{
  content:""; display:inline-block; width:6px; height:6px; margin-left:9px;
  border-radius:50%; background:var(--cyan); vertical-align:middle;
  box-shadow:0 0 12px var(--cyan); animation:pulseDot 2.4s ease-in-out infinite;
}
@keyframes pulseDot{ 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.3;transform:scale(.75)} }

/* ---- 输入框 ---- */
.searchbar input,.hero-search input{
  background:rgba(9,15,26,.88); color:var(--text); font-family:inherit;
  border:1px solid rgba(34,211,238,.18); outline:none;
  transition:border-color .25s,box-shadow .25s,background .25s;
}
.searchbar input:hover,.hero-search input:hover{ border-color:rgba(34,211,238,.38) }
.searchbar input:focus,.hero-search input:focus{
  background:rgba(10,18,30,.95);
  border-color:rgba(34,211,238,.72);
  box-shadow:0 0 0 3px rgba(34,211,238,.13), 0 0 34px rgba(34,211,238,.22),
             inset 0 0 26px rgba(34,211,238,.07);
}
.searchbar input::placeholder,.hero-search input::placeholder{ color:#4d5f78 }

/* ---- 按钮：霓虹 + 光扫 ---- */
.searchbar button,.hero-search button{
  position:relative; overflow:hidden; border:0; cursor:pointer;
  background:linear-gradient(135deg,#0891b2,#22d3ee 60%,#67e8f9);
  color:#02161d; font-weight:700; letter-spacing:.5px;
  box-shadow:0 0 26px rgba(34,211,238,.35);
  transition:box-shadow .25s,filter .25s,transform .18s;
}
.searchbar button:hover,.hero-search button:hover{
  box-shadow:0 0 42px rgba(34,211,238,.6); filter:brightness(1.08);
}
.searchbar button:active,.hero-search button:active{ transform:translateY(1px) scale(.99) }
.searchbar button::after,.hero-search button::after{
  content:""; position:absolute; inset:0; pointer-events:none;
  background:linear-gradient(110deg,transparent 35%,rgba(255,255,255,.55) 50%,transparent 65%);
  transform:translateX(-120%); transition:transform .6s ease;
}
.searchbar button:hover::after,.hero-search button:hover::after{ transform:translateX(120%) }

/* ---- 标题 ---- */
.hero h1{
  font-size:clamp(26px,3.4vw,40px); letter-spacing:1px;
  background:linear-gradient(96deg,#eaf7ff 6%,#22d3ee 46%,#a78bfa 96%);
  -webkit-background-clip:text; background-clip:text; color:transparent;
  filter:drop-shadow(0 0 26px rgba(34,211,238,.28));
}
.hero p{ color:#7d8ea6 }
.hero__tag{
  font-family:var(--mono); font-size:12px !important; letter-spacing:1.5px;
  color:rgba(34,211,238,.72) !important; margin-bottom:14px !important;
}

/* ---- 快捷词 ---- */
.chip{
  font-family:var(--mono); border:1px solid rgba(34,211,238,.16);
  background:rgba(11,18,30,.7); color:#8fa4bd;
  transition:color .22s,background .22s,border-color .22s,box-shadow .22s,transform .22s;
}
.chip:hover{
  color:#02161d; background:linear-gradient(135deg,#22d3ee,#67e8f9);
  border-color:transparent; box-shadow:0 0 26px rgba(34,211,238,.5);
  transform:translateY(-2px);
}

/* ---- 卡片：渐变底 + 跟随高光 + 入场 ---- */
.card{
  position:relative; overflow:hidden;
  background:linear-gradient(158deg,rgba(17,27,45,.92),rgba(8,13,22,.94));
  border:1px solid rgba(34,211,238,.15);
  border-radius:var(--radius);
  transform-style:preserve-3d; will-change:transform;
  animation:cardIn .5s cubic-bezier(.2,.8,.2,1) both;
  transition:transform .3s cubic-bezier(.2,.8,.2,1),
             border-color .3s,box-shadow .3s;
}
@keyframes cardIn{
  from{ opacity:0; filter:blur(6px); translate:0 16px }
  to{ opacity:1; filter:blur(0); translate:0 0 }
}
.card::before{
  content:""; position:absolute; inset:0; z-index:1; pointer-events:none;
  background:radial-gradient(320px 180px at var(--mx,50%) var(--my,0%),
             rgba(34,211,238,.17),transparent 70%);
  opacity:0; transition:opacity .3s;
}
.card:hover::before{ opacity:1 }
.card:hover{
  border-color:rgba(34,211,238,.45);
  box-shadow:0 0 0 1px rgba(34,211,238,.2), 0 20px 46px rgba(0,0,0,.6),
             0 0 70px -24px rgba(34,211,238,.75);
}
.card .meta h3{ color:#e6f2ff }
.card .meta p{ color:#7d8ea6; font-family:var(--mono); font-size:12px }
.thumb .play{
  color:#22d3ee;
  text-shadow:0 0 24px rgba(34,211,238,.9);
  transition:opacity .2s,transform .2s;
}
.card:hover .thumb .play{ transform:scale(1.12) }

/* ---- 标签 ---- */
.tag{
  font-family:var(--mono); border:1px solid rgba(34,211,238,.14);
  background:rgba(11,18,30,.75); color:#8fa4bd;
}

/* ---- 播放器容器 ---- */
.player{
  border:1px solid rgba(34,211,238,.2);
  box-shadow:0 0 0 1px rgba(34,211,238,.06), 0 26px 60px rgba(0,0,0,.62),
             0 0 90px -30px rgba(34,211,238,.7);
}

/* ---- 剧集列表 ---- */
.episodes a{
  border:1px solid transparent;
  transition:background .18s,color .18s,border-color .18s,transform .18s;
}
.episodes a:hover{
  background:rgba(34,211,238,.09); border-color:rgba(34,211,238,.28);
  color:var(--cyan); transform:translateX(5px);
}
.episodes a[aria-current="true"]{
  background:linear-gradient(100deg,rgba(34,211,238,.92),rgba(34,211,238,.55));
  color:#02161d; font-weight:700;
  box-shadow:0 0 24px rgba(34,211,238,.45);
}
.episodes::-webkit-scrollbar{ width:6px }
.episodes::-webkit-scrollbar-track{ background:transparent }
.episodes::-webkit-scrollbar-thumb{
  background:rgba(34,211,238,.28); border-radius:3px;
}
.episodes::-webkit-scrollbar-thumb:hover{ background:rgba(34,211,238,.5) }

/* ---- 其他 ---- */
.ep-search{
  background:rgba(9,15,26,.88); color:var(--text);
  border:1px solid rgba(34,211,238,.16);
}
.ep-search:focus{
  border-color:rgba(34,211,238,.6);
  box-shadow:0 0 22px rgba(34,211,238,.18);
  outline:none;
}
.empty{
  background:rgba(11,18,30,.6);
  border-color:rgba(34,211,238,.18);
  font-family:var(--mono);
}
.site-footer{
  color:#4d5f78; font-family:var(--mono);
  font-size:12px; letter-spacing:.4px;
}
::selection{ background:rgba(34,211,238,.35); color:#eaf7ff }

/* ---- 尊重系统的"减少动态效果" ---- */
@media (prefers-reduced-motion: reduce){
  body::after{ animation:none }
  .card{ animation:none }
  .site-header .logo::after{ animation:none }
  .searchbar button::after,.hero-search button::after{ display:none }
}
"""

# 合并到页面附加样式（模板里只替换 __EXTRA_CSS__，所以追加即可全局生效）
PAGE_EXTRA_CSS = PAGE_EXTRA_CSS + TECH_THEME_CSS

# 注意：模板里的 __XXX__ 用 str.replace 替换，切勿改成 f-string（内含大量 {}）
SHELL_HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__PAGE_TITLE__</title>
<style>
__SITE_CSS__
__EXTRA_CSS__
</style>
</head>
<body>
<header class="site-header">
  <a class="logo" href="/">&#9654; FreeView</a>
  <form class="searchbar" action="/search" method="get" autocomplete="off">
    <input type="search" name="wd" placeholder="搜索片名…" value="__WD__"/>
    <button type="submit">搜索</button>
  </form>
</header>
"""

SHELL_FOOT = """<footer class="site-footer">
  &copy; FreeView &middot; 仅供学习交流，资源来自第三方采集站，请勿用于商业用途
</footer>
<script>
/* 源站检索一次要 3 秒，点下按钮立刻给反馈，别让人以为没反应 */
(function(){
  Array.prototype.forEach.call(document.querySelectorAll('form[action="/search"]'), function(f){
    f.addEventListener('submit', function(){
      var i = f.querySelector('input[name="wd"]');
      if(i && !i.value.trim()) return;
      var b = f.querySelector('button');
      if(b){ b.disabled = true; b.textContent = '搜索中…'; }
      document.body.style.cursor = 'progress';
    });
  });
})();
</script>
<script>
/* ---- 鼠标互动：光斑跟随 / 卡片 3D 倾斜 / 点击涟漪 ---- */
(function(){
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));

  /* 卡片入场依次错开，像列表在"刷"出来 */
  cards.forEach(function(c, i){
    c.style.animationDelay = Math.min(i * 0.05, 0.4).toFixed(2) + 's';
  });

  if(reduce) return;

  /* 1. 跟随鼠标的光斑：用 lerp 追帧，拖尾才有质感 */
  var glow = document.createElement('div');
  glow.className = 'fx-glow';
  document.body.appendChild(glow);
  var tx = window.innerWidth / 2, ty = window.innerHeight * 0.3;
  var cx = tx, cy = ty, ticking = false;
  function tick(){
    cx += (tx - cx) * 0.14;
    cy += (ty - cy) * 0.14;
    glow.style.transform = 'translate3d(' + cx.toFixed(1) + 'px,' + cy.toFixed(1) + 'px,0)';
    if(Math.abs(tx - cx) > 0.4 || Math.abs(ty - cy) > 0.4) requestAnimationFrame(tick);
    else ticking = false;
  }
  document.addEventListener('mousemove', function(e){
    tx = e.clientX; ty = e.clientY;
    document.body.classList.add('fx-on');
    if(!ticking){ ticking = true; requestAnimationFrame(tick); }
  });

  /* 2. 卡片跟着鼠标做 3D 倾斜，高光同时跟着走 */
  cards.forEach(function(card){
    card.addEventListener('mousemove', function(e){
      var r = card.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width;
      var py = (e.clientY - r.top) / r.height;
      card.style.transform =
        'perspective(820px) rotateX(' + ((0.5 - py) * 11).toFixed(2) + 'deg) ' +
        'rotateY(' + ((px - 0.5) * 13).toFixed(2) + 'deg) translateZ(12px)';
      card.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
      card.style.setProperty('--my', (py * 100).toFixed(1) + '%');
    });
    card.addEventListener('mouseleave', function(){ card.style.transform = ''; });
  });

  /* 3. 点击涟漪 */
  document.addEventListener('click', function(e){
    if(e.target.closest('input, textarea, select, option')) return;
    var s = document.createElement('span');
    s.className = 'fx-ripple';
    s.style.left = e.clientX + 'px';
    s.style.top = e.clientY + 'px';
    document.body.appendChild(s);
    setTimeout(function(){ s.remove(); }, 680);
  });
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 首页
# ---------------------------------------------------------------------------
INDEX_BODY = """<main class="site-main">
  <section class="hero" style="text-align:center">
    <p class="hero__tag">&gt;_ 直连采集源 · 实时索引 · 无广告插播</p>
    <h1>想看什么，搜一下</h1>
    <p>搜到就能直接播，无需下载</p>
    <form class="hero-search" action="/search" method="get" autocomplete="off">
      <input type="search" name="wd" placeholder="输入片名，如：凡人修仙传" autofocus/>
      <button type="submit">搜索</button>
    </form>
    <div style="margin-top:18px">__CHIPS__</div>
  </section>
</main>"""


def render_index() -> str:
    chips = "".join(
        f'<a class="chip" href="/search?wd={quote(k)}">{esc(k)}</a>'
        for k in WARM_KEYWORDS
    )
    body = INDEX_BODY.replace("__CHIPS__", chips)
    return (
        SHELL_HEAD.replace("__PAGE_TITLE__", "FreeView · 在线影视")
        .replace("__WD__", "")
        .replace("__SITE_CSS__", site.SITE_CSS)
        .replace("__EXTRA_CSS__", PAGE_EXTRA_CSS)
    ) + body + SHELL_FOOT


# ---------------------------------------------------------------------------
# 搜索结果
# ---------------------------------------------------------------------------
SEARCH_BODY = """<main class="site-main">
  <section class="hero">
    <h1>搜索结果</h1>
    <p>__SUMMARY__</p>
  </section>
  <section class="grid">
__CARDS__
  </section>
</main>"""

# 搜索结果先秒开（占位图），再异步把真实海报贴上去。
# 海报在详情页里，取一张要多抓一个 200KB 的页面，不能拖慢搜索。
POSTER_LAZY_JS = r"""
(function(){
  var cards = document.querySelectorAll('[data-poster]');
  if(!cards.length) return;
  Array.prototype.forEach.call(cards, function(card){
    var vid = card.getAttribute('data-poster');
    var thumb = card.querySelector('.thumb');
    if(!vid || !thumb) return;
    fetch('/api/poster?vid=' + encodeURIComponent(vid))
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){
        if(!d || !d.poster) return;
        var url = String(d.poster).replace(/["'\\\s]/g, '');
        if(!/^(https?:\/\/|\/static\/)/i.test(url)) return;   // 服务端可能已换成本地缓存地址
        var probe = new Image();
        probe.onload = function(){ thumb.style.backgroundImage = 'url("' + url + '")'; };
        probe.src = url;   // 图片能加载出来再换，避免留个白框
      })
      .catch(function(){});
  });
})();
"""


def _result_card(h: dict) -> str:
    tags = "".join(
        f'<span class="tag">{esc(v)}</span>'
        for v in (h.get("status"), h.get("region"), h.get("category"), h.get("update_time"))
        if v
    )
    return (
        '<a class="card" href="/play?vid={vid}" data-poster="{vid}">'
        '<div class="thumb" style="background-image:url(\'{poster}\')">'
        '<span class="play">&#9654;</span></div>'
        '<div class="meta"><h3>{title}</h3><p>{tags}</p></div></a>'
    ).format(
        vid=esc(h["vid"]),
        title=esc(h["title"]),
        tags=tags,
        poster=site.PLACEHOLDER_URI,
    )


def render_search(keyword: str, hits: list) -> str:
    if hits:
        summary = f"「{esc(keyword)}」找到 <strong>{len(hits)}</strong> 个结果"
        cards = "\n".join(_result_card(h) for h in hits)
    else:
        summary = f"「{esc(keyword)}」没有找到结果"
        cards = (
            '<div class="empty"><p>换个关键词试试，或者回'
            '<a href="/" style="color:var(--accent)">首页</a>重新搜索。</p></div>'
        )
    body = SEARCH_BODY.replace("__SUMMARY__", summary).replace("__CARDS__", cards)
    head = (
        SHELL_HEAD.replace("__PAGE_TITLE__", f"{esc(keyword)} 的搜索结果 · FreeView")
        .replace("__WD__", esc(keyword))
        .replace("__SITE_CSS__", site.SITE_CSS)
        .replace("__EXTRA_CSS__", PAGE_EXTRA_CSS)
    )
    return head + body + "<script>" + POSTER_LAZY_JS + "</script>\n" + SHELL_FOOT


# ---------------------------------------------------------------------------
# 播放页：播放器骨架与 main.py 的 PLAYER_JS 一一对应
# ---------------------------------------------------------------------------
WATCH_HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__PAGE_TITLE__</title>
<style>
__SITE_CSS__
__PLAYER_CSS__
__EXTRA_CSS__
</style>
__PRECONNECT__
</head>
<body>
<header class="site-header">
  <a class="logo" href="/">&#9654; FreeView</a>
  <form class="searchbar" action="/search" method="get" autocomplete="off">
    <input type="search" name="wd" placeholder="搜索片名…" value=""/>
    <button type="submit">搜索</button>
  </form>
</header>
"""

WATCH_BODY = """<main class="site-main">
<section class="watch-layout">
  <div class="watch-main">

    <div id="player" class="player" data-player>
      <video class="player__video" playsinline preload="metadata"__POSTER__
             data-src="__M3U8__">
        <p class="v-fallback">您的浏览器不支持 HTML5 视频。</p>
      </video>

      <button class="player__center-play" type="button" aria-label="播放" data-action="play">
        <svg viewBox="0 0 64 64" aria-hidden="true"><path d="M20 14 L20 50 L52 32 Z"/></svg>
      </button>

      <div class="player__loading" data-loading hidden>
        <div class="spinner"></div>
      </div>

      <div class="player__error" data-error hidden>
        <div class="player__error-card">
          <strong>播放失败</strong>
          <p data-error-msg>请检查网络或稍后重试。</p>
          <button type="button" class="btn" data-hls-retry>重试</button>
        </div>
      </div>

      <div class="player__overlay-top" data-overlay-top>
        <span class="player__title">__PLAYER_TITLE__</span>
      </div>

      <div class="player__controls" data-controls>
        <div class="player__progress" data-progress>
          <div class="player__progress-buffer" data-buffer></div>
          <div class="player__progress-played" data-played></div>
          <div class="player__progress-thumb" data-thumb></div>
          <input class="player__progress-input" type="range" min="0" max="100" step="0.1" value="0"
                 aria-label="进度" data-progress-input/>
        </div>

        <div class="player__row">
          <button class="btn btn--icon" type="button" data-action="play" aria-label="播放/暂停">
            <svg class="ic ic-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            <svg class="ic ic-pause" viewBox="0 0 24 24" hidden><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
          </button>

          <button class="btn btn--icon" type="button" data-action="back" aria-label="快退 10 秒">
            <svg viewBox="0 0 24 24"><path d="M11 5L4 12l7 7v-4a6 6 0 1 1-6 6"/></svg>
          </button>
          <button class="btn btn--icon" type="button" data-action="forward" aria-label="快进 10 秒">
            <svg viewBox="0 0 24 24"><path d="M13 5l7 7-7 7v-4a6 6 0 1 0 6 6"/></svg>
          </button>

          <div class="player__volume" data-volume>
            <button class="btn btn--icon" type="button" data-action="mute" aria-label="静音">
              <svg class="ic ic-vol" viewBox="0 0 24 24"><path d="M3 10v4h4l5 5V5L7 10H3zm13.5 2A4.5 4.5 0 0 0 14 7.97v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06a7 7 0 0 1 0 13.42v2.06A9 9 0 0 0 14 3.23z"/></svg>
              <svg class="ic ic-mute" viewBox="0 0 24 24" hidden><path d="M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.21.05-.42.05-.63zM19 12c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.8 8.8 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25A8.99 8.99 0 0 1 12 21v-2.43l5.49 5.49L18.93 22.6 1.41 5.08"/></svg>
            </button>
            <input class="player__volume-slider" type="range" min="0" max="1" step="0.01" value="1"
                   aria-label="音量" data-volume-input/>
          </div>

          <span class="player__time">
            <span data-time-current>00:00</span>
            <span class="sep">/</span>
            <span data-time-duration>00:00</span>
          </span>

          <div class="spacer"></div>

          <a class="btn btn--icon" href="__PREV_URL__" aria-label="上一集"
             style="text-decoration:none;font-size:13px" title="上一集">&#8678;</a>
          <a class="btn btn--icon" href="__NEXT_URL__" aria-label="下一集"
             style="text-decoration:none;font-size:13px" title="下一集">&#8680;</a>

          <div class="menu" data-menu="speed">
            <button class="btn" type="button" data-menu-toggle>速度 <span data-speed-label>1.0x</span></button>
            <ul class="menu__list" role="menu" hidden>__SPEED_LIST__</ul>
          </div>

          <button class="btn btn--icon" type="button" data-action="fullscreen" aria-label="全屏">
            <svg class="ic ic-fs-enter" viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
            <svg class="ic ic-fs-exit" viewBox="0 0 24 24" hidden><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>
          </button>
        </div>
      </div>
    </div>

    <section class="info">
      <h2>__TITLE__</h2>
      <p class="info__desc">__DESC__</p>
      <details class="info__keys">
        <summary>键盘快捷键</summary>
        <ul>
          <li><kbd>Space</kbd> / <kbd>K</kbd> 播放 / 暂停</li>
          <li><kbd>&#8592;</kbd> / <kbd>&#8594;</kbd> 快退 / 快进 5 秒</li>
          <li><kbd>J</kbd> / <kbd>L</kbd> 快退 / 快进 10 秒</li>
          <li><kbd>&#8593;</kbd> / <kbd>&#8595;</kbd> 音量 + / -</li>
          <li><kbd>M</kbd> 静音 &middot; <kbd>F</kbd> 全屏</li>
          <li><kbd>0</kbd>&ndash;<kbd>9</kbd> 跳到 0% &ndash; 90%</li>
        </ul>
      </details>
    </section>

  </div>

  <aside class="watch-side">
    <h3>__EP_HEAD__</h3>
    <input class="ep-search" type="search" placeholder="输入集数快速定位，如 88" data-ep-filter/>
    <ul class="episodes" data-ep-list>__EPISODES__</ul>
  </aside>
</section>
</main>
"""

# 播放页专属逻辑：hls.js 接入、进度记忆、自动下一集
HLS_INIT_JS = r"""
(function(){
  var v = document.querySelector('.player__video');
  if(!v) return;

  var src = v.getAttribute('data-src');
  var KEY = __KEY_LITERAL__;
  var NEXT = __NEXT_LITERAL__;
  var hls = null;
  var netRetry = 0;

  function fail(msg){
    var box = document.querySelector('[data-error]');
    var txt = document.querySelector('[data-error-msg]');
    var ld  = document.querySelector('[data-loading]');
    if(txt) txt.textContent = msg;
    if(box) box.hidden = false;
    if(ld)  ld.hidden = true;
  }
  function clearErr(){
    var box = document.querySelector('[data-error]');
    if(box) box.hidden = true;
  }

  /* ---- 断点续播 ---- */
  v.addEventListener('loadedmetadata', function(){
    try{
      var saved = parseFloat(localStorage.getItem(KEY) || '0');
      if(saved > 5 && v.duration && saved < v.duration - 5) v.currentTime = saved;
    }catch(_){}
  });
  var saving = null;
  v.addEventListener('timeupdate', function(){
    if(saving) return;
    saving = setTimeout(function(){
      saving = null;
      try{ localStorage.setItem(KEY, String(v.currentTime)); }catch(_){}
    }, 1000);
  });

  /* ---- 自动下一集 ---- */
  v.addEventListener('ended', function(){
    if(NEXT){ location.href = NEXT; }
  });

  /* ---- 起播 / 重试 ---- */
  var lastStart = 0;
  function start(){
    var now = Date.now();
    if(now - lastStart < 300) return;   // 连点重试去抖，避免堆出一串废弃的 MediaSource
    lastStart = now;
    clearErr();
    var ld = document.querySelector('[data-loading]');
    if(ld) ld.hidden = false;
    netRetry = 0;

    if(hls){ try{ hls.destroy(); }catch(_){} hls = null; }
    try{ v.removeAttribute('src'); }catch(_){}

    if(window.Hls && window.Hls.isSupported()){
      hls = new window.Hls({
        maxBufferLength: 45,            // 实测带宽只有码率的 1.6 倍，缓冲要拉深才不卡
        maxMaxBufferLength: 90,
        maxBufferSize: 60 * 1000 * 1000,
        backBufferLength: 30,           // 播过的及时释放，别把内存顶满
        testBandwidth: false,           // 跳过起播前的测速往返
        startFragPrefetch: true,        // 起播前先把首个分片抓下来
        fragLoadingMaxRetry: 6,         // 弱网下多给几次机会
        fragLoadingRetryDelay: 500,
        manifestLoadingMaxRetry: 4,
        enableWorker: false,            // 主线程解析，少一层兼容性变量
        lowLatencyMode: false
      });

      hls.on(window.Hls.Events.MANIFEST_PARSED, function(){
        // 清单已就绪，此时起播不会再抛 NotSupportedError；
        // 若被浏览器自动播放策略拦下也无妨，用户点播放按钮即可
        var p = v.play();
        if(p && p.catch) p.catch(function(){});
      });

      hls.on(window.Hls.Events.ERROR, function(_, data){
        if(!data || !data.fatal) return;
        if(data.type === window.Hls.ErrorTypes.NETWORK_ERROR){
          if(++netRetry <= 3){      // 网络抖动：有限次重试拉流
            hls.startLoad();
          }else{
            fail('网络错误：拉流失败（点"重试"重新开始）');
          }
        }else if(data.type === window.Hls.ErrorTypes.MEDIA_ERROR){
          hls.recoverMediaError();  // 解码错误：尝试恢复
        }else{
          fail('播放失败：' + (data.details || data.type));
        }
      });

      hls.loadSource(src);
      hls.attachMedia(v);
    }else if(v.canPlayType('application/vnd.apple.mpegurl')){
      /* Safari / iOS 原生支持 HLS */
      v.src = src;
      var p2 = v.play();
      if(p2 && p2.catch) p2.catch(function(){});
    }else{
      fail('当前浏览器不支持 HLS 播放（建议用 Chrome / Edge）');
    }
  }

  /* 真正出画面 / 出声后把错误卡片收掉：
     用户点得比 hls 就绪还早时会先冒一下错误，等清单解析完自动起播就该收起来 */
  v.addEventListener('playing', clearErr);
  v.addEventListener('canplay', clearErr);

  var retryBtn = document.querySelector('[data-hls-retry]');
  if(retryBtn) retryBtn.addEventListener('click', function(e){
    e.preventDefault();
    start();
  });

  start();

  /* ---- 剧集快速定位 ---- */
  var filter = document.querySelector('[data-ep-filter]');
  var list = document.querySelector('[data-ep-list]');
  if(filter && list){
    filter.addEventListener('input', function(){
      var q = (filter.value || '').trim();
      list.querySelectorAll('li').forEach(function(li){
        li.style.display = (!q || li.textContent.indexOf(q) > -1) ? '' : 'none';
      });
    });
  }
  var cur = list && list.querySelector('a[aria-current="true"]');
  if(cur && cur.scrollIntoView){ cur.scrollIntoView({block: 'center'}); }
})();
"""


def _speed_list() -> str:
    items = []
    for s in site.SPEED_PRESETS:
        sel = ' aria-selected="true"' if abs(s - 1.0) < 1e-9 else ""
        items.append(f'<li><button type="button" data-speed="{s}"{sel}>{s}x</button></li>')
    return "".join(items)


def _ep_items(vid: str, eps: list, current: int) -> str:
    out = []
    for i, ep in enumerate(eps[:EP_PAGE_LIMIT], 1):
        cur = ' aria-current="true"' if i == current else ""
        href = esc(f"/play?vid={vid}&ep={i}")
        out.append(f'<li><a href="{href}"{cur}>{esc(ep["name"])}</a></li>')
    return "".join(out)


def render_watch(
    detail: dict, eps: list, sources: list, chosen_source: str, idx: int,
    play: dict, ad_removed: float = 0.0,
) -> str:
    vid = detail["vid"]
    ep = eps[idx - 1]
    title = detail["title"] or f"视频 {vid}"

    prev_url = f"/play?vid={vid}&ep={idx - 1}" if idx > 1 else "#"
    next_url = f"/play?vid={vid}&ep={idx + 1}" if idx < len(eps) else "#"

    # 优先用播放页里的视频截图，拿不到时退回详情页海报
    poster = play.get("poster") or detail.get("poster") or ""
    poster_attr = f' poster="{esc(poster)}"' if poster else ""

    # 提前与播放域名建连，省掉起播前的 DNS + TLS 握手
    # （play["m3u8"] 若已换成本地 /hls/ 路径，要用原始地址的域名）
    preconnect = ""
    parsed = urlparse(play.get("raw") or play["m3u8"])
    if parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        preconnect = (
            f'<link rel="preconnect" href="{esc(origin)}" crossorigin/>\n'
            f'<link rel="dns-prefetch" href="{esc(origin)}"/>'
        )

    desc_bits = [f"第 {idx} / {len(eps)} 集"]
    if len(sources) > 1:
        desc_bits.append(f"播放源：{chosen_source}")
    if ad_removed:
        desc_bits.append(f"已跳过 {ad_removed:.0f}s 插播广告")
    desc = " &middot; ".join(desc_bits) + " &nbsp;|&nbsp; 已自动保存播放进度"

    head = (
        WATCH_HEAD.replace("__PAGE_TITLE__", f"{esc(title)} {esc(ep['name'])} · FreeView")
        .replace("__SITE_CSS__", site.SITE_CSS)
        .replace("__PLAYER_CSS__", site.PLAYER_CSS)
        .replace("__EXTRA_CSS__", PAGE_EXTRA_CSS)
        .replace("__PRECONNECT__", preconnect)
    )

    body = (
        WATCH_BODY.replace("__POSTER__", poster_attr)
        .replace("__M3U8__", esc(play["m3u8"]))
        .replace("__PLAYER_TITLE__", esc(f"{title} · {ep['name']}"))
        .replace("__PREV_URL__", esc(prev_url))
        .replace("__NEXT_URL__", esc(next_url))
        .replace("__SPEED_LIST__", _speed_list())
        .replace("__TITLE__", esc(title))
        .replace("__DESC__", desc)
        .replace("__EP_HEAD__", esc(f"剧集（{len(eps)}）"))
        .replace("__EPISODES__", _ep_items(vid, eps, idx))
    )

    hls_init = (
        HLS_INIT_JS.replace("__KEY_LITERAL__", json.dumps(f"fv:{vid}:{idx}"))
        .replace("__NEXT_LITERAL__", json.dumps(next_url if next_url != "#" else ""))
    )

    # 拼接 script 时绝不能用 f-string（PLAYER_JS 内含大量 {...}）
    scripts = (
        '<script src="/static/hls.min.js"></script>\n'
        "<script>" + hls_init + "</script>\n"
        "<script>" + site.PLAYER_JS + "</script>\n"
    )
    return head + body + scripts + SHELL_FOOT


def render_error(title: str, message: str, code: int = 200) -> tuple[int, str]:
    body = (
        '<main class="site-main"><section class="hero">'
        f"<h1>{esc(title)}</h1><p>{esc(message)}</p>"
        '<p style="margin-top:16px"><a href="/" style="color:var(--accent)">返回首页</a></p>'
        "</section></main>"
    )
    html = (
        SHELL_HEAD.replace("__PAGE_TITLE__", f"{esc(title)} · FreeView")
        .replace("__WD__", "")
        .replace("__SITE_CSS__", site.SITE_CSS)
        .replace("__EXTRA_CSS__", PAGE_EXTRA_CSS)
    ) + body + SHELL_FOOT
    return code, html


# ============================================================================
# HTTP 服务
# ============================================================================
class FreeViewSite(BaseHTTPRequestHandler):
    server_version = "FreeViewSite/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ---------- 工具 ----------
    def _send(self, status: int, body, content_type="text/html; charset=utf-8", cache="no-cache"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, status=200, cache="no-cache"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", cache=cache)

    def _static(self, rel: str):
        try:
            target = (STATIC_DIR / rel).resolve()
        except OSError:
            return self._send(400, "bad path", "text/plain; charset=utf-8")
        root = STATIC_DIR.resolve()
        if target != root and root not in target.parents:
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        mime = "application/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                code, html = render_error("出错了", f"{type(e).__name__}: {e}", 500)
                self._send(code, html)
            except Exception:
                pass

    def _route(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        def arg(name: str, default: str = "") -> str:
            return (q.get(name) or [default])[0].strip()

        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path.startswith("/hls/"):
            # 清洗过的播放列表（已去掉插播广告）
            key = path[len("/hls/"):]
            body = _cache_get(_hls_cache, key)
            if not body:
                return self._send(404, "#EXTM3U\n#EXT-X-ENDLIST\n",
                                  "application/vnd.apple.mpegurl")
            return self._send(200, body, "application/vnd.apple.mpegurl")
        if path in ("/healthz", "/health"):
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if path == "/api/poster":
            vid = arg("vid")
            if not vid.isdigit():
                return self._send_json({"vid": vid, "poster": ""})
            detail = get_detail(vid)
            remote = (detail or {}).get("poster") or ""
            local = cache_remote_image(remote) if remote else ""
            spawn(prefetch_episode, vid)   # 顺手把第 1 集的 m3u8 也备好
            return self._send_json({"vid": vid, "poster": local}, cache="public, max-age=3600")
        if path in ("/", "/index.html"):
            return self._send(200, render_index())
        if path == "/search":
            wd = arg("wd")
            if not wd:
                return self._send(200, render_index())
            try:
                page = max(1, int(arg("page", "1") or 1))
            except ValueError:
                page = 1
            hits = search_cached(wd, page=page)
            return self._send(200, render_search(wd, hits))
        if path == "/play":
            return self._play(arg("vid"), arg("ep", "1"), arg("source"))

        code, html = render_error("404", f"没有这个页面：{path}", 404)
        return self._send(code, html)

    def _play(self, vid: str, ep_raw: str, source: str):
        if not vid.isdigit():
            code, html = render_error("参数不对", "缺少有效的 vid 参数", 400)
            return self._send(code, html)

        detail = get_detail(vid)
        if not detail or not detail.get("episodes"):
            code, html = render_error(
                "没拿到播放地址",
                "详情页解析失败，可能是站点结构变了，或网络不通。",
                502,
            )
            return self._send(code, html)

        eps, sources, chosen = pick_episodes(detail, source)
        if not eps:
            code, html = render_error("没有剧集", "这个播放源下没有可用剧集", 404)
            return self._send(code, html)

        try:
            idx = int(ep_raw)
        except ValueError:
            idx = 1
        idx = min(max(idx, 1), len(eps))

        play = get_play(eps[idx - 1]["share_url"])
        if not play:
            code, html = render_error(
                "解析播放地址失败",
                f"{eps[idx - 1]['name']} 的 m3u8 没解析出来，换一集或稍后重试。",
                502,
            )
            return self._send(code, html)

        # 清洗播放列表：去掉插播广告段，分片地址转绝对，交给本地 /hls/ 提供
        raw_url = play["m3u8"]
        ad_removed = 0.0
        if STRIP_AD_BLOCK:
            cleaned = clean_playlist_cached(raw_url)
            if cleaned:
                m3u8_text, ad_removed = cleaned
                key = hashlib.sha1(raw_url.encode("utf-8")).hexdigest()[:20] + ".m3u8"
                _cache_put(_hls_cache, key, m3u8_text)
                play = {**play, "m3u8": "/hls/" + key, "raw": raw_url}

        # 顺手预热下一集，用户点"下一集"时就能秒开
        if idx < len(eps):
            spawn(get_play, eps[idx]["share_url"])

        return self._send(
            200, render_watch(detail, eps, sources, chosen, idx, play, ad_removed)
        )


# ============================================================================
# 启动
# ============================================================================
def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("HOST", "0.0.0.0")

    if not HLS_JS_FILE.is_file():
        print(f"[FATAL] 缺少 {HLS_JS_FILE}，播放器无法工作。")
        print("        下载：Invoke-WebRequest https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js "
              "-OutFile static/hls.min.js")
        return 1

    server = ThreadingHTTPServer((host, port), FreeViewSite)
    print("=" * 64)
    print("[FreeView] 在线影视站已启动")
    print(f"  本机访问:   http://127.0.0.1:{port}/")
    print(f"  局域网:     http://{local_ip()}:{port}/")
    print(f"  播放器:     复用 main.py 控件 + 本地 hls.js ({HLS_JS_FILE.stat().st_size // 1024} KB)")
    print(f"  数据来源:   1.py 抓取非凡资源网")
    print("  退出:       Ctrl+C")
    print("=" * 64)
    spawn(warm_up)   # 后台预热首页快捷词，点第一个词就是秒开
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[FreeView] 关闭...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
