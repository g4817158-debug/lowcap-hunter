#!/usr/bin/env python3
"""
Lowcap Hunter - Solana wallet scanner built on the gmgn-cli.

Modes:
  python scanner.py probe            -> run CLI commands, dump RAW json to /app/data/probe/
                                        (run this FIRST so we can see the real field names)
  python scanner.py scan             -> one full scan, tier wallets, export JSON, post to Telegram
  python scanner.py loop             -> scan every SCAN_EVERY_HOURS hours (Railway default)

Env vars (set in Railway):
  GMGN_API_KEY          required
  TELEGRAM_BOT_TOKEN    optional (to receive results)
  TELEGRAM_CHAT_ID      optional
  SEED_WALLETS          optional, comma-separated extra wallets to deep scan
  REQUEST_DELAY_SEC     default 2.5  (GMGN rate limits are strict; do not go lower)
  MAX_WALLETS           default 60   (cap per scan)
  SCAN_EVERY_HOURS      default 12
  DATA_DIR              default /app/data
"""

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import uuid
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DELAY = float(os.getenv("REQUEST_DELAY_SEC", "2.5"))
MAX_WALLETS = int(os.getenv("MAX_WALLETS", "60"))
SCAN_EVERY_HOURS = float(os.getenv("SCAN_EVERY_HOURS", "12"))
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
CHAIN = "sol"

# Tier thresholds. Edit these, they are the whole "edge".
# A wallet must pass ALL gates of a tier. Checked S -> A -> B -> C.
# If a metric is missing from GMGN's response (None), that gate FAILS for S and A,
# so unknown data can never put a wallet in a top tier.
TIERS = {
    "S": dict(min_tokens=10, min_winrate=0.45, max_median_entry_mcap=50_000,  min_pnl_30d=20_000, max_flags=0),
    "A": dict(min_tokens=8,  min_winrate=0.40, max_median_entry_mcap=100_000, min_pnl_30d=8_000,  max_flags=1),
    "B": dict(min_tokens=5,  min_winrate=0.35, max_median_entry_mcap=250_000, min_pnl_30d=2_000,  max_flags=2),
    "C": dict(min_tokens=3,  min_winrate=0.30, max_median_entry_mcap=None,    min_pnl_30d=500,    max_flags=3),
}
MIN_AVG_HOLD_SEC_SNIPER_FLAG = 20   # avg hold below this = sniper you can't copy

