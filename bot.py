import krakenex
import time
import logging
import os
from datetime import datetime, timedelta, timezone
from collections import deque

# ============================================================
#  CONFIGURATION
# ============================================================
API_KEY    = os.environ.get("API_KEY", "METS_TA_CLE_API_ICI")
API_SECRET = os.environ.get("API_SECRET", "METS_TON_SECRET_ICI")

# ============================================================
#  PARAMETRES
# ============================================================
PUMP_THRESHOLD    = 5.0    # % hausse en 10 min pour acheter
WINDOW_MINUTES    = 10     # Fenetre detection pump
TRAILING_STOP_PCT = 5.0    # % chute depuis le plus haut -> vente benefice
STOP_LOSS_PCT     = 1.0    # % perte max depuis achat -> stop loss ABSOLU
DUMP_PCT          = 5.0    # % chute en 60s -> vente si en profit
DUMP_WINDOW_SEC   = 60     # Fenetre detection dump
SCAN_INTERVAL     = 30     # Secondes entre chaque scan
POSITION_INTERVAL = 3      # Secondes entre chaque verif en position
MIN_VOLUME_USD    = 50000  # Volume minimum USD
BATCH_SIZE        = 50     # Batch API Kraken

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("pumpbot.log")
    ]
)
log = logging.getLogger(__name__)

# ============================================================
#  CONNEXION KRAKEN
# ============================================================
k = krakenex.API(key=API_KEY, secret=API_SECRET)

# ============================================================
#  ETAT DU BOT
# ============================================================
position = {
    "active": False,
    "pair": None,
    "buy_price": None,
    "highest_price": None,
    "volume": None,
}

price_history  = {}
position_prices = deque(maxlen=1200)

# Blacklist automatique des tokens restreints FR
BLACKLISTED_PAIRS = set()


# ============================================================
#  FONCTIONS
# ============================================================

def get_usd_balance():
    try:
        resp = k.query_private("Balance")
        if resp.get("error"):
            log.error(f"Erreur balance: {resp['error']}")
            return 0.0
        balances = resp.get("result", {})
        usd = float(balances.get("ZUSD", 0)) + float(balances.get("USD", 0))
        log.info(f"Solde USD disponible : {usd:.2f} $")
        return usd
    except Exception as e:
        log.error(f"Exception get_usd_balance: {e}")
        return 0.0


def get_all_usd_pairs():
    try:
        resp = k.query_public("AssetPairs")
        if resp.get("error"):
            log.error(f"Erreur AssetPairs: {resp['error']}")
            return []
        pairs = []
        for pair_name, pair_info in resp["result"].items():
            quote  = pair_info.get("quote", "")
            status = pair_info.get("status", "online")
            if (quote in ("ZUSD", "USD") and
                ".d" not in pair_name and
                status == "online" and
                pair_name not in BLACKLISTED_PAIRS):
                pairs.append(pair_name)
        log.info(f"{len(pairs)} paires USD actives trouvees")
        return pairs
    except Exception as e:
        log.error(f"Exception get_all_usd_pairs: {e}")
        return []


def get_ticker(pairs):
    try:
        resp = k.query_public("Ticker", {"pair": ",".join(pairs)})
        if resp.get("error") and not resp.get("result"):
            return {}
        return resp.get("result", {})
    except Exception as e:
        log.error(f"Exception get_ticker: {e}")
        return {}


def record_prices(ticker_data):
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=15)
    for pair, data in ticker_data.items():
        try:
            price = float(data["c"][0])
            if price <= 0:
                continue
            if pair not in price_history:
                price_history[pair] = []
            price_history[pair].append((now, price))
            price_history[pair] = [(t, p) for t, p in price_history[pair] if t > cutoff]
        except (KeyError, ValueError, IndexError):
            continue


