#!/usr/bin/env python3
"""openworld.eu.org 免费 VPS 库存监控

- 用已登录 session cookie 拉 /createvps，解析页内 data-plan JSON 的 stock 字段
- 免费档 (price == 0) 有库存 → TG 报警（30 分钟内不重复）
- cookie 失效 → TG 报警（6 小时内不重复）
- 状态落 state.json，由 Actions cache 滚动保存，避免刷屏
"""
import json
import os
import re
import sys
import time
import html as H
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openworld.eu.org"
TARGET = BASE + "/createvps"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/155.0.0.0 Safari/537.36")
STOCK_COOLDOWN = 30 * 60      # 有货后 30 分钟内不重复推
COOKIE_COOLDOWN = 6 * 3600    # cookie 失效 6 小时提醒一次
STATE_FILE = "state.json"


def log(*a):
    print(*a, flush=True)


def bj(ts=None):
    ts = ts or time.time()
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + 8 * 3600))


def tg(text, quiet=False):
    tok = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if quiet:
        log("[tg] quiet，跳过")
        return
    if not tok or not chat:
        log("[tg] 未配置 TG_BOT_TOKEN/TG_CHAT_ID，跳过")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            log("[tg] ok=", json.loads(r.read().decode()).get("ok"))
    except Exception as e:
        log("[tg] 发送失败:", e)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        log("[state] 写失败:", e)


def cookie_header():
    raw = os.environ.get("OW_COOKIE", "").strip()
    if not raw:
        return ""
    if "=" not in raw.split(";")[0]:
        # 允许只用 __session 值
        return "__session=" + raw
    return raw


def fetch(path="/createvps"):
    ck = cookie_header()
    req = urllib.request.Request(BASE + path, headers={
        "Cookie": ck,
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/dashboard",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.geturl(), r.read().decode("utf-8", "ignore")


def parse_plans(page):
    plans = []
    for m in re.finditer(r"data-plan='([^']+)'", page):
        try:
            plans.append(json.loads(H.unescape(m.group(1))))
        except Exception:
            pass
    return plans


def main():
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    quiet = os.environ.get("QUIET", "").lower() in ("1", "true", "yes")
    st = load_state()
    now = time.time()

    if not cookie_header():
        log("关键: 没有 OW_COOKIE")
        tg("❌ openworld 库存监控：没有配置 OW_COOKIE\n"
           "https://github.com/btpp03/openworld-stock-hermes")
        return 2

    try:
        status, url, page = fetch()
    except Exception as e:
        log("拉取失败:", e)
        tg("⚠️ openworld 库存监控：拉取 /createvps 失败\n%s\n"
           "https://github.com/btpp03/openworld-stock-hermes" % e)
        return 1

    log("http=%s url=%s len=%d" % (status, url, len(page)))
    plans = parse_plans(page)
    free = [p for p in plans if float(p.get("price") or 0) == 0]
    log("plans=%d free=%d" % (len(plans), len(free)))
    for p in free:
        log("  free plan: id=%s name=%s stock=%s node=%s"
            % (p.get("id"), p.get("name"), p.get("stock"), p.get("node_type")))

    # cookie / 登录失效判定
    if not plans or "sign in" in page.lower() or url.rstrip("/").endswith("/login"):
        log("会话失效")
        if now - st.get("cookie_alert", 0) > COOKIE_COOLDOWN or force:
            st["cookie_alert"] = now
            save_state(st)
            tg("⚠️ openworld 库存监控：登录会话失效，需要换 cookie\n"
               "拉到的地址: %s\n"
               "https://github.com/btpp03/openworld-stock-hermes" % url, quiet=quiet and not force)
        return 0

    save_state(st)  # 保留 cookie_alert

    avail = [p for p in free if int(p.get("stock") or 0) > 0]
    if not avail:
        log("免费档无库存")
        st["last_stock"] = 0
        save_state(st)
        if force:
            tg("ℹ️ openworld 库存监控自检正常：免费档当前 Out of stock\n"
               "https://github.com/btpp03/openworld-stock-hermes")
        return 0

    p = avail[0]
    locs = [l.get("name") for l in (p.get("locations") or [])]
    msg = ("🚨 openworld 免费 VPS 有库存了！\n"
           "计划: %s  stock=%s  节点: %s%s\n"
           "规格: %s vCPU / %s MB RAM / %s GB 盘\n"
           "→ 立刻下单: %s\n"
           "（下单要过站方 6 阶段点选人机验证，得你手点）\n"
           "https://github.com/btpp03/openworld-stock-hermes"
           % (p.get("name"), p.get("stock"), p.get("node_type"),
              (" / " + ", ".join(locs)) if locs else "",
              p.get("cores") or p.get("vcpu") or 1, p.get("ram"), p.get("disk"), TARGET))
    log("IN STOCK:", p.get("stock"))
    if now - st.get("stock_alert", 0) > STOCK_COOLDOWN or force:
        st["stock_alert"] = now
        st["last_stock"] = p.get("stock")
        save_state(st)
        tg(msg)
    else:
        log("[tg] 冷却期内，跳过（距上次 %d 分钟）"
            % int((now - st.get("stock_alert", 0)) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
