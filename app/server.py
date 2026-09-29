"""鳥璇點餐系統 —— 店內伺服器

在店裡一台電腦上執行,手機、平板連同一個 Wi-Fi,用畫面上顯示的網址打開就能點餐。
不需要網路、不需要任何帳號;資料存在這個資料夾的 data.json。

兩種人會連進來:
  店員   —— 開 http://<IP>:8800/ ,第一次要輸入店員密碼(這台電腦本身免密碼)
  客人   —— 掃桌上的 QR code,開 /order?t=桌號,只能點餐、看自己這桌點了什麼

只用 Python 標準函式庫:導入現場少裝一個套件,就少一個出錯點。
"""
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("NX_PORT", "8800"))
COLS = ("tables", "orders", "settles", "days", "config", "shift", "expenses")
KEEP_BACKUP_DAYS = 60
DAY_CUTOFF_HOUR = 5          # 營業日切在清晨 5 點,跟點餐頁一致
GUEST_COOLDOWN_SEC = 3       # 同一支手機連續送單的間隔,擋手滑連按
PIN_MAX_FAILS = 8            # 密碼連錯幾次就鎖
PIN_LOCK_SEC = 600

# 打包成 exe 後,資料要放在 exe 旁邊,而不是解壓縮的暫存資料夾
BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
ASSET = getattr(sys, "_MEIPASS", BASE)
DATA = os.path.join(BASE, "data.json")
CONF = os.path.join(BASE, "設定.json")
BACKUP = os.path.join(BASE, "備份")

lock = threading.Lock()
state = {"version": 0, "data": {c: {} for c in COLS}}
conf = {}
MENU = []            # [{id,name,en,items:[{id,name,jp,price}]}] —— 從 index.html 讀,菜單只有一個來源
ITEMS = {}
DEFAULT_TABLES = []  # [{id,label}]
guest_last = {}      # ip -> 上次送單時間
pin_fails = {}       # ip -> (次數, 鎖到什麼時候)


def load():
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            saved = json.load(f)
        state["version"] = saved.get("version", 0)
        for c in COLS:
            state["data"][c] = saved.get("data", {}).get(c, {})


def load_conf():
    if os.path.exists(CONF):
        with open(CONF, encoding="utf-8") as f:
            conf.update(json.load(f))
    if not re.fullmatch(r"\d{4,8}", str(conf.get("staff_pin", ""))):
        conf["staff_pin"] = f"{secrets.randbelow(10000):04d}"
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)


def load_menu():
    """菜單寫在 index.html 的 MENU 裡;伺服器照那份算客人點單的價格,避免兩邊不一致。"""
    with open(os.path.join(ASSET, "index.html"), encoding="utf-8") as f:
        html = f.read()
    start = html.index("const MENU = [")
    block = html[start:html.index("\n];", start)]
    for m in re.finditer(r'\{ id:"([\w-]+)", name:"([^"]+)", en:"([^"]*)", items:\[(.*?\])\s*\]\}', block, re.S):
        items = [{"id": a, "name": b, "jp": c, "price": int(d)}
                 for a, b, c, d in re.findall(r'\["([\w-]+)","([^"]+)","([^"]*)",(\d+)\]', m.group(4))]
        MENU.append({"id": m.group(1), "name": m.group(2), "en": m.group(3), "items": items})
        for it in items:
            ITEMS[it["id"]] = it
    tstart = html.index("const DEFAULT_TABLES = [")
    tblock = html[tstart:html.index("];", tstart)]
    DEFAULT_TABLES.extend({"id": a, "label": b} for a, b in re.findall(r'\["(t\d+)","([^"]+)"\]', tblock))
    if not ITEMS or not DEFAULT_TABLES:
        raise RuntimeError("index.html 裡找不到菜單或桌位")


def save():
    # 先寫暫存檔再換名:寫到一半斷電也不會把資料檔弄壞
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, DATA)
    # 每天第一次寫入時留一份備份
    os.makedirs(BACKUP, exist_ok=True)
    today = os.path.join(BACKUP, f"data-{datetime.now():%Y-%m-%d}.json")
    if not os.path.exists(today):
        shutil.copyfile(DATA, today)
        cutoff = f"data-{datetime.now() - timedelta(days=KEEP_BACKUP_DAYS):%Y-%m-%d}.json"
        for name in os.listdir(BACKUP):
            if name.startswith("data-") and name < cutoff:
                os.remove(os.path.join(BACKUP, name))


