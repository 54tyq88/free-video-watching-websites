import importlib.util
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fv_app", BASE / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

PORT = 8772
srv = ThreadingHTTPServer(("127.0.0.1", PORT), app.FreeViewSite)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(p, timeout=180):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


h = get("/")
print("=== 首页 ===")
for name, ok in [
    ("主题色 --accent:#22d3ee", "--accent:#22d3ee" in h),
    ("深空背景层 body::before", "body::before{" in h),
    ("透视网格动画 gridMove", "@keyframes gridMove" in h),
    ("鼠标跟随光斑 .fx-glow", ".fx-glow{" in h),
    ("卡片 3D 倾斜 JS", "rotateX(" in h),
    ("点击涟漪 fx-ripple", "fx-ripple" in h),
    ("终端风格标签 hero__tag", "hero__tag" in h),
    ("卡片入场动画 cardIn", "@keyframes cardIn" in h),
    ("按钮光扫 ::after", "translateX(120%)" in h),
    ("减少动态效果媒体查询", "prefers-reduced-motion" in h),
]:
    print(f"  {'OK ' if ok else 'FAIL'} {name}")

h2 = get("/play?vid=28356&ep=1")
print("\n=== 播放页 ===")
for name, ok in [
    ("播放器主题 --p-accent:#22d3ee", "--p-accent:#22d3ee" in h2),
    ("播放器外发光", "0 0 90px -30px rgba(34,211,238,.7)" in h2),
    ("鼠标特效已注入", "fx-glow" in h2),
    ("PLAYER_JS 仍完整", "data-progress-input" in h2 and "player__volume-slider" in h2),
    ("全屏修复仍在", "var e = d.documentElement" in h2),
]:
    print(f"  {'OK ' if ok else 'FAIL'} {name}")
print("  残留占位符:", sorted(set(re.findall(r"__[A-Z_]+__", h2))) or "无")

h3 = get("/search?wd=%E5%87%A1%E4%BA%BA%E4%BF%AE%E4%BB%99%E4%BC%A0")
print("\n=== 搜索页 ===")
print("  卡片数:", h3.count('class="card"'))
print("  新主题已注入:", "--accent:#22d3ee" in h3)
print("  鼠标特效已注入:", "fx-glow" in h3)
print("  残留占位符:", sorted(set(re.findall(r"__[A-Z_]+__", h3))) or "无")

srv.shutdown()