os.makedirs(DATA_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# CLI WRAPPER (rate limited)
# ----------------------------------------------------------------------------
_last_call = 0.0


def cli(args, retries=3):
    """Run gmgn-cli with --raw style JSON output where possible. Returns parsed JSON or None."""
    global _last_call
    for attempt in range(retries):
        wait = DELAY - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            p = subprocess.run(
                ["gmgn-cli"] + args,
                capture_output=True, text=True, timeout=60,
                env={**os.environ},
            )
        except Exception as e:
            log(f"cli error {args}: {e}")
            continue
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            if "429" in err or "rate" in err.lower():
                backoff = 10 * (attempt + 1)
                log(f"rate limited, sleeping {backoff}s")
                time.sleep(backoff)
                continue
            log(f"cli failed {args}: {err[:300]}")
            return None
        try:
            return json.loads(out)
        except Exception:
            # some commands may print text before json; try to find the first { or [
            for i, ch in enumerate(out):
                if ch in "[{":
                    try:
                        return json.loads(out[i:])
                    except Exception:
                        break
            log(f"non-json output for {args}: {out[:200]}")
            return None
    return None


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# FLEXIBLE FIELD ACCESS
# GMGN field names are not documented to me, so we try several candidates.
# After running `probe`, we replace these guesses with the exact names.
# ----------------------------------------------------------------------------
def dig(obj, *names):
    """Find first matching key anywhere (shallow then nested) in obj."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] not in (None, ""):
                return obj[n]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = dig(v, *names)
                if r is not None:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = dig(v, *names)
            if r is not None:
                return r
    return None


def num(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def find_list(obj, *names):
    """Find a list of dicts under any of the key names."""
    v = dig(obj, *names)
    return v if isinstance(v, list) else []


# ----------------------------------------------------------------------------
# DISCOVERY: where do candidate wallets come from?
# ----------------------------------------------------------------------------
def discover_wallets():
    wallets = {}

    # 1) Smart money + KOL trades from gmgn track
    for sub in (["track", "smartmoney", "--chain", CHAIN], ["track", "kol", "--chain", CHAIN]):
        data = cli(sub)
        if not data:
            continue
        rows = find_list(data, "list", "data", "trades", "items", "history")
        if not rows and isinstance(data, list):
            rows = data
        for r in rows:
            addr = dig(r, "maker", "wallet", "wallet_address", "address")
            if isinstance(addr, str) and len(addr) > 30:
                wallets[addr] = wallets.get(addr, 0) + 1

    # 2) your own seed wallets
    for a in os.getenv("SEED_WALLETS", "").split(","):
        a = a.strip()
        if a:
            wallets[a] = wallets.get(a, 0) + 5

    # most frequently seen first
    ranked = sorted(wallets, key=lambda w: wallets[w], reverse=True)
    log(f"discovered {len(ranked)} candidate wallets")
    return ranked[:MAX_WALLETS]


# ----------------------------------------------------------------------------
# WALLET METRICS
# ----------------------------------------------------------------------------
def wallet_metrics(addr):
    data = cli(["portfolio", "wallet", "--address", addr, "--chain", CHAIN])
    if not data:
        return None

    m = {"address": addr}
    m["pnl_30d"] = num(dig(data, "realized_profit_30d", "pnl_30d", "realized_profit", "total_profit"))
    m["roi_30d"] = num(dig(data, "pnl_30d_roi", "roi_30d", "roi"))
    m["winrate"] = num(dig(data, "winrate_30d", "winrate", "win_rate"))
    if m["winrate"] is not None and m["winrate"] > 1:
        m["winrate"] /= 100.0
    m["tokens"] = num(dig(data, "token_num", "tokens_traded", "total_tokens", "token_count"))
    m["avg_hold_sec"] = num(dig(data, "avg_holding_peroid", "avg_hold_time", "avg_holding_time"))
    m["funder"] = dig(data, "funded_by", "funder", "funding_address", "fund_from")
    m["tags"] = dig(data, "tags", "wallet_tags") or []

    # median entry mcap: only if per-token rows expose an entry/buy market cap
    rows = find_list(data, "holdings", "tokens", "activities", "list")
    entries = []
    for r in rows:
        mc = num(dig(r, "buy_market_cap", "entry_market_cap", "avg_buy_mcap", "first_buy_mcap"))
        if mc:
            entries.append(mc)
    m["median_entry_mcap"] = statistics.median(entries) if len(entries) >= 3 else None
    return m


def red_flags(m, funders):
    flags = []
    if m.get("tokens") is not None and m["tokens"] < 5:
        flags.append("low sample (<5 tokens)")
    if m.get("avg_hold_sec") is not None and m["avg_hold_sec"] < MIN_AVG_HOLD_SEC_SNIPER_FLAG:
        flags.append("pure sniper hold time")
    if m.get("funder") and funders.get(m["funder"], 0) > 1:
        flags.append("same-funder cluster")
    if m.get("winrate") is not None and m["winrate"] > 0.85 and (m.get("tokens") or 0) < 10:
        flags.append("unrealistic winrate on small sample")
    if m.get("median_entry_mcap") is None:
        flags.append("entry mcap unknown")
    return flags


def assign_tier(m):
    n_flags = len(m["flags"])
    for tier in ("S", "A", "B", "C"):
        t = TIERS[tier]
        strict = tier in ("S", "A")
        checks = [
            (m.get("tokens"), lambda v: v >= t["min_tokens"]),
            (m.get("winrate"), lambda v: v >= t["min_winrate"]),
            (m.get("pnl_30d"), lambda v: v >= t["min_pnl_30d"]),
        ]
        if t["max_median_entry_mcap"] is not None:
            checks.append((m.get("median_entry_mcap"), lambda v: v <= t["max_median_entry_mcap"]))
        ok = True
        for val, fn in checks:
            if val is None:
                if strict:
                    ok = False
                    break
                continue  # unknown data does not block B/C, but flags are counted
            if not fn(val):
                ok = False
                break
        if ok and n_flags <= t["max_flags"]:
            return tier
    return None


# ----------------------------------------------------------------------------
# EXPORT
# ----------------------------------------------------------------------------
def note_for(m):
    parts = []
    if m.get("pnl_30d") is not None:
        parts.append(f"30d PnL ${m['pnl_30d']:,.0f}")
    if m.get("roi_30d") is not None:
        parts.append(f"ROI {m['roi_30d']*100:.0f}%" if abs(m["roi_30d"]) < 100 else f"ROI {m['roi_30d']:.0f}%")
    if m.get("winrate") is not None:
        parts.append(f"WR {m['winrate']*100:.0f}%")
    if m.get("median_entry_mcap"):
        parts.append(f"median entry ${m['median_entry_mcap']:,.0f}")
    if m["flags"]:
        parts.append("flags: " + ", ".join(m["flags"]))
    return " | ".join(parts)


def export(results):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_dir = os.path.join(DATA_DIR, "exports", stamp)
    os.makedirs(out_dir, exist_ok=True)
    files = []
    counters = {"S": 0, "A": 0, "B": 0, "C": 0}
    all_rows = []
    by_tier = {"S": [], "A": [], "B": [], "C": []}
    for m in results:
        t = m["tier"]
        counters[t] += 1
        row = {
            "address": m["address"],
            "name": f"{t}-{counters[t]:02d}",
            "groups": [f"TIER {t}"],
            "note": note_for(m),
        }
        by_tier[t].append(row)
        all_rows.append(row)

    def dump(name, rows):
        p = os.path.join(out_dir, name)
        with open(p, "w") as f:
            json.dump(rows, f, indent=2)
        files.append(p)

    dump("gmgn_import_all.json", all_rows)
    for t, rows in by_tier.items():
        dump(f"gmgn_import_{t}.json", rows)
    # full metrics for your own analysis
    dump("full_metrics.json", results)
    return files, counters


# ----------------------------------------------------------------------------
# STATE (so re-scans can report only NEW wallets and track tier changes)
# ----------------------------------------------------------------------------
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"wallets": {}}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------
def tg_send(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("telegram not configured, skipping")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    except Exception as e:
        log(f"telegram text failed: {e}")


def tg_send_file(path):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/json\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"telegram file failed: {e}")


# ----------------------------------------------------------------------------
# MODES
# ----------------------------------------------------------------------------
def probe():
    """Dump raw CLI output so field names can be confirmed. Safe, read only."""
    out = os.path.join(DATA_DIR, "probe")
    os.makedirs(out, exist_ok=True)
    cmds = {
        "trending": ["market", "trending", "--chain", CHAIN, "--interval", "1h", "--limit", "3"],
        "smartmoney": ["track", "smartmoney", "--chain", CHAIN],
        "kol": ["track", "kol", "--chain", CHAIN],
    }
    sample_wallet = None
    for name, args in cmds.items():
        data = cli(args)
        with open(os.path.join(out, f"{name}.json"), "w") as f:
            json.dump(data, f, indent=2)
        log(f"probe {name}: {'ok' if data else 'FAILED'}")
        if data and not sample_wallet:
            sample_wallet = dig(data, "maker", "wallet", "wallet_address", "address")
    if sample_wallet:
        data = cli(["portfolio", "wallet", "--address", sample_wallet, "--chain", CHAIN])
        with open(os.path.join(out, "portfolio_wallet.json"), "w") as f:
            json.dump(data, f, indent=2)
        log(f"probe portfolio_wallet: {'ok' if data else 'FAILED'}")
    # post a compact summary to telegram so you can read it on your phone
    summary = []
    for fn in sorted(os.listdir(out)):
        with open(os.path.join(out, fn)) as f:
            txt = f.read()
        summary.append(f"== {fn} ({len(txt)} bytes) ==\n{txt[:700]}")
    tg_send("PROBE RESULT\n\n" + "\n\n".join(summary))
    for fn in sorted(os.listdir(out)):
        tg_send_file(os.path.join(out, fn))
    log("probe done. Files in " + out)


def scan():
    if not os.getenv("GMGN_API_KEY"):
        log("GMGN_API_KEY missing. Set it in Railway variables.")
        return
    state = load_state()
    candidates = discover_wallets()
    if not candidates:
        tg_send("Lowcap Hunter: no candidate wallets found. Run probe mode and send me the output.")
        return

    metrics = []
    for i, addr in enumerate(candidates, 1):
        log(f"[{i}/{len(candidates)}] {addr[:8]}...")
        m = wallet_metrics(addr)
        if m:
            metrics.append(m)

    funders = {}
    for m in metrics:
        if m.get("funder"):
            funders[m["funder"]] = funders.get(m["funder"], 0) + 1

    results = []
    for m in metrics:
        m["flags"] = red_flags(m, funders)
        tier = assign_tier(m)
        if tier:
            m["tier"] = tier
            results.append(m)

    order = {"S": 0, "A": 1, "B": 2, "C": 3}
    results.sort(key=lambda x: (order[x["tier"]], -(x.get("pnl_30d") or 0)))

    files, counters = export(results)

    new_wallets = [m for m in results if m["address"] not in state["wallets"]]
    changed = [m for m in results
               if m["address"] in state["wallets"] and state["wallets"][m["address"]]["tier"] != m["tier"]]
    now = datetime.now(timezone.utc).isoformat()
    for m in results:
        prev = state["wallets"].get(m["address"], {})
        state["wallets"][m["address"]] = {"tier": m["tier"], "first_seen": prev.get("first_seen", now), "last_seen": now}
    save_state(state)

    lines = [
        "LOWCAP HUNTER SCAN DONE",
        f"scanned {len(metrics)}/{len(candidates)} wallets",
        f"S:{counters['S']}  A:{counters['A']}  B:{counters['B']}  C:{counters['C']}",
        f"new this scan: {len(new_wallets)} | tier changed: {len(changed)}",
        "",
    ]
    for m in results[:10]:
        lines.append(f"{m['tier']} {m['address'][:6]}..{m['address'][-4:]} {note_for(m)}")
    lines.append("\nNote: GMGN PnL may not subtract all fees/tips. Past PnL is not a guarantee.")
    tg_send("\n".join(lines))
    for p in files:
        tg_send_file(p)
    log("scan complete")


def loop():
    while True:
        try:
            scan()
        except Exception as e:
            log(f"scan crashed: {e}")
            tg_send(f"Lowcap Hunter crashed: {e}")
        time.sleep(SCAN_EVERY_HOURS * 3600)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"probe": probe, "scan": scan, "loop": loop}.get(mode, scan)()