def apply(op):
    kind, col, doc_id = op.get("op"), op.get("col"), str(op.get("id", ""))
    if col not in COLS or not doc_id or len(doc_id) > 100:
        raise ValueError("bad target")
    docs = state["data"][col]
    if kind == "put":
        if not isinstance(op.get("data"), dict):
            raise ValueError("bad data")
        docs[doc_id] = op["data"]
    elif kind == "patch":
        if doc_id not in docs or not isinstance(op.get("data"), dict):
            raise ValueError("missing doc")
        docs[doc_id] = {**docs[doc_id], **op["data"]}
    elif kind == "del":
        docs.pop(doc_id, None)
    else:
        raise ValueError("bad op")


# ---------- 客人點餐 ----------
def rid():
    return f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def now_iso():
    # 跟瀏覽器 toISOString() 同格式(UTC、毫秒、Z 結尾),點餐頁用字串比先後才不會亂
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def biz_day():
    return (datetime.now() - timedelta(hours=DAY_CUTOFF_HOUR)).strftime("%Y-%m-%d")


def table_list():
    return state["data"]["config"].get("main", {}).get("tables") or DEFAULT_TABLES


def base_label(tid):
    return next((t["label"] for t in table_list() if t["id"] == tid), tid)


def main_table(tid):
    """被併掉的桌子,客人點的單要算到主桌。"""
    t = state["data"]["tables"].get(tid) or {}
    if t.get("status") == "merged" and (state["data"]["tables"].get(t.get("into")) or {}).get("status") == "dining":
        return t["into"]
    return tid


def label(tid):
    t = state["data"]["tables"].get(tid) or {}
    merged = (t.get("merged") or []) if t.get("status") == "dining" else []
    return "+".join([base_label(tid)] + [base_label(m) for m in merged])


def guest_view(tid):
    main = main_table(tid)
    t = state["data"]["tables"].get(main) or {}
    open_ = t.get("status") == "dining"
    orders = [o for o in state["data"]["orders"].values() if open_ and o.get("session") == t.get("session")]
    orders.sort(key=lambda o: o.get("at", ""))
    return {
        "table": main, "label": label(main), "open": open_, "people": t.get("people") if open_ else None,
        "orders": [{"round": i + 1, "at": o.get("at"), "status": o.get("status"), "guest": bool(o.get("guest")),
                    "lines": [{"name": l["name"], "q": l["q"], "price": l["price"]} for l in o.get("lines", []) if l.get("q", 0) > 0]}
                   for i, o in enumerate(orders)],
    }


