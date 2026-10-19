#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreeView · 单文件视频观看网站
================================
完全使用 Python 标准库实现，零第三方依赖。

用法：
    python main.py                  # 默认 http://127.0.0.1:5000
    PORT=8080 python main.py        # 自定义端口

内容源（三选一或多选，自动合并）：
    1. 本地视频：把 .mp4/.webm/.m3u8 放进 videos/ 目录
    2. 自定义元数据：编辑 videos/_catalog.json（可配多清晰度、海报）
    3. 远程直链：在下方 REMOTE_SOURCES 里填媒体直链

⚠ 请只使用你拥有版权或已获授权的视频资源。
"""
from __future__ import annotations

import html as _html
import json
import mimetypes
import os
import re
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# ============================================================================
# 路径与常量
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
CATALOG_FILE = VIDEO_DIR / "_catalog.json"
VIDEO_DIR.mkdir(exist_ok=True)

VIDEO_EXTS = {".mp4", ".webm", ".m3u8", ".mov", ".m4v", ".ogv"}
SPEED_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

# ---------------------------------------------------------------------------
# 远程视频源（可选）
# ---------------------------------------------------------------------------
# 下面预置了 3 部 Blender 基金会的开源电影（Creative Commons 授权，
# 属于可自由传播的公开片源），用于演示播放器效果。
# 你可以随时清空列表，或替换成自己的 CDN / OSS 直链。
#
# 格式：
#   {"title": "标题",
#    "src": "https://cdn.example.com/movie.mp4",   # 必须是媒体文件直链
#    "type": "video/mp4",                           # 可省略，自动推断
#    "poster": "https://cdn.example.com/cover.jpg"} # 可省略
#
# ⚠ 注意：
#   1. src 必须是媒体直链（.mp4/.m3u8/.webm…）。
#      形如 /share/xxxx 的网页分享地址，<video> 无法直接播放。
#   2. 只填你拥有版权或已获授权的视频资源。
REMOTE_SOURCES: list[dict] = [
    {
        "id": "big-buck-bunny",
        "title": "Big Buck Bunny（开源电影 · CC BY 3.0）",
        "description": "Blender 基金会出品，Creative Commons 授权，可自由传播。",
        "src": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
        "type": "video/mp4",
    },
    {
        "id": "elephants-dream",
        "title": "Elephants Dream（开源电影 · CC BY 2.5）",
        "description": "Blender 基金会首部开源电影，Creative Commons 授权。",
        "src": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4",
        "type": "video/mp4",
    },
    {
        "id": "sintel",
        "title": "Sintel（开源电影 · CC BY 3.0）",
        "description": "Blender 基金会开源动画短片，Creative Commons 授权。",
        "src": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
        "type": "video/mp4",
    },
]

# 远程 src 允许的协议白名单，避免 javascript: / data: 等危险协议注入
SAFE_URL_SCHEMES = ("http://", "https://")


# ============================================================================
# 工具函数
# ============================================================================
def slugify(name: str) -> str:
    """把名称转成 URL 友好的 id，保留中文等 Unicode 字母数字。"""
    name = re.sub(r"\s+", "-", str(name).strip().lower())
    # \w 在 Python 3 下默认匹配 Unicode 字母数字（含中文）
    name = re.sub(r"[^\w\-.]", "", name, flags=re.UNICODE)
    # 合并连续连字符，去掉首尾的连字符 / 点
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    return name or "video"


def html_escape(s) -> str:
    return "" if s is None else _html.escape(str(s), quote=True)


def guess_mime(suffix: str) -> str:
    s = suffix.lower().lstrip(".")
    table = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "m3u8": "application/x-mpegURL",
        "mov": "video/mp4",
        "m4v": "video/mp4",
        "ogv": "video/ogg",
        "svg": "image/svg+xml",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "ico": "image/x-icon",
        "css": "text/css; charset=utf-8",
        "js": "application/javascript; charset=utf-8",
        "json": "application/json; charset=utf-8",
    }
    if s in table:
        return table[s]
    return mimetypes.guess_type(suffix)[0] or "application/octet-stream"


def safe_video_path(rel: str) -> Path:
    """防止路径穿越"""
    if not rel:
        raise ValueError("empty path")
    rel = rel.replace("\\", "/").lstrip("/")
    target = (VIDEO_DIR / rel).resolve()
    vroot = VIDEO_DIR.resolve()
    if vroot != target and vroot not in target.parents:
        raise ValueError("path traversal blocked")
    return target


# ============================================================================
# 视频目录：扫描 / 加载 / 保存
# ============================================================================
def load_catalog() -> list:
    """加载视频清单：_catalog.json 优先 → 本地扫描 → 合并远程源。"""
    items = []
    if CATALOG_FILE.exists():
        try:
            data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                items = data
        except Exception as e:
            print(f"[warn] _catalog.json 解析失败: {e}，将扫描目录")

    if not items:
        items = _scan_local_videos()

    items.extend(_build_remote_items())
    return _dedupe_ids(items)


def _scan_local_videos() -> list:
    """扫描 videos/ 目录，把本地媒体文件转成标准视频条目。"""
    items = []
    for p in sorted(VIDEO_DIR.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        rel = p.relative_to(VIDEO_DIR).as_posix()
        items.append({
            "id": slugify(p.stem),
            "title": p.stem,
            "description": "本地视频",
            "poster": "",
            "sources": [
                {
                    "label": "默认",
                    "src": f"/videos/{rel}",
                    "type": guess_mime(p.suffix),
                }
            ],
        })
    return items


def _build_remote_items() -> list:
    """把 REMOTE_SOURCES 转成标准视频条目；过滤非法协议与空地址。"""
    out = []
    for idx, raw in enumerate(REMOTE_SOURCES):
        src = str(raw.get("src") or "").strip()
        if not src.lower().startswith(SAFE_URL_SCHEMES):
            print(f"[warn] 跳过 REMOTE_SOURCES[{idx}]：src 必须以 http:// 或 https:// 开头")
            continue
        # 去掉 query（如 ?v=1），仅用于推断扩展名
        clean = src.split("?", 1)[0]
        out.append({
            "id": raw.get("id") or slugify(raw.get("title") or f"remote-{idx + 1}"),
            "title": raw.get("title") or f"远程视频 {idx + 1}",
            "description": raw.get("description") or "远程视频源",
            "poster": raw.get("poster") or "",
            "sources": [
                {
                    "label": raw.get("label") or "默认",
                    "src": src,
                    "type": raw.get("type") or guess_mime(Path(clean).suffix),
                }
            ],
        })
    return out


def _dedupe_ids(items: list) -> list:
    """保证 id 唯一，避免同名条目在 /watch/<id> 路由上互相覆盖。"""
    seen = set()
    for it in items:
        base = it.get("id") or "video"
        uid, n = base, 2
        while uid in seen:
            uid = f"{base}-{n}"
            n += 1
        it["id"] = uid
        seen.add(uid)
    return items


def save_catalog(items: list) -> None:
    CATALOG_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def placeholder_data_uri() -> str:
    """内嵌的占位海报（SVG → data URI）。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 270" '
        'preserveAspectRatio="xMidYMid slice">'
        '<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1">'
        '<stop offset="0" stop-color="#1d2230"/>'
        '<stop offset="1" stop-color="#0f1115"/>'
        '</linearGradient></defs>'
        '<rect width="480" height="270" fill="url(#g)"/>'
        '<g fill="#fff" opacity=".85">'
        '<circle cx="240" cy="135" r="28" fill="rgba(255,59,88,.85)"/>'
        '<path d="M232 122 L232 148 L256 135 Z" fill="#fff"/>'
        '</g>'
        '<text x="240" y="200" text-anchor="middle" '
        'font-family="Segoe UI, Arial" font-size="14" fill="rgba(255,255,255,.6)">'
        'FreeView</text></svg>'
    )
    import urllib.parse
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


