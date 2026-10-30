#!/usr/bin/env python3
"""openworld.eu.org 免费 VPS 监控

1) 库存：用已登录 cookie 拉 /createvps，解析 data-plan JSON 的 stock，免费档有货 → TG 报警（30 分钟冷却）
2) 续期：拉 /dashboard 找实例 → 逐个拉 /vps/<uuid> 解析 "Renews until <UTC>"，按剩余时间提醒
   - >48h 完全静默
   - 48h 内每天一条
   - 6h 内每小时一条
3) cookie 失效 → TG 报警（6 小时冷却）
状态落 state.json，由 Actions cache 滚动保存。
"""
import calendar
import html as H
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openworld.eu.org"
TARGET = BASE + "/createvps"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/155.0.0.0 Safari/537.36")
STOCK_COOLDOWN = 30 * 60       # 有货后 30 分钟内不重复推
COOKIE_COOLDOWN = 6 * 3600     # cookie 失效 6 小时提醒一次
RENEW_DAILY_WINDOW = 48 * 3600  # 48h 内开始每天提醒
RENEW_HOURLY_WINDOW = 6 * 3600  # 6h 内每小时提醒
STATE_FILE = "state.json"
REPO = "https://github.com/btpp03/openworld-stock-hermes"


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
        return "__session=" + raw   # 允许只放 __session 值
    return raw


