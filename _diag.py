import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fv_app", BASE / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

ffzy = app.ffzy
print("API_URL:", ffzy.API_URL)

print("\n=== 直接打采集接口，比较不同参数写法 ===")
for params in (
    {"ac": "detail", "ids": "28356"},
    {"ac": "detail", "id": "28356"},
    {"ac": "detail", "wd": "凡人修仙传"},
):
    try:
        r = ffzy.session.get(ffzy.API_URL, params=params, timeout=20)
        data = r.json()
        items = data.get("list") or []
        print(f"\n{params}")
        print(f"  HTTP {r.status_code}  code={data.get('code')}  条数={len(items)}")
        if items:
            it = items[0]
            pu = str(it.get("vod_play_url") or "")
            print(f"  vod_id={it.get('vod_id')}  name={it.get('vod_name')}")
            print(f"  play_url 长度={len(pu)}  前 80: {pu[:80]}")
            print(f"  play_from={it.get('vod_play_from')}")
    except Exception as e:
        print(f"\n{params}\n  失败 {type(e).__name__}: {e}")

print("\n=== app 层的函数 ===")
d1 = ffzy.detail_api("28356")
print("detail_api   :", "None" if not d1 else f"{d1['title']} / {len(d1['episodes'])} 集")

d2 = ffzy.get_video_detail("28356")
print("html 详情页  :", "None" if not d2 else f"{d2['title']} / {len(d2['episodes'])} 集")

d3 = app.get_detail("28356")
print("app.get_detail:", "None" if not d3 else f"{d3['title']} / {len(d3['episodes'])} 集")