PLACEHOLDER_URI = placeholder_data_uri()


# ============================================================================
# 内嵌静态资源：CSS
# ============================================================================
SITE_CSS = r"""
:root{
  --bg:#0f1115;--bg-2:#161922;--card:#1d2230;--text:#e7eaf3;
  --text-dim:#9aa1b1;--accent:#ff3b58;--accent-2:#ff7a8c;
  --border:rgba(255,255,255,.08);--radius:12px;--shadow:0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 -apple-system,Segoe UI,Roboto,"PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",Arial,sans-serif}
a{color:inherit;text-decoration:none}
.site-header{position:sticky;top:0;z-index:50;
  display:flex;align-items:center;gap:24px;padding:12px 24px;
  background:rgba(15,17,21,.85);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--border)}
.site-header .logo{font-weight:700;letter-spacing:.5px;font-size:18px;color:var(--accent)}
.site-main{max-width:1280px;margin:0 auto;padding:24px}
.site-footer{text-align:center;color:var(--text-dim);padding:32px 16px 48px;font-size:13px}
.hero{padding:24px 0 8px}
.hero h1{font-size:28px;margin:0 0 6px}
.hero p{color:var(--text-dim);margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
  gap:18px;margin-top:18px}
.card{background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;
  transition:transform .15s ease,box-shadow .15s ease}
.card:hover{transform:translateY(-3px);box-shadow:var(--shadow);
  border-color:rgba(255,255,255,.15)}
.thumb{position:relative;aspect-ratio:16/9;background-color:#000;
  background-position:center;background-size:cover}
.thumb .play{position:absolute;inset:0;display:grid;place-items:center;
  font-size:36px;color:rgba(255,255,255,.9);
  text-shadow:0 6px 20px rgba(0,0,0,.6);opacity:0;transition:opacity .2s}
.card:hover .thumb .play{opacity:1}
.thumb--sm{width:120px;aspect-ratio:16/9;border-radius:8px;flex:none}
.meta{padding:10px 12px 14px}
.meta h3{margin:0 0 4px;font-size:15px}
.meta p{margin:0;color:var(--text-dim);font-size:13px}
.empty{grid-column:1/-1;text-align:center;padding:48px 16px;color:var(--text-dim);
  background:var(--card);border:1px dashed var(--border);border-radius:var(--radius)}
.empty code{background:rgba(255,255,255,.08);padding:1px 6px;border-radius:4px}
.watch-layout{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px}
@media (max-width:900px){.watch-layout{grid-template-columns:1fr}}
.watch-main{min-width:0}
.info{padding:8px 4px 16px}
.info h2{margin:14px 0 6px;font-size:20px}
.info__desc{color:var(--text-dim);margin:0 0 12px}
.info__keys summary{cursor:pointer;color:var(--text-dim);font-size:13px;user-select:none}
.info__keys ul{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:6px 18px;margin:10px 0 0;padding:0;list-style:none;
  color:var(--text-dim);font-size:13px}
kbd{display:inline-block;padding:1px 6px;border-radius:4px;
  background:rgba(255,255,255,.08);border:1px solid var(--border);
  font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px}
.watch-side h3{margin:14px 0 10px;font-size:15px;color:var(--text-dim)}
.related{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.related a{display:flex;gap:10px;padding:8px;border-radius:10px}
.related a:hover{background:var(--bg-2)}
.related strong{display:block;font-size:14px}
.related small{color:var(--text-dim);font-size:12px}
.related .empty{background:transparent;border:none;padding:8px}
.v-fallback{padding:24px;color:#fff;text-align:center}
"""

