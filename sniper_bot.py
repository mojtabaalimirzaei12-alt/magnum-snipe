#!/usr/bin/env python3
"""
Solana sniper bot (paper + live) - small-budget test version.

Flow: find new tokens (DexScreener) -> safety checks (mint authority, RugCheck,
round-trip sell quote) -> buy via Jupiter -> manage (take profit, stop loss,
trailing stop, max hold time).

Usage:
    python sniper_bot.py            run the bot (mode from config.json)
    python sniper_bot.py sellall    sell every open position and exit
"""
import base64
import json
import os
import sys
import time
from datetime import datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE, "config.json")
STATE_FILE = os.path.join(BASE, "state.json")
LOG_FILE = os.path.join(BASE, "trades.log")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000
DEX = "https://api.dexscreener.com"
BAD_EXT = {"transferFeeConfig", "permanentDelegate", "transferHook", "nonTransferable"}

DEFAULTS = {
    "mode": "paper",
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "jup_base": "https://lite-api.jup.ag/swap/v1",
    "wallet_file": "wallet.txt",
    "paper_balance_usd": 5.0,
    "buy_usd": 1.0,
    "max_total_usd": 3.0,
    "max_open": 3,
    "max_loss_usd": 2.0,
    "reserve_sol": 0.008,
    "take_profit_pct": 80,
    "stop_loss_pct": 25,
    "trail_activate_pct": 30,
    "trail_pct": 15,
    "max_hold_min": 90,
    "min_liquidity_usd": 15000,
    "min_mcap_usd": 30000,
    "min_volume_1h_usd": 10000,
    "min_age_min": 10,
    "max_age_hours": 48,
    "min_txns_1h": 60,
    "min_sells_1h": 15,
    "max_h1_pump_pct": 400,
    "min_h1_change_pct": -20,
    "buy_slippage_bps": 1000,
    "sell_slippage_bps": 2000,
    "max_roundtrip_loss_pct": 12,
    "max_rugcheck_score": 30,
    "priority_fee_lamports": 100000,
    "poll_sec": 20,
    "max_checks_per_cycle": 5,
    "max_quote_fail": 6,
    "verbose": True,
}

S = requests.Session()
S.headers.update({"User-Agent": "snipe-bot/1.0"})


# ---------------------------------------------------------------- utilities
def log(msg):
    line = "%s %s" % (datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_FILE):
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    if cfg["mode"] not in ("paper", "live"):
        sys.exit("mode must be paper or live")
    return cfg


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"positions": {}, "seen": [], "realized_lamports": 0, "paper_cash": None, "trades": 0}


