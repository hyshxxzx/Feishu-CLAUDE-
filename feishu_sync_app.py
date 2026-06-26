#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code → 飞书多维表格 同步助手
版本: 1.1.0 (现代 UI 重构版)
"""

import os, sys, json, time, sqlite3, threading, queue, urllib.request, urllib.error
import urllib.parse, datetime, shutil, platform, webbrowser, re, hashlib, base64
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# 系统托盘（可选依赖）：pip install pystray pillow
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

# ──────────────────────────────────────────────────
# 常量 & 路径
# ──────────────────────────────────────────────────
APP_VERSION    = "1.2.0"
APP_DIR        = os.path.expanduser("~/.claude_feishu_sync")
DB_PATH        = os.path.join(APP_DIR, "sync.db")
HOOK_SCRIPT    = os.path.join(APP_DIR, "hook_trigger.py")  # 旧版残留，仅用于卸载兼容
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
FEISHU_BASE    = "https://open.feishu.cn/open-apis"
DONE_RETAIN_DAYS = 30  # 已同步记录在队列表中的保留天数

os.makedirs(APP_DIR, exist_ok=True)

# ──────────────────────────────────────────────────
# 数据库连接 & 敏感字段加密
# ──────────────────────────────────────────────────
def _connect():
    """统一的 SQLite 连接：开启 WAL（读写不互斥）+ busy_timeout，缓解多进程并发锁库。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn

# 需要加密存储的配置键（机器绑定混淆，非强加密，但优于明文）
SENSITIVE_KEYS = {"app_secret"}
_ENC_PREFIX = "enc:v1:"

def _machine_key():
    seed = (platform.node() + "|" + APP_DIR + "|claude_feishu_sync_v1").encode("utf-8")
    return hashlib.sha256(seed).digest()

def _xor_stream(data: bytes) -> bytes:
    key = _machine_key()
    out = bytearray(len(data))
    k = key
    ki = 0
    for i in range(len(data)):
        if ki >= len(k):
            k = hashlib.sha256(k).digest(); ki = 0
        out[i] = data[i] ^ k[ki]; ki += 1
    return bytes(out)

def _enc(plain: str) -> str:
    if plain is None: return ""
    try:
        token = base64.b64encode(_xor_stream(plain.encode("utf-8"))).decode("ascii")
        return _ENC_PREFIX + token
    except Exception:
        return plain

def _dec(stored: str) -> str:
    if not stored: return stored
    if not stored.startswith(_ENC_PREFIX):
        return stored  # 旧的明文值，原样返回（下次保存时自动加密）
    try:
        raw = base64.b64decode(stored[len(_ENC_PREFIX):].encode("ascii"))
        return _xor_stream(raw).decode("utf-8")
    except Exception:
        return ""  # 解密失败（如换了机器）→ 返回空，引导用户重新填写

