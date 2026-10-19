#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
非凡资源网(ffzy) 三步抓取：搜索 → 详情 → 解析真实 m3u8
======================================================
第 1 步（搜索）  https://cj.ffzyapi.com/index.php/vod/search.html?wd=关键词
第 2 步（详情）  https://cj.ffzyapi.com/index.php/vod/detail/id/28356.html
第 3 步（播放）  https://vip.ffzy-play9.com/share/523d6984fa5aa355c0f4b63b564ce892
                ↑ 该页只是个壳，真正的地址写在页面内的 JS 里：
                  const url = "/20230104/32917_009575e7/index.m3u8?sign=..."

用法：
    python 1.py 凡人修仙传              # 搜索，并解析第 1 集的 m3u8
    python 1.py 凡人修仙传 --limit 5    # 解析前 5 集
    python 1.py 凡人修仙传 --all        # 解析全部集数
    python 1.py 凡人修仙传 --pick 2     # 搜索结果里选第 2 条（从 1 开始）
    python 1.py 凡人修仙传 --out data.json

依赖：pip install requests
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from urllib.parse import quote, urljoin

import requests

# --------------------------------------------------------------------------
# 常量与请求会话
# --------------------------------------------------------------------------
BASE = "https://cj.ffzyapi.com"
SEARCH_URL = BASE + "/index.php/vod/search.html"
DETAIL_URL = BASE + "/index.php/vod/detail/id/{vid}.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE + "/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)

# 控制台在 Windows 下可能是 GBK，强制 UTF-8 以免 print 中文报错
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fetch(url: str) -> str | None:
    """请求一个页面，返回 HTML 文本；失败返回 None。"""
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except requests.RequestException as e:
        print(f"[warn] 请求失败 {url} -> {e}", file=sys.stderr)
        return None


