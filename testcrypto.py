#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CODE2CRYPTO AUTO MINER
Low-CPU multi-account mining bot
"""

import requests
import time
import os
import logging
from datetime import datetime, timezone
from threading import Thread, Event

# =========================================================
# CONFIG
# =========================================================

BASE_URL = "https://telegram-node.litmexpresale.com/api"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://bot.litmexpresale.com",
    "Referer": "https://bot.litmexpresale.com/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/139.0.0.0 Mobile Safari/537.36",
}

# Telegram – replace with your own
BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID   = "YOUR_CHAT_ID"

# Timing (adjust as needed)
WAIT_BETWEEN_ACCOUNTS = 2.0      # seconds between accounts
CYCLE_SLEEP_HOURS     = 8        # hours between full cycles
TG_POLL_TIMEOUT       = 25       # long-poll seconds
IDLE_SLEEP            = 10       # seconds when idle / between checks
LOG_LINES             = 40

QUERY_FILE = "query.txt"
LOG_FILE   = "bot.log"

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("miner")

# =========================================================
# STATE
# =========================================================

stop_event = Event()
last_run_time = None
is_running_cycle = False

# =========================================================
# TELEGRAM
# =========================================================

def tg_send(text: str, parse_mode: str = "HTML"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=12,
        )
    except Exception as e:
        log.warning(f"TG send failed: {e}")


def tg_get_updates(offset: int | None = None) -> list:
    try:
        params = {
            "timeout": TG_POLL_TIMEOUT,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params=params,
            timeout=TG_POLL_TIMEOUT + 10,
        )
        return r.json().get("result", [])
    except Exception:
        return []


def handle_command(text: str) -> str | None:
    cmd = text.strip().lower().split()[0] if text else ""

    if cmd in ("/logs", "/log"):
        if not os.path.exists(LOG_FILE):
            return "No log file yet."
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        tail = lines[-LOG_LINES:]
        body = "".join(tail).strip() or "(empty)"
        if len(body) > 3800:
            body = body[-3800:]
        return f"<b>Last {len(tail)} log lines</b>\n\n<code>{body}</code>"

    if cmd == "/status":
        status = "🔄 Running cycle" if is_running_cycle else "😴 Idle"
        last = last_run_time.strftime("%Y-%m-%d %H:%M:%S UTC") if last_run_time else "Never"
        return (
            f"<b>Bot Status</b>\n\n"
            f"State: {status}\n"
            f"Last full run: <code>{last}</code>\n"
            f"Cycle sleep: {CYCLE_SLEEP_HOURS} h\n"
            f"Delay between accounts: {WAIT_BETWEEN_ACCOUNTS} s"
        )

    if cmd in ("/help", "/start"):
        return (
            "<b>Commands</b>\n\n"
            "/logs – last log lines\n"
            "/status – current status\n"
            "/help – this message"
        )

    return None


def telegram_listener():
    """Low-CPU long-polling listener."""
    offset = None
    log.info("Telegram listener started")
    while not stop_event.is_set():
        updates = tg_get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                continue
            reply = handle_command(msg.get("text") or "")
            if reply:
                tg_send(reply)
        # tiny pause only if long-poll returned empty quickly
        if not updates:
            time.sleep(1)


# =========================================================
# MINING API
# =========================================================

def start_mining(auth: str) -> bool:
    headers = HEADERS.copy()
    headers["Authorization"] = auth.strip()
    try:
        r = requests.post(f"{BASE_URL}/mining/start", headers=headers, timeout=18)
        return r.status_code == 201
    except Exception:
        return False


def get_status(auth: str) -> tuple[bool, int]:
    """Returns (is_running, remaining_seconds)."""
    headers = HEADERS.copy()
    headers["Authorization"] = auth.strip()
    try:
        r = requests.get(f"{BASE_URL}/mining/status", headers=headers, timeout=18)
        if r.status_code != 200:
            return False, 0
        data = r.json().get("data", {})
        is_running = bool(data.get("isRunning"))
        end_time = data.get("endTime")
        if not end_time:
            return is_running, 0

        end_str = end_time.replace("Z", "+00:00")
        try:
            end_dt = datetime.fromisoformat(end_str)
        except ValueError:
            end_dt = datetime.strptime(
                end_time.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f"
            ).replace(tzinfo=timezone.utc)

        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        remaining = int((end_dt - datetime.now(timezone.utc)).total_seconds())
        return is_running, max(0, remaining)
    except Exception:
        return False, 0


# =========================================================
# ACCOUNTS
# =========================================================

def load_accounts() -> list[str]:
    if not os.path.exists(QUERY_FILE):
        with open(QUERY_FILE, "w", encoding="utf-8") as f:
            f.write("# One Bearer token per line\nBearer YOUR_TOKEN_HERE\n")
        log.error(f"{QUERY_FILE} created – add tokens and restart")
        raise SystemExit(1)

    with open(QUERY_FILE, "r", encoding="utf-8") as f:
        accounts = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    if not accounts:
        log.error("No valid tokens in query.txt")
        raise SystemExit(1)

    return accounts


# =========================================================
# MAIN CYCLE
# =========================================================

def run_cycle(accounts: list[str]):
    global last_run_time, is_running_cycle

    is_running_cycle = True
    total = len(accounts)
    success = already = failed = 0

    log.info(f"=== Cycle started | {total} accounts ===")
    tg_send(
        f"🚀 <b>Mining cycle started</b>\n"
        f"Accounts: <b>{total}</b>\n"
        f"Delay: {WAIT_BETWEEN_ACCOUNTS}s"
    )

    for idx, auth in enumerate(accounts, 1):
        if stop_event.is_set():
            break

        running, remaining = get_status(auth)

        if running and remaining > 0:
            already += 1
            if idx % 50 == 0 or idx == total:
                hrs, mins = divmod(remaining // 60, 60)
                log.info(f"[{idx}/{total}] Already mining \~{hrs}h {mins}m left")
        else:
            if start_mining(auth):
                success += 1
            else:
                failed += 1

            if idx % 50 == 0 or idx == total:
                log.info(
                    f"[{idx}/{total}] started:{success} already:{already} failed:{failed}"
                )

        time.sleep(WAIT_BETWEEN_ACCOUNTS)

    last_run_time = datetime.now(timezone.utc)
    is_running_cycle = False

    log.info(f"=== Cycle finished | success={success} already={already} failed={failed} ===")
    tg_send(
        f"✅ <b>Cycle finished</b>\n\n"
        f"Total        : <b>{total}</b>\n"
        f"✅ Started    : <b>{success}</b>\n"
        f"⚡ Already    : <b>{already}</b>\n"
        f"❌ Failed     : <b>{failed}</b>\n\n"
        f"Last run : <code>{last_run_time.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
        f"Next cycle in \~{CYCLE_SLEEP_HOURS}h\n\n"
        f"/logs  /status"
    )


def main():
    print(
        """
