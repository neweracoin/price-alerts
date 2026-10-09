"""Token price alerts: checks prices on DexScreener and notifies your phone via ntfy.

Runs once per call. Schedule it (GitHub Actions does this every 10 minutes) and it
remembers what it already told you in state.json, so you aren't spammed.

No packages to install: uses only the Python standard library (Python 3.11+).
"""

import json
import math
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

CONFIG_FILE = os.environ.get("ALERTS_CONFIG", "alerts.toml")
STATE_FILE = os.environ.get("ALERTS_STATE", "state.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
FORCE_UPDATE = os.environ.get("FORCE_UPDATE") == "1"  # manual run: send every price now

REARM_MARGIN = 0.03   # a fired alert re-arms once price moves 3% back out of its zone
DEFAULT_NEAR = 3.0    # default +/- percent for "near" alerts
SLACK_SECONDS = 120   # scheduled runs drift; allow 2 minutes early for updates


# ---------- helpers ----------

def fmt_price(p):
    if p is None:
        return "?"
    if p >= 100:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.4f}".rstrip("0").rstrip(".")
    decimals = -math.floor(math.log10(p)) + 3  # four significant digits
    return f"${p:.{decimals}f}"


def fmt_money(x):
    if not x:
        return "?"
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= size:
            return f"${x / size:.1f}{suffix}"
    return f"${x:,.0f}"


def fmt_pct(x):
    return "?" if x is None else f"{x:+.1f}%"


def http_json(url, data=None):
    headers = {"User-Agent": "price-alerts/1.0", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "null")


# ---------- price data ----------

def fetch_pair(chain, address):
    """Return the most liquid DexScreener pair for this token, or None."""
    data = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    pairs = data.get("pairs") if isinstance(data, dict) else data
    pairs = [
        p for p in (pairs or [])
        if p.get("chainId") == chain
        and str(p.get("baseToken", {}).get("address", "")).lower() == address.lower()
        and p.get("priceUsd")
    ]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def read_pair(pair):
    pc = pair.get("priceChange") or {}
    return {
        "price": float(pair["priceUsd"]),
        "symbol": pair.get("baseToken", {}).get("symbol") or "?",
        "h1": pc.get("h1"),
        "h24": pc.get("h24"),
        "mcap": pair.get("marketCap") or pair.get("fdv"),
        "liq": (pair.get("liquidity") or {}).get("usd"),
        "url": pair.get("url"),
    }


# ---------- notifications ----------

def notify(topic, title, message, priority=3, tags=None, click=None):
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    try:
        http_json(NTFY_SERVER + "/", payload)
        print(f"  sent: {title} | {message}")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  could not send notification: {e}", file=sys.stderr)


def summary(d):
    parts = [f"1h {fmt_pct(d['h1'])}", f"24h {fmt_pct(d['h24'])}"]
    if d["mcap"]:
        parts.append(f"MC {fmt_money(d['mcap'])}")
    if d["liq"]:
        parts.append(f"Liq {fmt_money(d['liq'])}")
    return " · ".join(parts)


# ---------- alert rules ----------

def check_alert(rule, price):
    """Return (in_zone, out_of_zone_far_enough_to_rearm, description)."""
    when = rule.get("when", "near")
    target = float(rule["price"])
    if when == "above":
        return price >= target, price < target * (1 - REARM_MARGIN), f"rose above {fmt_price(target)}"
    if when == "below":
        return price <= target, price > target * (1 + REARM_MARGIN), f"fell below {fmt_price(target)}"
    band = float(rule.get("within_percent", DEFAULT_NEAR)) / 100
    gap = abs(price - target) / target
    return gap <= band, gap > band + REARM_MARGIN, f"is near your target {fmt_price(target)}"


def in_quiet_hours(cfg):
    spec = cfg.get("quiet_hours")
    if not spec:
        return False
    start, end = (int(x) for x in str(spec).split("-"))
    hour = datetime.now(ZoneInfo(cfg.get("timezone", "UTC"))).hour
    return start <= hour < end if start < end else hour >= start or hour < end


# ---------- main ----------

def main():
    with open(CONFIG_FILE, "rb") as f:
        cfg = tomllib.load(f)
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic or topic.startswith("change-me"):
        sys.exit("Set your ntfy topic: add an NTFY_TOPIC secret on GitHub, or ntfy_topic in alerts.toml.")

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    now = time.time()
    quiet = in_quiet_hours(cfg)
    new_state = {}

    for tok in cfg.get("token", []):
        chain = tok.get("chain", "solana")
        address = tok["address"].strip()
        key = f"{chain}:{address}"
        st = state.get(key, {})
        new_state[key] = st
        print(f"{tok.get('name', address)} ({chain})")

        try:
            pair = fetch_pair(chain, address)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  price lookup failed, will retry next run: {e}", file=sys.stderr)
            continue
        if not pair:
            if not st.get("missing_reported"):
                notify(topic, f"{tok.get('name', 'Token')}: not found",
                       f"DexScreener has no {chain} pair for {address}. Check the chain and address in alerts.toml.",
                       priority=3, tags=["warning"])
                st["missing_reported"] = True
            continue
        st.pop("missing_reported", None)

        d = read_pair(pair)
        name = tok.get("name") or d["symbol"]
        print(f"  price {fmt_price(d['price'])}  {summary(d)}")

        # 1. price targets
        fired = st.setdefault("fired", {})
        live_keys = set()
        for rule in tok.get("alerts", []):
            rkey = f"{rule.get('when', 'near')}|{rule['price']}|{rule.get('within_percent', '')}"
            live_keys.add(rkey)
            hit, rearm, desc = check_alert(rule, d["price"])
            if hit and not fired.get(rkey):
                notify(topic, f"{name} {desc}",
                       f"Now {fmt_price(d['price'])} · {summary(d)}",
                       priority=5, tags=["rotating_light"], click=d["url"])
                fired[rkey] = True
            elif fired.get(rkey) and rearm:
                fired[rkey] = False
        for old in set(fired) - live_keys:  # forget rules you deleted
            del fired[old]

        # 2. big moves in the last hour
        move = tok.get("move_alert_1h_percent")
        if move and d["h1"] is not None:
            if abs(d["h1"]) >= move and not st.get("move_fired"):
                up = d["h1"] > 0
                notify(topic, f"{name} {'up' if up else 'down'} {abs(d['h1']):.0f}% in 1h",
                       f"Now {fmt_price(d['price'])} · {summary(d)}",
                       priority=4, tags=["chart_with_upwards_trend" if up else "chart_with_downwards_trend"],
                       click=d["url"])
                st["move_fired"] = True
            elif abs(d["h1"]) < move / 2:
                st["move_fired"] = False

        # 3. regular price updates
        every = tok.get("update_every_minutes", 0)
        due = every and now - st.get("last_update", 0) >= every * 60 - SLACK_SECONDS
        if FORCE_UPDATE or (due and not quiet):
            notify(topic, f"{name} {fmt_price(d['price'])}", summary(d), priority=3,
                   tags=["chart_with_upwards_trend" if (d["h1"] or 0) >= 0 else "chart_with_downwards_trend"],
                   click=d["url"])
            st["last_update"] = now

    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)


if __name__ == "__main__":
    main()
