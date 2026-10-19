<?php
/**
 * FreeView · PHP 版
 * ==================
 * 部署：把本文件（和 hls.min.js）上传到 wwwroot/tv/，访问 http://你的域名/tv/
 *
 * 页面：
 *   /tv/                       首页
 *   /tv/?wd=关键词              搜索结果
 *   /tv/?vid=28356&ep=1        播放页
 *   /tv/?a=hls&k=xxx           清洗后的播放列表（内部使用）
 *   /tv/?a=poster&vid=28356    封面跳转（内部使用）
 *
 * 数据源：非凡资源网采集接口（cj.ffzyapi.com）
 */

declare(strict_types=1);

mb_internal_encoding('UTF-8');
date_default_timezone_set('Asia/Shanghai');
header('Content-Type: text/html; charset=utf-8');

ini_set('display_errors', '0');
error_reporting(E_ALL);

// ============================================================================
// 配置
// ============================================================================
const FFZY_BASE = 'https://cj.ffzyapi.com';
const FFZY_API  = FFZY_BASE . '/api.php/provide/vod/';
const CACHE_TTL = 600;          // 缓存 10 分钟
const HTTP_UA   = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

// 首页快捷词
$WARM_KEYWORDS = ['凡人修仙传', '斗破苍穹', '完美世界', '仙逆', '遮天', '吞噬星空'];

// 广告段识别阈值
const AD_MIN_SEC   = 8.0;
const AD_MAX_SEC   = 30.0;
const MAIN_MIN_SEC = 60.0;

$CACHE_DIR = __DIR__ . '/cache';
if (!is_dir($CACHE_DIR)) {
    @mkdir($CACHE_DIR, 0755, true);
}

// ============================================================================
// 通用工具
// ============================================================================
function e($s): string
{
    return htmlspecialchars((string)$s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/** 发起 GET 请求（必须带 UA，否则源站返回 403） */
function http_get(string $url, array $params = [], string $referer = ''): ?string
{
    if ($params) {
        $url .= (strpos($url, '?') === false ? '?' : '&') . http_build_query($params);
    }
    $ch = curl_init($url);
    if ($ch === false) {
        return null;
    }
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 20,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
        CURLOPT_USERAGENT      => HTTP_UA,
        CURLOPT_REFERER        => $referer !== '' ? $referer : FFZY_BASE . '/',
        CURLOPT_ENCODING       => 'gzip, deflate',
    ]);
    $res  = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($res === false || $code >= 400) {
        return null;
    }
    return (string)$res;
}

/** 把相对地址补成绝对地址 */
function absolute_url(string $u, string $base): string
{
    if (preg_match('#^https?://#i', $u)) {
        return $u;
    }
    $p      = parse_url($base);
    $scheme = $p['scheme'] ?? 'https';
    $host   = $p['host'] ?? '';
    if (strpos($u, '//') === 0) {
        return $scheme . ':' . $u;
    }
    if (strpos($u, '/') === 0) {
        return $scheme . '://' . $host . $u;
    }
    $path = $p['path'] ?? '/';
    $pos  = strrpos($path, '/');
    $dir  = $pos === false ? '/' : substr($path, 0, $pos + 1);
    return $scheme . '://' . $host . $dir . $u;
}

/** 取主机名 */
function url_origin(string $u): string
{
    $p = parse_url($u);
    if (!isset($p['scheme'], $p['host'])) {
        return '';
    }
    return $p['scheme'] . '://' . $p['host'];
}

// ============================================================================
// 文件缓存
// ============================================================================
function cache_file(string $key): string
{
    global $CACHE_DIR;
    return $CACHE_DIR . '/' . md5($key) . '.json';
}