PLAYER_CSS = r"""
:root{
  --p-bg:#000;--p-fg:#fff;--p-dim:rgba(255,255,255,.65);
  --p-accent:#ff3b58;--p-border:rgba(255,255,255,.12);--p-radius:10px;
}
.player{position:relative;width:100%;background:var(--p-bg);
  border-radius:14px;overflow:hidden;aspect-ratio:16/9;isolation:isolate;user-select:none}
.player[data-fullscreen="true"]{border-radius:0}
.player__video{width:100%;height:100%;display:block;background:#000;outline:none}
.player__center-play{position:absolute;inset:0;margin:auto;width:72px;height:72px;
  border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;
  display:grid;place-items:center;cursor:pointer;
  transition:transform .15s ease,background .15s ease,opacity .15s ease;
  backdrop-filter:blur(2px)}
.player__center-play:hover{background:rgba(0,0,0,.7);transform:scale(1.06)}
.player__center-play:active{transform:scale(.96)}
.player__center-play svg{width:36px;height:36px;fill:currentColor}
.player[data-playing="true"] .player__center-play{display:none}
.player__overlay-top{position:absolute;left:0;right:0;top:0;padding:14px 18px;
  background:linear-gradient(to bottom,rgba(0,0,0,.55),transparent);color:#fff;
  pointer-events:none;opacity:0;transition:opacity .25s ease}
.player__title{font-size:14px;font-weight:500;letter-spacing:.3px}
.player__controls{position:absolute;left:0;right:0;bottom:0;padding:14px 16px 12px;
  background:linear-gradient(to top,rgba(0,0,0,.55),transparent);
  transition:opacity .25s ease,transform .25s ease;opacity:1}
.player[data-cursor="hidden"] .player__controls,
.player[data-cursor="hidden"] .player__overlay-top{opacity:0;pointer-events:none}
.player[data-cursor="hidden"][data-playing="true"] .player__controls{opacity:0}
.player__row{display:flex;align-items:center;gap:6px}
.spacer{flex:1}
.btn{--size:36px;min-width:var(--size);height:var(--size);padding:0 10px;
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  background:rgba(255,255,255,.06);border:1px solid transparent;border-radius:8px;
  color:var(--p-fg);font-size:12px;cursor:pointer;
  transition:background .15s ease,transform .1s ease,border-color .15s ease}
.btn:hover{background:rgba(255,255,255,.12)}
.btn:active{transform:scale(.95)}
.btn--icon{padding:0;width:var(--size)}
.btn svg{width:20px;height:20px;fill:currentColor}
.player__progress{position:relative;height:18px;display:flex;align-items:center;
  margin-bottom:8px;cursor:pointer}
.player__progress::before{content:"";position:absolute;inset:50% 0 auto 0;
  height:4px;background:rgba(255,255,255,.22);border-radius:4px;
  transform:translateY(-50%)}
.player__progress-buffer,.player__progress-played{position:absolute;left:0;top:50%;
  height:4px;border-radius:4px;transform:translateY(-50%);pointer-events:none}
.player__progress-buffer{background:rgba(255,255,255,.35);width:0}
.player__progress-played{background:var(--p-accent);width:0}
.player__progress-thumb{position:absolute;left:0;top:50%;width:12px;height:12px;
  background:var(--p-accent);border-radius:50%;
  transform:translate(-50%,-50%) scale(0);transition:transform .15s ease;
  pointer-events:none;box-shadow:0 0 0 4px rgba(255,59,88,.25)}
.player__progress:hover .player__progress-thumb,
.player__progress[data-dragging="true"] .player__progress-thumb{transform:translate(-50%,-50%) scale(1)}
.player__progress-input{position:absolute;inset:0;width:100%;height:100%;margin:0;
  opacity:0;cursor:pointer;background:transparent}
.player__volume{display:flex;align-items:center;gap:4px}
.player__volume-slider{-webkit-appearance:none;appearance:none;width:80px;height:4px;
  background:rgba(255,255,255,.25);border-radius:4px;outline:none;cursor:pointer;margin:0 4px}
.player__volume-slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:12px;height:12px;background:#fff;border-radius:50%;border:0;cursor:pointer;
  box-shadow:0 0 0 3px rgba(255,255,255,.15)}
.player__volume-slider::-moz-range-thumb{width:12px;height:12px;background:#fff;
  border-radius:50%;border:0}
.player__time{font-variant-numeric:tabular-nums;font-size:12px;color:var(--p-dim);margin-left:6px}
.player__time .sep{margin:0 4px;opacity:.5}
.menu{position:relative}
.menu .btn{font-size:12px;padding:0 10px}
.menu__list{position:absolute;right:0;bottom:calc(100% + 8px);
  min-width:96px;margin:0;padding:4px;list-style:none;
  background:rgba(20,22,28,.92);border:1px solid var(--p-border);
  border-radius:10px;backdrop-filter:blur(6px);
  box-shadow:0 12px 28px rgba(0,0,0,.35);z-index:5}
.menu__list li button{width:100%;padding:6px 10px;background:transparent;border:0;
  color:#fff;font-size:13px;text-align:left;border-radius:6px;cursor:pointer}
.menu__list li button:hover,
.menu__list li button[aria-selected="true"]{background:rgba(255,255,255,.08)}
.player__loading{position:absolute;inset:0;display:grid;place-items:center;
  background:rgba(0,0,0,.25);pointer-events:none}
.spinner{width:48px;height:48px;border:4px solid rgba(255,255,255,.2);
  border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.player__error{position:absolute;inset:0;display:grid;place-items:center;
  background:rgba(0,0,0,.55)}
.player__error-card{background:rgba(28,30,38,.96);border:1px solid var(--p-border);
  border-radius:12px;padding:18px 22px;text-align:center;max-width:80%}
.player__error-card strong{font-size:16px}
.player__error-card p{margin:6px 0 12px;color:var(--p-dim);font-size:13px}
.player__error-card .btn{padding:6px 16px;font-size:13px}
.player[data-buffering="true"] .player__loading{display:grid}
@media (max-width:600px){
  .player__volume-slider{width:50px}
  .player__time{font-size:11px}
  .btn{--size:32px}
  .btn svg{width:18px;height:18px}
}
"""


