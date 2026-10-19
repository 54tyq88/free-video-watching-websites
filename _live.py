import time
import urllib.error
import urllib.request

for attempt in (1, 2):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/play?vid=28356", timeout=180) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        status = e.code
    dt = time.perf_counter() - t0

    print(f"第 {attempt} 次: HTTP {status}  {dt:.1f}s  {len(body)} 字节")
    print(f"  是否错误页: {'没拿到播放地址' in body}")
    print(f"  有播放器:   {'data-src' in body}")
    if "data-src" in body:
        import re
        m = re.search(r'data-src="([^"]+)"', body)
        print(f"  播放地址:   {m.group(1) if m else '?'}")
    if "没拿到播放地址" in body:
        import re
        m = re.search(r"<h1>([^<]+)</h1>", body)
        print(f"  标题:       {m.group(1) if m else '?'}")