def guest_order(body, ip):
    tid = str(body.get("t", ""))
    if not any(t["id"] == tid for t in table_list()):
        raise ValueError("找不到這個桌號,請跟店員確認")
    now = time.time()
    if now - guest_last.get(ip, 0) < GUEST_COOLDOWN_SEC:
        raise ValueError("剛剛才送出,請稍等幾秒再送")
    raw = body.get("lines")
    if not isinstance(raw, list) or not raw or len(raw) > 40:
        raise ValueError("請先選餐點")
    lines = []
    for x in raw:
        it = ITEMS.get(str(x.get("id")))
        q = x.get("q")
        if not it or not isinstance(q, int) or not 1 <= q <= 50:
            raise ValueError("餐點資料有誤,請重新整理頁面")
        lines.append({"id": it["id"], "name": it["name"], "price": it["price"], "q": q})
    note = str(body.get("note") or "")[:120]
    main = main_table(tid)
    t = state["data"]["tables"].get(main) or {}
    if t.get("status") != "dining":
        people = body.get("people")
        people = people if isinstance(people, int) and 1 <= people <= 30 else 1
        t = {"status": "dining", "people": people, "cork": 0, "session": rid(),
             "openedAt": now_iso(), "byGuest": True}
        state["data"]["tables"][main] = t
    rounds = sum(1 for o in state["data"]["orders"].values() if o.get("session") == t["session"])
    state["data"]["orders"][rid()] = {
        "table": main, "tableLabel": label(main), "session": t["session"], "round": rounds + 1,
        "lines": lines, "note": note, "status": "pending", "guest": True, "by": "客人",
        "at": now_iso(), "day": biz_day(),
    }
    guest_last[ip] = now
    return guest_view(main)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 不要把每次輪詢都印在畫面上
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _page(self, name):
        with open(os.path.join(ASSET, name), "rb") as f:
            return self._send(200, f.read(), "text/html; charset=utf-8")

    def _ip(self):
        return self.client_address[0]

    def _staff(self):
        """店員才能看營業資料、改資料。店裡這台電腦本身免密碼。"""
        ip = self._ip()
        if ip in ("127.0.0.1", "::1"):
            return True
        n, until = pin_fails.get(ip, (0, 0))
        if until > time.time():
            self._send(429, {"error": "密碼錯太多次,請 10 分鐘後再試"})
            return False
        given = self.headers.get("X-PIN", "")
        if given and hmac.compare_digest(given, str(conf["staff_pin"])):
            pin_fails.pop(ip, None)
            return True
        if given:
            n += 1
            pin_fails[ip] = (0, time.time() + PIN_LOCK_SEC) if n >= PIN_MAX_FAILS else (n, 0)
        self._send(401, {"error": "需要店員密碼"})
        return False

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 1_000_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            return self._page("index.html")
        if path == "/order":
            return self._page("guest.html")
        if path == "/api/menu":
            return self._send(200, {"cats": MENU, "tables": table_list()})
        if path == "/api/guest":
            with lock:
                tid = (qs.get("t") or [""])[0]
                if not any(t["id"] == tid for t in table_list()):
                    return self._send(404, {"error": "找不到這個桌號"})
                return self._send(200, guest_view(tid))
        if path == "/api/state":
            if not self._staff():
                return
            since = (qs.get("v") or [""])[0]
            with lock:
                if since.isdigit() and int(since) == state["version"]:
                    return self._send(200, {"version": state["version"]})
                return self._send(200, state)
        if path == "/api/info":
            if not self._staff():
                return
            return self._send(200, {"ips": lan_ips(), "port": PORT})
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/op":
                if not self._staff():
                    return
                op = self._body()
                with lock:
                    apply(op)
                    state["version"] += 1
                    save()
                    return self._send(200, state)
            if path == "/api/guest-order":
                body = self._body()
                with lock:
                    view = guest_order(body, self._ip())
                    state["version"] += 1
                    save()
                    return self._send(200, view)
            return self._send(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError, AttributeError, TypeError) as e:
            self._send(400, {"error": str(e)})


def lan_ips():
    """主要那張網卡(會連到路由器的)排第一;其他通常是虛擬網卡,手機連不到。"""
    primary, ips = None, set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 不會真的送出封包,只是讓系統挑出對外那張網卡
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    rest = sorted(ip for ip in ips if not ip.startswith("127.") and ip != primary)
    return ([primary] if primary and not primary.startswith("127.") else []) + rest


def main():
    # 店家電腦的主控台多半是 cp950,遇到印不出的字就替換掉,不要讓啟動訊息把程式弄當
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    load()
    load_conf()
    load_menu()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"\n  連接埠 {PORT} 已被占用 —— 點餐系統可能已經開著了。\n")
        input("  按 Enter 關閉…")
        return
    print("=" * 56)
    print("   鳥璇點餐系統 已啟動")
    print("=" * 56)
    print(f"\n   這台電腦:  http://localhost:{PORT}")
    for ip in lan_ips():
        print(f"   店員手機:  http://{ip}:{PORT}   (連同一個 Wi-Fi)")
    print(f"\n   店員密碼:  {conf['staff_pin']}   (店員手機第一次連線要輸入;這台電腦免密碼)")
    first = table_list()[0]["id"] if table_list() else "t01"
    print()
    print(f"   客人點餐頁(預覽): http://localhost:{PORT}/order?t={first}")
    print(f"   客人點餐 QR code: 「桌況・結帳 → 設定 → 列印桌號 QR code」印出來貼在桌上")
    print(f"\n   資料檔:    {DATA}")
    print(f"   改密碼:    用記事本打開 {CONF},改 staff_pin 後重開程式")
    print("\n   營業時間請不要關掉這個視窗。關掉 = 系統停止。")
    print("=" * 56)
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
