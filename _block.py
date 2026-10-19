import importlib.util
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fv_app", BASE / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

detail = app.get_detail("28356")
eps, _, chosen = app.pick_episodes(detail)
play = app.get_play(eps[0]["share_url"])
master = play["m3u8"]
H = {"User-Agent": UA, "Referer": "https://vip.ffzy-play9.com/"}

r = requests.get(master, headers=H, timeout=20)
txt = r.text
sub_url = master
if "#EXT-X-STREAM-INF" in txt:
    subs = [ln.strip() for ln in txt.splitlines() if ln.strip() and not ln.startswith("#")]
    sub_url = urljoin(master, subs[0])
    txt = requests.get(sub_url, headers=H, timeout=20).text

segs = app._parse_segments(txt)
blocks, cur = [], []
for s in segs:
    if s["disc"] and cur:
        blocks.append(cur)
        cur = []
    cur.append(s)
if cur:
    blocks.append(cur)

print(f"{eps[0]['name']}: 共 {len(segs)} 片 / {len(blocks)} 段")
for i, b in enumerate(blocks):
    print(f"  段{i + 1}: {len(b):3d} 片, {sum(x['dur'] for x in b):7.1f}s")


def kb(seg):
    u = urljoin(sub_url, seg["uri"])
    try:
        return len(requests.get(u, headers=H, timeout=30).content) / 1024
    except Exception as e:
        return -1


print("\n--- 段1 最后 3 片（正片参照）---")
for s in blocks[0][-3:]:
    print(f"  {s['dur']:5.2f}s  {kb(s):7.0f} KB")

if len(blocks) > 1:
    print("\n--- 段2 全部（疑似广告）---")
    for s in blocks[1]:
        print(f"  {s['dur']:5.2f}s  {kb(s):7.0f} KB")

if len(blocks) > 2:
    print("\n--- 段3 开头 3 片 ---")
    for s in blocks[2][:3]:
        print(f"  {s['dur']:5.2f}s  {kb(s):7.0f} KB")