# ============================================================================
# 播放器核心 JS
# ============================================================================
PLAYER_JS = r"""
(function(global){
  'use strict';
  var $ = function(s, r){ return (r||document).querySelector(s); };
  var $$ = function(s, r){ return Array.prototype.slice.call((r||document).querySelectorAll(s)); };
  var clamp = function(n, mn, mx){ return Math.max(mn, Math.min(mx, n)); };
  var fmt = function(s){
    if(!isFinite(s) || s < 0) s = 0;
    var h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = Math.floor(s%60);
    var mm = (m<10?'0':'')+m, ss2 = (ss<10?'0':'')+ss;
    return h>0 ? (h+':'+mm+':'+ss2) : (mm+':'+ss2);
  };

  function createPlayer(root){
    var video = $('video.player__video', root);
    if(!video) return;

    var $prog     = $('[data-progress]', root);
    var $buf      = $('[data-buffer]', root);
    var $played   = $('[data-played]', root);
    var $thumb    = $('[data-thumb]', root);
    var $pin      = $('[data-progress-input]', root);
    var $cur      = $('[data-time-current]', root);
    var $dur      = $('[data-time-duration]', root);
    var $vol      = $('[data-volume-input]', root);
    var $muteBtn  = $('[data-action="mute"]', root);
    var $icV      = $('.ic-vol', $muteBtn);
    var $icM      = $('.ic-mute', $muteBtn);
    var $playBtn  = $('[data-action="play"].btn', root);
    var $icP      = $('.ic-play', $playBtn);
    var $icPa     = $('.ic-pause', $playBtn);
    var $cp       = $('.player__center-play', root);
    var $fsBtn    = $('[data-action="fullscreen"]', root);
    var $icFsE    = $('.ic-fs-enter', $fsBtn);
    var $icFsX    = $('.ic-fs-exit', $fsBtn);
    var $ld       = $('[data-loading]', root);
    var $er       = $('[data-error]', root);
    var $erMsg    = $('[data-error-msg]', root);
    var $retry    = $('[data-action="retry"]', root);
    var $spL      = $('[data-speed-label]', root);

    var st = { playing: false, lastVol: 1, dur: 0, cursor: null, srcs: [], qi: 0 };
    st.srcs = $$('source', video).map(function(s){
      return { src: s.getAttribute('src'), type: s.getAttribute('type')||'', label: s.getAttribute('data-label')||'' };
    });
    if(!st.srcs.length && video.src) st.srcs = [{ src: video.src, type: '', label: '默认' }];

    function play(){
      var p = video.play();
      if(p && p.catch) p.catch(function(e){ err('自动播放被拦截，请手动播放（'+(e&&e.name||'')+'）。'); });
    }
    function pause(){ video.pause(); }
    function toggle(){ st.playing ? pause() : play(); }
    function syncIcon(){
      root.dataset.playing = st.playing ? 'true' : 'false';
      $icP.hidden = st.playing;
      $icPa.hidden = !st.playing;
    }

    function updateProg(){
      var dur = st.dur || video.duration || 0, cur = video.currentTime || 0;
      var r = dur ? (cur/dur)*100 : 0;
      $played.style.width = r+'%';
      $thumb.style.left = r+'%';
      $pin.value = r;
      $cur.textContent = fmt(cur);
      try {
        var bf = video.buffered;
        if(bf && bf.length){
          var e = bf.end(bf.length - 1);
          var br = dur ? (e/dur)*100 : 0;
          $buf.style.width = clamp(br, 0, 100) + '%';
        }
      } catch(_){}
    }
    function setFromEvt(e){
      var rc = $prog.getBoundingClientRect();
      var x = (e.touches ? e.touches[0].clientX : e.clientX) - rc.left;
      var r = clamp(x / rc.width, 0, 1);
      var dur = st.dur || video.duration || 0;
      if(dur > 0) video.currentTime = r * dur;
      $played.style.width = r*100 + '%';
      $thumb.style.left = r*100 + '%';
    }
    var drag = false;
    function stop(){ drag = false; $prog.dataset.dragging = 'false'; }
    $prog.addEventListener('mousedown', function(e){ drag = true; $prog.dataset.dragging='true'; setFromEvt(e); });
    window.addEventListener('mousemove', function(e){ if(drag) setFromEvt(e); });
    window.addEventListener('mouseup', stop);
    $prog.addEventListener('touchstart', function(e){ drag = true; $prog.dataset.dragging='true'; setFromEvt(e); }, { passive: true });
    window.addEventListener('touchmove', function(e){ if(drag) setFromEvt(e); }, { passive: true });
    window.addEventListener('touchend', stop);
    $pin.addEventListener('input', function(){
      var r = clamp(parseFloat($pin.value) || 0, 0, 100);
      var dur = st.dur || video.duration || 0;
      if(dur > 0) video.currentTime = (r/100) * dur;
      $played.style.width = r + '%';
      $thumb.style.left = r + '%';
    });

    function updDur(){ var d = isFinite(video.duration) ? video.duration : 0; st.dur = d; $dur.textContent = fmt(d); }

    function setVol(v){
      v = clamp(v, 0, 1);
      video.volume = v;
      video.muted = (v === 0);
      $vol.value = String(v);
      if(v > 0) st.lastVol = v;
      syncMute();
    }
    function toggleMute(){ if(video.muted || video.volume === 0) setVol(st.lastVol || 1); else setVol(0); }
    function syncMute(){ var m = video.muted || video.volume === 0; $icV.hidden = m; $icM.hidden = !m; }
    $vol.addEventListener('input', function(){ setVol(parseFloat($vol.value) || 0); });
    $vol.addEventListener('wheel', function(e){
      e.preventDefault();
      setVol(video.volume + (e.deltaY > 0 ? -0.05 : 0.05));
    }, { passive: false });
    $muteBtn.addEventListener('click', toggleMute);

    function fsApi(){
      var d = document;
      // requestFullscreen 挂在 Element 上，document.requestFullscreen 并不存在
      // （document.exitFullscreen 才是 Document 的方法）。
      // 这里从一个真实元素探测，再 call 到目标元素，否则 req 会全是 undefined。
      var e = d.documentElement;
      return {
        req: e.requestFullscreen || e.webkitRequestFullscreen || e.mozRequestFullScreen || e.msRequestFullscreen,
        ex:  d.exitFullscreen    || d.webkitExitFullscreen    || d.mozCancelFullScreen || d.msExitFullscreen,
        el:  function(){ return document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement; },
        evts: ['fullscreenchange','webkitfullscreenchange','mozfullscreenchange','MSFullscreenChange']
      };
    }
    function isFS(){ return !!fsApi().el(); }
    function reqFS(el){ var a = fsApi().req; return a && a.call(el); }
    function exFS(){   var a = fsApi().ex;  return a && a.call(document); }
    function togFS(){
      try {
        var p = isFS() ? exFS() : reqFS(root);
        if(p && p.catch){
          p.catch(function(e){
            var box = $('[data-error]', root), txt = $('[data-error-msg]', root);
            if(txt) txt.textContent = '无法进入全屏：' + ((e && e.message) || e);
            if(box){ box.hidden = false; setTimeout(function(){ box.hidden = true; }, 4000); }
          });
        }
      } catch(e){
        console.warn('[player] 全屏失败', e);
      }
    }
    fsApi().evts.forEach(function(ev){
      document.addEventListener(ev, function(){
        var on = isFS();
        root.dataset.fullscreen = on ? 'true' : 'false';
        $icFsE.hidden = on;
        $icFsX.hidden = !on;
      });
    });
    $fsBtn.addEventListener('click', togFS);

    function setSpeed(v){
      video.playbackRate = v;
      if($spL) $spL.textContent = (Math.round(v*100)/100) + 'x';
      $$('[data-menu="speed"] [data-speed]', root).forEach(function(b){
        b.setAttribute('aria-selected', parseFloat(b.dataset.speed) === v ? 'true' : 'false');
      });
    }
    function setQ(i){
      var t = st.srcs[i];
      if(!t || i === st.qi) return;
      var tm = video.currentTime, was = video.paused;
      st.qi = i;
      $$('source', video).forEach(function(s){ s.parentNode.removeChild(s); });
      var ns = document.createElement('source');
      ns.src = t.src;
      if(t.type) ns.type = t.type;
      ns.setAttribute('data-label', t.label);
      video.appendChild(ns);
      video.load();
      var onMeta = function(){
        try { video.currentTime = Math.min(tm, video.duration || tm); } catch(_){}
        if(!was) play();
        video.removeEventListener('loadedmetadata', onMeta);
      };
      video.addEventListener('loadedmetadata', onMeta);
      var $qL = $('[data-quality-label]', root);
      if($qL) $qL.textContent = t.label;
      $$('[data-menu="quality"] [data-quality]', root).forEach(function(b){
        b.setAttribute('aria-selected', parseInt(b.dataset.quality, 10) === i ? 'true' : 'false');
      });
    }

    $$('[data-menu]', root).forEach(function(m){
      var tg = $('[data-menu-toggle]', m);
      var ls = $('.menu__list', m);
      if(!tg || !ls) return;
      tg.addEventListener('click', function(e){
        e.stopPropagation();
        $$('[data-menu]', root).forEach(function(o){
          if(o !== m){
            $('.menu__list', o).hidden = true;
            $('[data-menu-toggle]', o).setAttribute('aria-expanded', 'false');
          }
        });
        ls.hidden = !ls.hidden;
        tg.setAttribute('aria-expanded', ls.hidden ? 'false' : 'true');
      });
      ls.addEventListener('click', function(e){
        var b = e.target.closest('button[data-speed], button[data-quality]');
        if(!b) return;
        if(b.dataset.speed) setSpeed(parseFloat(b.dataset.speed));
        else if(b.dataset.quality !== undefined) setQ(parseInt(b.dataset.quality, 10));
        ls.hidden = true;
        tg.setAttribute('aria-expanded', 'false');
      });
    });
    document.addEventListener('click', function(){
      $$('[data-menu] .menu__list', root).forEach(function(l){ l.hidden = true; });
      $$('[data-menu] [data-menu-toggle]', root).forEach(function(b){
        b.setAttribute('aria-expanded', 'false');
      });
    });

    root.tabIndex = 0;
    root.addEventListener('keydown', function(e){
      var tag = (e.target && e.target.tagName) || '';
      if(/INPUT|TEXTAREA|SELECT/.test(tag)) return;
      switch(e.key){
        case ' ': case 'Spacebar': e.preventDefault(); toggle(); break;
        case 'ArrowRight': e.preventDefault(); video.currentTime = clamp(video.currentTime + 5, 0, st.dur || 1e9); break;
        case 'ArrowLeft':  e.preventDefault(); video.currentTime = clamp(video.currentTime - 5, 0, st.dur || 1e9); break;
        case 'ArrowUp':    e.preventDefault(); setVol(clamp(video.volume + 0.05, 0, 1)); break;
        case 'ArrowDown':  e.preventDefault(); setVol(clamp(video.volume - 0.05, 0, 1)); break;
        case 'l': case 'L': e.preventDefault(); video.currentTime = clamp(video.currentTime + 10, 0, st.dur || 1e9); break;
        case 'j': case 'J': e.preventDefault(); video.currentTime = clamp(video.currentTime - 10, 0, st.dur || 1e9); break;
        case 'm': case 'M': e.preventDefault(); toggleMute(); break;
        case 'f': case 'F': e.preventDefault(); togFS(); break;
        case 'k': case 'K': e.preventDefault(); toggle(); break;
        case '0': case '1': case '2': case '3': case '4':
        case '5': case '6': case '7': case '8': case '9':
          if(st.dur) video.currentTime = st.dur * (parseInt(e.key, 10) / 10);
          break;
      }
    });

    function showCur(){
      root.dataset.cursor = 'visible';
      root.style.cursor = '';
      clearTimeout(st.cursor);
      if(st.playing) st.cursor = setTimeout(function(){
        root.dataset.cursor = 'hidden';
        root.style.cursor = 'none';
      }, 2500);
    }
    root.addEventListener('mousemove', showCur);
    root.addEventListener('mouseleave', function(){
      root.dataset.cursor = 'hidden';
      root.style.cursor = 'none';
    });
    root.addEventListener('mouseenter', showCur);

    var hidTm = null;
    function ldOn(){
      if(hidTm) clearTimeout(hidTm);
      root.dataset.buffering = 'true';
      $ld.hidden = false;
    }
    function ldOff(){
      if(hidTm) clearTimeout(hidTm);
      hidTm = setTimeout(function(){
        root.dataset.buffering = 'false';
        $ld.hidden = true;
      }, 200);
    }
    function err(msg){
      $erMsg.textContent = msg || '无法播放此视频';
      $er.hidden = false;
      ldOff();
    }
    video.addEventListener('loadstart', ldOn);
    video.addEventListener('waiting',   ldOn);
    video.addEventListener('stalled',   ldOn);
    video.addEventListener('canplay',   ldOff);
    video.addEventListener('playing',   ldOff);
    video.addEventListener('seeked',    ldOff);
    video.addEventListener('play',  function(){ st.playing = true;  syncIcon(); showCur(); });
    video.addEventListener('pause', function(){ st.playing = false; syncIcon(); showCur(); });
    video.addEventListener('loadedmetadata', function(){ updDur(); updateProg(); $er.hidden = true; });
    video.addEventListener('durationchange', updDur);
    video.addEventListener('timeupdate', updateProg);
    video.addEventListener('progress',   updateProg);
    video.addEventListener('ratechange', function(){
      if($spL) $spL.textContent = (Math.round(video.playbackRate*100)/100) + 'x';
    });
    video.addEventListener('volumechange', function(){
      $vol.value = String(video.muted ? 0 : video.volume);
      syncMute();
    });
    video.addEventListener('ended', function(){
      st.playing = false; syncIcon(); showCur();
    });
    video.addEventListener('error', function(){
      var e = video.error, map = {1:'加载被中止', 2:'网络错误', 3:'解码错误', 4:'视频源不可用或浏览器不支持'};
      err((map[e && e.code]) || '视频加载失败');
    });
    if($retry) $retry.addEventListener('click', function(){ $er.hidden = true; video.load(); play(); });

    $playBtn.addEventListener('click', toggle);
    $cp.addEventListener('click', toggle);
    var $fwd = $('[data-action="forward"]', root);
    var $back = $('[data-action="back"]', root);
    if($fwd) $fwd.addEventListener('click', function(){ video.currentTime = clamp(video.currentTime + 10, 0, st.dur || 1e9); });
    if($back) $back.addEventListener('click', function(){ video.currentTime = clamp(video.currentTime - 10, 0, st.dur || 1e9); });
    video.addEventListener('click', toggle);
    video.addEventListener('dblclick', togFS);

    setSpeed(1);
    syncIcon();
    syncMute();
    setVol(video.volume || 1);
    showCur();
  }

  function boot(){
    $$('[data-player]').forEach(function(el){
      try { createPlayer(el); } catch(e){ console.error(e); }
    });
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
  global.Player = { create: createPlayer };
})(window);
"""