function cache_get(string $key)
{
    $f = cache_file($key);
    if (!is_file($f)) {
        return null;
    }
    if (time() - (int)filemtime($f) > CACHE_TTL) {
        return null;
    }
    $raw = @file_get_contents($f);
    if ($raw === false || $raw === '') {
        return null;
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : null;
}

function cache_put(string $key, $value): void
{
    $f = cache_file($key);
    @file_put_contents($f, json_encode($value, JSON_UNESCAPED_UNICODE), LOCK_EX);
}

// ============================================================================
// 采集接口
// ============================================================================
/** 解析 vod_play_url：多源用 $$$ 分隔，源内多集用 # 分隔，集名与地址用 $ 分隔 */
function parse_play_url($raw, $from): array
{
    $sources = array_values(array_filter(explode('$$$', (string)$from), 'strlen'));
    $out     = [];
    $blocks  = explode('$$$', (string)$raw);
    foreach ($blocks as $i => $block) {
        $src = $sources[$i] ?? ('source' . ($i + 1));
        foreach (explode('#', $block) as $item) {
            $item = trim($item);
            if ($item === '') {
                continue;
            }
            $pos = strpos($item, '$');
            if ($pos === false) {
                $name = '';
                $url  = $item;
            } else {
                $name = substr($item, 0, $pos);
                $url  = substr($item, $pos + 1);
            }
            $url = trim($url);
            if (stripos($url, 'http') !== 0) {
                continue;
            }
            $out[] = [
                'source'    => $src,
                'name'      => trim($name) !== '' ? trim($name) : ('第' . (count($out) + 1) . '集'),
                'share_url' => $url,
            ];
        }
    }
    return $out;
}

function normalize_item(array $it): array
{
    return [
        'vid'         => (string)($it['vod_id'] ?? ''),
        'title'       => trim((string)($it['vod_name'] ?? '')),
        'poster'      => trim((string)($it['vod_pic'] ?? '')),
        'status'      => trim((string)($it['vod_remarks'] ?? '')),
        'region'      => trim((string)($it['vod_area'] ?? '')),
        'category'    => trim((string)($it['type_name'] ?? '')),
        'update_time' => substr(trim((string)($it['vod_time'] ?? '')), 0, 10),
        'episodes'    => parse_play_url($it['vod_play_url'] ?? '', $it['vod_play_from'] ?? ''),
    ];
}

function api_call(array $params): ?array
{
    $raw = http_get(FFZY_API, $params);
    if ($raw === null) {
        return null;
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || (string)($data['code'] ?? '') !== '1') {
        return null;
    }
    $list = $data['list'] ?? [];
    return is_array($list) ? $list : [];
}

/** 搜索（一次拿到结果 + 海报 + 全部剧集地址） */
function api_search(string $wd, int $page = 1): ?array
{
    $list = api_call(['ac' => 'detail', 'wd' => $wd, 'pg' => $page]);
    if ($list === null) {
        return null;
    }
    $out = [];
    foreach ($list as $it) {
        if (empty($it['vod_id'])) {
            continue;
        }
        $out[] = normalize_item($it);
    }
    return $out;
}

/** 按 id 取详情 */
function api_detail(string $vid): ?array
{
    $list = api_call(['ac' => 'detail', 'ids' => $vid]);
    if (empty($list)) {
        return null;
    }
    $item = normalize_item($list[0]);
    return $item['episodes'] ? $item : null;
}

/** 搜索结果缓存 + 顺带把详情写进缓存 */
function search_cached(string $wd, int $page = 1): array
{
    $key = 'search:' . $wd . ':' . $page;
    $c   = cache_get($key);
    if ($c !== null) {
        return $c;
    }
    $hits = api_search($wd, $page);
    $hits = $hits ?? [];

    foreach ($hits as $h) {
        if (!empty($h['episodes'])) {
            cache_put('detail:' . $h['vid'], $h);
        }
    }
    cache_put($key, $hits);
    return $hits;
}

function detail_cached(string $vid): ?array
{
    $c = cache_get('detail:' . $vid);
    if ($c !== null && !empty($c['episodes'])) {
        return $c;
    }
    $d = api_detail($vid);
    if ($d !== null) {
        cache_put('detail:' . $vid, $d);
    }
    return $d;
}

/** 选播放源：优先「直接给 m3u8 直链」的源，省一次页面解析 */
function pick_source(array $eps): array
{
    $sources = [];
    foreach ($eps as $x) {
        if (!in_array($x['source'], $sources, true)) {
            $sources[] = $x['source'];
        }
    }
    $chosen = $sources[0] ?? '';
    foreach ($sources as $s) {
        foreach ($eps as $x) {
            if ($x['source'] === $s && stripos($x['share_url'], '.m3u8') !== false) {
                $chosen = $s;
                break 2;
            }
        }
    }
    $list = [];
    foreach ($eps as $x) {
        if ($x['source'] === $chosen) {
            $list[] = $x;
        }
    }
    return [$list, $sources, $chosen];
}

/** share 页 → m3u8 地址 */
function resolve_m3u8(string $share_url): ?string
{
    if (stripos($share_url, '.m3u8') !== false) {
        return $share_url;
    }
    $html = http_get($share_url);
    if ($html === null) {
        return null;
    }
    if (preg_match('/const\s+url\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']/i', $html, $m)) {
        return absolute_url($m[1], $share_url);
    }
    return null;
}

// ============================================================================
// m3u8 清洗：去掉插播广告段
// ============================================================================
function clean_playlist(string $master): ?array
{
    $txt = http_get($master, [], url_origin($master) . '/');
    if ($txt === null || strpos($txt, '#EXTM3U') === false) {
        return null;
    }

    $sub = $master;
    if (strpos($txt, '#EXT-X-STREAM-INF') !== false) {
        $first = '';
        foreach (preg_split('/\r?\n/', $txt) as $ln) {
            $ln = trim($ln);
            if ($ln !== '' && $ln[0] !== '#') {
                $first = $ln;
                break;
            }
        }
        if ($first === '') {
            return null;
        }
        $sub = absolute_url($first, $master);
        $txt = http_get($sub, [], url_origin($sub) . '/');
        if ($txt === null) {
            return null;
        }
    }

    // 解析分片
    $segs = [];
    $disc = false;
    $dur  = null;
    foreach (preg_split('/\r?\n/', $txt) as $ln) {
        $ln = trim($ln);
        if ($ln === '') {
            continue;
        }
        if (strpos($ln, '#EXT-X-DISCONTINUITY') === 0) {
            $disc = true;
            continue;
        }
        if (strpos($ln, '#EXTINF:') === 0) {
            $dur = (float)substr($ln, 8);
            continue;
        }
        if ($ln[0] === '#') {
            continue;
        }
        $segs[] = ['dur' => $dur ?? 0.0, 'uri' => $ln, 'disc' => $disc];
        $disc = false;
        $dur  = null;
    }
    if (!$segs) {
        return null;
    }

    // 按 DISCONTINUITY 分块
    $blocks = [];
    $cur    = [];
    foreach ($segs as $s) {
        if ($s['disc'] && $cur) {
            $blocks[] = $cur;
            $cur      = [];
        }
        $cur[] = $s;
    }
    if ($cur) {
        $blocks[] = $cur;
    }

    // 命中「长正片 + 短广告」就删掉广告块
    $removed = 0.0;
    if (count($blocks) >= 3) {
        $d1 = 0.0;
        foreach ($blocks[0] as $x) {
            $d1 += $x['dur'];
        }
        $d2 = 0.0;
        foreach ($blocks[1] as $x) {
            $d2 += $x['dur'];
        }
        if ($d1 >= MAIN_MIN_SEC && $d2 >= AD_MIN_SEC && $d2 <= AD_MAX_SEC) {
            $removed = $d2;
            array_splice($blocks, 1, 1);
        }
    }

    $kept = [];
    foreach ($blocks as $b) {
        foreach ($b as $s) {
            $kept[] = $s;
        }
    }
    if (!$kept) {
        return null;
    }

    $maxDur = 0.0;
    foreach ($kept as $s) {
        if ($s['dur'] > $maxDur) {
            $maxDur = $s['dur'];
        }
    }

    $out  = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n";
    $out .= '#EXT-X-TARGETDURATION:' . ((int)$maxDur + 1) . "\n";
    foreach ($kept as $s) {
        if ($s['disc']) {
            $out .= "#EXT-X-DISCONTINUITY\n";
        }
        $out .= '#EXTINF:' . number_format($s['dur'], 6, '.', '') . ",\n";
        $out .= absolute_url($s['uri'], $sub) . "\n";
    }
    $out .= "#EXT-X-ENDLIST\n";

    return [$out, $removed];
}

function clean_cached(string $master): ?array
{
    $key = 'clean:' . md5($master);
    $c   = cache_get($key);
    if ($c !== null && isset($c[0])) {
        return $c;
    }
    $r = clean_playlist($master);
    if ($r !== null) {
        cache_put($key, $r);
    }
    return $r;
}

// ============================================================================
// 样式
// ============================================================================
$CSS = <<<'CSS'
:root{
  --bg:#04060c;--bg2:#0b1422;--card:rgba(13,21,35,.66);
  --text:#dbe7f5;--dim:#7d8ea6;--accent:#22d3ee;--violet:#7c3aed;
  --border:rgba(34,211,238,.14);--mono:ui-monospace,"Cascadia Code",Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  background:#04060c;color:var(--text);font:15px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
body::before{
  content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:
    radial-gradient(900px 520px at 12% -8%,rgba(34,211,238,.16),transparent 62%),
    radial-gradient(760px 460px at 88% 2%,rgba(124,58,237,.14),transparent 60%),
    linear-gradient(180deg,#04060c,#060b15 48%,#04060c);
}
body::after{
  content:"";position:fixed;inset:-56px 0 0 0;z-index:-1;pointer-events:none;
  background-image:linear-gradient(rgba(34,211,238,.055) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(34,211,238,.055) 1px,transparent 1px);
  background-size:56px 56px;
  -webkit-mask-image:radial-gradient(ellipse 92% 72% at 50% 0%,#000 8%,transparent 78%);
  mask-image:radial-gradient(ellipse 92% 72% at 50% 0%,#000 8%,transparent 78%);
}
a{color:inherit;text-decoration:none}
.header{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:18px;
  padding:12px 24px;background:rgba(4,6,12,.75);
  border-bottom:1px solid rgba(34,211,238,.12);backdrop-filter:blur(16px)}
.logo{font-family:var(--mono);font-size:17px;font-weight:700;letter-spacing:2.5px;
  color:var(--accent);text-shadow:0 0 20px rgba(34,211,238,.75)}
.logo::after{content:"";display:inline-block;width:6px;height:6px;margin-left:9px;
  border-radius:50%;background:var(--accent);vertical-align:middle;box-shadow:0 0 12px var(--accent)}
.sbar{display:flex;gap:8px;margin-left:auto}
.sbar input{width:240px;padding:8px 12px;border-radius:8px;font-size:14px;
  background:rgba(9,15,26,.9);border:1px solid rgba(34,211,238,.18);color:var(--text);outline:none;
  transition:border-color .25s,box-shadow .25s}
.sbar input:focus{border-color:rgba(34,211,238,.7);
  box-shadow:0 0 0 3px rgba(34,211,238,.12),0 0 30px rgba(34,211,238,.2)}
.sbar button,.hsearch button{border:0;cursor:pointer;border-radius:8px;
  background:linear-gradient(135deg,#0891b2,#22d3ee 60%,#67e8f9);color:#02161d;
  font-weight:700;font-size:14px;padding:8px 18px;
  box-shadow:0 0 26px rgba(34,211,238,.35);transition:box-shadow .25s,filter .25s}
.sbar button:hover,.hsearch button:hover{box-shadow:0 0 40px rgba(34,211,238,.55);filter:brightness(1.08)}
.main{max-width:1240px;margin:0 auto;padding:24px}
.hero{text-align:center;padding:26px 0 10px}
.hero h1{font-size:clamp(24px,3.2vw,38px);letter-spacing:1px;margin:0 0 8px;
  background:linear-gradient(96deg,#eaf7ff 6%,#22d3ee 46%,#a78bfa 96%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:var(--dim);margin:0 0 18px}
.tag{font-family:var(--mono);font-size:12px;letter-spacing:1.5px;color:rgba(34,211,238,.72);margin-bottom:12px}
.hsearch{display:flex;gap:10px;max-width:560px;margin:0 auto}
.hsearch input{flex:1;padding:14px 18px;border-radius:12px;font-size:15px;
  background:rgba(9,15,26,.9);border:1px solid rgba(34,211,238,.2);color:var(--text);outline:none;
  transition:border-color .25s,box-shadow .25s}
.hsearch input:focus{border-color:rgba(34,211,238,.72);
  box-shadow:0 0 0 3px rgba(34,211,238,.12),0 0 34px rgba(34,211,238,.22)}
.hsearch button{padding:0 26px;border-radius:12px;font-size:15px}
.chips{margin-top:18px}
.chip{display:inline-block;margin:4px 6px 0 0;padding:5px 13px;border-radius:999px;
  font-family:var(--mono);font-size:13px;color:#8fa4bd;background:rgba(11,18,30,.7);
  border:1px solid rgba(34,211,238,.16);
  transition:color .2s,background .2s,box-shadow .2s,transform .2s}
.chip:hover{color:#02161d;background:linear-gradient(135deg,#22d3ee,#67e8f9);
  box-shadow:0 0 26px rgba(34,211,238,.5);transform:translateY(-2px)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:18px;margin-top:20px}
.card{display:block;border-radius:14px;overflow:hidden;position:relative;
  background:linear-gradient(158deg,rgba(17,27,45,.92),rgba(8,13,22,.94));
  border:1px solid rgba(34,211,238,.15);
  transition:transform .3s cubic-bezier(.2,.8,.2,1),border-color .3s,box-shadow .3s;
  animation:cardIn .5s cubic-bezier(.2,.8,.2,1) both}
@keyframes cardIn{from{opacity:0;filter:blur(6px);translate:0 14px}to{opacity:1;filter:blur(0);translate:0 0}}
.card:hover{transform:translateY(-4px);border-color:rgba(34,211,238,.45);
  box-shadow:0 0 0 1px rgba(34,211,238,.2),0 20px 46px rgba(0,0,0,.6),0 0 70px -24px rgba(34,211,238,.75)}
.thumb{position:relative;aspect-ratio:16/9;background:#0a1018 center/cover no-repeat}
.thumb .play{position:absolute;inset:0;display:grid;place-items:center;font-size:34px;
  color:#22d3ee;text-shadow:0 0 24px rgba(34,211,238,.9);opacity:0;transition:opacity .2s}
.card:hover .thumb .play{opacity:1}
.meta{padding:10px 12px 13px}
.meta h3{margin:0 0 5px;font-size:15px;color:#e6f2ff;font-weight:600;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta p{margin:0;font-family:var(--mono);font-size:12px;color:var(--dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.watch{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px}
@media(max-width:900px){.watch{grid-template-columns:1fr}}
.player{position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:14px;
  overflow:hidden;border:1px solid rgba(34,211,238,.2);
  box-shadow:0 26px 60px rgba(0,0,0,.6),0 0 90px -30px rgba(34,211,238,.7)}
.player video{width:100%;height:100%;display:block;background:#000}
.info h2{font-size:19px;margin:16px 0 6px}
.info p{color:var(--dim);margin:0;font-size:13px}
.info .ok{color:var(--accent)}
.side h3{font-size:15px;color:var(--dim);margin:14px 0 10px}
.epfilter{width:100%;margin-bottom:10px;padding:7px 10px;border-radius:8px;font-size:13px;
  background:rgba(9,15,26,.9);border:1px solid rgba(34,211,238,.16);color:var(--text);outline:none}
.epfilter:focus{border-color:rgba(34,211,238,.6);box-shadow:0 0 22px rgba(34,211,238,.18)}
.eps{list-style:none;margin:0;padding:0;max-height:min(70vh,700px);overflow-y:auto;
  display:flex;flex-direction:column;gap:2px}
.eps a{display:block;padding:7px 12px;border-radius:8px;font-size:14px;color:var(--dim);
  border:1px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:background .18s,color .18s,border-color .18s,transform .18s}
.eps a:hover{background:rgba(34,211,238,.09);border-color:rgba(34,211,238,.28);
  color:var(--accent);transform:translateX(5px)}
.eps a.cur{background:linear-gradient(100deg,rgba(34,211,238,.92),rgba(34,211,238,.55));
  color:#02161d;font-weight:700;box-shadow:0 0 24px rgba(34,211,238,.45)}
.eps::-webkit-scrollbar{width:6px}
.eps::-webkit-scrollbar-thumb{background:rgba(34,211,238,.28);border-radius:3px}
.empty{text-align:center;padding:48px 16px;color:var(--dim);font-family:var(--mono);
  background:rgba(11,18,30,.6);border:1px dashed rgba(34,211,238,.18);border-radius:14px}
.footer{text-align:center;color:#4d5f78;font-family:var(--mono);font-size:12px;padding:34px 16px 46px}
.err{padding:60px 16px;text-align:center}
.err h1{color:var(--accent);font-size:22px}

/* ---- 鼠标跟随光斑 ---- */
.fx-glow{
  position:fixed;left:0;top:0;width:560px;height:560px;margin:-280px 0 0 -280px;
  border-radius:50%;pointer-events:none;z-index:0;opacity:0;
  background:radial-gradient(circle,rgba(34,211,238,.13),rgba(34,211,238,.05) 42%,transparent 68%);
  transition:opacity .4s ease;will-change:transform;
}
body.fx-on .fx-glow{opacity:1}

/* ---- 点击涟漪 ---- */
.fx-ripple{
  position:fixed;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;
  border:1px solid rgba(34,211,238,.9);pointer-events:none;z-index:9999;
  animation:rippleOut .62s cubic-bezier(.2,.7,.3,1) forwards;
}
@keyframes rippleOut{
  0%{width:10px;height:10px;margin:-5px 0 0 -5px;opacity:.9}
  100%{width:150px;height:150px;margin:-75px 0 0 -75px;opacity:0}
}

/* ---- 卡片跟随高光 ---- */
.card::before{
  content:"";position:absolute;inset:0;z-index:1;pointer-events:none;
  background:radial-gradient(320px 180px at var(--mx,50%) var(--my,0%),
             rgba(34,211,238,.17),transparent 70%);
  opacity:0;transition:opacity .3s;
}
.card:hover::before{opacity:1}
.card{will-change:transform}

@media (prefers-reduced-motion: reduce){
  .fx-glow,.fx-ripple{display:none}
  .card{animation:none}
}
CSS;

// ============================================================================
// 页面片段
// ============================================================================
function page_head(string $title, string $css): string
{
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>'
        . '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        . '<title>' . e($title) . '</title><style>' . $css . '</style></head><body>'
        . '<header class="header"><a class="logo" href="./">&#9654; FREEVIEW</a>'
        . '<form class="sbar" action="./" method="get" autocomplete="off">'
        . '<input type="search" name="wd" placeholder="搜索片名…" value="">'
        . '<button type="submit">搜索</button></form></header>';
}

function page_foot(): string
{
    $fx = <<<'JS'
<script>
/* 鼠标互动：光斑跟随 / 卡片 3D 倾斜 / 点击涟漪 */
(function(){
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));

  /* 卡片入场依次错开 */
  cards.forEach(function(c, i){
    c.style.animationDelay = Math.min(i * 0.05, 0.4).toFixed(2) + 's';
  });

  if(reduce) return;

  /* 1. 跟随鼠标的光斑：lerp 追帧，拖尾才有惯性感 */
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
    if(e.target.closest && e.target.closest('input, textarea, select, option')) return;
    var s = document.createElement('span');
    s.className = 'fx-ripple';
    s.style.left = e.clientX + 'px';
    s.style.top = e.clientY + 'px';
    document.body.appendChild(s);
    setTimeout(function(){ s.remove(); }, 680);
  });
})();
</script>
JS;

    return '<footer class="footer">&copy; FreeView PHP &middot; 仅供学习交流，资源来自第三方采集站</footer>'
        . $fx
        . '</body></html>';
}

// ============================================================================
// 路由
// ============================================================================
$action = isset($_GET['a']) ? (string)$_GET['a'] : '';

// ---- 返回清洗后的播放列表 ----
if ($action === 'hls') {
    $k = isset($_GET['k']) ? (string)$_GET['k'] : '';
    $c = cache_get('hlsout:' . $k);
    header('Content-Type: application/vnd.apple.mpegurl; charset=utf-8');
    header('Cache-Control: no-cache');
    if (is_array($c) && isset($c[0])) {
        echo $c[0];
    } else {
        echo "#EXTM3U\n#EXT-X-ENDLIST\n";
    }
    exit;
}

// ---- 封面跳转 ----
if ($action === 'poster') {
    $vid = isset($_GET['vid']) ? preg_replace('/\D/', '', (string)$_GET['vid']) : '';
    $pic = '';
    if ($vid !== '') {
        $d = cache_get('detail:' . $vid);
        if (is_array($d) && !empty($d['poster'])) {
            $pic = $d['poster'];
        }
    }
    if ($pic === '' || stripos($pic, 'http') !== 0) {
        http_response_code(404);
        exit;
    }
    header('Location: ' . $pic, true, 302);
    exit;
}

// ---- 首页 ----
if (!isset($_GET['wd']) && !isset($_GET['vid'])) {
    $chips = '';
    foreach ($WARM_KEYWORDS as $k) {
        $chips .= '<a class="chip" href="./?wd=' . urlencode($k) . '">' . e($k) . '</a>';
    }
    echo page_head('FreeView · 在线影视', $CSS);
    echo '<main class="main"><section class="hero">';
    echo '<p class="tag">&gt;_ 直连采集源 · 实时索引</p>';
    echo '<h1>想看什么，搜一下</h1>';
    echo '<p>搜到就能直接播，无需下载</p>';
    echo '<form class="hsearch" action="./" method="get" autocomplete="off">';
    echo '<input type="search" name="wd" placeholder="输入片名，如：凡人修仙传" autofocus/>';
    echo '<button type="submit">搜索</button></form>';
    echo '<div class="chips">' . $chips . '</div>';
    echo '</section></main>';
    echo page_foot();
    exit;
}

// ---- 搜索结果 ----
if (isset($_GET['wd'])) {
    $wd  = trim((string)$_GET['wd']);
    $hit = $wd === '' ? [] : search_cached($wd);

    echo page_head($wd . ' 的搜索结果 · FreeView', $CSS);
    echo '<main class="main"><section class="hero"><h1>搜索结果</h1>';
    echo '<p>「' . e($wd) . '」找到 <strong>' . count($hit) . '</strong> 个结果</p></section>';

    if (!$hit) {
        echo '<div class="empty">没有找到相关影片，换个关键词试试。</div>';
    } else {
        echo '<section class="grid">';
        foreach ($hit as $i => $h) {
            $tags = array_filter([$h['status'], $h['region'], $h['category'], $h['update_time']]);
            echo '<a class="card" href="./?vid=' . e($h['vid']) . '" style="animation-delay:' . min($i * 0.05, 0.4) . 's">';
            echo '<div class="thumb" data-poster="' . e($h['vid']) . '"><span class="play">&#9654;</span></div>';
            echo '<div class="meta"><h3>' . e($h['title']) . '</h3>';
            echo '<p>' . e(implode(' · ', $tags)) . '</p></div></a>';
        }
        echo '</section>';
    }
    echo '</main>';
    // 封面懒加载
    echo '<script>(function(){'
        . 'var cards=document.querySelectorAll("[data-poster]");'
        . 'Array.prototype.forEach.call(cards,function(t){'
        . 'var v=t.getAttribute("data-poster");if(!v)return;'
        . 'var im=new Image();'
        . 'im.onload=function(){t.style.backgroundImage=\'url("\'+im.src+\'")\'};'
        . 'im.src="./?a=poster&vid="+encodeURIComponent(v);'
        . '});})();</script>';
    echo page_foot();
    exit;
}

// ---- 播放页 ----
$vid = isset($_GET['vid']) ? preg_replace('/\D/', '', (string)$_GET['vid']) : '';
if ($vid === '') {
    http_response_code(400);
    echo page_head('参数错误', $CSS);
    echo '<main class="main"><div class="err"><h1>参数不对</h1><p>缺少有效的 vid</p></div></main>';
    echo page_foot();
    exit;
}

$detail = detail_cached($vid);
if ($detail === null || empty($detail['episodes'])) {
    http_response_code(502);
    echo page_head('获取失败', $CSS);
    echo '<main class="main"><div class="err"><h1>没拿到播放地址</h1>'
        . '<p>采集接口暂时没响应，刷新一下页面重试。</p>'
        . '<p style="margin-top:16px"><a href="./" style="color:#22d3ee">返回首页</a></p>'
        . '</div></main>';
    echo page_foot();
    exit;
}

[$eps, $sources, $chosen] = pick_source($detail['episodes']);
$total = count($eps);

$ep = isset($_GET['ep']) ? (int)$_GET['ep'] : 1;
if ($ep < 1) {
    $ep = 1;
}
if ($ep > $total) {
    $ep = $total;
}

$master = resolve_m3u8($eps[$ep - 1]['share_url']);
if ($master === null) {
    http_response_code(502);
    echo page_head('解析失败', $CSS);
    echo '<main class="main"><div class="err"><h1>解析播放地址失败</h1>'
        . '<p>换一集试试，或稍后刷新。</p>'
        . '<p style="margin-top:16px"><a href="./?vid=' . e($vid) . '" style="color:#22d3ee">换一集</a></p>'
        . '</div></main>';
    echo page_foot();
    exit;
}

// 清洗播放列表
$src = $master;
$removed = 0.0;
$needClientClean = false;
$clean = clean_cached($master);
if ($clean !== null) {
    $k = substr(md5($master), 0, 20);
    cache_put('hlsout:' . $k, $clean);
    $removed = (float)($clean[1] ?? 0.0);
    $src = './?a=hls&k=' . $k;
} else {
    // 虚拟主机常常连不上播放域名：标记一下，交给浏览器自己清洗
    $needClientClean = true;
}

$origin = url_origin($master);

echo page_head($detail['title'] . ' ' . $eps[$ep - 1]['name'] . ' · FreeView', $CSS);
echo '<link rel="preconnect" href="' . e($origin) . '" crossorigin/>';
echo '<main class="main"><section class="watch"><div>';

echo '<div class="player"><video id="v" controls playsinline preload="metadata"'
    . (!empty($detail['poster']) ? ' poster="' . e($detail['poster']) . '"' : '')
    . ' data-src="' . e($src) . '"'
    . ($needClientClean ? ' data-raw="' . e($master) . '"' : '')
    . '></video></div>';

echo '<div class="info"><h2>' . e($detail['title']) . '</h2>';
echo '<p>第 ' . $ep . ' / ' . $total . ' 集';
if (count($sources) > 1) {
    echo ' &middot; 播放源：' . e($chosen);
}
if ($removed > 0) {
    echo ' &middot; <span class="ok">已跳过 ' . (int)round($removed) . 's 插播广告</span>';
}
echo '</p></div>';

echo '</div><aside class="side"><h3>剧集（' . $total . '）</h3>';
echo '<input class="epfilter" type="search" placeholder="输入集数快速定位，如 88" data-epf/>';
echo '<ul class="eps" id="epl">';
foreach ($eps as $i => $item) {
    $n   = $i + 1;
    $cls = $n === $ep ? ' class="cur"' : '';
    echo '<li><a href="./?vid=' . e($vid) . '&amp;ep=' . $n . '"' . $cls . '>' . e($item['name']) . '</a></li>';
}
echo '</ul></aside></section></main>';

// 播放器逻辑
echo '<script src="hls.min.js"></script>';
echo <<<'JS'
<script>
(function(){
  var v = document.getElementById('v');
  if(!v) return;

  var src = v.getAttribute('data-src') || '';
  var raw = v.getAttribute('data-raw') || '';
  var hls = null;

  function fail(msg){
    var p = v.parentNode, d = document.createElement('div');
    d.style.cssText = 'position:absolute;inset:0;display:grid;place-items:center;'
      + 'background:rgba(0,0,0,.75);color:#ff6b81;font-size:14px;text-align:center;padding:20px';
    d.textContent = msg;
    p.appendChild(d);
  }

  /* 相对地址补成绝对地址 */
  function abso(u, base){
    if(/^https?:\/\//i.test(u)) return u;
    var a = document.createElement('a');
    a.href = base;
    if(u.indexOf('//') === 0) return a.protocol + u;
    if(u.charAt(0) === '/') return a.protocol + '//' + a.host + u;
    return a.protocol + '//' + a.host + a.pathname.replace(/[^\/]*$/, '') + u;
  }

  /* 剔除插播广告段（与 PHP 端同一套规则） */
  function cleanText(text, base){
    var lines = text.split(/\r?\n/), segs = [], disc = false, dur = null, i, ln;
    for(i = 0; i < lines.length; i++){
      ln = lines[i].replace(/^\s+|\s+$/g, '');
      if(!ln) continue;
      if(ln.indexOf('#EXT-X-DISCONTINUITY') === 0){ disc = true; continue; }
      if(ln.indexOf('#EXTINF:') === 0){ dur = parseFloat(ln.substring(8)); continue; }
      if(ln.charAt(0) === '#') continue;
      segs.push({ dur: dur || 0, uri: ln, disc: disc });
      disc = false; dur = null;
    }
    if(!segs.length) return null;

    var blocks = [], cur = [], s, j, d1, d2;
    for(i = 0; i < segs.length; i++){
      s = segs[i];
      if(s.disc && cur.length){ blocks.push(cur); cur = []; }
      cur.push(s);
    }
    if(cur.length) blocks.push(cur);

    var removed = 0;
    if(blocks.length >= 3){
      d1 = 0; for(j = 0; j < blocks[0].length; j++){ d1 += blocks[0][j].dur; }
      d2 = 0; for(j = 0; j < blocks[1].length; j++){ d2 += blocks[1][j].dur; }
      if(d1 >= 60 && d2 >= 8 && d2 <= 30){ removed = d2; blocks.splice(1, 1); }
    }

    var kept = [], maxDur = 0, b;
    for(i = 0; i < blocks.length; i++){
      b = blocks[i];
      for(j = 0; j < b.length; j++){
        kept.push(b[j]);
        if(b[j].dur > maxDur) maxDur = b[j].dur;
      }
    }
    if(!kept.length) return null;

    var out = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-PLAYLIST-TYPE:VOD',
               '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-TARGETDURATION:' + (Math.floor(maxDur) + 1)];
    for(i = 0; i < kept.length; i++){
      if(kept[i].disc) out.push('#EXT-X-DISCONTINUITY');
      out.push('#EXTINF:' + kept[i].dur.toFixed(6) + ',');
      out.push(abso(kept[i].uri, base));
    }
    out.push('#EXT-X-ENDLIST');
    return { text: out.join('\n'), removed: removed };
  }

  /* 拉取播放列表：如果是 master，就再取第一条子列表 */
  function fetchPlaylist(url){
    return fetch(url).then(function(r){
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(txt){
      if(txt.indexOf('#EXT-X-STREAM-INF') === -1) return { text: txt, base: url };
      var lines = txt.split(/\r?\n/), first = '', i, ln;
      for(i = 0; i < lines.length; i++){
        ln = lines[i].replace(/^\s+|\s+$/g, '');
        if(ln && ln.charAt(0) !== '#'){ first = ln; break; }
      }
      if(!first) return { text: txt, base: url };
      var sub = abso(first, url);
      return fetch(sub).then(function(r){ return r.text(); })
        .then(function(t){ return { text: t, base: sub }; });
    });
  }

  function markRemoved(sec){
    if(!(sec > 0)) return;
    var p = document.querySelector('.info p');
    if(!p) return;
    var sp = document.createElement('span');
    sp.className = 'ok';
    sp.innerHTML = ' &middot; 已跳过 ' + Math.round(sec) + 's 插播广告';
    p.appendChild(sp);
  }

  function start(playUrl){
    if(window.Hls && window.Hls.isSupported()){
      if(hls){ try{ hls.destroy(); }catch(e){} hls = null; }
      hls = new window.Hls({
        maxBufferLength:45, maxMaxBufferLength:90, backBufferLength:30,
        testBandwidth:false, startFragPrefetch:true,
        fragLoadingMaxRetry:6, manifestLoadingMaxRetry:4, enableWorker:false
      });
      hls.on(window.Hls.Events.ERROR, function(_, data){
        if(!data || !data.fatal) return;
        if(data.type === window.Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if(data.type === window.Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else fail('播放失败：' + (data.details || data.type));
      });
      hls.loadSource(playUrl);
      hls.attachMedia(v);
    } else if(v.canPlayType('application/vnd.apple.mpegurl')){
      v.src = playUrl;
    } else {
      fail('当前浏览器不支持 HLS 播放');
    }
  }

  /* raw 有值 = 服务器没能清洗播放列表（虚拟主机连不上播放域名），
     改由浏览器自己拉取并剔除广告段；任何一步失败都退回原始地址，保证能看。 */
  if(raw && window.fetch && window.URL && URL.createObjectURL){
    fetchPlaylist(raw).then(function(p){
      var c = null;
      try { c = cleanText(p.text, p.base); } catch(e){ c = null; }
      if(!c || !c.text){ start(raw); return; }
      start(URL.createObjectURL(new Blob([c.text], { type: 'application/vnd.apple.mpegurl' })));
      markRemoved(c.removed);
    }).then(null, function(){
      start(raw);
    });
  } else {
    start(raw || src);
  }

  var f = document.querySelector('[data-epf]');
  var l = document.getElementById('epl');
  if(f && l){
    f.addEventListener('input', function(){
      var q = (f.value || '').trim();
      Array.prototype.forEach.call(l.querySelectorAll('li'), function(li){
        li.style.display = (!q || li.textContent.indexOf(q) > -1) ? '' : 'none';
      });
    });
  }
  var cur = l && l.querySelector('a.cur');
  if(cur && cur.scrollIntoView){ cur.scrollIntoView({block:'center'}); }
})();
</script>
JS;

echo page_foot();