def detect_pump(ticker_data):
    now        = datetime.now(timezone.utc)
    cutoff_min = now - timedelta(minutes=WINDOW_MINUTES + 1)
    cutoff_max = now - timedelta(minutes=WINDOW_MINUTES - 1)
    best_pump  = None
    best_pct   = PUMP_THRESHOLD

    for pair, data in ticker_data.items():
        # Ignore les paires blacklistees
        if pair in BLACKLISTED_PAIRS:
            continue
        try:
            current_price = float(data["c"][0])
            volume_usd    = float(data["v"][1]) * current_price

            if volume_usd < MIN_VOLUME_USD or current_price <= 0:
                continue

            history = price_history.get(pair, [])

            # Prix de reference il y a exactement ~10 min
            ref_prices = [(t, p) for t, p in history if cutoff_min <= t <= cutoff_max]
            if not ref_prices:
                old = [(t, p) for t, p in history if t <= cutoff_max]
                if not old:
                    continue
                ref_prices = [old[-1]]

            ref_price = sum(p for _, p in ref_prices) / len(ref_prices)
            if ref_price <= 0:
                continue

            pct_change = ((current_price - ref_price) / ref_price) * 100

            # Verifie que ca monte encore MAINTENANT (momentum positif)
            recent = [(t, p) for t, p in history if t >= now - timedelta(minutes=2)]
            if len(recent) >= 2:
                momentum = ((recent[-1][1] - recent[0][1]) / recent[0][1]) * 100
                if momentum < 0:
                    continue  # Prix en train de baisser -> pas bon signal

            if pct_change > best_pct:
                best_pct  = pct_change
                best_pump = (pair, current_price, pct_change)

        except (KeyError, ValueError, IndexError, ZeroDivisionError):
            continue

    return best_pump


def detect_dump(current_price):
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=DUMP_WINDOW_SEC)

    while position_prices and position_prices[0][0] < cutoff:
        position_prices.popleft()

    if not position_prices:
        return False, 0.0

    highest_in_window = max(p for _, p in position_prices)
    if highest_in_window <= 0:
        return False, 0.0

    pct_drop = ((current_price - highest_in_window) / highest_in_window) * 100
    return (pct_drop <= -DUMP_PCT), pct_drop


def get_pair_info(pair):
    try:
        resp = k.query_public("AssetPairs", {"pair": pair})
        if resp.get("error"):
            return None
        result = resp.get("result", {})
        if not result:
            return None
        return list(result.values())[0]
    except Exception as e:
        log.error(f"Exception get_pair_info: {e}")
        return None


def get_real_crypto_balance(pair_info, base_currency):
    """Recupere le vrai solde de crypto sur Kraken avant de vendre."""
    try:
        resp = k.query_private("Balance")
        if resp.get("error"):
            return None
        balances = resp.get("result", {})
        balance = float(balances.get(base_currency, 0))
        if balance == 0:
            balance = float(balances.get("X" + base_currency, 0))
        if balance == 0:
            balance = float(balances.get("Z" + base_currency, 0))
        return balance if balance > 0 else None
    except Exception as e:
        log.error(f"Exception get_real_crypto_balance: {e}")
        return None


def buy_market(pair, usd_amount):
    try:
        pair_info = get_pair_info(pair)
        if not pair_info:
            return None

        # Securite USD
        quote = pair_info.get("quote", "")
        if quote not in ("ZUSD", "USD"):
            log.error(f"REFUS : {pair} n'est pas une paire USD")
            return None

        lot_decimals = int(pair_info.get("lot_decimals", 8))
        order_min    = float(pair_info.get("ordermin", 0))
        cost_min     = float(pair_info.get("costmin", 0))

        if usd_amount < cost_min:
            log.warning(f"Montant {usd_amount:.2f}$ < costmin {cost_min}$")
            return None

        ticker_resp = k.query_public("Ticker", {"pair": pair})
        if ticker_resp.get("error") or not ticker_resp.get("result"):
            return None

        ticker_result = list(ticker_resp["result"].values())[0]
        current_price = float(ticker_result["c"][0])

        if current_price <= 0:
            return None

        volume = round(usd_amount / current_price, lot_decimals)

        if volume < order_min:
            log.warning(f"Volume {volume} < ordermin {order_min}")
            return None

        log.info(f"[ACHAT] ACHAT {pair} | Prix: {current_price:.6f} | Volume: {volume} | Total: {usd_amount:.2f}$")

        resp = k.query_private("AddOrder", {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": str(volume),
            "oflags": "fciq"
        })

        if resp.get("error"):
            error_msg = str(resp["error"])
            log.error(f"Erreur AddOrder achat: {error_msg}")
            # Blackliste automatiquement les tokens restreints FR
            if "Invalid permissions" in error_msg or "trading restricted" in error_msg:
                log.warning(f"[BLACKLIST] {pair} restreint en France -> blackliste")
                BLACKLISTED_PAIRS.add(pair)
            return None

        log.info(f"[OK] Ordre achat place: {resp['result']}")
        return volume, current_price

    except Exception as e:
        log.error(f"Exception buy_market: {e}")
        return None