# ============================================================================
# 渲染：主页 / 观看页
# ============================================================================
def render_index_html(items: list) -> str:
    if items:
        cards = "\n".join(_render_card(v) for v in items)
        hero_p = f"当前共 <strong>{len(items)}</strong> 个视频，选择一张海报开始观看。"
    else:
        cards = (
            '<div class="empty">'
            "<p>暂无视频。你可以：</p>"
            "<p>1. 把 <code>.mp4 / .webm / .m3u8</code> 放进 <code>videos/</code> 目录；</p>"
            "<p>2. 编辑 <code>videos/_catalog.json</code> 自定义元数据；</p>"
            "<p>3. 或在 <code>main.py</code> 的 <code>REMOTE_SOURCES</code> 里填远程直链。</p>"
            "</div>"
        )
        hero_p = "当前共 <strong>0</strong> 个视频。"

    return (INDEX_HTML
            .replace("__SITE_CSS__", SITE_CSS)
            .replace("__CARDS__", cards)
            .replace("__HERO_P__", hero_p))


def _render_card(v: dict) -> str:
    poster = v.get("poster") or PLACEHOLDER_URI
    return (
        '<a class="card" href="/watch/{id}">'
        '<div class="thumb" style="background-image:url(\'{poster}\')">'
        '<span class="play">&#9654;</span></div>'
        '<div class="meta"><h3>{title}</h3>'
        '<p>{desc}</p></div></a>'
    ).format(
        id=html_escape(v["id"]),
        title=html_escape(v["title"]),
        desc=html_escape(v.get("description") or ""),
        poster=html_escape(poster),
    )