def strip_tags(s: str) -> str:
    """去掉 HTML 标签并压缩空白。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


# ==========================================================================
# 第 1 步：搜索
# ==========================================================================
# 搜索结果的 HTML（一个 <li> 一条）：
#   <ul class="videoContent">
#     <li><a class="videoName" target="_blank"
#            href="/index.php/vod/detail/id/28356.html">凡人修仙传<i>更新至第195集</i></a>
#         <span class="region">大陆</span>
#         <span class="category type">国产动漫</span>
#         <a href="/index.php/vod/detail/id/28356.html" class="address">点击进入</a>
#         <span class="time1">2026-10-10</span></li>
#   </ul>
RE_LIST_BLOCK = re.compile(r'<ul class="videoContent">(.*?)</ul>', re.S)
RE_LIST_ITEM = re.compile(r"<li>(.*?)</li>", re.S)
RE_ITEM_HREF = re.compile(r'class="videoName"[^>]*href="([^"]+)"', re.S)
RE_ITEM_TITLE = re.compile(r'class="videoName"[^>]*>(.*?)</a>', re.S)
RE_ITEM_STATUS = re.compile(r"<i>(.*?)</i>", re.S)
RE_ITEM_REGION = re.compile(r'class="region"[^>]*>(.*?)</span>', re.S)
RE_ITEM_CATEGORY = re.compile(r'class="category[^"]*"[^>]*>(.*?)</span>', re.S)
RE_ITEM_TIME = re.compile(r'class="time1"[^>]*>(.*?)</span>', re.S)
RE_VID = re.compile(r"/id/(\d+)\.html")


def search_video(keyword: str, page: int = 1) -> list[dict]:
    """第 1 步：按关键词搜索，返回 [{vid, title, status, region, category, update_time, url}]。"""
    if page <= 1:
        url = f"{SEARCH_URL}?wd={quote(keyword)}"
    else:
        url = f"{BASE}/index.php/vod/search/page/{page}/wd/{quote(keyword)}.html"

    html = fetch(url)
    if not html:
        return []

    block = RE_LIST_BLOCK.search(html)
    if not block:
        return []

    results: list[dict] = []
    for raw in RE_LIST_ITEM.findall(block.group(1)):
        href_m = RE_ITEM_HREF.search(raw)
        if not href_m:
            continue
        href = href_m.group(1)
        vid_m = RE_VID.search(href)
        if not vid_m:
            continue

        def pick(pattern: re.Pattern, text: str = raw) -> str:
            m = pattern.search(text)
            return strip_tags(m.group(1)) if m else ""

        title_html = RE_ITEM_TITLE.search(raw)
        title_html = title_html.group(1) if title_html else ""
        status = pick(RE_ITEM_STATUS, title_html)
        title = strip_tags(title_html)
        if status and title.endswith(status):
            title = title[: -len(status)].strip()

        results.append(
            {
                "vid": vid_m.group(1),
                "title": title,
                "status": status,
                "region": pick(RE_ITEM_REGION),
                "category": pick(RE_ITEM_CATEGORY),
                "update_time": pick(RE_ITEM_TIME),
                "url": urljoin(BASE, href),
            }
        )
    return results


# ==========================================================================
# 第 2 步：详情页 —— 拿到每一集的「播放页」地址
# ==========================================================================
# 详情页里每条剧集（注意 URL 里是「集名$地址」）：
#   <div class="playlist wbox feifan">
#     <li><input name="copy_feifan[]" type="checkbox"
#                value="第01集$https://vip.ffzy-play9.com/share/523d6984...">
#         <a title="第01集" href="https://vip.ffzy-play9.com/share/523d6984...">
RE_EPISODE = re.compile(
    r'<input[^>]*name="copy_(?P<source>[^"\[]*)\[\]"[^>]*value="(?P<value>[^"]+)"',
    re.S,
)
# 详情页没有可用的 <h1>（那个 h1 是内容无关的「资源详情」），片名只在 <title> 里：
#   <title>凡人修仙传详情介绍-凡人修仙传在线观看-凡人修仙传高清资源 - 非凡资源网</title>
RE_DETAIL_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
RE_TITLE_TAIL = re.compile(r"(详情介绍|在线观看|高清资源|免费观看|剧情介绍|在线播放).*$")
# 详情页的封面海报；站标 logo 在 /template/ 下，用 /upload/ 过滤掉
RE_DETAIL_POSTER = re.compile(
    r'<img[^>]+src="(https?://[^"]+/upload/[^"]+\.(?:jpg|jpeg|png|webp))"', re.I
)


def get_video_detail(vid: str) -> dict | None:
    """第 2 步：进详情页，返回 {vid, title, episodes:[{source, name, share_url}]}。"""
    url = DETAIL_URL.format(vid=vid)
    html = fetch(url)
    if not html:
        return None

    title = ""
    t = RE_DETAIL_TITLE.search(html)
    if t:
        head = re.split(r"\s*[-\u2013|_]\s*", strip_tags(t.group(1)))[0]
        title = RE_TITLE_TAIL.sub("", head).strip(" -·|")

    pm = RE_DETAIL_POSTER.search(html)
    poster = pm.group(1) if pm else ""

    episodes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in RE_EPISODE.finditer(html):
        value = m.group("value")
        # value 形如：第01集$https://vip.ffzy-play9.com/share/xxx
        name, sep, rest = value.partition("$")
        if not sep or not rest:
            continue
        share_url = rest.split("$")[0].strip()  # 少数条目是多地址拼接，取第一个
        if not share_url.startswith("http"):
            continue
        key = (m.group("source"), name)
        if key in seen:
            continue
        seen.add(key)
        episodes.append(
            {"source": m.group("source"), "name": name.strip(), "share_url": share_url}
        )

    return {
        "vid": vid,
        "title": title,
        "poster": poster,
        "detail_url": url,
        "episodes": episodes,
    }


# ==========================================================================
# 第 3 步：播放页 —— 解出真正的 m3u8
# ==========================================================================
# 播放页 https://vip.ffzy-play9.com/share/523d6984fa5aa355c0f4b63b564ce892 内容极少，
# 关键就这几行：
#   const vid  = "523d6984fa5aa355c0f4b63b564ce892";
#   const url  = "/20230104/32917_009575e7/index.m3u8?sign=8999be7e...";
#   const pic  = "/20230104/32917_009575e7/1.jpg";
RE_PLAY_URL = re.compile(r"""const\s+url\s*=\s*["']([^"']+\.m3u8[^"']*)["']""", re.I)
RE_PLAY_PIC = re.compile(r"""const\s+pic\s*=\s*["']([^"']+)["']""", re.I)