def sell_market(pair, volume, reason=""):
    try:
        pair_info     = get_pair_info(pair)
        base_currency = pair_info.get("base", "") if pair_info else ""
        lot_decimals  = int(pair_info.get("lot_decimals", 8)) if pair_info else 8

        # Recupere le VRAI solde de crypto sur Kraken
        real_balance = get_real_crypto_balance(pair_info, base_currency)
        if real_balance and real_balance < float(volume):
            log.warning(f"Volume ajuste: {volume} -> {real_balance} (solde reel)")
            volume = real_balance

        volume_str = str(round(float(volume), lot_decimals))
        log.info(f"[VENTE] VENTE {pair} | Volume: {volume_str} | Raison: {reason}")

        resp = k.query_private("AddOrder", {
            "pair": pair,
            "type": "sell",
            "ordertype": "market",
            "volume": volume_str
        })

        if resp.get("error"):
            log.error(f"Erreur AddOrder vente: {resp['error']}")
            return False

        log.info(f"[OK] Ordre vente place: {resp['result']}")
        return True

    except Exception as e:
        log.error(f"Exception sell_market: {e}")
        return False


def get_current_price(pair):
    try:
        resp = k.query_public("Ticker", {"pair": pair})
        if resp.get("error") or not resp.get("result"):
            return None
        return float(list(resp["result"].values())[0]["c"][0])
    except Exception as e:
        log.error(f"Exception get_current_price: {e}")
        return None


def reset_position():
    global position
    position = {
        "active": False,
        "pair": None,
        "buy_price": None,
        "highest_price": None,
        "volume": None,
    }
    position_prices.clear()
    log.info("[RESET] Position reinitialisee - En attente du prochain pump")


# ============================================================
#  BOUCLE PRINCIPALE
# ============================================================

