import krakenex
import time
import logging
import os
from datetime import datetime, timedelta, timezone
from collections import deque

# ============================================================
#  CONFIGURATION — Clés API via variables d'environnement
#  Sur Railway : configure API_KEY et API_SECRET dans Variables
# ============================================================
API_KEY    = os.environ.get("API_KEY", "METS_TA_CLE_API_ICI")
API_SECRET = os.environ.get("API_SECRET", "METS_TON_SECRET_ICI")

# ============================================================
#  PARAMÈTRES DE LA STRATÉGIE
# ============================================================
PUMP_THRESHOLD     = 5.0   # % de hausse en 10 min pour déclencher un achat
WINDOW_MINUTES     = 10    # Fenêtre de détection du pump (minutes)
TRAILING_STOP_PCT  = 5.0   # % de chute depuis le plus haut → vente en bénéf
STOP_LOSS_PCT      = 1.0   # % de perte max depuis l'achat → stop loss
DUMP_PCT           = 5.0   # % de chute en 60 secondes → vente dump brutal
DUMP_WINDOW_SEC    = 60    # Fenêtre de détection dump (secondes)
SCAN_INTERVAL      = 30    # Secondes entre chaque scan sans position
POSITION_INTERVAL  = 3     # Secondes entre chaque vérification en position
MIN_VOLUME_USD     = 50000 # Volume minimum USD pour filtrer tokens illiquides
BATCH_SIZE         = 50    # Taille des batch pour l'API Kraken

# ============================================================
#  SETUP LOGGING
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
#  ÉTAT DU BOT
# ============================================================
position = {
    "active": False,
    "pair": None,
    "buy_price": None,
    "highest_price": None,
    "volume": None,
}

price_history = {}       # Pour détecter les pumps { pair: [(timestamp, price)] }
position_prices = deque(maxlen=1200) # Max 1200 entrées (3s * 1200 = 1h), évite fuite mémoire


# ============================================================
#  FONCTIONS UTILITAIRES
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
            quote = pair_info.get("quote", "")
            status = pair_info.get("status", "online")
            if quote in ("ZUSD", "USD") and ".d" not in pair_name and status == "online":
                pairs.append(pair_name)
        log.info(f"{len(pairs)} paires USD actives trouvées")
        return pairs
    except Exception as e:
        log.error(f"Exception get_all_usd_pairs: {e}")
        return []


def get_ticker(pairs):
    try:
        pairs_str = ",".join(pairs)
        resp = k.query_public("Ticker", {"pair": pairs_str})
        if resp.get("error") and not resp.get("result"):
            return {}
        return resp.get("result", {})
    except Exception as e:
        log.error(f"Exception get_ticker: {e}")
        return {}


def record_prices(ticker_data):
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    best_pump = None
    best_pct = PUMP_THRESHOLD

    for pair, data in ticker_data.items():
        try:
            current_price = float(data["c"][0])
            volume_usd = float(data["v"][1]) * current_price

            if volume_usd < MIN_VOLUME_USD or current_price <= 0:
                continue

            history = price_history.get(pair, [])
            old_prices = [(t, p) for t, p in history if t <= cutoff]

            if not old_prices:
                continue

            oldest_price = old_prices[-1][1]
            if oldest_price <= 0:
                continue

            pct_change = ((current_price - oldest_price) / oldest_price) * 100

            if pct_change > best_pct:
                best_pct = pct_change
                best_pump = (pair, current_price, pct_change)

        except (KeyError, ValueError, IndexError, ZeroDivisionError):
            continue

    return best_pump