# ──────────────────────────────────────────────────
# 数据库层
# ──────────────────────────────────────────────────
def init_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS synced_uuids (
            uuid TEXT PRIMARY KEY, session_id TEXT, synced_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS upload_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, cwd TEXT, role TEXT,
            content TEXT, timestamp INTEGER, uuid TEXT UNIQUE,
            retry_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT, message TEXT, created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS file_offsets (
            path TEXT PRIMARY KEY, offset INTEGER DEFAULT 0
        );
    """)
    conn.commit(); conn.close()

def db_get(key, default=None):
    conn = _connect()
    r = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    if not r: return default
    return _dec(r[0]) if key in SENSITIVE_KEYS else r[0]

def db_set(key, value):
    val = str(value) if value is not None else ""
    if key in SENSITIVE_KEYS and val:
        val = _enc(val)
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, val))
    conn.commit(); conn.close()

def db_get_config():
    conn = _connect()
    rows = conn.execute("SELECT key,value FROM config").fetchall()
    conn.close()
    cfg = dict(rows)
    for k in SENSITIVE_KEYS:
        if k in cfg: cfg[k] = _dec(cfg[k])
    return cfg

def cleanup_old_records(days=DONE_RETAIN_DAYS):
    """删除 N 天前已同步(done)的队列记录；去重仍由 synced_uuids 保障。"""
    cutoff = int(time.time()) - days * 86400
    try:
        conn = _connect()
        conn.execute("DELETE FROM upload_queue WHERE status='done' AND created_at < ?", (cutoff,))
        conn.commit(); conn.close()
    except Exception: pass

def get_file_offset(path):
    conn = _connect()
    r = conn.execute("SELECT offset FROM file_offsets WHERE path=?", (path,)).fetchone()
    conn.close()
    return int(r[0]) if r else 0

def set_file_offset(path, offset):
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO file_offsets VALUES (?,?)", (path, int(offset)))
    conn.commit(); conn.close()

def is_uuid_synced(uuid):
    conn = _connect()
    r = conn.execute("SELECT 1 FROM synced_uuids WHERE uuid=?", (uuid,)).fetchone()
    conn.close(); return r is not None

def mark_uuid_synced(uuid, session_id):
    conn = _connect()
    conn.execute("INSERT OR IGNORE INTO synced_uuids VALUES (?,?,?)", (uuid, session_id, int(time.time())))
    conn.commit(); conn.close()

def enqueue_records(records):
    conn = _connect(); now = int(time.time()); errs = 0
    for r in records:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO upload_queue (session_id,cwd,role,content,timestamp,uuid,created_at) VALUES (?,?,?,?,?,?,?)",
                (r['session_id'], r['cwd'], r['role'], r['content'], r['ts'], r['uuid'], now)
            )
        except Exception: errs += 1
    conn.commit(); conn.close()
    if errs: add_log("ERROR", f"入队时 {errs} 条记录写入失败")

def get_pending_records(limit=50):
    conn = _connect()
    rows = conn.execute(
        "SELECT id,session_id,cwd,role,content,timestamp,uuid,retry_count FROM upload_queue WHERE status='pending' ORDER BY timestamp ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close(); return rows

def mark_records_status(ids, status):
    if not ids: return
    conn = _connect()
    conn.execute(f"UPDATE upload_queue SET status=? WHERE id IN ({','.join('?'*len(ids))})", [status]+list(ids))
    conn.commit(); conn.close()

def increment_retry(ids):
    if not ids: return
    conn = _connect()
    conn.execute(f"UPDATE upload_queue SET retry_count=retry_count+1 WHERE id IN ({','.join('?'*len(ids))})", list(ids))
    conn.execute("UPDATE upload_queue SET status='failed' WHERE retry_count>=5 AND status='pending'")
    conn.commit(); conn.close()

def get_queue_stats():
    conn = _connect()
    stats = dict(conn.execute("SELECT status,COUNT(*) FROM upload_queue GROUP BY status").fetchall())
    conn.close(); return stats

def get_extra_scan_paths():
    raw = db_get("extra_scan_paths", "[]") or "[]"
    try:    return json.loads(raw)
    except: return []

def set_extra_scan_paths(paths):
    db_set("extra_scan_paths", json.dumps(list(dict.fromkeys(p for p in paths if p))))

def get_all_scan_paths():
    """默认路径 + 用户自定义路径（去重）"""
    paths = [CLAUDE_PROJECTS] + get_extra_scan_paths()
    seen = set(); result = []
    for p in paths:
        if p not in seen: seen.add(p); result.append(p)
    return result

def add_log(level, msg):
    conn = _connect()
    conn.execute("INSERT INTO sync_log (level,message,created_at) VALUES (?,?,?)", (level, msg, int(time.time())))
    conn.execute("DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 2000)")
    conn.commit(); conn.close()

def get_recent_logs(n=300):
    conn = _connect()
    rows = conn.execute("SELECT level,message,created_at FROM sync_log ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    conn.close(); return list(reversed(rows))

# ──────────────────────────────────────────────────
# 飞书 API 层
# ──────────────────────────────────────────────────
_token_cache = {"token": None, "expires_at": 0}
_token_lock  = threading.Lock()

def get_access_token(app_id, app_secret):
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 120:
            return _token_cache["token"]
        data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
        req = urllib.request.Request(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal", data=data,
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        if resp.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {resp.get('msg')}")
        _token_cache["token"]      = resp["tenant_access_token"]
        _token_cache["expires_at"] = time.time() + resp.get("expire", 7200)
        return _token_cache["token"]

def _http(method, url, token=None, body=None, timeout=20):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token: headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            raise

def test_connection(app_id, app_secret):
    try:
        token = get_access_token(app_id, app_secret)
        return True, f"连接成功 (Token: {token[:8]}...)"
    except Exception as e:
        return False, f"连接失败: {e}"

def auto_create_table(app_id, app_secret):
    """自动创建多维表格，返回 (app_token, table_id, error)"""
    try:
        token = get_access_token(app_id, app_secret)
        resp  = _http("POST", f"{FEISHU_BASE}/bitable/v1/apps", token,
                      {"name": "Claude Code 对话记录"})
        if resp.get("code") != 0:
            code = resp.get("code"); msg = resp.get("msg")
            hint = ""
            if code in (99991672, 99991679, 1254302, 1254040) or "permission" in str(msg).lower():
                hint = ("\n👉 权限不足：请到飞书开放平台 →「权限管理」开通"
                        "「查看、评论、编辑和管理多维表格」(bitable)，"
                        "然后「创建版本 → 申请发布」审核通过后重试。")
            return None, None, f"创建表格失败 (code={code}): {msg}{hint}"
        app_token = resp["data"]["app"]["app_token"]
        resp2 = _http("GET", f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables", token)
        items = resp2.get("data", {}).get("items", [])
        if not items:
            return None, None, "无法获取数据表 ID"
        table_id = items[0]["table_id"]
        # 创建额外字段
        for fname, ftype in [("时间",5),("会话ID",1),("项目",1),("角色",1),("内容",1)]:
            try:
                _http("POST", f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                      token, {"field_name": fname, "type": ftype})
            except Exception:
                pass
        return app_token, table_id, None
    except Exception as e:
        return None, None, str(e)

def verify_table_access(app_id, app_secret, app_token, table_id):
    try:
        token = get_access_token(app_id, app_secret)
        resp  = _http("GET", f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields", token)
        if resp.get("code") == 0:
            return True, "表格访问正常 ✓"
        return False, f"访问失败 (code={resp.get('code')}): {resp.get('msg')}"
    except Exception as e:
        return False, str(e)

def batch_create_records(app_id, app_secret, app_token, table_id, records):
    token   = get_access_token(app_id, app_secret)
    payload = []
    for r in records:
        _, sid, cwd, role, content, ts, uid, _ = r
        ts_ms = int(ts)*1000 if ts and int(ts) > 1e6 else int(time.time()*1000)
        payload.append({"fields": {
            "时间":   ts_ms,
            "会话ID": (sid or "")[:200],
            "项目":   os.path.basename(cwd) if cwd else "",
            "角色":   role or "",
            "内容":   (content or "")[:9000],
        }})
    resp = _http("POST",
        f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
        token, {"records": payload})
    if resp.get("code") == 0:
        return [r[0] for r in records], []
    if resp.get("code") in (1254290, 1254291):
        return [], [r[0] for r in records]
    raise RuntimeError(f"写入失败 code={resp.get('code')}: {resp.get('msg')}")

# ──────────────────────────────────────────────────
# JSONL 解析
# ──────────────────────────────────────────────────
# 无意义口水短句（精确匹配，去标点/空格后比对；只滤纯口水，不误伤有内容的短句）
FILLER_WORDS = {
    "继续","继续吧","继续继续","接着","接着来","go","next",
    "好","好的","好滴","好嘞","好吧","行","行吧","可以","可","ok","okay","okk","k","嗯","嗯嗯","嗯好",
    "对","对的","对对","对对对","是","是的","没错","没问题","木有问题",
    "收到","明白","懂了","知道了","了解","清楚了","好的明白","好的收到","好的知道了",
    "谢谢","谢啦","多谢","thx","thanks","3q","辛苦了","赞","不错","可以的","很好","棒","厉害",
    "这样","就这样","好的这样","可以了","行了","done","完成",
    "嗯嗯好的","好的谢谢","好的好的","ko","yes","y","n","no","嗯好的",
}
_FILLER_STRIP = re.compile(r"[\s，。、！？!?,.~～…·:：;；\"'`（）()【】\[\]]+")

def _is_filler(text):
    if not text: return True
    t = _FILLER_STRIP.sub("", text).strip().lower()
    return (not t) or (t in FILLER_WORDS)

# ── 结构化噪音过滤（恒开，不是真正对话）──────────────────────
# Claude Code 会把斜杠命令、本地命令输出、系统提醒、caveat 等以「纯文本」形式
# 写进 message.content，旧的 tool_use/tool_result 块过滤管不到它们，需要在文本层兜底。
#
# 1) 成对注入区块：常被「追加」在一条真实用户消息后面，只剜掉区块、保留真实内容；
_NOISE_SPAN_RE = re.compile(
    r"<(system-reminder|local-command-caveat|user-prompt-submit-hook)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE)
# 2) 整条即噪音：斜杠命令回执、本地命令 stdout/stderr、bash 输入输出等独立消息整条丢弃；
_NOISE_PURE_RE = re.compile(
    r"</?(command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr|local-command-caveat|"
    r"bash-input|bash-stdout|bash-stderr|system-reminder)\b",
    re.IGNORECASE)
_CAVEAT_PREFIX = "Caveat: The messages below were generated by the user"

def _clean_noise(text):
    """剜掉追加在真实消息中的系统提醒 / caveat 区块，返回去噪后的正文。"""
    if not text: return text
    return _NOISE_SPAN_RE.sub("", text).strip()

def _is_command_message(text):
    """判断整条消息是否为斜杠命令 / 本地命令输出 / caveat 这类纯噪音，应整条丢弃。"""
    if not text: return False
    t = text.strip()
    if _CAVEAT_PREFIX in t[:120]: return True
    return bool(_NOISE_PURE_RE.search(t))

# ── 助手「过程旁白」启发式过滤（可开关，默认开）──────────────
# 只针对 assistant 的「短消息」，识别『已启动…正常』『语法通过，验证一下…』
# 『状态页正常，看一下配置页』这类中间过程旁白。长消息（真正的分析/结论）一律放行。
_PROC_CUE_RE = re.compile(
    r"(验证一下|验证下|看一下|看看|看下|瞧一下|跑一下|跑下|运行一下|执行一下|"
    r"试一下|试试|已启动|启动了|启动成功|已启动|语法通过|编译通过|测试通过|检查通过|"
    r"通过了|请查看|请看|确认一下|确认下|检查一下|检查下|状态正常|一切正常|"
    r"正常运行|个进程|个 electron|接下来我|下面我|现在我|让我先|我先来|我来看|稍等)"
)

def _is_process_narration(text, role):
    """启发式：识别助手的『过程旁白』短消息，避免它们污染飞书归档。
    仅作用于 assistant，且只在短消息上生效，长内容（结论）不动。"""
    if role != "assistant" or not text:
        return False
    t = text.strip()
    if len(t) > 60:                          # 偏长 → 视为有内容，放行
        return False
    if len(t) <= 40 and t.endswith(("：", ":")):   # 短句以冒号收尾 → 多为「我接下来要…」引子
        return True
    return bool(_PROC_CUE_RE.search(t))

def _extract_text(content, skip_tool_use=True, skip_tool_result=True, tr_limit=300):
    """提取文本。tr_limit 控制工具结果截断长度，None 表示完整保留（用于 Markdown 导出）。"""
    def _cut(s):
        return s if tr_limit is None else s[:tr_limit]
    if isinstance(content, str):  return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict): continue
            t = b.get("type","")
            if t == "text":
                parts.append(b.get("text","").strip())
            elif t == "tool_use" and not skip_tool_use:
                parts.append(f"[工具调用: {b.get('name','')}]")
            elif t == "tool_result" and not skip_tool_result:
                inner = b.get("content","")
                if isinstance(inner, list):
                    for x in inner:
                        if isinstance(x,dict) and x.get("type")=="text":
                            parts.append(f"[工具结果: {_cut(x.get('text',''))}]")
                elif isinstance(inner, str):
                    parts.append(f"[工具结果: {_cut(inner)}]")
        return "\n".join(p for p in parts if p)
    return ""

def parse_jsonl(path, session_id, cwd):
    cfg              = db_get_config()
    filter_roles     = cfg.get("filter_roles", "both")
    skip_tool_use    = cfg.get("filter_tool_use",    "1") == "1"
    skip_tool_result = cfg.get("filter_tool_result", "1") == "1"
    skip_sidechain   = cfg.get("filter_sidechain",   "1") == "1"
    skip_filler      = cfg.get("filter_short",       "1") == "1"
    skip_process     = cfg.get("filter_process",     "1") == "1"
    msgs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: obj = json.loads(line)
                except Exception: continue
                if obj.get("type") not in ("user","assistant"): continue
                if skip_sidechain and obj.get("isSidechain"): continue
                m    = obj.get("message", {})
                role = m.get("role", obj.get("type",""))
                if filter_roles == "user"      and role != "user":      continue
                if filter_roles == "assistant" and role != "assistant": continue
                text = _extract_text(m.get("content",""), skip_tool_use, skip_tool_result)
                text = _clean_noise(text)
                if not text: continue
                if _is_command_message(text): continue          # 斜杠命令/本地命令输出/caveat 恒过滤
                if skip_filler and _is_filler(text): continue
                if skip_process and _is_process_narration(text, role): continue  # 助手过程旁白
                uuid   = obj.get("uuid","")
                ts_str = obj.get("timestamp","")
                ts     = int(time.time())
                if ts_str:
                    try:
                        dt = datetime.datetime.fromisoformat(ts_str.replace("Z","+00:00"))
                        ts = int(dt.timestamp())
                    except Exception: pass
                msgs.append({"uuid":uuid,"role":role,"content":text,"ts":ts,"session_id":session_id,"cwd":cwd})
    except Exception as e:
        add_log("ERROR", f"解析会话文件失败 {os.path.basename(path)}: {e}")
    return msgs

def find_all_claude_projects():
    found = []
    candidates = []

    if platform.system() == "Windows":
        users_root = os.path.join(os.environ.get("SystemDrive", "C:"), os.sep, "Users")
        if os.path.isdir(users_root):
            for name in os.listdir(users_root):
                candidates.append(os.path.join(users_root, name, ".claude", "projects"))
        for env in ("USERPROFILE",):
            v = os.environ.get(env)
            if v: candidates.append(os.path.join(v, ".claude", "projects"))
    else:
        for base in ("/Users", "/home"):
            if os.path.isdir(base):
                for name in os.listdir(base):
                    candidates.append(os.path.join(base, name, ".claude", "projects"))
        candidates.append("/root/.claude/projects")

    seen = set()
    for p in candidates:
        p = os.path.normpath(p)
        if p in seen or not os.path.isdir(p): continue
        seen.add(p)
        if p != os.path.normpath(CLAUDE_PROJECTS):
            found.append(p)
    return found

def scan_file_for_new(transcript_path, session_id, cwd):
    msgs     = parse_jsonl(transcript_path, session_id, cwd)
    new_msgs = [m for m in msgs if m["uuid"] and not is_uuid_synced(m["uuid"])]
    if new_msgs: enqueue_records(new_msgs)
    return len(new_msgs)

def scan_all_history(progress_cb=None, paths=None):
    if paths is None: paths = get_all_scan_paths()
    all_files = []
    for base in paths:
        if not os.path.exists(base): continue
        for root, _, files in os.walk(base):
            for f in files:
                if f.endswith(".jsonl"):
                    all_files.append((os.path.join(root, f), base))
    total = 0
    for i, (fp, base) in enumerate(all_files):
        sid = os.path.splitext(os.path.basename(fp))[0]
        cnt = scan_file_for_new(fp, sid, os.path.dirname(fp))
        total += cnt
        if progress_cb: progress_cb(i+1, len(all_files), total)
    return total

# ──────────────────────────────────────────────────
# 会话导出为 Markdown（每个会话一个 .md 文件）
# ──────────────────────────────────────────────────
def _safe_filename(s, maxlen=80):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = (s[:maxlen]).strip(" .")
    return s or "conversation"

def _fmt_ts(ts_str, fmt="%Y-%m-%d %H:%M:%S"):
    if not ts_str: return ""
    try:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).strftime(fmt)
    except Exception:
        return ts_str

def _parse_for_export(path):
    """为 Markdown 导出解析单个会话：原样保留全部内容，不做任何过滤
    （含工具调用、工具结果、子代理、命令等），仅跳过完全空白的消息。"""
    msgs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: obj = json.loads(line)
                except Exception: continue
                if obj.get("type") not in ("user", "assistant"): continue
                m    = obj.get("message", {})
                role = m.get("role", obj.get("type", ""))
                # 不过滤：保留工具调用/结果，工具结果不截断
                text = _extract_text(m.get("content", ""),
                                     skip_tool_use=False, skip_tool_result=False, tr_limit=None)
                if not text: continue
                msgs.append((role, text, obj.get("timestamp", "")))
    except Exception as e:
        add_log("ERROR", f"导出解析失败 {os.path.basename(path)}: {e}")
    return msgs

def _render_md(session_id, project, msgs):
    times = [t for _, _, t in msgs if t]
    lines = [f"# 会话记录 · {project or '未知项目'}", ""]
    lines.append(f"- 会话 ID：`{session_id}`")
    lines.append(f"- 项目：{project or '—'}")
    lines.append(f"- 消息数：{len(msgs)}")
    if times:
        lines.append(f"- 时间：{_fmt_ts(times[0], '%Y-%m-%d %H:%M')} ~ {_fmt_ts(times[-1], '%Y-%m-%d %H:%M')}")
    lines += ["", "---", ""]
    for role, text, ts in msgs:
        who  = "🧑 我" if role == "user" else "🤖 Claude"
        tstr = _fmt_ts(ts, "%H:%M:%S")
        lines.append(f"## {who}" + (f"  ·  {tstr}" if tstr else ""))
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)

def export_all_to_md(out_dir, paths=None, progress_cb=None):
    """把所有会话 JSONL 导出为 Markdown，一个会话一个文件。返回 (写出文件数, 扫描文件数)。"""
    if paths is None: paths = get_all_scan_paths()
    files = []
    for base in paths:
        if not os.path.exists(base): continue
        for root, _, fs in os.walk(base):
            for fn in fs:
                if fn.endswith(".jsonl"):
                    files.append(os.path.join(root, fn))
    os.makedirs(out_dir, exist_ok=True)
    written = 0; used = set()
    for i, fp in enumerate(files):
        msgs = _parse_for_export(fp)
        if msgs:
            sid  = os.path.splitext(os.path.basename(fp))[0]
            proj = os.path.basename(os.path.dirname(fp))
            datestr = ""
            for _, _, ts in msgs:
                d = _fmt_ts(ts, "%Y%m%d")
                if d: datestr = d; break
            first_user = next((t for r, t, _ in msgs if r == "user"), msgs[0][1])
            title = _safe_filename(first_user.splitlines()[0], 40)
            name  = f"{(datestr + '_') if datestr else ''}{_safe_filename(proj, 20)}_{title}_{sid[:8]}.md"
            name  = _safe_filename(name, 120) + ".md" if not name.endswith(".md") else _safe_filename(name[:-3], 120) + ".md"
            base_name, k = name, 1
            while name in used:
                name = f"{base_name[:-3]}_{k}.md"; k += 1
            used.add(name)
            try:
                with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                    f.write(_render_md(sid, proj, msgs))
                written += 1
            except Exception as e:
                add_log("ERROR", f"导出写入失败 {name}: {e}")
        if progress_cb: progress_cb(i + 1, len(files), written)
    return written, len(files)

# ──────────────────────────────────────────────────
# Hook：进程内触发（不再依赖外部 Python，exe 直接以 --hook 调用自身）
# ──────────────────────────────────────────────────
def _read_stdin_payload():
    """读取 Hook 负载。打包成 --windowed exe 时 sys.stdin 为 None，需直接读文件描述符 0。"""
    if sys.stdin is not None:
        try:
            data = sys.stdin.read()
            if data: return data
        except Exception:
            pass
    try:
        chunks = []
        while True:
            b = os.read(0, 65536)
            if not b: break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")
    except Exception:
        return ""

def run_hook():
    """以 --hook 模式运行：读取 stdin 的 Claude Code Hook 负载，增量解析会话文件并入队。"""
    try:
        hook = json.loads(_read_stdin_payload())
    except Exception:
        return
    transcript = hook.get("transcript_path", "")
    session_id = hook.get("session_id", "unknown")
    cwd        = hook.get("cwd", "")
    if not transcript or not os.path.exists(transcript): return
    if not os.path.exists(DB_PATH): return

    cfg              = db_get_config()
    filter_roles     = cfg.get("filter_roles", "both")
    skip_tool_use    = cfg.get("filter_tool_use",    "1") == "1"
    skip_tool_result = cfg.get("filter_tool_result", "1") == "1"
    skip_sidechain   = cfg.get("filter_sidechain",   "1") == "1"
    skip_filler      = cfg.get("filter_short",       "1") == "1"
    skip_process     = cfg.get("filter_process",     "1") == "1"

    # 增量读取：从上次记录的偏移开始，避免每轮重扫整个文件
    offset = get_file_offset(transcript)
    try:
        if offset > os.path.getsize(transcript):  # 文件被轮转/截断
            offset = 0
    except OSError:
        return

    now = int(time.time()); rows = []; new_offset = offset
    try:
        # 二进制模式：按完整行（以 \n 结尾）推进偏移，避免读到 Claude 仍在写入的半行
        with open(transcript, "rb") as f:
            f.seek(offset)
            pos = offset
            while True:
                raw = f.readline()
                if not raw: break
                if not raw.endswith(b"\n"): break   # 末尾不完整行，留待下次
                pos += len(raw)
                s = raw.decode("utf-8", "replace").strip()
                if not s: continue
                try: obj = json.loads(s)
                except Exception: continue
                if obj.get("type") not in ("user", "assistant"): continue
                if skip_sidechain and obj.get("isSidechain"): continue
                m    = obj.get("message", {})
                role = m.get("role", obj.get("type", ""))
                if filter_roles == "user"      and role != "user":      continue
                if filter_roles == "assistant" and role != "assistant": continue
                text = _extract_text(m.get("content", ""), skip_tool_use, skip_tool_result)
                text = _clean_noise(text)
                if not text: continue
                if _is_command_message(text): continue          # 斜杠命令/本地命令输出/caveat 恒过滤
                if skip_filler and _is_filler(text): continue
                if skip_process and _is_process_narration(text, role): continue  # 助手过程旁白
                uuid = obj.get("uuid", "")
                if not uuid: continue
                ts_str = obj.get("timestamp", ""); ts = now
                if ts_str:
                    try:
                        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts = int(dt.timestamp())
                    except Exception: pass
                rows.append((session_id, cwd, role, text[:9000], ts, uuid, now))
            new_offset = pos
    except Exception:
        return

    if rows:
        try:
            conn = _connect()
            conn.executemany(
                "INSERT OR IGNORE INTO upload_queue (session_id,cwd,role,content,timestamp,uuid,created_at) VALUES (?,?,?,?,?,?,?)",
                rows)
            conn.commit(); conn.close()
        except Exception:
            return  # 入队失败则不推进偏移，下次重试
    set_file_offset(transcript, new_offset)

# Hook 命令构建与识别
def _hook_command():
    exe = sys.executable
    if getattr(sys, "frozen", False):
        return f'"{exe}" --hook'                                  # 打包后：调用 exe 自身，零 Python 依赖
    return f'"{exe}" "{os.path.abspath(__file__)}" --hook'        # 源码运行：python 主程序 --hook

def _is_our_hook(h):
    if h.get("_feishu_sync"): return True                         # 本程序注册标记
    cmd = (h.get("command", "") or "")
    if HOOK_SCRIPT and HOOK_SCRIPT in cmd: return True            # 旧版独立脚本，兼容卸载
    if "--hook" in cmd and "feishu" in cmd.lower(): return True
    return False

def write_hook_script():
    """旧版会生成独立脚本，现已改为进程内 --hook，保留空实现以兼容调用点。"""
    return

def register_hook():
    settings_dir = os.path.dirname(CLAUDE_SETTINGS)
    os.makedirs(settings_dir, exist_ok=True)
    settings = {}
    if os.path.exists(CLAUDE_SETTINGS):
        try:
            with open(CLAUDE_SETTINGS, encoding="utf-8") as f: settings = json.load(f)
        except Exception: settings = {}
    hooks      = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    # 已存在则更新命令（例如从旧的 python 脚本方式迁移到 --hook）
    for grp in stop_hooks:
        for h in grp.get("hooks", []):
            if _is_our_hook(h):
                h["command"] = _hook_command(); h["_feishu_sync"] = True
                with open(CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
                    json.dump(settings, f, ensure_ascii=False, indent=2)
                return True, "Hook 已更新为最新方式 ✓ 请重启 Claude Code"
    stop_hooks.append({"hooks": [{"type": "command", "command": _hook_command(), "_feishu_sync": True}]})
    with open(CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return True, "Hook 注册成功 ✓ 请重启 Claude Code"

def unregister_hook():
    if not os.path.exists(CLAUDE_SETTINGS): return True, "配置文件不存在，无需操作"
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f: settings = json.load(f)
    except Exception: return False, "读取配置文件失败"
    hooks      = settings.get("hooks", {})
    stop_hooks = hooks.get("Stop", [])
    new_stop   = []; removed = 0
    for grp in stop_hooks:
        inner = [h for h in grp.get("hooks", []) if not _is_our_hook(h)]
        if len(inner) < len(grp.get("hooks", [])): removed += 1
        if inner: new_stop.append({**grp, "hooks": inner})
    if removed == 0: return True, "未找到已注册的 Hook"
    hooks["Stop"]     = new_stop
    settings["hooks"] = hooks
    with open(CLAUDE_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return True, "Hook 成功移除"

# ──────────────────────────────────────────────────
# 诊断
# ──────────────────────────────────────────────────
def run_health_check(cfg):
    results = []
    ok_claude = os.path.exists(CLAUDE_PROJECTS)
    results.append(("✅" if ok_claude else "❌", "Claude 对话目录", CLAUDE_PROJECTS if ok_claude else "未找到"))
    mode = "打包模式(exe 自身)" if getattr(sys, "frozen", False) else "源码模式(python)"
    results.append(("✅", "Hook 触发方式", f"进程内 --hook，{mode}"))
    registered = False
    if os.path.exists(CLAUDE_SETTINGS):
        try:
            with open(CLAUDE_SETTINGS) as f: s = json.load(f)
            for g in s.get("hooks",{}).get("Stop",[]):
                for h in g.get("hooks",[]):
                    if _is_our_hook(h): registered = True
        except Exception: pass
    results.append(("✅" if registered else "❌", "Hook 已注册", "是" if registered else "否（请注册 Hook）"))
    app_id = cfg.get("app_id"); app_secret = cfg.get("app_secret")
    if app_id and app_secret:
        ok, msg = test_connection(app_id, app_secret)
        results.append(("✅" if ok else "❌", "飞书 API 连接", msg))
    else:
        results.append(("⚠️", "飞书 API 连接", "未配置凭证"))
    app_token = cfg.get("app_token"); table_id = cfg.get("table_id")
    if all([app_id, app_secret, app_token, table_id]):
        ok, msg = verify_table_access(app_id, app_secret, app_token, table_id)
        results.append(("✅" if ok else "❌", "多维表格权限", msg))
    else:
        results.append(("⚠️", "多维表格权限", "未配置表格信息"))
    stats   = get_queue_stats()
    pending = stats.get("pending",0); failed = stats.get("failed",0); done = stats.get("done",0)
    results.append(("✅", "队列状态", f"待上传 {pending} | 成功 {done} | 失败 {failed}"))
    return results

# ──────────────────────────────────────────────────
# 后台同步引擎
# ──────────────────────────────────────────────────
class SyncEngine:
    def __init__(self, status_cb=None, log_cb=None):
        self.status_cb  = status_cb or (lambda **kw: None)
        self.log_cb     = log_cb    or (lambda msg, level="INFO": None)
        self._running   = False
        self._thread    = None
        self._total     = int(db_get("total_synced", 0) or 0)
        self._last_time = db_get("last_sync_time", "从未") or "从未"

    @property
    def running(self): return self._running

    def start(self):
        if self._running: return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="SyncEngine")
        self._thread.start()
        self._log("同步引擎已启动")

    def stop(self):
        self._running = False
        self._log("同步引擎已停止")

    def _log(self, msg, level="INFO"):
        add_log(level, msg)
        self.log_cb(msg, level)

    def _run(self):
        while self._running:
            try: self._process()
            except Exception as e: self._log(f"引擎异常: {e}", "ERROR")
            for _ in range(10):
                if not self._running: break
                time.sleep(1)

    def _process(self):
        cfg = db_get_config()
        if not all(cfg.get(k) for k in ("app_id","app_secret","app_token","table_id")): return
        records = get_pending_records(50)
        if not records: return
        self._log(f"开始上传 {len(records)} 条记录")
        for i in range(0, len(records), 50):
            batch = records[i:i+50]
            try:
                ok_ids, fail_ids = batch_create_records(
                    cfg["app_id"], cfg["app_secret"],
                    cfg["app_token"], cfg["table_id"], batch
                )
                if ok_ids:
                    mark_records_status(ok_ids, "done")
                    for r in batch:
                        if r[0] in ok_ids: mark_uuid_synced(r[6], r[1])
                    self._total    += len(ok_ids)
                    self._last_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    db_set("total_synced",   self._total)
                    db_set("last_sync_time", self._last_time)
                    self._log(f"成功上传 {len(ok_ids)} 条 | 累计 {self._total} 条")
                    self.status_cb(total=self._total, last_time=self._last_time, queue=get_queue_stats())
                if fail_ids:
                    increment_retry(fail_ids)
                    self._log(f"{len(fail_ids)} 条写入失败，进入重试队列", "WARN")
                time.sleep(0.5)
            except Exception as e:
                self._log(f"批次同步出错: {e}", "ERROR")
                increment_retry([r[0] for r in batch])
                time.sleep(5)

# ──────────────────────────────────────────────────
# 现代 UI 统一样式助手
# ──────────────────────────────────────────────────
class Theme:
    # 调色板
    PRIMARY      = "#10B981"  # 飞书绿/翡翠绿
    PRIMARY_HOVER= "#059669"
    BG_DARK      = "#111827"  # 顶级深蓝灰 (标题栏)
    BG_LIGHT     = "#F9FAFB"  # 极淡底色
    CARD_BG      = "#FFFFFF"  # 白卡片
    TEXT_MAIN    = "#1F2937"  # 主要文字
    TEXT_MUTED   = "#6B7280"  # 辅助灰色
    BORDER       = "#E5E7EB"  # 浅灰框
    
    # 状态色
    SUCCESS      = "#10B981"
    WARNING      = "#F59E0B"
    DANGER       = "#EF4444"

    # 字体
    FONT_FAMILY  = "Microsoft YaHei UI" if platform.system() == "Windows" else "PingFang SC"
    
    @classmethod
    def apply(cls, root):
        """配置 ttk 统一样式类"""
        style = ttk.Style()
        style.theme_use('clam')
        
        # 统配基础背景
        style.configure('.', font=(cls.FONT_FAMILY, 10), background=cls.BG_LIGHT, foreground=cls.TEXT_MAIN)
        
        # TNotebook (选项卡) 现代化极简扁平样式
        style.configure('TNotebook', background=cls.BG_LIGHT, borderwidth=0)
        style.configure('TNotebook.Tab', font=(cls.FONT_FAMILY, 10), padding=(18, 6),
                        background=cls.BG_LIGHT, foreground=cls.TEXT_MUTED, borderwidth=1, bordercolor=cls.BORDER)
        style.map('TNotebook.Tab',
                  background=[('selected', '#FFFFFF')],
                  foreground=[('selected', cls.PRIMARY)],
                  lightcolor=[('selected', cls.PRIMARY)])

        # TButton 现代扁平按钮样式
        style.configure('TButton', font=(cls.FONT_FAMILY, 10, "bold"), padding=(12, 6),
                        background=cls.PRIMARY, foreground='white', borderwidth=0)
        style.map('TButton',
                  background=[('active', cls.PRIMARY_HOVER), ('disabled', '#D1D5DB')],
                  foreground=[('disabled', '#9CA3AF')])
        
        # Secondary.TButton 描边幽灵按钮
        style.configure('Secondary.TButton', font=(cls.FONT_FAMILY, 10), padding=(12, 6),
                        background='#E5E7EB', foreground=cls.TEXT_MAIN, borderwidth=0)
        style.map('Secondary.TButton',
                  background=[('active', '#D1D5DB')],
                  foreground=[('active', cls.TEXT_MAIN)])

        # TEntry 输入框
        style.configure('TEntry', fieldbackground='white', bordercolor=cls.BORDER, lightcolor=cls.BORDER, darkcolor=cls.BORDER, borderwidth=1)
        
        # TCheckbutton / TRadiobutton 浅色适配
        style.configure('TCheckbutton', background=cls.BG_LIGHT)
        style.configure('TRadiobutton', background=cls.BG_LIGHT)
        
        # TLabeledFrame 标签框架适配
        style.configure('TLabelframe', background=cls.BG_LIGHT, bordercolor=cls.BORDER, borderwidth=1)
        style.configure('TLabelframe.Label', background=cls.BG_LIGHT, foreground=cls.TEXT_MAIN, font=(cls.FONT_FAMILY, 10, "bold"))
        
        # TProgressbar
        style.configure('TProgressbar', background=cls.PRIMARY, borderwidth=0, troughcolor='#E5E7EB')

# ──────────────────────────────────────────────────
# 设置向导 (重构为极现代的引导对话框)
# ──────────────────────────────────────────────────
class SetupWizard(tk.Toplevel):
    STEPS = ["欢迎使用", "飞书凭证配置", "数据表格绑定", "自动化配置", "完成部署"]

    def __init__(self, parent, on_complete=None):
        super().__init__(parent)
        self.title("同步助手 — 首次配置向导")
        self.geometry("640x550")
        self.resizable(False, False)
        self.grab_set()
        self.focus_set()
        self.on_complete = on_complete
        self.step_index  = 0
        self.data        = {}
        
        self.configure(bg=Theme.BG_LIGHT)
        self._build()
        self._show(0)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        # 顶部极简深色 Banner 替代以往的粗橙色快
        banner = tk.Frame(self, bg=Theme.BG_DARK, height=60)
        banner.pack(fill=tk.X)
        banner.pack_propagate(False)
        tk.Label(banner, text="首次使用引导配置", bg=Theme.BG_DARK, fg="white",
                 font=(Theme.FONT_FAMILY, 14, "bold")).pack(side=tk.LEFT, padx=24, pady=15)
        
        # 步骤状态追踪轴
        self.nav_frame = tk.Frame(self, bg="#FFFFFF", bd=0, height=50)
        self.nav_frame.pack(fill=tk.X)
        self.step_labels = []
        for i, s in enumerate(self.STEPS):
            lbl = tk.Label(self.nav_frame, text=s, bg="#FFFFFF", fg=Theme.TEXT_MUTED, font=(Theme.FONT_FAMILY, 9))
            lbl.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
            self.step_labels.append(lbl)

        # 分割线
        sep = tk.Frame(self, bg=Theme.BORDER, height=1)
        sep.pack(fill=tk.X)

        # 内容面板主体
        self.content = tk.Frame(self, padx=36, pady=24, bg=Theme.BG_LIGHT)
        self.content.pack(fill=tk.BOTH, expand=True)
        
        # 底部控制区（固定高度、固定位置）
        b_sep = tk.Frame(self, bg=Theme.BORDER, height=1)
        b_sep.pack(fill=tk.X)

        bf = tk.Frame(self, padx=24, pady=16, bg="#FFFFFF")
        bf.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.btn_back = ttk.Button(bf, text="← 上一步", style="Secondary.TButton", command=self._back)
        self.btn_back.pack(side=tk.LEFT)
        
        self.msg_lbl = tk.Label(bf, text="", font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MUTED, bg="#FFFFFF")
        self.msg_lbl.pack(side=tk.LEFT, padx=12)
        
        self.btn_next = ttk.Button(bf, text="下一步 →", command=self._next)
        self.btn_next.pack(side=tk.RIGHT)

    def _update_nav(self):
        for i, lbl in enumerate(self.step_labels):
            if i < self.step_index:
                lbl.config(fg=Theme.PRIMARY, font=(Theme.FONT_FAMILY, 9, "bold"))
            elif i == self.step_index:
                lbl.config(fg=Theme.PRIMARY_HOVER, font=(Theme.FONT_FAMILY, 9, "bold"))
            else:
                lbl.config(fg=Theme.TEXT_MUTED, font=(Theme.FONT_FAMILY, 9))

    def _clear(self):
        for w in self.content.winfo_children(): w.destroy()
        self.msg_lbl.config(text="")

    def _show(self, idx):
        self.step_index = idx
        self._update_nav()
        self._clear()
        self.btn_back.config(state=tk.NORMAL if idx > 0 else tk.DISABLED)
        self.btn_next.config(text="开启同步 ✓" if idx == 4 else "下一步 →")
        
        # 实例化步骤页面
        [self._s0, self._s1, self._s2, self._s3, self._s4][idx]()

    def _msg(self, text, ok=True):
        self.msg_lbl.config(text=text, fg=Theme.SUCCESS if ok else Theme.DANGER)

    # ── Step 0: 欢迎 ──
    def _s0(self):
        tk.Label(self.content, text="👋 欢迎使用 Claude Code 同步助手", font=(Theme.FONT_FAMILY, 15, "bold"),
                 bg=Theme.BG_LIGHT, fg=Theme.TEXT_MAIN).pack(anchor="w", pady=(0, 16))
        
        intro_text = (
            "本同步助手能帮助您实现终端 AI 工具 Claude Code 与飞书的高效协同：\n\n"
            "•  🛡️ 极速同步：Claude Code 每一轮对话结束后无感秒级入队。\n"
            "•  📈 集中化归档：支持自动一键导入或回填全部历史对话记录。\n"
            "•  🤖 后台静默运行：后台独立引擎推送，无任何命令行卡顿或性能损耗。\n"
            "•  🔑 纯净自建环境：使用您自己注册的免费飞书应用，数据更安全。"
        )
        tk.Label(self.content, text=intro_text, font=(Theme.FONT_FAMILY, 11), fg=Theme.TEXT_MUTED,
                 bg=Theme.BG_LIGHT, justify=tk.LEFT, anchor="w", wraplength=550).pack(fill=tk.X)

    # ── Step 1: 凭证 ──
    def _s1(self):
        tk.Label(self.content, text="第 1 步：配置飞书凭证", font=(Theme.FONT_FAMILY, 13, "bold"),
                 bg=Theme.BG_LIGHT, fg=Theme.TEXT_MAIN).pack(anchor="w", pady=(0, 10))
        
        # 教程超链接引导区
        guide_frame = tk.Frame(self.content, bg=Theme.BG_LIGHT)
        guide_frame.pack(anchor="w", pady=(0, 8))
        tk.Label(guide_frame, text="📖 第一次用？点这里打开：", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT).pack(side=tk.LEFT)
        lnk = tk.Label(guide_frame, text="飞书开放平台 open.feishu.cn", fg=Theme.PRIMARY, cursor="hand2",
                       font=(Theme.FONT_FAMILY, 10, "underline"), bg=Theme.BG_LIGHT)
        lnk.pack(side=tk.LEFT)
        lnk.bind("<Button-1>", lambda e: webbrowser.open("https://open.feishu.cn/app"))

        steps = (
            "① 登录后点「创建企业自建应用」，填个名称（如 Claude 同步），头像可留空 → 创建\n"
            "② 左侧「权限管理」→ 搜索 bitable → 勾选「查看、评论、编辑和管理多维表格」\n"
            "③ 顶部「版本管理与发布」→ 创建版本 → 申请发布（个人/管理员一键通过）\n"
            "④ 左侧「凭证与基础信息」→ 复制 App ID 和 App Secret，粘贴到下方："
        )
        tk.Label(self.content, text=steps, font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MAIN,
                 bg=Theme.BG_LIGHT, justify=tk.LEFT, anchor="w", wraplength=560).pack(anchor="w", pady=(0,12))

        # 表单
        self._v = {}
        for lbl, key, is_pwd in [("App ID:", "app_id", False), ("App Secret:", "app_secret", True)]:
            row = tk.Frame(self.content, bg=Theme.BG_LIGHT)
            row.pack(fill=tk.X, pady=6)
            tk.Label(row, text=lbl, width=14, anchor="w", bg=Theme.BG_LIGHT, font=(Theme.FONT_FAMILY, 10, "bold")).pack(side=tk.LEFT)
            
            v = tk.StringVar(value=self.data.get(key, ""))
            self._v[key] = v
            ent = ttk.Entry(row, textvariable=v, show="•" if is_pwd else "", width=42, font=(Theme.FONT_FAMILY, 10))
            ent.pack(side=tk.LEFT)

        test_row = tk.Frame(self.content, bg=Theme.BG_LIGHT)
        test_row.pack(anchor="w", pady=16)
        ttk.Button(test_row, text="🧪 连通性测试", command=self._test_conn).pack(side=tk.LEFT)
        self._conn_lbl = tk.Label(test_row, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT)
        self._conn_lbl.pack(side=tk.LEFT, padx=12)

    def _test_conn(self):
        ai = self._v["app_id"].get().strip(); ase = self._v["app_secret"].get().strip()
        if not ai or not ase:
            self._conn_lbl.config(text="❌ 请同时填写 App ID 和 Secret", fg=Theme.DANGER)
            return
        self._conn_lbl.config(text="正在连接飞书服务器...", fg=Theme.TEXT_MUTED); self.update()
        def _do():
            ok, msg = test_connection(ai, ase)
            self.after(0, lambda: self._conn_lbl.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER))
        threading.Thread(target=_do, daemon=True).start()

    # ── Step 2: 表格 ──
    def _s2(self):
        tk.Label(self.content, text="第 2 步：绑定飞书多维表格", font=(Theme.FONT_FAMILY, 13, "bold"),
                 bg=Theme.BG_LIGHT, fg=Theme.TEXT_MAIN).pack(anchor="w", pady=(0, 10))
        
        self._tmode = tk.StringVar(value=self.data.get("tmode", "auto"))

        # 选项
        ttk.Radiobutton(self.content, text="✨ 极智自动建表 (推荐，一键完成标准字段建立)",
                        variable=self._tmode, value="auto", command=self._tog).pack(anchor="w", pady=4)
        ttk.Radiobutton(self.content, text="📝 手动填写已有数据表",
                        variable=self._tmode, value="manual", command=self._tog).pack(anchor="w", pady=4)

        # 自动面板
        self._af = tk.Frame(self.content, bg=Theme.BG_LIGHT)
        hint_lbl = tk.Label(self._af, text="💡 自动创建的表格归你的应用所有，应用本身就有读写权限，无需任何额外授权步骤。\n创建后会自动校验连通性，并给出表格链接方便你查看。",
                            font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MUTED, bg=Theme.BG_LIGHT, justify=tk.LEFT)
        hint_lbl.pack(anchor="w", pady=(8, 12))
        
        act_row = tk.Frame(self._af, bg=Theme.BG_LIGHT)
        act_row.pack(anchor="w")
        ttk.Button(act_row, text="🪄 一键自动建表", command=self._do_create).pack(side=tk.LEFT)
        self._create_lbl = tk.Label(act_row, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT)
        self._create_lbl.pack(side=tk.LEFT, padx=12)
        
        self._create_detail = tk.Label(self._af, text="", font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MUTED, bg=Theme.BG_LIGHT, justify=tk.LEFT)
        self._create_detail.pack(anchor="w", pady=10)

        # 手动面板
        self._mf = tk.Frame(self.content, bg=Theme.BG_LIGHT)
        man_guide = tk.Label(self._mf, justify=tk.LEFT, anchor="w", wraplength=560,
            font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MAIN, bg=Theme.BG_LIGHT,
            text=("使用已有多维表格时，从浏览器地址栏复制两段 ID：\n"
                  "  feishu.cn/base/【App Token】?table=【Table ID】&view=...\n"
                  "  例：.../base/Xab1...cdE?table=tblYyZ...  → 前段填 App Token，table= 后面填 Table ID"))
        man_guide.pack(anchor="w", pady=(8, 6))
        self._mv = {}
        for lbl, key in [("App Token:", "app_token"), ("Table ID:", "table_id")]:
            row = tk.Frame(self._mf, bg=Theme.BG_LIGHT)
            row.pack(fill=tk.X, pady=6)
            tk.Label(row, text=lbl, width=14, anchor="w", bg=Theme.BG_LIGHT, font=(Theme.FONT_FAMILY, 10, "bold")).pack(side=tk.LEFT)

            v = tk.StringVar(value=self.data.get(key, ""))
            self._mv[key] = v
            ttk.Entry(row, textvariable=v, width=38, font=(Theme.FONT_FAMILY, 10)).pack(side=tk.LEFT)

        man_hint = tk.Label(self._mf, text="⚠️ 已有表格需在表格右上角「···」→「···更多」→「添加文档应用」中授权你的自建应用", font=(Theme.FONT_FAMILY, 9), fg=Theme.WARNING, bg=Theme.BG_LIGHT, wraplength=560, justify=tk.LEFT)
        man_hint.pack(anchor="w", pady=8)
        
        vf_row = tk.Frame(self._mf, bg=Theme.BG_LIGHT)
        vf_row.pack(anchor="w")
        ttk.Button(vf_row, text="🔐 校验表权限", command=self._verify_table).pack(side=tk.LEFT)
        self._verify_lbl = tk.Label(vf_row, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT)
        self._verify_lbl.pack(side=tk.LEFT, padx=12)

        self._tog()

    def _tog(self):
        if self._tmode.get() == "auto":
            self._af.pack(fill=tk.X, pady=10); self._mf.pack_forget()
        else:
            self._mf.pack(fill=tk.X, pady=10); self._af.pack_forget()

    def _do_create(self):
        ai = self.data.get("app_id", ""); ase = self.data.get("app_secret", "")
        if not ai or not ase:
            self._create_lbl.config(text="❌ 请先退回上一步配置自建应用凭证", fg=Theme.DANGER); return
        self._create_lbl.config(text="正在创建表格...", fg=Theme.TEXT_MUTED); self.update()
        def _do():
            at, tid, err = auto_create_table(ai, ase)
            # 建表成功后立即自检读写权限（应用自建的表本就有权限，无需手动授权）
            verified = False
            if not err:
                verified, _vmsg = verify_table_access(ai, ase, at, tid)
            def _upd():
                if err:
                    self._create_lbl.config(text="❌ 表格创建失败", fg=Theme.DANGER)
                    self._create_detail.config(text=f"{err}")
                else:
                    self.data["app_token"] = at; self.data["table_id"] = tid
                    url = f"https://feishu.cn/base/{at}"
                    if verified:
                        self._create_lbl.config(text="✅ 创建成功，已就绪（无需额外授权）", fg=Theme.SUCCESS)
                    else:
                        self._create_lbl.config(text="✅ 创建成功", fg=Theme.WARNING)
                    self._create_detail.config(
                        text=f"App Token: {at}\nTable ID: {tid}\n表格链接: {url}\n\n可直接点「下一步」。")
                    # 提供可点击链接打开表格
                    if not getattr(self, "_open_link", None):
                        self._open_link = tk.Label(self._af, text="🔗 在浏览器中打开此表格",
                                                   fg=Theme.PRIMARY, cursor="hand2",
                                                   font=(Theme.FONT_FAMILY, 9, "underline"), bg=Theme.BG_LIGHT)
                        self._open_link.pack(anchor="w")
                    self._open_link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            self.after(0, _upd)
        threading.Thread(target=_do, daemon=True).start()

    def _verify_table(self):
        ai = self.data.get("app_id",""); ase = self.data.get("app_secret","")
        at = self._mv["app_token"].get().strip(); tid = self._mv["table_id"].get().strip()
        if not all([ai,ase,at,tid]):
            self._verify_lbl.config(text="❌ 配置项存在残缺", fg=Theme.DANGER); return
        self._verify_lbl.config(text="正在检验飞书对接校验码...", fg=Theme.TEXT_MUTED); self.update()
        def _do():
            ok, msg = verify_table_access(ai, ase, at, tid)
            self.after(0, lambda: self._verify_lbl.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER))
        threading.Thread(target=_do, daemon=True).start()

    # ── Step 3: Hook ──
    def _s3(self):
        tk.Label(self.content, text="第 3 步：注册自动化 Hook", font=(Theme.FONT_FAMILY, 13, "bold"),
                 bg=Theme.BG_LIGHT, fg=Theme.TEXT_MAIN).pack(anchor="w", pady=(0, 10))
        
        intro_hook = (
            "Hook 系统可以让 Claude Code 每一轮对话结束时，自动把最新的对话文本缓存写入您的本机数据库队列，\n"
            "主程序会在后台定时推送这个队列。这种解耦方式可以确保哪怕在网络断开时也不会卡顿或丢单。\n"
        )
        tk.Label(self.content, text=intro_hook, font=(Theme.FONT_FAMILY, 10), fg=Theme.TEXT_MUTED,
                 bg=Theme.BG_LIGHT, justify=tk.LEFT, wraplength=550).pack(anchor="w", pady=(0, 16))
        
        btn_row = tk.Frame(self.content, bg=Theme.BG_LIGHT)
        btn_row.pack(anchor="w", pady=10)
        ttk.Button(btn_row, text="⚡ 快速注册 Hook", command=self._do_reg_hook).pack(side=tk.LEFT)
        self._hook_lbl = tk.Label(btn_row, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT)
        self._hook_lbl.pack(side=tk.LEFT, padx=12)

    def _do_reg_hook(self):
        write_hook_script()
        ok, msg = register_hook()
        self._hook_lbl.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER)

    # ── Step 4: 完成 ──
    def _s4(self):
        tk.Label(self.content, text="🎉 同步助手部署就绪！", font=(Theme.FONT_FAMILY, 16, "bold"),
                 bg=Theme.BG_LIGHT, fg=Theme.PRIMARY).pack(anchor="w", pady=(0, 16))
        
        con_text = (
            "系统已完成所有连接认证！\n\n"
            "•  💬 正常体验：您可以重启 Claude Code 进行自由对话，同步全程静默无感。\n"
            "•  🔄 历史迁移：可在主界面的「📥 历史回填」选项卡下批量回溯之前的所有历史记录。\n"
            "•  🔧 自主管理：您可以在「⚙️ 配置」中调整过滤策略（例如忽略工具链 Bash 执行日志）。"
        )
        tk.Label(self.content, text=con_text, font=(Theme.FONT_FAMILY, 11), fg=Theme.TEXT_MAIN,
                 bg=Theme.BG_LIGHT, justify=tk.LEFT, anchor="w", wraplength=550).pack(fill=tk.X)

    def _next(self):
        if self.step_index == 1:
            ai = self._v["app_id"].get().strip(); ase = self._v["app_secret"].get().strip()
            if not ai or not ase: self._msg("自建应用 ID 及 Secret 缺一不可", False); return
            self.data["app_id"] = ai; self.data["app_secret"] = ase
        elif self.step_index == 2:
            mode = self._tmode.get(); self.data["tmode"] = mode
            if mode == "manual":
                at  = self._mv["app_token"].get().strip()
                tid = self._mv["table_id"].get().strip()
                if not at or not tid: self._msg("多维表格 Token 及表格 ID 不能为空", False); return
                self.data["app_token"] = at; self.data["table_id"] = tid
            elif not self.data.get("app_token"):
                self._msg("请先点击建表按钮以获得云端表实例", False); return
        elif self.step_index == 4:
            self._finish(); return
        self._show(self.step_index + 1)

    def _back(self):
        if self.step_index > 0: self._show(self.step_index - 1)

    def _finish(self):
        for k,v in self.data.items():
            if k != "tmode" and v: db_set(k, v)
        db_set("setup_done","1")
        self.destroy()
        if self.on_complete: self.on_complete()

    def _close(self):
        if messagebox.askyesno("中断引导", "确定退出引导？配置可能未保存。", parent=self):
            self.destroy()

# ──────────────────────────────────────────────────
# 主窗口 (完全升级为极现代的 Flat-Modern 界面)
# ──────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"飞书多维表格同步助手 v{APP_VERSION}")
        self.geometry("780x620")
        self.minsize(700, 550)
        self.configure(bg=Theme.BG_LIGHT)
        
        init_db()
        cleanup_old_records()   # 清理 N 天前已同步记录，防止队列表无限膨胀
        Theme.apply(self)

        self.engine: SyncEngine = None
        self._tray = None

        self._build()
        self._refresh_stats()
        self._setup_tray()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._init_engine_or_wizard)
        self.after(12000, self._periodic)

    # ── 构建全局 UI ──
    def _build(self):
        # 极简扁平高贵深色标题栏 (2026风格)
        hdr = tk.Frame(self, bg=Theme.BG_DARK, height=75)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        
        tk.Label(hdr, text="  Claude Code  ✦  飞书同步助手",
                 bg=Theme.BG_DARK, fg="white", font=(Theme.FONT_FAMILY, 15, "bold")).pack(side=tk.LEFT, padx=16, pady=24)
        
        self._dot = tk.Label(hdr, text="●  未启动", bg=Theme.BG_DARK, fg=Theme.TEXT_MUTED, font=(Theme.FONT_FAMILY, 10, "bold"))
        self._dot.pack(side=tk.RIGHT, padx=24, pady=24)

        # 全局选项卡容器
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        
        self._tab_status(nb)
        self._tab_config(nb)
        self._tab_history(nb)
        self._tab_log(nb)

    # ── 状态标签页重构 ──
    def _tab_status(self, nb):
        f = tk.Frame(nb, padx=20, pady=20, bg=Theme.BG_LIGHT)
        nb.add(f, text="  📊 状态大屏  ")
        
        # 3 个流线现代化大卡片区
        cf = tk.Frame(f, bg=Theme.BG_LIGHT)
        cf.pack(fill=tk.X, pady=(0, 16))
        
        self._c_total   = self._card(cf, "累计同步消息",  "0 条",  Theme.PRIMARY)
        self._c_pending = self._card(cf, "本地待传队列",  "0 条",  Theme.WARNING)
        self._c_failed  = self._card(cf, "同步失败条目",  "0 条",  Theme.DANGER)
        for c in (self._c_total, self._c_pending, self._c_failed):
            c.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=6)

        # 同步元信息块
        info = ttk.LabelFrame(f, text=" 传输控制状态 ")
        info.pack(fill=tk.X, pady=(0, 16))
        
        inner_info = tk.Frame(info, bg=Theme.BG_LIGHT, padx=16, pady=12)
        inner_info.pack(fill=tk.X)
        
        self._lbl_last = tk.Label(inner_info, text="上次同步：从未", font=(Theme.FONT_FAMILY, 10), anchor="w", bg=Theme.BG_LIGHT)
        self._lbl_last.pack(fill=tk.X, pady=3)
        self._lbl_eng  = tk.Label(inner_info, text="引擎状态：未启动", font=(Theme.FONT_FAMILY, 10), anchor="w", bg=Theme.BG_LIGHT)
        self._lbl_eng.pack(fill=tk.X, pady=3)

        # 控制台操作区
        br = tk.Frame(f, bg=Theme.BG_LIGHT)
        br.pack(fill=tk.X, pady=4)
        
        self._btn_start = ttk.Button(br, text="▶ 启动同步",  command=self._start_engine)
        self._btn_start.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(br, text="⏹ 停止", style="Secondary.TButton", command=self._stop_engine).pack(side=tk.LEFT, padx=5)
        ttk.Button(br, text="🔍 体检", style="Secondary.TButton", command=self._health_check).pack(side=tk.LEFT, padx=5)
        ttk.Button(br, text="🧹 清空失败", style="Secondary.TButton", command=self._clear_failed).pack(side=tk.LEFT, padx=5)
        ttk.Button(br, text="🔄 重置队列", style="Secondary.TButton", command=self._reset_queue).pack(side=tk.LEFT, padx=5)
        ttk.Button(br, text="⏻ 退出", style="Secondary.TButton", command=self._quit_app).pack(side=tk.LEFT, padx=5)

        # 诊断日志
        self._hf = ttk.LabelFrame(f, text=" 系统体检自诊报告 ")
        self._ht = tk.Text(self._hf, height=8, state=tk.DISABLED, font=("Courier New", 9),
                           bg="#F3F4F6", fg=Theme.TEXT_MAIN, relief=tk.FLAT, wrap=tk.WORD, highlightthickness=0)
        self._ht.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

    def _card(self, parent, title, val, accent_color):
        # 现代阴影/浅边框圆角风格面板
        fr = tk.Frame(parent, bg=Theme.CARD_BG, highlightbackground=Theme.BORDER, highlightthickness=1, bd=0)
        
        # 顶部的彩色装饰指示条
        bar = tk.Frame(fr, bg=accent_color, height=4)
        bar.pack(fill=tk.X, side=tk.TOP)
        
        tk.Label(fr, text=title, bg=Theme.CARD_BG, fg=Theme.TEXT_MUTED, font=(Theme.FONT_FAMILY, 9)).pack(pady=(12, 2))
        vl = tk.Label(fr, text=val, bg=Theme.CARD_BG, fg=Theme.TEXT_MAIN, font=(Theme.FONT_FAMILY, 18, "bold"))
        vl.pack(pady=(0, 16))
        
        fr._vl = vl
        return fr

    # ── 配置标签页重构 ──
    def _tab_config(self, nb):
        outer = tk.Frame(nb, padx=20, pady=20, bg=Theme.BG_LIGHT)
        nb.add(outer, text="  ⚙️ 策略配置  ")
        
        cv = tk.Canvas(outer, highlightthickness=0, bg=Theme.BG_LIGHT)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        fr = tk.Frame(cv, bg=Theme.BG_LIGHT)

        fr.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        _win = cv.create_window((0, 0), window=fr, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        # 内框宽度跟随 Canvas 宽度，保证 wraplength 标签正确换行
        cv.bind("<Configure>", lambda e: cv.itemconfig(_win, width=e.width))
        # 鼠标滚轮绑定：进入 Canvas 时接管，离开时还原
        def _on_wheel(e): cv.yview_scroll(-1 * (e.delta // 120), "units")
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", _on_wheel))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))

        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._cv = {}
        for label, key, pwd in [
            ("自建应用 App ID", "app_id", False),
            ("自建应用 App Secret", "app_secret", True),
            ("飞书 App Token (多维表格)", "app_token", False),
            ("数据表 Table ID (单页表)",  "table_id", False),
        ]:
            row = tk.Frame(fr, pady=6, bg=Theme.BG_LIGHT)
            row.pack(fill=tk.X)
            tk.Label(row, text=label+":", width=25, anchor="w", bg=Theme.BG_LIGHT, font=(Theme.FONT_FAMILY, 10, "bold")).pack(side=tk.LEFT)
            
            v = tk.StringVar(value=db_get(key,"") or "")
            self._cv[key] = v
            ent = ttk.Entry(row, textvariable=v, show="•" if pwd else "", width=40, font=(Theme.FONT_FAMILY, 10))
            ent.pack(side=tk.LEFT, padx=4)

        # 控制操作区
        ttk.Separator(fr).pack(fill=tk.X, pady=12)
        btn_row = tk.Frame(fr, bg=Theme.BG_LIGHT)
        btn_row.pack(anchor="w", pady=6)
        
        ttk.Button(btn_row, text="💾 保存策略配置", command=self._save_cfg).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="🧪 单独校验连通性", style="Secondary.TButton", command=self._test_cfg).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_row, text="🔄 重走首次配置向导", style="Secondary.TButton", command=self._open_wizard).pack(side=tk.LEFT, padx=8)

        btn_row2 = tk.Frame(fr, bg=Theme.BG_LIGHT)
        btn_row2.pack(anchor="w", pady=(0, 2))
        ttk.Button(btn_row2, text="🧹 清除飞书凭证", style="Secondary.TButton", command=self._clear_creds).pack(side=tk.LEFT)
        tk.Label(btn_row2, text="（清空 App ID / Secret / Token / Table ID，用于换号或退出共享电脑）",
                 font=(Theme.FONT_FAMILY, 8), fg=Theme.TEXT_MUTED, bg=Theme.BG_LIGHT).pack(side=tk.LEFT, padx=8)

        # 操作反馈（紧跟按钮，保证点击后能立刻看到结果）
        self._cfg_msg = tk.Label(fr, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT, wraplength=560, justify=tk.LEFT)
        self._cfg_msg.pack(anchor="w", pady=(6, 2))

        # 进阶过滤规则
        ttk.Separator(fr).pack(fill=tk.X, pady=12)
        tk.Label(fr, text="📂 归档数据流过滤规则", font=(Theme.FONT_FAMILY, 11, "bold"), bg=Theme.BG_LIGHT).pack(anchor="w", pady=(0,4))
        tk.Label(fr, text="在此处选择哪些类型的消息流允许同步推送到飞书。变更需要点击「保存策略配置」生效。",
                 font=(Theme.FONT_FAMILY, 9), fg=Theme.TEXT_MUTED, bg=Theme.BG_LIGHT).pack(anchor="w", pady=(0,10))

        # 角色过滤
        tk.Label(fr, text="🎯 消息角色归档范围：", font=(Theme.FONT_FAMILY, 10, "bold"), bg=Theme.BG_LIGHT).pack(anchor="w")
        role_row = tk.Frame(fr, bg=Theme.BG_LIGHT)
        role_row.pack(anchor="w", padx=16, pady=(4,10))
        
        self._fv_role = tk.StringVar(value=db_get("filter_roles","both") or "both")
        for label, val in [("两者全部接收 (User + Assistant)","both"),
                           ("仅记录我的输入 (User)","user"),
                           ("仅记录 Claude 的应答 (Assistant)","assistant")]:
            ttk.Radiobutton(role_row, text=label, variable=self._fv_role, value=val).pack(anchor="w", pady=2)

        # 工具过滤
        tk.Label(fr, text="⚙️ 交互内容清洁度控制：", font=(Theme.FONT_FAMILY, 10, "bold"), bg=Theme.BG_LIGHT).pack(anchor="w")
        flt_row = tk.Frame(fr, bg=Theme.BG_LIGHT)
        flt_row.pack(anchor="w", padx=16, pady=(4,10))
        
        self._fv_tool_use    = tk.BooleanVar(value=(db_get("filter_tool_use","1") or "1") == "1")
        self._fv_tool_result = tk.BooleanVar(value=(db_get("filter_tool_result","1") or "1") == "1")
        self._fv_sidechain   = tk.BooleanVar(value=(db_get("filter_sidechain","1") or "1") == "1")
        ttk.Checkbutton(flt_row, text="过滤工具调用（如 [工具调用: Bash]）",
                        variable=self._fv_tool_use).pack(anchor="w", pady=2)
        ttk.Checkbutton(flt_row, text="过滤工具结果（大量终端/代码输出）",
                        variable=self._fv_tool_result).pack(anchor="w", pady=2)
        ttk.Checkbutton(flt_row, text="过滤子代理消息（subagent 内部对话，通常为噪音）",
                        variable=self._fv_sidechain).pack(anchor="w", pady=2)
        self._fv_short = tk.BooleanVar(value=(db_get("filter_short","1") or "1") == "1")
        ttk.Checkbutton(flt_row, text="过滤无意义短消息（继续 / 好的 / 嗯 / ok 等口水词）",
                        variable=self._fv_short).pack(anchor="w", pady=2)
        self._fv_process = tk.BooleanVar(value=(db_get("filter_process","1") or "1") == "1")
        ttk.Checkbutton(flt_row, text="过滤助手过程旁白（如「已启动…正常」「语法通过，验证一下…」等短句，仅保留问题与结论）",
                        variable=self._fv_process).pack(anchor="w", pady=2)

        # 自动化 Hook 挂载
        ttk.Separator(fr).pack(fill=tk.X, pady=12)
        tk.Label(fr, text="⚡ 自动化 Hook 控制链", font=(Theme.FONT_FAMILY, 11, "bold"), bg=Theme.BG_LIGHT).pack(anchor="w", pady=(0, 6))
        
        hook_row = tk.Frame(fr, bg=Theme.BG_LIGHT)
        hook_row.pack(anchor="w", pady=4)
        ttk.Button(hook_row, text="📌 重新注册触发 Hook", command=self._reg_hook).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(hook_row, text="🗑 彻底移除自动化 Hook", style="Secondary.TButton", command=self._unreg_hook).pack(side=tk.LEFT)

        self._hook_msg = tk.Label(fr, text="", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT, wraplength=560, justify=tk.LEFT)
        self._hook_msg.pack(anchor="w", pady=10)

    # ── 历史回填标签页重构 ──
    def _tab_history(self, nb):
        f = tk.Frame(nb, padx=20, pady=20, bg=Theme.BG_LIGHT)
        nb.add(f, text="  📥 历史回填  ")

        # 扫描路径卡片
        plf = ttk.LabelFrame(f, text=" 📁 对话搜索历史路径（自动排除冲突） ")
        plf.pack(fill=tk.X, pady=(0, 12))

        plist_fr = tk.Frame(plf, bg=Theme.BG_LIGHT)
        plist_fr.pack(fill=tk.X, padx=12, pady=10)
        
        psb = ttk.Scrollbar(plist_fr, orient=tk.VERTICAL)
        self._plb = tk.Listbox(plist_fr, height=4, yscrollcommand=psb.set,
                               font=("Courier New", 10), selectmode=tk.SINGLE, relief=tk.FLAT, bd=1, highlightbackground=Theme.BORDER)
        psb.config(command=self._plb.yview)
        self._plb.pack(side=tk.LEFT, fill=tk.X, expand=True)
        psb.pack(side=tk.LEFT, fill=tk.Y)

        pbr = tk.Frame(plf, bg=Theme.BG_LIGHT)
        pbr.pack(anchor="w", padx=12, pady=(0, 10))
        
        ttk.Button(pbr, text="🔍 探查其他用户路径", command=self._auto_find_paths).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(pbr, text="📂 导入自定义路径", style="Secondary.TButton", command=self._add_scan_path).pack(side=tk.LEFT, padx=6)
        ttk.Button(pbr, text="➖ 移出当前路径", style="Secondary.TButton", command=self._del_scan_path).pack(side=tk.LEFT, padx=6)
        tk.Label(pbr, text="（🔒 为默认路径，不可删除）", font=(Theme.FONT_FAMILY, 8), fg=Theme.TEXT_MUTED, bg=Theme.BG_LIGHT).pack(side=tk.LEFT, padx=8)
        
        self._reload_path_list()

        # 扫描与入队操作面板
        br = tk.Frame(f, bg=Theme.BG_LIGHT)
        br.pack(anchor="w", pady=(0, 6))
        ttk.Button(br, text="🔍 全盘检索历史 JSONL 会话", command=self._scan_hist).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(br, text="📥 将发现的数据导入飞书队列", command=self._import_hist).pack(side=tk.LEFT)
        ttk.Button(br, text="📝 导出为 Markdown", style="Secondary.TButton", command=self._export_md).pack(side=tk.LEFT, padx=(8, 0))

        self._hp = ttk.Progressbar(f, length=600, mode='determinate')
        self._hp.pack(fill=tk.X, pady=8)
        
        self._hs = tk.Label(f, text="就绪，点击「扫描历史记录」开始。", font=(Theme.FONT_FAMILY, 10), bg=Theme.BG_LIGHT, fg=Theme.TEXT_MUTED)
        self._hs.pack(anchor="w", pady=2)

        lf = ttk.LabelFrame(f, text=" 检索到的具体 JSONL 会话档案 ")
        lf.pack(fill=tk.BOTH, expand=True, pady=6)
        
        sb = ttk.Scrollbar(lf)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._hlb = tk.Listbox(lf, height=6, yscrollcommand=sb.set, font=("Courier New", 9), relief=tk.FLAT, bd=0)
        self._hlb.pack(fill=tk.BOTH, expand=True)
        sb.config(command=self._hlb.yview)

    # ── 系统日志重构 ──
    def _tab_log(self, nb):
        f = tk.Frame(nb, padx=12, pady=12, bg=Theme.BG_LIGHT)
        nb.add(f, text="  📋 运行日志  ")
        
        br = tk.Frame(f, bg=Theme.BG_LIGHT)
        br.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(br, text="🔄 手动刷新", command=self._refresh_log).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(br, text="🗑 干净擦除", style="Secondary.TButton", command=self._clear_log).pack(side=tk.LEFT)
        
        # 使用更温和的终端底色
        self._log = scrolledtext.ScrolledText(f, wrap=tk.WORD, state=tk.DISABLED,
                                              font=("Courier New", 10), bg="#1F2937", fg="#F9FAFB", highlightthickness=0, bd=0)
        self._log.pack(fill=tk.BOTH, expand=True)
        for tag, fg in [("ERROR","#EF4444"), ("WARN","#F59E0B"), ("INFO","#34D399")]:
            self._log.tag_config(tag, foreground=fg)

    # ── 引擎线程拉起 ──
    def _init_engine_or_wizard(self):
        # 启动时不自动开启同步服务：仅在未配置时拉起向导，已配置则待命，
        # 由用户在状态页手动点击「▶ 启动同步」决定何时开始上传。
        if not db_get("setup_done"):
            self._open_wizard()
        else:
            self._lbl_eng.config(text="引擎状态：⏸ 待命（点击「▶ 启动同步」开始上传）")
            self._dot.config(text="●  待命", fg=Theme.WARNING)

    def _start_engine(self):
        if self.engine and self.engine.running: return
        self.engine = SyncEngine(status_cb=self._on_status, log_cb=self._on_log)
        self.engine.start()
        self._lbl_eng.config(text="引擎状态：✅ 运行中（每 10 秒上传一次）")
        self._dot.config(text="●  同步运行中", fg=Theme.SUCCESS)
        self._btn_start.config(state=tk.DISABLED)

    def _stop_engine(self):
        if self.engine: self.engine.stop()
        self._lbl_eng.config(text="引擎状态：⏹ 已停止")
        self._dot.config(text="●  已停止", fg=Theme.TEXT_MUTED)
        self._btn_start.config(state=tk.NORMAL)

    def _on_status(self, total=0, last_time="", queue=None):
        self.after(0, lambda: self._update_stats(total, last_time, queue or {}))

    def _on_log(self, msg, level="INFO"):
        ts   = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}\n"
        self.after(0, lambda: self._append_log(line, level))

    def _append_log(self, line, level="INFO"):
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, line, level)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _update_stats(self, total, last_time, qs):
        self._c_total._vl.config(text=f"{total} 条")
        self._c_pending._vl.config(text=f"{qs.get('pending',0)} 条")
        self._c_failed._vl.config(text=f"{qs.get('failed',0)} 条")
        self._lbl_last.config(text=f"上次同步：{last_time or '从未'}")

    # ── 配置保存 ──
    def _save_cfg(self):
        global _token_cache
        for k,v in self._cv.items():
            val = v.get().strip()
            if val: db_set(k, val)
        db_set("filter_roles",       self._fv_role.get())
        db_set("filter_tool_use",    "1" if self._fv_tool_use.get()    else "0")
        db_set("filter_tool_result", "1" if self._fv_tool_result.get() else "0")
        db_set("filter_sidechain",   "1" if self._fv_sidechain.get()   else "0")
        db_set("filter_short",       "1" if self._fv_short.get()       else "0")
        db_set("filter_process",     "1" if self._fv_process.get()     else "0")
        _token_cache = {"token":None,"expires_at":0}
        self._cfg_msg.config(text="✅ 配置已保存", fg=Theme.SUCCESS)

    def _test_cfg(self):
        ai = self._cv.get("app_id", tk.StringVar()).get().strip()
        ase= self._cv.get("app_secret", tk.StringVar()).get().strip()
        self._cfg_msg.config(text="正在连接飞书 API...", fg=Theme.TEXT_MUTED); self.update()
        def _do():
            ok, msg = test_connection(ai, ase)
            self.after(0, lambda: self._cfg_msg.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER))
        threading.Thread(target=_do, daemon=True).start()

    def _clear_creds(self):
        global _token_cache
        if not messagebox.askyesno(
            "清除飞书凭证",
            "将清空本机保存的 App ID / App Secret / App Token / Table ID。\n\n"
            "（不会删除已同步的历史记录与本地队列；清空后同步会暂停，需重新填写凭证才能继续）\n\n确定清除？"):
            return
        for k in ("app_id", "app_secret", "app_token", "table_id"):
            db_set(k, "")
        db_set("setup_done", "")          # 下次启动重新引导
        _token_cache = {"token": None, "expires_at": 0}
        for k, v in self._cv.items():     # 同步清空界面输入框
            v.set("")
        if self.engine: self.engine.stop()
        self._stop_engine()
        self._cfg_msg.config(text="✅ 已清除飞书凭证，同步已暂停", fg=Theme.SUCCESS)

    def _open_wizard(self):
        SetupWizard(self, on_complete=self._wizard_done)

    def _wizard_done(self):
        for k, v in self._cv.items(): v.set(db_get(k,"") or "")
        # 向导完成后同样不自动启动，保持「待命」，由用户主动点击启动。
        self._lbl_eng.config(text="引擎状态：⏸ 待命（配置完成，点击「▶ 启动同步」开始）")
        self._dot.config(text="●  待命", fg=Theme.WARNING)

    def _reg_hook(self):
        write_hook_script(); ok, msg = register_hook()
        self._hook_msg.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER)

    def _unreg_hook(self):
        if messagebox.askyesno("解绑确认", "确定彻底停用 Hook？\n移除后，Claude Code 的新对话将不会进入同步备份池。"):
            ok, msg = unregister_hook()
            self._hook_msg.config(text=f"{'✅' if ok else '❌'} {msg}", fg=Theme.SUCCESS if ok else Theme.DANGER)

    # ── 一键体检自诊 ──
    def _health_check(self):
        self._hf.pack(fill=tk.BOTH, expand=True, pady=(8,0))
        self._ht.config(state=tk.NORMAL); self._ht.delete(1.0,tk.END)
        self._ht.insert(tk.END,"深度排查全链路配置项中...\n"); self._ht.config(state=tk.DISABLED); self.update()
        def _do():
            res = run_health_check(db_get_config())
            lines = [f"  {ic}  {nm:22s}  {detail}" for ic,nm,detail in res]
            def _upd():
                self._ht.config(state=tk.NORMAL); self._ht.delete(1.0,tk.END)
                for l in lines: self._ht.insert(tk.END, l+"\n")
                self._ht.config(state=tk.DISABLED)
            self.after(0, _upd)
        threading.Thread(target=_do, daemon=True).start()

    def _clear_failed(self):
        conn = _connect()
        conn.execute("DELETE FROM upload_queue WHERE status='failed'"); conn.commit(); conn.close()
        messagebox.showinfo("完成", "已清理所有失败记录")
        self._refresh_stats()

    def _reset_queue(self):
        if not messagebox.askyesno(
            "重置确认",
            "将清空整个本地队列（待上传 / 失败 / 已完成记录），并把「累计同步消息」计数清零。\n\n"
            "（不影响飞书里已写入的数据，也不影响去重记录——不会重复上传旧消息）\n\n确定重置？"):
            return
        conn = _connect()
        n = conn.execute("DELETE FROM upload_queue").rowcount   # 清空全部，含 done
        conn.commit(); conn.close()
        db_set("total_synced", 0)
        db_set("last_sync_time", "从未")
        if self.engine:                      # 同步引擎内存计数也归零
            self.engine._total = 0
            self.engine._last_time = "从未"
        messagebox.showinfo("完成", f"已重置：清空本地队列 {n} 条记录，累计计数已清零")
        self._refresh_stats()

    # ── 托盘控制 (PyStray) ──
    def _setup_tray(self):
        if not HAS_TRAY:
            self._tray = None
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self._tray_show, default=True),
            pystray.MenuItem("开始同步", self._tray_start),
            pystray.MenuItem("停止同步", self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_exit),
        )
        self._tray = pystray.Icon("feishu_sync", self._make_tray_image(),
                                  "飞书同步助手", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _make_tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=Theme.PRIMARY)
        d.ellipse((22, 18, 42, 38), fill="white")
        d.rectangle((26, 34, 38, 50), fill="white")
        return img

    def _tray_show(self, icon=None, item=None):  self.after(0, self._show_window)
    def _tray_start(self, icon=None, item=None): self.after(0, self._start_engine)
    def _tray_stop(self, icon=None, item=None):  self.after(0, self._stop_engine)
    def _tray_exit(self, icon=None, item=None):  self.after(0, self._quit_app)

    def _show_window(self):
        self.deiconify(); self.lift(); self.focus_force()

    def _on_close(self):
        # 关闭按钮 = 最小化到任务栏常驻（不退出），任务栏仍保留按钮，点击即可还原。
        # 彻底退出请用托盘菜单「退出」或状态页的退出入口。
        if self._tray is not None:
            self.iconify()
            if not getattr(self, "_close_tip_shown", False):
                self._close_tip_shown = True
                messagebox.showinfo("已最小化",
                    "程序已最小化到任务栏，后台继续同步。\n"
                    "如需彻底退出：右下角托盘图标右键 →「退出」，或状态页「⏻ 退出程序」。")
            return
        # 无托盘依赖：询问是最小化到任务栏还是退出
        if messagebox.askyesno("关闭确认",
                               "点击「是」最小化到任务栏继续后台同步；\n"
                               "点击「否」退出程序。"):
            self.iconify()
        else:
            self._quit_app()

    def _quit_app(self):
        try:
            if self.engine: self.engine.stop()
        except Exception: pass
        if self._tray is not None:
            try: self._tray.stop()
            except Exception: pass
        self.destroy()

    # ── 检索路径管理 ──
    def _reload_path_list(self):
        self._plb.delete(0, tk.END)
        for i, p in enumerate(get_all_scan_paths()):
            prefix = "🔒 " if i == 0 else "   "
            self._plb.insert(tk.END, f"{prefix}{p}")

    def _auto_find_paths(self):
        self._hs.config(text="搜索中…"); self.update()
        def _do():
            found = find_all_claude_projects()
            self.after(0, lambda: self._show_found_paths(found))
        threading.Thread(target=_do, daemon=True).start()

    def _show_found_paths(self, found):
        extras = get_extra_scan_paths()
        new_found = [p for p in found if p not in extras]
        if not new_found:
            if found:
                self._hs.config(text=f"找到 {len(found)} 个路径，已全部在列表中")
            else:
                self._hs.config(text="未找到其他用户的 .claude/projects 目录")
            return

        win = tk.Toplevel(self)
        win.title("勾选探查到的历史源")
        win.geometry("560x320")
        win.resizable(False, False)
        win.grab_set()
        win.configure(bg=Theme.BG_LIGHT)

        tk.Label(win, text=f"检索到 {len(new_found)} 个其他系统的会话流档案，请勾选需要导入的路径：",
                 font=(Theme.FONT_FAMILY, 10), pady=12, bg=Theme.BG_LIGHT).pack(anchor="w", padx=16)

        lf = tk.Frame(win, padx=16, bg=Theme.BG_LIGHT)
        lf.pack(fill=tk.BOTH, expand=True)
        
        vars_ = []
        for p in new_found:
            parts = p.replace("\\","/").split("/")
            try:    username = parts[-3]
            except: username = p
            v = tk.BooleanVar(value=True)
            vars_.append((v, p))
            ttk.Checkbutton(lf, text=f"👤 归属用户 {username}   →  {p}", variable=v).pack(anchor="w", pady=3)

        bf = tk.Frame(win, pady=12, bg=Theme.BG_LIGHT)
        bf.pack()
        def _add():
            chosen = [p for v,p in vars_ if v.get()]
            if chosen:
                cur = get_extra_scan_paths()
                cur.extend(p for p in chosen if p not in cur)
                set_extra_scan_paths(cur)
                self._reload_path_list()
                self._hs.config(text=f"已添加 {len(chosen)} 个路径")
            win.destroy()
            
        ttk.Button(bf, text="✅ 绑定这些路径", command=_add).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="取消", style="Secondary.TButton", command=win.destroy).pack(side=tk.LEFT)

    def _add_scan_path(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="选择外部 Claude Projects 目录", parent=self)
        if not path: return
        path = os.path.normpath(path)
        extras = get_extra_scan_paths()
        if path == CLAUDE_PROJECTS or path in extras:
            messagebox.showinfo("提示", "该路径已经添加在案"); return
        extras.append(path)
        set_extra_scan_paths(extras)
        self._reload_path_list()

    def _del_scan_path(self):
        sel = self._plb.curselection()
        if not sel: return
        idx = sel[0]
        if idx == 0:
            messagebox.showwarning("提示", "默认路径不可删除"); return
        extras = get_extra_scan_paths()
        extra_idx = idx - 1
        if extra_idx < len(extras):
            removed = extras.pop(extra_idx)
            set_extra_scan_paths(extras)
            self._reload_path_list()
            self._hs.config(text=f"已删除路径：{removed}")

    # ── 历史归档回溯 ──
    def _scan_hist(self):
        self._hlb.delete(0, tk.END)
        self._hs.config(text="扫描中…"); self.update()
        def _do():
            paths = get_all_scan_paths()
            rows, missing = [], []
            for base in paths:
                if not os.path.exists(base):
                    missing.append(base); continue
                for root, _, files in os.walk(base):
                    for fn in files:
                        if fn.endswith(".jsonl"):
                            fp = os.path.join(root, fn)
                            kb = os.path.getsize(fp) // 1024
                            try:   rel = os.path.relpath(fp, base)
                            except: rel = fp
                            rows.append(f"[{os.path.basename(base)}] {rel}  ({kb} KB)")
            def _upd():
                for r in rows: self._hlb.insert(tk.END, r)
                tip = f"找到 {len(rows)} 个会话文件（扫描 {len(paths)} 个路径）"
                if missing: tip += f"  ⚠️ 路径不存在：{', '.join(missing)}"
                self._hs.config(text=tip)
            self.after(0, _upd)
        threading.Thread(target=_do, daemon=True).start()

    def _import_hist(self):
        paths = get_all_scan_paths()
        exist = [p for p in paths if os.path.exists(p)]
        if not exist:
            messagebox.showwarning("提示", "所有配置的路径均不存在，请先添加有效路径"); return
        if not messagebox.askyesno("确认导入",
            f"将扫描以下 {len(exist)} 个路径并导入所有历史对话（可能需要数分钟）：\n\n"
            + "\n".join(f"  • {p}" for p in exist) + "\n\n确定继续？"): return
        self._hs.config(text="扫描中…"); self._hp["value"] = 0; self.update()
        def _prog(done, total, new_msgs):
            pct = done / total * 100 if total else 0
            self.after(0, lambda: [self._hp.__setitem__("value", pct),
                                   self._hs.config(text=f"进度 {done}/{total}，已入队 {new_msgs} 条新消息")])
        def _do():
            total = scan_all_history(progress_cb=_prog, paths=exist)
            self.after(0, lambda: [self._hp.__setitem__("value", 100),
                                   self._hs.config(text=f"✅ 完成！共 {total} 条新消息已入队，后台将逐步上传到飞书")])
        threading.Thread(target=_do, daemon=True).start()

    def _export_md(self):
        from tkinter import filedialog
        paths = get_all_scan_paths()
        exist = [p for p in paths if os.path.exists(p)]
        if not exist:
            messagebox.showwarning("提示", "所有配置的路径均不存在，请先添加有效路径"); return
        out = filedialog.askdirectory(title="选择 Markdown 导出目录", parent=self)
        if not out: return
        if not messagebox.askyesno("确认导出",
            f"将把以下 {len(exist)} 个路径下的所有会话导出为 Markdown\n（每个会话一个 .md 文件，不联网、不上传飞书）：\n\n"
            + "\n".join(f"  • {p}" for p in exist)
            + f"\n\n导出到：{out}\n\n确定继续？"): return
        self._hs.config(text="导出中…"); self._hp["value"] = 0; self.update()
        def _prog(done, total, written):
            pct = done / total * 100 if total else 0
            self.after(0, lambda: [self._hp.__setitem__("value", pct),
                                   self._hs.config(text=f"导出进度 {done}/{total}，已生成 {written} 个 md 文件")])
        def _do():
            written, total = export_all_to_md(out, paths=exist, progress_cb=_prog)
            def _done():
                self._hp["value"] = 100
                self._hs.config(text=f"✅ 导出完成！共 {written} 个 Markdown 文件已保存到 {out}")
                if messagebox.askyesno("完成", f"已导出 {written} 个 Markdown 文件（扫描 {total} 个会话）。\n是否打开导出目录？"):
                    try:
                        if platform.system() == "Windows": os.startfile(out)
                        elif platform.system() == "Darwin": os.system(f'open "{out}"')
                        else: os.system(f'xdg-open "{out}"')
                    except Exception: pass
            self.after(0, _done)
        threading.Thread(target=_do, daemon=True).start()

    # ── 日志刷新 ──
    def _refresh_log(self):
        logs = get_recent_logs(300)
        self._log.config(state=tk.NORMAL); self._log.delete(1.0,tk.END)
        for lvl, msg, ts in logs:
            t = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            self._log.insert(tk.END, f"[{t}] [{lvl}] {msg}\n", lvl)
        self._log.see(tk.END); self._log.config(state=tk.DISABLED)

    def _clear_log(self):
        conn = _connect()
        conn.execute("DELETE FROM sync_log"); conn.commit(); conn.close()
        self._log.config(state=tk.NORMAL); self._log.delete(1.0,tk.END)
        self._log.config(state=tk.DISABLED)

    def _refresh_stats(self):
        qs    = get_queue_stats()
        total = int(db_get("total_synced",0) or 0)
        ltime = db_get("last_sync_time","从未同步") or "从未同步"
        self._update_stats(total, ltime, qs)

    def _periodic(self):
        self._refresh_stats(); self.after(15000, self._periodic)

# ──────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────
if __name__ == "__main__":
    # Hook 模式：由 Claude Code 的 Stop Hook 调用（exe 或 python 均走这里，无需外部依赖）
    if "--hook" in sys.argv[1:]:
        run_hook()
        sys.exit(0)
    app = App()
    app.mainloop()