def resolve_play_url(share_url: str) -> dict | None:
    """第 3 步：进 share 页，解出 m3u8 直链。返回 {m3u8, poster} 或 None。"""
    # ffm3u8 这类源直接给 m3u8 直链，不用再抓一层页面
    if ".m3u8" in share_url.lower():
        return {"m3u8": share_url, "poster": ""}

    html = fetch(share_url)
    if not html:
        return None

    m = RE_PLAY_URL.search(html)
    if not m:
        print(f"[warn] {share_url} 里没找到 m3u8（页面结构可能变了）", file=sys.stderr)
        return None

    pic = RE_PLAY_PIC.search(html)
    return {
        "m3u8": urljoin(share_url, m.group(1)),          # 把 "/20230104/..." 补成绝对地址
        "poster": urljoin(share_url, pic.group(1)) if pic else "",
    }


# ==========================================================================
# 采集 JSON 接口：比 HTML 页面快一个数量级，优先走这里
# ==========================================================================
# 实测同一关键词：HTML 搜索页 4.61s / ac=list 0.23s / ac=detail 0.48s
# 而且 ac=detail 一次就带回海报(vod_pic)和全部剧集地址(vod_play_url)，
# 省掉了「搜索页 + 详情页」两次 HTML 抓取。
API_URL = BASE + "/api.php/provide/vod/"
API_SEP_SOURCE = "$$$"   # 多播放源之间的分隔
API_SEP_EP = "#"         # 同一播放源内多集的分隔
API_SEP_URL = "$"        # 集名与地址之间的分隔