def fetch(path="/createvps", attempts=3, backoff=(5, 15)):
    """单次 SSL/连接抖动不该报警：内层重试 attempts 次。"""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(BASE + path, headers={
            "Cookie": cookie_header(),
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE + "/dashboard",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            log("[fetch] 第 %d/%d 次失败: %s" % (i + 1, attempts, e))
            if i < attempts - 1:
                time.sleep(backoff[min(i, len(backoff) - 1)])
    raise last



def parse_plans(page):
    plans = []
    for m in re.finditer(r"data-plan='([^']+)'", page):
        try:
            plans.append(json.loads(H.unescape(m.group(1))))
        except Exception:
            pass
    return plans


def parse_renew_until(page):
    """返回 [(epoch, 'YYYY-MM-DD HH:MM:SS')]，页面时间是 UTC。

    页面结构: <span data-time="2026-11-02 22:16:56" data-time-tz>2026-11-02 22:16:56 (UTC)</span>
    """
    out = []
    pats = [
        r'data-time="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"[^>]*data-time-tz',
        r"Renews until[\s\S]{0,200}?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
    ]
    seen = set()
    for pat in pats:
        for s in re.findall(pat, page):
            try:
                ep = calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            if ep not in seen:
                seen.add(ep)
                out.append((ep, s))
    return out


def parse_instances(dash):
    """从 /dashboard 里抓实例 uuid + 展示名 + 状态（按 data-vps-uuid 切块）。"""
    insts = []
    for seg in re.split(r'(?=<a href="/vps/)', dash)[1:]:
        u = re.search(r'href="/vps/([0-9a-f\-]{36})"', seg)
        if not u:
            continue
        nm = re.search(r'dash-hostname">([^<]+)<', seg)
        st = (re.search(r'data-status="([a-z]+)"', seg)
              or re.search(r'data-status-label>([A-Za-z]+)<', seg))
        insts.append({
            "uuid": u.group(1),
            "name": (nm.group(1).strip() if nm else ""),
            "status": (st.group(1).capitalize() if st else ""),
        })
        if len(insts) >= 50:
            break
    return insts


def check_renew(st, now, quiet, force):
    try:
        status, url, dash = fetch("/dashboard")
    except Exception as e:
        log("[renew] dashboard 拉取失败:", e)
        return
    if "/login" in url or "sign in" in dash[:2000].lower():
        log("[renew] 会话失效，跳过续期检查")
        return
    insts = parse_instances(dash)
    log("[renew] 实例数=%d" % len(insts))
    if not insts:
        return
    rows = []
    for inst in insts:
        try:
            _, _, page = fetch("/vps/" + inst["uuid"])
        except Exception as e:
            log("[renew] 拉实例 %s 失败: %s" % (inst["uuid"], e))
            continue
        untils = parse_renew_until(page)
        nm = inst["name"] or inst["uuid"][:8]
        if not untils:
            log("[renew] %s 未解析到到期时间" % nm)
            continue
        epoch, raw = min(untils)
        rows.append({"uuid": inst["uuid"], "name": nm, "epoch": epoch,
                     "raw": raw, "status": inst["status"]})
        log("[renew] %s 到期北京 %s（剩 %.1f 小时）状态=%s"
            % (nm, bj(epoch), (epoch - now) / 3600.0, inst["status"]))
    if not rows:
        return
    rows.sort(key=lambda r: r["epoch"])
    soon = rows[0]
    left = soon["epoch"] - now

    if left > RENEW_DAILY_WINDOW and not force:
        log("[renew] 还剩 %.1f 小时 > 48h，静默" % (left / 3600.0))
        return

    if left > RENEW_HOURLY_WINDOW:
        key = "renew_day"
        stamp = bj(now)[:10]
        if st.get(key) == stamp and not force:
            log("[renew] 今日已提醒过，跳过")
            return
    else:
        key = "renew_hour"
        stamp = bj(now)[:13]
        if st.get(key) == stamp and not force:
            log("[renew] 本小时已提醒过，跳过")
            return

    st[key] = stamp
    save_state(st)

    hh = max(0.0, left / 3600.0)
    urgent = "🔴 " if hh <= 6 else "🟡 "
    msg = ("%sopenworld 实例 %s 还有 %.1f 小时到期（北京 %s）\n"
           "状态: %s\n"
           "→ 去续期: %s\n"
           "（点 \"Renew free\"，续期也要过站方验证码，得你手点）\n%s"
           % (urgent, soon["name"], hh, bj(soon["epoch"]),
              soon["status"] or "未知", BASE + "/vps/" + soon["uuid"], REPO))
    log("[renew] 提醒: %.1f 小时到期" % hh)
    tg(msg, quiet=quiet and not force)


def main():
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    quiet = os.environ.get("QUIET", "").lower() in ("1", "true", "yes")
    st = load_state()
    now = time.time()

    if not cookie_header():
        log("关键: 没有 OW_COOKIE")
        tg("❌ openworld 监控：没有配置 OW_COOKIE\n" + REPO)
        return 2

    FAIL_ALERT_AFTER = 6          # 连续 6 轮（约 12 分钟）才算站点真挂了
    FAIL_REPEAT = 3600            # 挂了之后每小时最多再提一次
    try:
        status, url, page = fetch("/createvps")
    except Exception as e:
        fails = int(st.get("fetch_fail") or 0) + 1
        st["fetch_fail"] = fails
        if fails == 1:
            st["fetch_first"] = now
        save_state(st)
        first = st.get("fetch_first") or now
        mins = int((now - first) / 60)
        log("拉取失败（连续 %d 次，已 %d 分钟）: %s" % (fails, mins, e))
        alerted = st.get("fail_alerted")
        # 用户明确要求：站点故障/恢复都不要推 TG，只写日志（只有"有货"才推送）
        log("[tg] 静默（连续 %d 轮失败，不打扰）" % fails)
        return 1

    # 恢复正常（同样不推送，只记日志）
    if int(st.get("fetch_fail") or 0) >= FAIL_ALERT_AFTER:
        mins = int((now - (st.get("fetch_first") or now)) / 60)
        log("[recover] 站点恢复，断了约 %d 分钟（不推送）" % mins)
    st["fetch_fail"] = 0
    st["fetch_first"] = 0
    st["fail_alerted"] = 0

    log("http=%s url=%s len=%d" % (status, url, len(page)))
    plans = parse_plans(page)
    free = [p for p in plans if float(p.get("price") or 0) == 0]
    log("plans=%d free=%d" % (len(plans), len(free)))
    for p in free:
        log("  free plan: id=%s name=%s stock=%s node=%s"
            % (p.get("id"), p.get("name"), p.get("stock"), p.get("node_type")))

    if not plans or "sign in" in page.lower() or url.rstrip("/").endswith("/login"):
        log("会话失效")
        if now - st.get("cookie_alert", 0) > COOKIE_COOLDOWN or force:
            st["cookie_alert"] = now
            save_state(st)
            tg("⚠️ openworld 登录失效，监控暂时看不到库存\n"
               "（我这边需要重登一次，重登前不会推有货通知）",
               quiet=quiet and not force)
        return 0

    save_state(st)  # 保留 cookie_alert

    avail = [p for p in free if int(p.get("stock") or 0) > 0]
    if not avail:
        log("免费档无库存")
        st["last_stock"] = 0
        save_state(st)
        if force:
            tg("ℹ️ openworld 监控自检正常：免费档当前 Out of stock\n" + REPO)
    else:
        p = avail[0]
        locs = [l.get("name") for l in (p.get("locations") or [])]
        disk = p.get("disk") or 0
        disk_s = ("%d GB" % round(disk / 1024)) if disk >= 1000 else ("%s MB" % disk)
        msg = ("🚨 免费位有货 stock=%s\n"
               "%s\n"
               "%s vCPU / %s MB / %s · %s\n"
               "（6 阶段点选，慢一步就没了）"
               % (p.get("stock"), TARGET,
                  p.get("cores") or p.get("vcpu") or 1, p.get("ram"), disk_s,
                  ", ".join(locs) if locs else (p.get("node_type") or "")))
        log("IN STOCK:", p.get("stock"))
        if now - st.get("stock_alert", 0) > STOCK_COOLDOWN or force:
            st["stock_alert"] = now
            st["last_stock"] = p.get("stock")
            save_state(st)
            tg(msg)
        else:
            log("[tg] 冷却期内，跳过（距上次 %d 分钟）"
                % int((now - st.get("stock_alert", 0)) / 60))

    check_renew(st, now, quiet, force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