def detect_dump(current_price):
    """
    Détecte un dump brutal : -DUMP_PCT% en DUMP_WINDOW_SEC secondes.
    Analyse les prix enregistrés depuis l'ouverture de la position.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=DUMP_WINDOW_SEC)

    # Nettoie les vieux prix hors fenêtre
    while position_prices and position_prices[0][0] < cutoff:
        position_prices.popleft()

    if not position_prices:
        return False, 0.0

    # Prix le plus haut dans la fenêtre des 60 dernières secondes
    highest_in_window = max(p for _, p in position_prices)

    if highest_in_window <= 0:
        return False, 0.0

    pct_drop = ((current_price - highest_in_window) / highest_in_window) * 100

    if pct_drop <= -DUMP_PCT:
        return True, pct_drop

    return False, pct_drop


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


def buy_market(pair, usd_amount):
    try:
        pair_info = get_pair_info(pair)
        if not pair_info:
            log.error(f"Impossible de récupérer les infos de {pair}")
            return None

        # SECURITE — Refuse tout achat sur paire non-USD
        quote = pair_info.get("quote", "")
        if quote not in ("ZUSD", "USD"):
            log.error(f"REFUS ACHAT : {pair} n'est pas une paire USD (quote={quote})")
            return None

        lot_decimals = int(pair_info.get("lot_decimals", 8))
        order_min    = float(pair_info.get("ordermin", 0))
        cost_min     = float(pair_info.get("costmin", 0))

        if usd_amount < cost_min:
            log.warning(f"Montant {usd_amount:.2f}$ < costmin {cost_min}$ pour {pair}")
            return None

        ticker_resp = k.query_public("Ticker", {"pair": pair})
        if ticker_resp.get("error") or not ticker_resp.get("result"):
            log.error(f"Impossible de récupérer le prix de {pair}")
            return None

        ticker_result = list(ticker_resp["result"].values())[0]
        current_price = float(ticker_result["c"][0])

        if current_price <= 0:
            return None

        volume = round(usd_amount / current_price, lot_decimals)

        if volume < order_min:
            log.warning(f"Volume {volume} < ordermin {order_min} pour {pair}")
            return None

        log.info(f"[ACHAT] ACHAT {pair} | Prix: {current_price:.6f} | Volume: {volume} | Total: {usd_amount:.2f}$")

        # oflags fciq = frais déduits en USD (pas en crypto)
        resp = k.query_private("AddOrder", {
            "pair": pair,
            "type": "buy",
            "ordertype": "market",
            "volume": str(volume),
            "oflags": "fciq"
        })

        if resp.get("error"):
            log.error(f"Erreur AddOrder achat: {resp['error']}")
            return None

        log.info(f"[OK] Ordre achat placé: {resp['result']}")
        return volume, current_price

    except Exception as e:
        log.error(f"Exception buy_market: {e}")
        return None


def sell_market(pair, volume, reason=""):
    try:
        volume_str = str(round(float(volume), 8))
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

        log.info(f"[OK] Ordre vente placé: {resp['result']}")
        return True

    except Exception as e:
        log.error(f"Exception sell_market: {e}")
        return False


def get_current_price(pair):
    try:
        resp = k.query_public("Ticker", {"pair": pair})
        if resp.get("error") or not resp.get("result"):
            return None
        ticker = list(resp["result"].values())[0]
        return float(ticker["c"][0])
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
    log.info("[RESET] Position réinitialisée — En attente du prochain pump")


# ============================================================
#  BOUCLE PRINCIPALE
# ============================================================

def main():
    log.info("=" * 60)
    log.info("[BOT] PumpBot Kraken démarré")
    log.info(f"   Seuil pump       : +{PUMP_THRESHOLD}% en {WINDOW_MINUTES} min")
    log.info(f"   Trailing stop    : -{TRAILING_STOP_PCT}% depuis le plus haut")
    log.info(f"   Stop loss        : -{STOP_LOSS_PCT}% depuis l'achat")
    log.info(f"   Détection dump   : -{DUMP_PCT}% en {DUMP_WINDOW_SEC}s → vente immédiate")
    log.info(f"   Vérif position   : toutes les {POSITION_INTERVAL}s")
    log.info("=" * 60)

    if API_KEY == "METS_TA_CLE_API_ICI":
        log.error("[ERREUR] Tu n'as pas configuré tes clés API ! Arrêt du bot.")
        return

    all_pairs = get_all_usd_pairs()
    if not all_pairs:
        log.error("Impossible de récupérer les paires. Vérifie ta connexion.")
        return

    log.info("[ATTENTE] Préchauffage de 10 minutes pour constituer l'historique des prix...")
    warmup_end = datetime.now(timezone.utc) + timedelta(minutes=10)

    while datetime.now(timezone.utc) < warmup_end:
        remaining = int((warmup_end - datetime.now(timezone.utc)).total_seconds())
        log.info(f"   Préchauffage... encore {remaining}s")
        for i in range(0, len(all_pairs), BATCH_SIZE):
            batch = all_pairs[i:i+BATCH_SIZE]
            ticker_data = get_ticker(batch)
            record_prices(ticker_data)
            time.sleep(1)
        time.sleep(SCAN_INTERVAL)

    log.info("[OK] Préchauffage terminé — Le bot trade maintenant !")

    while True:
        try:
            # -----------------------------------------------
            # CAS 1 : Position active → surveillance rapide
            # -----------------------------------------------
            if position["active"]:
                current_price = get_current_price(position["pair"])

                if current_price is None:
                    log.warning("Prix indisponible, nouvelle tentative...")
                    time.sleep(2)
                    continue

                # Enregistre le prix dans la fenêtre dump
                position_prices.append((datetime.now(timezone.utc), current_price))

                # Mise à jour du plus haut
                if current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    log.info(f"[HAUSSE] Nouveau plus haut : {current_price:.6f}")

                buy_price    = position["buy_price"]
                highest      = position["highest_price"]
                pct_from_buy = ((current_price - buy_price) / buy_price) * 100
                pct_from_top = ((current_price - highest) / highest) * 100

                # Détection dump brutal
                is_dump, dump_pct = detect_dump(current_price)

                log.info(
                    f"[INFO] {position['pair']} | Prix: {current_price:.6f} | "
                    f"Achat: {pct_from_buy:+.2f}% | "
                    f"Sommet: {pct_from_top:+.2f}% | "
                    f"Dump60s: {dump_pct:+.2f}%"
                )

                # 🚨 PRIORITÉ 1 — DUMP BRUTAL : -5% en 60 secondes
                if is_dump:
                    log.warning(f"[DUMP] DUMP BRUTAL DÉTECTÉ ! {dump_pct:.2f}% en {DUMP_WINDOW_SEC}s → VENTE IMMÉDIATE")
                    success = sell_market(position["pair"], position["volume"], reason="DUMP BRUTAL")
                    if success:
                        log.info(f"[RAPIDE] Sorti rapidement | P&L: {pct_from_buy:.2f}%")
                        reset_position()
                    else:
                        log.error("[ATTENTION] Vente dump échouée ! Nouvelle tentative dans 2s...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="DUMP BRUTAL RETRY")

                # [STOP] PRIORITÉ 2 — STOP LOSS : -1% depuis l'achat
                elif pct_from_buy <= -STOP_LOSS_PCT:
                    log.warning(f"[STOP] STOP LOSS déclenché à {pct_from_buy:.2f}%")
                    success = sell_market(position["pair"], position["volume"], reason="STOP LOSS")
                    if success:
                        log.info(f"[PERTE] Trade fermé en perte: {pct_from_buy:.2f}%")
                        reset_position()
                    else:
                        log.error("[ATTENTION] Vente stop loss échouée ! Nouvelle tentative dans 2s...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="STOP LOSS RETRY")

                # [TP] PRIORITÉ 3 — TRAILING STOP : -5% depuis le plus haut ET en profit
                elif pct_from_top <= -TRAILING_STOP_PCT and pct_from_buy > 0:
                    log.info(f"[TP] TRAILING STOP | Profit final: {pct_from_buy:.2f}%")
                    success = sell_market(position["pair"], position["volume"], reason="TRAILING STOP")
                    if success:
                        log.info(f"[BENEF] Trade fermé en bénéfice: {pct_from_buy:.2f}%")
                        reset_position()
                    else:
                        log.error("[ATTENTION] Vente trailing stop échouée ! Nouvelle tentative dans 2s...")
                        time.sleep(2)
                        sell_market(position["pair"], position["volume"], reason="TRAILING STOP RETRY")

                # Vérification rapide toutes les 3 secondes en position
                time.sleep(POSITION_INTERVAL)

            # -----------------------------------------------
            # CAS 2 : Pas de position → scanner le marché
            # -----------------------------------------------
            else:
                log.info(f"[SCAN] Scan du marché ({len(all_pairs)} paires)...")

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
                    log.info(f"[PUMP] PUMP DÉTECTÉ ! {pair} | +{pct:.2f}% en {WINDOW_MINUTES} min")

                    usd_balance = get_usd_balance()

                    if usd_balance < 10:
                        log.warning(f"Solde insuffisant : {usd_balance:.2f}$ (minimum 10$)")
                        time.sleep(SCAN_INTERVAL)
                        continue

                    usd_to_use = usd_balance * 0.98

                    result = buy_market(pair, usd_to_use)

                    if result:
                        volume, buy_price = result
                        position["active"]        = True
                        position["pair"]          = pair
                        position["buy_price"]     = buy_price
                        position["highest_price"] = buy_price
                        position["volume"]        = volume
                        # Initialise la fenêtre dump avec le prix d'achat
                        position_prices.clear()
                        position_prices.append((datetime.now(timezone.utc), buy_price))
                        log.info(f"[POSITION] Position ouverte | {pair} | Prix: {buy_price:.6f} | Volume: {volume}")
                        log.info(f"[RAPIDE] Surveillance rapide activée (toutes les {POSITION_INTERVAL}s)")
                    else:
                        log.warning(f"Achat échoué sur {pair}, scan suivant...")

                else:
                    log.info("[ATTENTE] Aucun pump détecté. Attente...")

                time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("[ARRET] Bot arrêté manuellement.")
            if position["active"]:
                log.warning(f"[ATTENTION]  ATTENTION: Position encore ouverte sur {position['pair']} !")
                log.warning(f"   Pense à vendre manuellement sur Kraken !")
            break
        except Exception as e:
            log.error(f"Erreur inattendue: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