def build_watch_html(video: dict, related: list) -> str:
    sources = video.get("sources") or []
    sources_html = "".join(
        f'<source src="{html_escape(s["src"])}" type="{html_escape(s["type"])}" '
        f'data-label="{html_escape(s["label"])}"/>'
        for s in sources
    )

    speed_items = []
    for s in SPEED_PRESETS:
        sel = ' aria-selected="true"' if abs(s - 1.0) < 1e-9 else ""
        speed_items.append(f'<li><button type="button" data-speed="{s}"{sel}>{s}x</button></li>')
    speed_list = "".join(speed_items)

    # 清晰度
    if len(sources) <= 1:
        qlist_html = ""
        qlabel = sources[0]["label"] if sources else "默认"
        single_src = True
    else:
        qitems = []
        for i, s in enumerate(sources):
            sel = ' aria-selected="true"' if i == 0 else ""
            qitems.append(
                f'<li><button type="button" data-quality="{i}" '
                f'data-src="{html_escape(s["src"])}" '
                f'data-label="{html_escape(s["label"])}"{sel}>'
                f'{html_escape(s["label"])}</button></li>'
            )
        qlist_html = "".join(qitems)
        qlabel = sources[0]["label"]
        single_src = False

    poster = video.get("poster") or ""
    poster_attr = f' poster="{html_escape(poster)}"' if poster else ""

    # 相关
    rel_items = []
    for r in related:
        rp = r.get("poster") or PLACEHOLDER_URI
        rel_items.append(
            f'<li><a href="/watch/{html_escape(r["id"])}">'
            f'<div class="thumb thumb--sm" style="background-image:url(\'{html_escape(rp)}\')"></div>'
            f"<div><strong>{html_escape(r['title'])}</strong>"
            f"<small>{html_escape(r.get('description') or '')}</small></div></a></li>"
        )
    if not rel_items:
        rel_items = ['<li class="empty">没有更多推荐</li>']
    rel_html = "\n".join(rel_items)

    title = html_escape(video["title"])
    desc = html_escape(video.get("description") or "")

    body = (
        WATCH_BODY
        .replace("__TITLE__", title)
        .replace("__DESC__", desc)
        .replace("__POSTER__", poster_attr)
        .replace("__SOURCES__", sources_html)
        .replace("__SPEED_LIST__", speed_list)
        .replace("__QUALITY_LIST__", qlist_html)
        .replace("__QLABEL__", html_escape(qlabel))
        .replace("__RELATED__", rel_html)
    )

    # 单清晰度时禁用菜单
    if single_src:
        body = re.sub(
            r'<div class="menu" data-menu="quality">.*?</div>',
            DISABLED_Q_BUTTON,
            body,
            count=1,
            flags=re.DOTALL,
        )

    head = (
        WATCH_HEAD
        .replace("__TITLE__", title)
        .replace("__SITE_CSS__", SITE_CSS)
        .replace("__PLAYER_CSS__", PLAYER_CSS)
    )

    # ⚠ 拼接 script 时绝不能用 f-string（PLAYER_JS 内含大量 {...}）
    config_json = json.dumps(video, ensure_ascii=False)
    script_tag = (
        "<script>window.__PLAYER_VIDEO__=" + config_json + ";</script>\n"
        + "<script>" + PLAYER_JS + "</script>"
    )

    return head + body.replace("__PLAYER_SCRIPT__", script_tag)