def _api_get(params: dict) -> dict | None:
    """请求采集接口，失败返回 None（由调用方回退 HTML）。"""
    try:
        r = session.get(API_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[warn] JSON 接口失败 {params}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or str(data.get("code")) != "1":
        return None
    return data


def _parse_play_url(raw, play_from) -> list[dict]:
    """拆 vod_play_url：[{source, name, share_url}]。

    格式形如 「第01集$https://...#第02集$https://...」，
    多播放源时源之间用 $$$ 分隔（源名在 vod_play_from 里同样用 $$$ 分隔）。
    """
    out: list[dict] = []
    sources = [s for s in str(play_from or "").split(API_SEP_SOURCE) if s]
    for si, block in enumerate(str(raw or "").split(API_SEP_SOURCE)):
        src = sources[si] if si < len(sources) else f"source{si + 1}"
        for item in block.split(API_SEP_EP):
            item = item.strip()
            if not item:
                continue
            name, sep, url = item.partition(API_SEP_URL)
            if not sep:
                name, url = "", item
            url = url.strip()
            if not url.startswith("http"):
                continue
            out.append({
                "source": src,
                "name": name.strip() or f"第{len(out) + 1}集",
                "share_url": url,
            })
    return out


def _api_item_to_detail(it: dict) -> dict:
    return {
        "vid": str(it.get("vod_id") or ""),
        "title": str(it.get("vod_name") or "").strip(),
        "poster": str(it.get("vod_pic") or "").strip(),
        "status": str(it.get("vod_remarks") or "").strip(),
        "region": str(it.get("vod_area") or "").strip(),
        "category": str(it.get("type_name") or "").strip(),
        "update_time": str(it.get("vod_time") or "").strip()[:10],
        "episodes": _parse_play_url(it.get("vod_play_url"), it.get("vod_play_from")),
    }


def search_api(keyword: str, page: int = 1) -> list[dict] | None:
    """一次请求拿到「搜索结果 + 海报 + 全部剧集地址」。接口不可用时返回 None。"""
    data = _api_get({"ac": "detail", "wd": keyword, "pg": page})
    if data is None:
        return None
    return [
        _api_item_to_detail(it)
        for it in (data.get("list") or [])
        if it.get("vod_id")
    ]


def detail_api(vid: str) -> dict | None:
    """按 id 取单片详情（同样带海报与全部剧集地址）。"""
    data = _api_get({"ac": "detail", "ids": vid})
    if data is None:
        return None
    items = data.get("list") or []
    return _api_item_to_detail(items[0]) if items else None


# ==========================================================================
# 串联三步 + 命令行
# ==========================================================================
def crawl(
    keyword: str, pick: int = 1, limit: int = 1, page: int = 1, source: str = ""
) -> dict | None:
    """搜索 → 详情 → 逐集解析 m3u8。limit<=0 表示全部集数。"""
    print(f"\n[1/3] 搜索：{keyword}")
    hits = search_api(keyword, page=page)       # 采集接口快得多
    if hits is None:                            # 接口不可用 → 回退 HTML 搜索页
        hits = search_video(keyword, page=page)
    if not hits:
        print("      × 没有搜索结果")
        return None
    for i, h in enumerate(hits, 1):
        mark = "→" if i == pick else " "
        print(f"   {mark} {i}. {h['title']}（{h.get('status', '')} · {h.get('region', '')} · "
              f"{h.get('category', '')} · {h.get('update_time', '')}） id={h['vid']}")

    if not 1 <= pick <= len(hits):
        print(f"      × --pick 超出范围（共 {len(hits)} 条）")
        return None
    hit = hits[pick - 1]

    detail_url = hit.get("url") or DETAIL_URL.format(vid=hit["vid"])
    print(f"\n[2/3] 详情：{detail_url}")
    if hit.get("episodes"):                     # 采集接口已带回全部剧集，直接复用
        detail = {**hit, "detail_url": detail_url}
    else:
        detail = get_video_detail(hit["vid"])
    if not detail or not detail["episodes"]:
        print("      × 没解析到剧集列表（页面结构可能变了）")
        return None
    eps = detail["episodes"]
    sources = list(dict.fromkeys(e["source"] for e in eps))
    if len(sources) > 1:
        print(f"      播放源：{'、'.join(sources)}")
        if source:
            eps = [e for e in eps if e["source"] == source]
        else:
            # 同一部剧的多条线路，集数完全重复；默认只用第一个源
            eps = [e for e in eps if e["source"] == sources[0]]
        print(f"      使用源「{source or sources[0]}」，{len(eps)} 集")
    if not eps:
        print("      × 该播放源下没有剧集")
        return None

    title = hit["title"] or detail["title"]
    print(f"      {title}：共 {len(eps)} 集，前几集："
          f"{', '.join(e['name'] for e in eps[:6])}")

    targets = eps if limit <= 0 else eps[:limit]
    print(f"\n[3/3] 解析 m3u8（{len(targets)}/{len(eps)} 集）")
    resolved = []
    for i, ep in enumerate(targets, 1):
        got = resolve_play_url(ep["share_url"])
        if got:
            print(f"   [{i}/{len(targets)}] {ep['name']}  {got['m3u8']}")
            resolved.append({**ep, **got})
        else:
            print(f"   [{i}/{len(targets)}] {ep['name']}  解析失败")
        if i < len(targets):
            time.sleep(0.3)  # 轻微节流，别把人家服务器打爆

    return {
        "keyword": keyword,
        "vid": detail["vid"],
        "title": title,
        "status": hit["status"],
        "region": hit["region"],
        "category": hit["category"],
        "poster": resolved[0]["poster"] if resolved else "",
        "detail_url": detail["detail_url"],
        "episodes": resolved,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="非凡资源网 搜索→详情→m3u8 三步抓取")
    ap.add_argument(
        "keyword",
        nargs="?",
        default="",
        help="搜索关键词，如：凡人修仙传（省略则运行时交互输入）",
    )
    ap.add_argument("--pick", type=int, default=1, help="选第几条搜索结果（从 1 开始，默认 1）")
    ap.add_argument("--limit", type=int, default=1, help="解析前 N 集（默认 1）")
    ap.add_argument("--all", action="store_true", help="解析全部集数")
    ap.add_argument("--page", type=int, default=1, help="搜索结果翻页")
    ap.add_argument("--source", default="", help="指定播放源，如 feifan / ffm3u8（默认第一个）")
    ap.add_argument("--out", default="", help="把结果保存为 JSON 文件")
    args = ap.parse_args()

    keyword = args.keyword.strip()
    if not keyword:
        try:
            keyword = input("请输入要搜索的片名：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        # stdin 被重定向时（如 echo xxx | python 1.py），编码可能对不上而读入
        # 代理字符 \udcXX，会让 quote() 直接抛异常；这里丢弃它们
        keyword = keyword.encode("utf-8", "ignore").decode("utf-8", "ignore").strip()
    if not keyword:
        ap.error("必须提供搜索关键词，例如：python 1.py 凡人修仙传")

    data = crawl(
        keyword,
        pick=args.pick,
        limit=0 if args.all else max(1, args.limit),
        page=args.page,
        source=args.source,
    )
    if not data:
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n已保存：{args.out}")
    print(
        "\n提示：m3u8 链接带 sign 签名，需要 Referer 才能播放，"
        "且可能有时效，重新跑一次即可刷新。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