def main():
    log.info("=" * 60)
    log.info("[BOT] PumpBot Kraken demarre")
    log.info(f"   Seuil pump       : +{PUMP_THRESHOLD}% en {WINDOW_MINUTES} min")
    log.info(f"   Stop loss        : -{STOP_LOSS_PCT}% depuis achat (PRIORITE 1)")
    log.info(f"   Dump brutal      : -{DUMP_PCT}% en {DUMP_WINDOW_SEC}s si en profit (PRIORITE 2)")
    log.info(f"   Trailing stop    : -{TRAILING_STOP_PCT}% depuis sommet si en profit (PRIORITE 3)")
    log.info(f"   Verif position   : toutes les {POSITION_INTERVAL}s")
    log.info("=" * 60)

    if API_KEY == "METS_TA_CLE_API_ICI":
        log.error("[ERREUR] Cles API non configurees ! Arret.")
        return

    all_pairs = get_all_usd_pairs()
    if not all_pairs:
        log.error("Impossible de recuperer les paires.")
        return

    log.info("[ATTENTE] Prechauffage de 10 minutes...")
    warmup_end = datetime.now(timezone.utc) + timedelta(minutes=10)

    while datetime.now(timezone.utc) < warmup_end:
        remaining = int((warmup_end - datetime.now(timezone.utc)).total_seconds())
        log.info(f"   Prechauffage... encore {remaining}s")
        for i in range(0, len(all_pairs), BATCH_SIZE):
            batch = all_pairs[i:i+BATCH_SIZE]
            ticker_data = get_ticker(batch)
            record_prices(ticker_data)
            time.sleep(1)
        time.sleep(SCAN_INTERVAL)

    log.info("[OK] Prechauffage termine - Le bot trade maintenant !")

    while True:
        try:
            # CAS 1 : Position active
            if position["active"]:
                current_price = get_current_price(position["pair"])

                if current_price is None:
                    time.sleep(2)
                    continue

                position_prices.append((datetime.now(timezone.utc), current_price))

                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    log.info(f"[HAUSSE] Nouveau plus haut : {current_price:.6f}")

                buy_price    = position["buy_price"]
                highest      = position["highest_price"]
                pct_from_buy = ((current_price - buy_price) / buy_price) * 100
                pct_from_top = ((current_price - highest) / highest) * 100
                is_dump, dump_pct = detect_dump(current_price)

                log.info(
                    f"[INFO] {position['pair']} | Prix: {current_price:.6f} | "
                    f"Achat: {pct_from_buy:+.2f}% | "
                    f"Sommet: {pct_from_top:+.2f}% | "
                    f"Dump60s: {dump_pct:+.2f}%"
                )

                # PRIORITE 1 — STOP LOSS -1% ABSOLU
                if pct_from_buy <= -STOP_LOSS_PCT:
                    log.warning(f"[STOP] STOP LOSS a {pct_from_buy:.2f}%")
                    success = sell_market(position["pair"], position["volume"], reason="STOP LOSS")
                    if success:
                        log.info(f"[PERTE] Trade ferme : {pct_from_buy:.2f}%")
                    else:
                        log.error("[ATTENTION] Vente echouee, nouvelle tentative...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="STOP LOSS RETRY")
                    reset_position()

                # PRIORITE 2 — DUMP BRUTAL si en profit
                elif is_dump and pct_from_buy > 0:
                    log.warning(f"[DUMP] DUMP BRUTAL en profit ! {dump_pct:.2f}%")
                    success = sell_market(position["pair"], position["volume"], reason="DUMP BRUTAL")
                    if success:
                        log.info(f"[BENEF] Sorti en profit : {pct_from_buy:.2f}%")
                    else:
                        log.error("[ATTENTION] Vente dump echouee, nouvelle tentative...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="DUMP RETRY")
                    reset_position()

                # PRIORITE 3 — TRAILING STOP si en profit
                elif pct_from_top <= -TRAILING_STOP_PCT and pct_from_buy > 0:
                    log.info(f"[TP] TRAILING STOP | Profit : {pct_from_buy:.2f}%")
                    success = sell_market(position["pair"], position["volume"], reason="TRAILING STOP")
                    if success:
                        log.info(f"[BENEF] Trade ferme en benefice : {pct_from_buy:.2f}%")
                    else:
                        log.error("[ATTENTION] Vente TP echouee, nouvelle tentative...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="TRAILING RETRY")
                    reset_position()

                time.sleep(POSITION_INTERVAL)

            # CAS 2 : Pas de position
            else:
                # Refresh des paires (pour inclure la blacklist)
                all_pairs = get_all_usd_pairs()
                log.info(f"[SCAN] Scan du marche ({len(all_pairs)} paires)...")

                all_ticker = {}
                for i in range(0, len(all_pairs), BATCH_SIZE):
                    batch = all_pairs[i:i+BATCH_SIZE]
                    ticker_data = get_ticker(batch)
                    record_prices(ticker_data)
                    all_ticker.update(ticker_data)
                    time.sleep(1)

                pump = detect_pump(all_ticker)

                if pump:
                    pair, price, pct = pump
                    log.info(f"[PUMP] PUMP DETECTE ! {pair} | +{pct:.2f}% en {WINDOW_MINUTES} min")

                    usd_balance = get_usd_balance()

                    if usd_balance < 10:
                        log.warning(f"Solde insuffisant : {usd_balance:.2f}$")
                        time.sleep(SCAN_INTERVAL)
                        continue

                    usd_to_use = usd_balance * 0.98
                    result     = buy_market(pair, usd_to_use)

                    if result:
                        volume, buy_price = result
                        position["active"]        = True
                        position["pair"]          = pair
                        position["buy_price"]     = buy_price
                        position["highest_price"] = buy_price
                        position["volume"]        = volume
                        position_prices.clear()
                        position_prices.append((datetime.now(timezone.utc), buy_price))
                        log.info(f"[POSITION] Position ouverte | {pair} | Prix: {buy_price:.6f} | Volume: {volume}")
                        log.info(f"[RAPIDE] Surveillance rapide activee (toutes les {POSITION_INTERVAL}s)")
                    else:
                        log.warning(f"Achat echoue sur {pair}, scan suivant...")

                else:
                    log.info("[ATTENTE] Aucun pump detecte. Attente...")

                time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("[ARRET] Bot arrete manuellement.")
            if position["active"]:
                log.warning(f"[ATTENTION] Position ouverte sur {position['pair']} !")
                log.warning("Pense a vendre manuellement sur Kraken !")
            break
        except Exception as e:
            log.error(f"Erreur inattendue: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