# 单清晰度时显示的禁用按钮
DISABLED_Q_BUTTON = (
    '<button class="btn btn--icon" type="button" data-menu="quality" '
    'data-menu-toggle disabled style="opacity:.5;cursor:not-allowed" '
    'title="当前视频只有一个清晰度">'
    '<svg viewBox="0 0 24 24">'
    '<path d="M19 5v14H5V5h14m0-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z"/>'
    '<path d="M7 9h10v2H7zm0 4h7v2H7z"/></svg>'
    "</button>"
)


# ============================================================================
# HTML 模板（用 __KEY__ 占位符 + str.replace 替换，避免 f-string 解析 JS 问题）
# ============================================================================
INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>免费视频观看 - FreeView</title>
<style>
__SITE_CSS__
</style>
</head>
<body>
<header class="site-header">
  <a class="logo" href="/">&#9654; FreeView</a>
</header>

<main class="site-main">
  <section class="hero">
    <h1>随便看，都是免费的好视频</h1>
    <p>__HERO_P__</p>
  </section>
  <section class="grid">
    __CARDS__
  </section>
</main>

<footer class="site-footer">&copy; FreeView &middot; 一个轻量的 Python 视频网站</footer>
</body>
</html>"""


WATCH_HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__ &middot; FreeView</title>
<style>
__SITE_CSS__
__PLAYER_CSS__
</style>
</head>"""