╔════════════════════════════════════════════╗
║       CODE2CRYPTO AUTO MINER               ║
║       Low-CPU • Clean • Multi-account      ║
╚════════════════════════════════════════════╝
"""
    )

    accounts = load_accounts()
    log.info(f"Loaded {len(accounts)} accounts")

    # Start listener
    Thread(target=telegram_listener, daemon=True).start()

    tg_send(
        f"🤖 <b>Bot started</b>\n"
        f"Accounts: <b>{len(accounts)}</b>\n"
        f"Commands: /logs  /status  /help"
    )

    try:
        while not stop_event.is_set():
            run_cycle(accounts)

            sleep_sec = CYCLE_SLEEP_HOURS * 3600
            log.info(f"Sleeping {CYCLE_SLEEP_HOURS} hours until next cycle...")
            # Sleep in larger chunks → very low CPU
            end = time.time() + sleep_sec
            while time.time() < end and not stop_event.is_set():
                time.sleep(IDLE_SLEEP)

    except KeyboardInterrupt:
        log.info("Stopped by user")
        stop_event.set()
        tg_send("🛑 <b>Bot stopped</b>")
    except Exception as e:
        log.exception(f"Fatal: {e}")
        tg_send(f"❌ <b>Bot crashed</b>\n<code>{e}</code>")
        stop_event.set()


if __name__ == "__main__":
    main()