def save_state(st):
    st["seen"] = st["seen"][-3000:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def get_json(url, params=None, timeout=15, tries=2):
    last = None
    for _ in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(3)
                last = "rate limited"
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError("GET failed %s: %s" % (url, last))


def rpc(cfg, method, params):
    r = S.post(cfg["rpc_url"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=25)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError("rpc %s: %s" % (method, j["error"]))
    return j["result"]


def usd(lam, price):
    return lam / LAMPORTS * price


# ---------------------------------------------------------------- jupiter
def jup_quote(cfg, inp, out, amount, slippage):
    q = get_json(
        cfg["jup_base"] + "/quote",
        {
            "inputMint": inp,
            "outputMint": out,
            "amount": str(int(amount)),
            "slippageBps": int(slippage),
            "restrictIntermediateTokens": "true",
        },
    )
    if "outAmount" not in q:
        raise RuntimeError("no route: %s" % q.get("error", q))
    return q


def sol_price_usd(cfg):
    q = jup_quote(cfg, WSOL, USDC, LAMPORTS, 100)
    return int(q["outAmount"]) / 1e6


# ---------------------------------------------------------------- wallet (live only)
class Wallet:
    def __init__(self, path):
        from solders.keypair import Keypair  # only needed in live mode

        p = path if os.path.isabs(path) else os.path.join(BASE, path)
        raw = open(p, "r", encoding="utf-8").read().strip()
        if raw.startswith("["):
            self.kp = Keypair.from_bytes(bytes(json.loads(raw)))
        else:
            self.kp = Keypair.from_base58_string(raw)
        self.pub = str(self.kp.pubkey())

    def sign(self, b64):
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(base64.b64decode(b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        return base64.b64encode(bytes(signed)).decode()


def sol_balance(cfg, owner):
    return rpc(cfg, "getBalance", [owner])["value"]


def token_balance(cfg, owner, mint):
    res = rpc(cfg, "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = 0
    for a in res["value"]:
        total += int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return total


def live_swap(cfg, wallet, quote):
    body = {
        "quoteResponse": quote,
        "userPublicKey": wallet.pub,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": int(cfg["priority_fee_lamports"]),
                "priorityLevel": "high",
            }
        },
    }
    r = S.post(cfg["jup_base"] + "/swap", json=body, timeout=25)
    r.raise_for_status()
    signed = wallet.sign(r.json()["swapTransaction"])
    sig = rpc(
        cfg,
        "sendTransaction",
        [signed, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3, "preflightCommitment": "processed"}],
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        res = rpc(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        s = res["value"][0]
        if s:
            if s.get("err"):
                raise RuntimeError("tx failed: %s" % (s["err"],))
            if s.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
    raise RuntimeError("tx not confirmed in 60s: %s" % sig)


# ---------------------------------------------------------------- safety checks
def mint_check(cfg, mint):
    res = rpc(cfg, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    v = res.get("value")
    if not v:
        return False, "mint not found"
    data = v.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not parsed or parsed.get("type") != "mint":
        return False, "not a mint"
    info = parsed.get("info", {})
    if info.get("mintAuthority"):
        return False, "mint authority still active"
    if info.get("freezeAuthority"):
        return False, "freeze authority still active"
    for e in info.get("extensions") or []:
        if e.get("extension") in BAD_EXT:
            return False, "risky extension: %s" % e.get("extension")
    return True, "ok"


def rug_check(cfg, mint):
    j = get_json("https://api.rugcheck.xyz/v1/tokens/%s/report/summary" % mint)
    danger = [str(r.get("name")) for r in (j.get("risks") or []) if str(r.get("level", "")).lower() == "danger"]
    if danger:
        return False, "rugcheck danger: " + ", ".join(danger[:3])
    score = j.get("score_normalised", j.get("score"))
    if score is not None and score > cfg["max_rugcheck_score"]:
        return False, "rugcheck score %s" % score
    return True, "ok"


def pair_ok(cfg, p):
    """returns (ok, reason, retry_later)"""
    liq = (p.get("liquidity") or {}).get("usd") or 0
    if liq < cfg["min_liquidity_usd"]:
        return False, "liquidity %.0f" % liq, False
    mc = p.get("marketCap") or p.get("fdv") or 0
    if mc < cfg["min_mcap_usd"]:
        return False, "mcap %.0f" % mc, False
    created = p.get("pairCreatedAt")
    if created:
        age_min = (time.time() * 1000 - created) / 60000.0
        if age_min < cfg["min_age_min"]:
            return False, "too new (%.0f min)" % age_min, True
        if age_min > cfg["max_age_hours"] * 60:
            return False, "too old", False
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    if buys + sells < cfg["min_txns_1h"]:
        return False, "few txns %d" % (buys + sells), False
    if sells < cfg["min_sells_1h"]:
        return False, "few sells %d (honeypot sign)" % sells, False
    vol = (p.get("volume") or {}).get("h1") or 0
    if vol < cfg["min_volume_1h_usd"]:
        return False, "volume %.0f" % vol, False
    ch = (p.get("priceChange") or {}).get("h1")
    if ch is not None and ch > cfg["max_h1_pump_pct"]:
        return False, "already pumped %.0f%%" % ch, False
    if ch is not None and ch < cfg["min_h1_change_pct"]:
        return False, "falling %.0f%%" % ch, False
    return True, "ok", False


# ---------------------------------------------------------------- discovery
def discover():
    addrs = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            data = get_json(DEX + path)
            if isinstance(data, dict):
                data = [data]
            for t in data or []:
                if t.get("chainId") == "solana" and t.get("tokenAddress"):
                    addrs.append(t["tokenAddress"])
        except Exception as e:
            log("discover error: %s" % e)
    return list(dict.fromkeys(addrs))


def best_pairs(addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i : i + 30]
        try:
            data = get_json("%s/tokens/v1/solana/%s" % (DEX, ",".join(chunk)))
        except Exception as e:
            log("pairs error: %s" % e)
            continue
        for p in data or []:
            a = (p.get("baseToken") or {}).get("address")
            if not a:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            old = out.get(a)
            old_liq = ((old.get("liquidity") or {}).get("usd") or 0) if old else -1
            if liq > old_liq:
                out[a] = p
    return out


# ---------------------------------------------------------------- trading
def open_cost(st):
    return sum(p["cost_lamports"] for p in st["positions"].values())


def killed(cfg, st, sol_price):
    return usd(st["realized_lamports"], sol_price) <= -cfg["max_loss_usd"]


def try_buy(cfg, st, wallet, mint, symbol, sol_price):
    spend = int(cfg["buy_usd"] / sol_price * LAMPORTS)
    if usd(open_cost(st) + spend, sol_price) > cfg["max_total_usd"] + 0.01:
        log("skip %s: budget limit reached" % symbol)
        return False

    ok, why = mint_check(cfg, mint)
    if not ok:
        log("skip %s: %s" % (symbol, why))
        return False
    ok, why = rug_check(cfg, mint)
    if not ok:
        log("skip %s: %s" % (symbol, why))
        return False

    q = jup_quote(cfg, WSOL, mint, spend, cfg["buy_slippage_bps"])
    out = int(q["outAmount"])
    q2 = jup_quote(cfg, mint, WSOL, out, cfg["sell_slippage_bps"])
    back = int(q2["outAmount"])
    loss = (1 - back / float(spend)) * 100
    if loss > cfg["max_roundtrip_loss_pct"]:
        log("skip %s: round-trip loss %.1f%% (tax/low liquidity?)" % (symbol, loss))
        return False

    if cfg["mode"] == "paper":
        if st["paper_cash"] < spend:
            log("skip %s: paper cash too low" % symbol)
            return False
        st["paper_cash"] -= spend
        tokens, sig = out, "paper"
    else:
        if sol_balance(cfg, wallet.pub) < spend + int(cfg["reserve_sol"] * LAMPORTS):
            log("skip %s: wallet SOL too low (keeping reserve)" % symbol)
            return False
        sig = live_swap(cfg, wallet, q)
        tokens = 0
        for _ in range(5):
            tokens = token_balance(cfg, wallet.pub, mint)
            if tokens > 0:
                break
            time.sleep(3)
        if tokens == 0:
            log("WARNING %s: swap confirmed but token balance is 0 (%s)" % (symbol, sig))
            return False

    st["positions"][mint] = {
        "symbol": symbol,
        "cost_lamports": spend,
        "tokens": tokens,
        "entry_ts": time.time(),
        "peak_value": spend,
        "quote_fail": 0,
        "sell_fail": 0,
    }
    st["trades"] += 1
    log("BUY  %s  $%.2f  mode=%s  sig=%s  mint=%s" % (symbol, usd(spend, sol_price), cfg["mode"], sig, mint))
    return True


def do_sell(cfg, st, wallet, mint, pos, value, reason, sol_price):
    sym = pos["symbol"]
    if cfg["mode"] == "paper":
        st["paper_cash"] += value
    else:
        actual = token_balance(cfg, wallet.pub, mint)
        if actual <= 0:
            log("%s: no tokens left in wallet, dropping position" % sym)
            del st["positions"][mint]
            return True
        slip = min(5000, cfg["sell_slippage_bps"] + 500 * pos.get("sell_fail", 0))
        try:
            q = jup_quote(cfg, mint, WSOL, actual, slip)
            sig = live_swap(cfg, wallet, q)
            value = int(q["outAmount"])
        except Exception as e:
            pos["sell_fail"] = pos.get("sell_fail", 0) + 1
            log("SELL FAILED %s (try %d): %s" % (sym, pos["sell_fail"], e))
            return False
        log("sell sig %s" % sig)
    pnl = value - pos["cost_lamports"]
    st["realized_lamports"] += pnl
    del st["positions"][mint]
    log(
        "SELL %s  %s  pnl=%+.2f$ (%+.0f%%)  total realized=%+.2f$"
        % (
            sym,
            reason,
            usd(pnl, sol_price),
            pnl / float(pos["cost_lamports"]) * 100,
            usd(st["realized_lamports"], sol_price),
        )
    )
    return True


def manage(cfg, st, wallet, sol_price, force=False):
    for mint, pos in list(st["positions"].items()):
        try:
            q = jup_quote(cfg, mint, WSOL, pos["tokens"], cfg["sell_slippage_bps"])
        except Exception as e:
            pos["quote_fail"] = pos.get("quote_fail", 0) + 1
            log("%s: cannot quote sell (%d): %s" % (pos["symbol"], pos["quote_fail"], e))
            if pos["quote_fail"] >= cfg["max_quote_fail"]:
                if cfg["mode"] == "paper":
                    st["realized_lamports"] -= pos["cost_lamports"]
                    del st["positions"][mint]
                    log("WRITE-OFF %s (no sell route) loss=%.2f$" % (pos["symbol"], usd(pos["cost_lamports"], sol_price)))
                else:
                    log("WARNING %s: no sell route for a long time. Check it manually. mint=%s" % (pos["symbol"], mint))
            continue
        pos["quote_fail"] = 0
        value = int(q["outAmount"])
        cost = pos["cost_lamports"]
        pnl = (value / float(cost) - 1) * 100
        pos["peak_value"] = max(pos.get("peak_value", cost), value)
        peak_pnl = (pos["peak_value"] / float(cost) - 1) * 100
        held_min = (time.time() - pos["entry_ts"]) / 60.0

        reason = None
        if force:
            reason = "manual sellall"
        elif pnl >= cfg["take_profit_pct"]:
            reason = "take profit"
        elif pnl <= -cfg["stop_loss_pct"]:
            reason = "stop loss"
        elif peak_pnl >= cfg["trail_activate_pct"] and value <= pos["peak_value"] * (1 - cfg["trail_pct"] / 100.0):
            reason = "trailing stop"
        elif held_min >= cfg["max_hold_min"]:
            reason = "max hold time"

        if reason:
            do_sell(cfg, st, wallet, mint, pos, value, reason, sol_price)
        elif cfg["verbose"]:
            log("hold %s  pnl=%+.1f%%  peak=%+.1f%%  held=%.0fm" % (pos["symbol"], pnl, peak_pnl, held_min))


def scan_and_buy(cfg, st, wallet, sol_price):
    if len(st["positions"]) >= cfg["max_open"]:
        return
    if killed(cfg, st, sol_price):
        log("loss limit reached (%.2f$), no new buys" % cfg["max_loss_usd"])
        return
    addrs = [a for a in discover() if a not in st["seen"] and a not in st["positions"]]
    if not addrs:
        return
    pairs = best_pairs(addrs[:90])
    checks = 0
    for a in addrs:
        if checks >= cfg["max_checks_per_cycle"] or len(st["positions"]) >= cfg["max_open"]:
            break
        p = pairs.get(a)
        if not p:
            continue  # no pair yet, look again next cycle
        sym = (p.get("baseToken") or {}).get("symbol", "?")
        ok, why, retry = pair_ok(cfg, p)
        if not ok:
            if not retry:
                st["seen"].append(a)
            if cfg["verbose"]:
                log("skip %s: %s" % (sym, why))
            continue
        checks += 1
        st["seen"].append(a)
        try:
            try_buy(cfg, st, wallet, a, sym, sol_price)
        except Exception as e:
            log("buy error %s: %s" % (sym, e))
        time.sleep(1)


# ---------------------------------------------------------------- main
def summary(cfg, st, sol_price):
    cash = ""
    if cfg["mode"] == "paper" and st["paper_cash"] is not None:
        cash = " paper cash=%.2f$" % usd(st["paper_cash"], sol_price)
    log(
        "status: open=%d trades=%d realized=%+.2f$%s"
        % (len(st["positions"]), st["trades"], usd(st["realized_lamports"], sol_price), cash)
    )


def main():
    cfg = load_cfg()
    st = load_state()
    sellall = len(sys.argv) > 1 and sys.argv[1] == "sellall"

    wallet = None
    if cfg["mode"] == "live":
        wallet = Wallet(cfg["wallet_file"])
        bal = sol_balance(cfg, wallet.pub)
        log("LIVE mode. wallet=%s balance=%.4f SOL" % (wallet.pub, bal / LAMPORTS))
        log("Only use a separate wallet with money you can afford to lose.")
    else:
        log("PAPER mode (no real money).")

    sol_price = sol_price_usd(cfg)
    log("SOL price: %.2f$" % sol_price)
    if cfg["mode"] == "paper" and st["paper_cash"] is None:
        st["paper_cash"] = int(cfg["paper_balance_usd"] / sol_price * LAMPORTS)

    if sellall:
        manage(cfg, st, wallet, sol_price, force=True)
        save_state(st)
        summary(cfg, st, sol_price)
        return

    try:
        while True:
            try:
                sol_price = sol_price_usd(cfg)
            except Exception as e:
                log("price error (using old price): %s" % e)
            try:
                manage(cfg, st, wallet, sol_price)
                scan_and_buy(cfg, st, wallet, sol_price)
            except Exception as e:
                log("cycle error: %s" % e)
            save_state(st)
            summary(cfg, st, sol_price)
            time.sleep(cfg["poll_sec"])
    except KeyboardInterrupt:
        save_state(st)
        log("stopped. open positions stay open; run 'python sniper_bot.py sellall' to close them.")


if __name__ == "__main__":
    main()