WATCH_BODY = """<body>
<header class="site-header">
  <a class="logo" href="/">&#9654; FreeView</a>
</header>

<main class="site-main">
<section class="watch-layout">
  <div class="watch-main">

    <div id="player" class="player" data-player>
      <video class="player__video" playsinline preload="metadata"__POSTER__>
        __SOURCES__
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
          <strong>视频加载失败</strong>
          <p data-error-msg>请检查网络或稍后重试。</p>
          <button type="button" class="btn" data-action="retry">重试</button>
        </div>
      </div>

      <div class="player__overlay-top" data-overlay-top>
        <span class="player__title">__TITLE__</span>
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

          <div class="menu" data-menu="speed">
            <button class="btn" type="button" data-menu-toggle>速度 <span data-speed-label>1.0x</span></button>
            <ul class="menu__list" role="menu" hidden>__SPEED_LIST__</ul>
          </div>

          <div class="menu" data-menu="quality">
            <button class="btn" type="button" data-menu-toggle>画质 <span data-quality-label>__QLABEL__</span></button>
            <ul class="menu__list" role="menu" hidden>__QUALITY_LIST__</ul>
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
          <li><kbd>Space</kbd> 播放 / 暂停</li>
          <li><kbd>&#8592;</kbd> / <kbd>&#8594;</kbd> 快退 / 快进 5 秒</li>
          <li><kbd>J</kbd> / <kbd>L</kbd> 快退 / 快进 10 秒</li>
          <li><kbd>&#8593;</kbd> / <kbd>&#8595;</kbd> 音量 + / -</li>
          <li><kbd>M</kbd> 静音切换</li>
          <li><kbd>F</kbd> 全屏切换</li>
          <li><kbd>0</kbd>&ndash;<kbd>9</kbd> 跳转到 0% &ndash; 90%</li>
        </ul>
      </details>
    </section>

  </div>

  <aside class="watch-side">
    <h3>相关视频</h3>
    <ul class="related">__RELATED__</ul>
  </aside>
</section>
</main>

<footer class="site-footer">&copy; FreeView &middot; 一个轻量的 Python 视频网站</footer>
__PLAYER_SCRIPT__
</body>
</html>"""


# ============================================================================
# HTTP 服务
# ============================================================================
class FreeViewHandler(BaseHTTPRequestHandler):
    server_version = "FreeView/1.0 (Python)"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ---------
    def _send(self, status: int, body, content_type="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _stream_file(self, target: Path, mime_type: str):
        size = target.stat().st_size
        range_header = self.headers.get("Range", "")
        try:
            if range_header:
                m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if not m:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                length = end - start + 1

                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                with target.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        data = f.read(min(chunk_size, remaining))
                        if not data:
                            break
                        try:
                            self.wfile.write(data)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        remaining -= len(data)
            else:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with target.open("rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return
        except BrokenPipeError:
            pass

    # ---------
    def do_GET(self):
        try:
            self._handle()
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, f"Server Error: {e}", "text/plain; charset=utf-8")
            except Exception:
                pass

    def _handle(self):
        raw = self.path.split("?", 1)[0]
        path = unquote(raw)

        # JSON API
        if path == "/api/videos":
            return self._send_json(load_catalog())

        m = re.match(r"^/api/videos/([\w\-.]+)$", path)
        if m:
            items = load_catalog()
            v = next((x for x in items if x["id"] == m.group(1)), None)
            if not v:
                return self._send(404, "Video not found", "text/plain; charset=utf-8")
            return self._send_json(v)

        # 视频文件流
        if path.startswith("/videos/"):
            try:
                target = safe_video_path(path[len("/videos/"):])
            except ValueError:
                return self._send(400, "Bad path", "text/plain; charset=utf-8")
            if not target.is_file():
                return self._send(404, "Video not found", "text/plain; charset=utf-8")
            return self._stream_file(target, guess_mime(target.suffix))

        # 健康检查
        if path in ("/healthz", "/health"):
            return self._send(200, "ok", "text/plain; charset=utf-8")

        # 首页
        if path in ("/", "/index.html"):
            return self._send(200, render_index_html(load_catalog()))

        # 观看页
        m = re.match(r"^/watch/([\w\-.]+)$", path)
        if m:
            items = load_catalog()
            v = next((x for x in items if x["id"] == m.group(1)), None)
            if not v:
                return self._send(
                    404,
                    "<h1>404 &middot; 找不到视频</h1>"
                    '<p><a href="/">返回首页</a></p>',
                    "text/html; charset=utf-8",
                )
            related = [x for x in items if x["id"] != v["id"]][:8]
            return self._send(200, build_watch_html(v, related))

        return self._send(
            404,
            f"<h1>404</h1><p>{html_escape(path)} not found. "
            "<a href='/'>返回首页</a></p>",
            "text/html; charset=utf-8",
        )


# ============================================================================
# 启动
# ============================================================================
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), FreeViewHandler)
    local_ip = get_local_ip()
    print("=" * 64)
    print("[FreeView] Python 标准库单文件视频网站已启动")
    print(f"  本机访问:   http://127.0.0.1:{port}/")
    print(f"  局域网:     http://{local_ip}:{port}/")
    print(f"  视频目录:   {VIDEO_DIR}")
    print(f"  元数据:     {CATALOG_FILE}  (可选)")
    print(f"  远程源:     REMOTE_SOURCES 共 {len(REMOTE_SOURCES)} 条")
    print(f"  REST API:   GET /api/videos")
    print("  退出:       Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[FreeView] 关闭...")
        server.shutdown()


if __name__ == "__main__":
    main()
