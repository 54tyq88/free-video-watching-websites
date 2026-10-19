# FreeView · Python 单文件视频观看网站

> 一个视频观看网站，**全部由 Python 标准库实现** —— 零第三方依赖，零前端构建步骤。

```
.
├── main.py       # 全部后端 + 内嵌的 HTML/CSS/JS（播放器）
├── videos/       # 把 .mp4 / .webm / .m3u8 放进来
└── README.md
```

## ✨ 内置播放器能力

| 需求 | 实现 |
| --- | --- |
| 播放 / 暂停 | 中心按钮、控制条按钮、视频点击、`Space` |
| 进度条拖拽 + 时间显示 | 鼠标 / 触屏拖拽、自动格式化 `h:mm:ss` / `mm:ss`、缓冲进度 |
| 音量调节 + 静音切换 | 滑动条 + 滚轮微调 + 静音切换 + 记忆上次音量 |
| 全屏播放 | 浏览器原生全屏 API（含 Safari/旧版前缀），双击视频也可 |
| 清晰度切换 | `<source data-label>` 多码率，切换后保留当前进度 |
| 播放速度 | 0.5x / 0.75x / 1x / 1.25x / 1.5x / 2x |
| 键盘快捷键 | `Space`/`K` 播放暂停 · `←→` 5 秒 · `JL` 10 秒 · `↑↓` 音量 · `M` 静音 · `F` 全屏 · `0–9` 百分比 |
| 加载状态 | 圆形 spinner，`loadstart/waiting/stalled` 自动显示 |
| 错误提示 | `MEDIA_ERR_*` 自动映射为中文，附"重试"按钮 |

加分项：播放中鼠标静止 2.5s 自动隐藏控件和光标；自定义元数据 / 海报 / 多清晰度。

## 🚀 运行

```bash
python main.py
```

打开 [http://127.0.0.1:5000/](http://127.0.0.1:5000/)

自定义端口：

```bash
PORT=8080 python main.py
```

## 🎬 添加视频

把视频文件丢进 `videos/` 即可自动出现在首页，例如：

```
videos/
├── big-buck-bunny.mp4
├── subfolder/
│   └── clip.webm
└── _catalog.json   # 可选：自定义元数据
```

支持格式：`.mp4` `.webm` `.m3u8` `.mov` `.m4v` `.ogv`

### 自定义元数据 / 多清晰度（可选）

新建 `videos/_catalog.json`：

```json
[
  {
    "id": "big-buck-bunny",
    "title": "Big Buck Bunny",
    "description": "开放动画短片，用于演示播放器。",
    "poster": "",
    "sources": [
      { "label": "1080p", "src": "/videos/bbb/bbb-1080p.mp4", "type": "video/mp4" },
      { "label": "720p",  "src": "/videos/bbb/bbb-720p.mp4",  "type": "video/mp4" },
      { "label": "480p",  "src": "/videos/bbb/bbb-480p.mp4",  "type": "video/mp4" }
    ]
  }
]
```

- `id`：出现在 URL `/watch/<id>` 中
- `poster`：可选，填图片 URL
- `sources`：清晰度列表，每项 `label` 会显示在画质菜单里
- 单清晰度时画质按钮自动禁用

## 🧱 REST API

| 路由 | 说明 |
| --- | --- |
| `GET /` | 视频列表（HTML） |
| `GET /watch/<id>` | 播放页（HTML） |
| `GET /api/videos` | 全部视频元数据（JSON） |
| `GET /api/videos/<id>` | 单视频元数据（JSON） |
| `GET /videos/<path>` | 视频文件（支持 HTTP `Range`，可拖动快进） |
| `GET /healthz` | 健康检查（`ok`） |

## ⌨️ 键盘快捷键一览

| 按键 | 动作 |
| --- | --- |
| `Space` / `K` | 播放 / 暂停 |
| `←` / `→` | 快退 / 快进 5 秒 |
| `J` / `L` | 快退 / 快进 10 秒 |
| `↑` / `↓` | 音量 + / − |
| `M` | 静音切换 |
| `F` | 全屏切换 |
| `0`–`9` | 跳到 0% / 10% / … / 90% |

> 在视频区域点击一次，键盘快捷键才会响应。

## 🔍 实现要点

- **后端**：`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`，多线程处理并发请求
- **Range 流式**：`bytes=start-end` 分片响应，64KB 分块写，避免大文件占内存
- **安全**：`safe_video_path` 校验路径，防 `../` 穿越
- **模板**：自实现 `__KEY__` 占位符替换，避免 f-string 解析 JS 中的 `{...}` 出错
- **占位海报**：内嵌 SVG → `data:` URI，海报缺失时也有封面
- **静态资源**：CSS / JS 都内嵌在 `main.py`，无任何文件依赖

## 📝 许可

仅作示例使用，请使用合法、获得授权的视频资源。
