import os
import re
import time
import sqlite3
from typing import Optional, Tuple, List, Dict, Any
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

print("APP_ID:", os.getenv("ML_APP_ID"))
print("SECRET:", os.getenv("ML_CLIENT_SECRET"))
print("REFRESH:", os.getenv("ML_REFRESH_TOKEN"))


#access_token : APP_USR-2540677790047495-021609-e482b3388eae111ab182f97e6a2d89dc-149015608
#token_type   : Bearer
#expires_in   : 21600
#scope        : read urn:global:admin:info:/read-only urn:global:admin:info:/read-write
# urn:global:admin:oauth:/read-only urn:global:admin:oauth:/read-write urn:global:admin:users:/read-only
# urn:global:admin:users:/read-write urn:ml:mktp:metrics:/read-only write
# user_id      : 149015608
# refresh_token:  "TG-69931adbca92a800014908a7-149015608"

# code tg TG-699314e3a90eda0001f6b0e5-149015608
#BOT_TOKEN = "8563760926:AAFdcDs9URD4fsaLY76h8NZioV9pRV

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
DEFAULT_UNDERCUT_REAIS = float(os.getenv("DEFAULT_UNDERCUT_REAIS", "1.00"))

HTTP_TIMEOUT = 20
DB_FILE = "tracker.db"
SITE_ID = "MLB"  # Brasil


# =========================
# ML OAuth (do .env)
# =========================
ML_APP_ID = os.getenv("ML_APP_ID", "").strip()
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "").strip()
ML_ACCESS_TOKEN = os.getenv("ML_ACCESS_TOKEN", "").strip()
ML_REFRESH_TOKEN = os.getenv("ML_REFRESH_TOKEN", "").strip()
ML_TOKEN_EXPIRES_AT = 0  # calculado em runtime


# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tracked_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT UNIQUE NOT NULL,
        title TEXT,
        my_price REAL NOT NULL,
        undercut_reais REAL NOT NULL DEFAULT 1.0,

        mode TEXT NOT NULL DEFAULT 'listing',  -- 'listing' | 'catalog'

        my_seller_id INTEGER,
        catalog_product_id TEXT,

        last_seen_price REAL,
        last_alert_price REAL,
        last_state TEXT,     -- "OK" | "UNDERCUT"
        updated_at INTEGER
    )
    """)
    conn.commit()
    conn.close()


# =========================
# Mercado Livre OAuth helpers
# =========================
def _persist_tokens_to_env(access_token: str, refresh_token: str) -> None:
    # Atualiza ML_ACCESS_TOKEN e ML_REFRESH_TOKEN no .env
    env_file = env_path
    lines = env_file.read_text(encoding="utf-8").splitlines()

    def upsert(key: str, value: str, arr: List[str]) -> List[str]:
        found = False
        out: List[str] = []
        for line in arr:
            if line.startswith(f"{key}="):
                out.append(f"{key}={value}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"{key}={value}")
        return out

    lines = upsert("ML_ACCESS_TOKEN", access_token, lines)
    lines = upsert("ML_REFRESH_TOKEN", refresh_token, lines)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ml_headers() -> dict:
    global ML_ACCESS_TOKEN
    h = {"Accept": "application/json"}
    if ML_ACCESS_TOKEN:
        h["Authorization"] = f"Bearer {ML_ACCESS_TOKEN}"
    return h


def ml_refresh_access_token() -> bool:
    """
    Renova access_token usando refresh_token.
    Atualiza ML_ACCESS_TOKEN em memória e no .env (pra você não perder).
    """
    global ML_ACCESS_TOKEN, ML_REFRESH_TOKEN, ML_TOKEN_EXPIRES_AT

    if not ML_APP_ID or not ML_CLIENT_SECRET or not ML_REFRESH_TOKEN:
        print("ML OAuth: faltando ML_APP_ID / ML_CLIENT_SECRET / ML_REFRESH_TOKEN (verifique .env)")
        return False

    url = "https://api.mercadolibre.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": ML_APP_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": ML_REFRESH_TOKEN,
    }

    r = requests.post(url, data=data, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        print("Falha ao renovar token ML:", r.status_code, r.text[:300])
        return False

    payload = r.json()
    ML_ACCESS_TOKEN = payload.get("access_token", ML_ACCESS_TOKEN)
    ML_REFRESH_TOKEN = payload.get("refresh_token", ML_REFRESH_TOKEN)  # pode rotacionar

    expires_in = int(payload.get("expires_in", 21600))
    ML_TOKEN_EXPIRES_AT = int(time.time()) + max(60, expires_in - 120)  # renova 2 min antes

    try:
        _persist_tokens_to_env(ML_ACCESS_TOKEN, ML_REFRESH_TOKEN)
    except Exception as e:
        print("Aviso: não consegui atualizar .env automaticamente:", e)

    return True


def ml_ensure_token() -> None:
    global ML_TOKEN_EXPIRES_AT
    if ML_TOKEN_EXPIRES_AT == 0:
        # força refresh no start para ter expiração controlada
        ml_refresh_access_token()
        return
    if int(time.time()) >= ML_TOKEN_EXPIRES_AT:
        ml_refresh_access_token()


# =========================
# Mercado Livre API
# =========================
def extract_item_id(text: str) -> Optional[str]:
    t = text.strip()
    m = re.search(r"(MLB\d{6,})", t.upper())
    return m.group(1) if m else None


def ml_get_item(item_id: str) -> Tuple[Optional[str], Optional[float], Optional[int], Optional[str]]:
    ml_ensure_token()

    url = f"https://api.mercadolibre.com/items/{item_id}"
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers=ml_headers())

    if r.status_code != 200:
        # log útil para você
        print(f"ML /items erro {r.status_code} para {item_id}: {r.text[:200]}")
        return None, None, None, None

    data = r.json()
    title = data.get("title")
    price = data.get("price")
    seller_id = data.get("seller_id")
    catalog_product_id = data.get("catalog_product_id")

    try:
        price = float(price) if price is not None else None
    except:
        price = None

    try:
        seller_id = int(seller_id) if seller_id is not None else None
    except:
        seller_id = None

    return title, price, seller_id, catalog_product_id


def ml_search_by_catalog(catalog_product_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    ml_ensure_token()

    url = f"https://api.mercadolibre.com/sites/{SITE_ID}/search"
    params = {"catalog_product_id": catalog_product_id, "limit": limit}

    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=ml_headers())
    if r.status_code != 200:
        print(f"ML /search erro {r.status_code} catalog {catalog_product_id}: {r.text[:200]}")
        return []

    data = r.json()
    return data.get("results", []) or []


def ml_item_link(item_id: str) -> str:
    return f"https://www.mercadolivre.com.br/{item_id}"


# =========================
# Telegram helpers
# =========================
async def tg_reply(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text)


async def tg_send(app, text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("ERRO: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID não configurados no .env")
        return
    await app.bot.send_message(chat_id=CHAT_ID, text=text, disable_web_page_preview=False)


# =========================
# Alert logic
# =========================
def should_alert(my_price: float, undercut: float, competitor_price: float) -> bool:
    return competitor_price <= (my_price - undercut)


def fmt_price(v: Optional[float]) -> str:
    return f"R$ {v:.2f}" if isinstance(v, (int, float)) else "—"


# =========================
# Commands
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ ML Tracker ON (Listing + Catalog)\n\n"
        "Comandos:\n"
        "/add <MLB... ou link> <meu_preco> [undercut_reais] [mode]\n"
        "mode: listing | catalog (padrão: listing)\n\n"
        "/list\n"
        "/remove <MLB...>\n"
        "/setprice <MLB...> <meu_preco>\n"
        "/setundercut <MLB...> <reais>\n"
        "/setmode <MLB...> <listing|catalog>\n"
    )
    await tg_reply(update, msg)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await tg_reply(update, "Uso: /add <MLB... ou link> <meu_preco> [undercut_reais] [listing|catalog]")
        return

    item_id = extract_item_id(args[0])
    if not item_id:
        await tg_reply(update, "Não consegui identificar o ITEM_ID (MLB...). Envie MLB... ou link do anúncio.")
        return

    try:
        my_price = float(str(args[1]).replace(",", "."))
    except:
        await tg_reply(update, "Preço inválido. Ex: /add MLB123 299.90 1.00 catalog")
        return

    undercut = DEFAULT_UNDERCUT_REAIS
    mode = "listing"

    if len(args) >= 3:
        third = str(args[2]).lower()
        if third in ("listing", "catalog"):
            mode = third
        else:
            try:
                undercut = float(str(args[2]).replace(",", "."))
            except:
                await tg_reply(update, "undercut_reais inválido. Ex: /add MLB123 299.90 1.00 catalog")
                return

    if len(args) >= 4:
        mode = str(args[3]).lower().strip()
        if mode not in ("listing", "catalog"):
            await tg_reply(update, "Mode inválido. Use: listing ou catalog.")
            return

    title, price, seller_id, catalog_product_id = ml_get_item(item_id)
    if price is None:
        await tg_reply(update, "Não consegui puxar preço via API autenticada do ML. Verifique o item e tente de novo.")
        return

    if mode == "catalog" and not catalog_product_id:
        await tg_reply(
            update,
            "⚠️ Esse item não está vinculado a catálogo (catalog_product_id vazio).\n"
            f"Use listing:\n/add {item_id} {my_price:.2f} {undercut:.2f} listing"
        )
        return

    conn = db()
    cur = conn.cursor()
    now = int(time.time())

    cur.execute("""
    INSERT INTO tracked_items (
        item_id, title, my_price, undercut_reais, mode,
        my_seller_id, catalog_product_id,
        last_seen_price, last_state, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
        title=excluded.title,
        my_price=excluded.my_price,
        undercut_reais=excluded.undercut_reais,
        mode=excluded.mode,
        my_seller_id=excluded.my_seller_id,
        catalog_product_id=excluded.catalog_product_id,
        last_seen_price=excluded.last_seen_price,
        updated_at=excluded.updated_at
    """, (
        item_id, title, my_price, undercut, mode,
        seller_id, catalog_product_id,
        price, "OK", now
    ))

    conn.commit()
    conn.close()

    await tg_reply(
        update,
        "✅ Adicionado:\n"
        f"{title}\n"
        f"ID: {item_id}\n"
        f"Modo: {mode}\n"
        f"Preço atual: {fmt_price(price)}\n"
        f"Seu preço: {fmt_price(my_price)}\n"
        f"Margem: {fmt_price(undercut)}\n"
        f"Catalog ID: {catalog_product_id or '—'}"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT item_id, title, my_price, undercut_reais, mode, last_seen_price, last_state, catalog_product_id
        FROM tracked_items
        ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await tg_reply(update, "Nenhum item monitorado ainda. Use /add")
        return

    lines = ["📦 Itens monitorados:"]
    for r in rows:
        lines.append(
            f"\n• {r['item_id']} ({r['mode']})\n"
            f"{(r['title'] or '')[:80]}\n"
            f"Meu: {fmt_price(r['my_price'])} | Margem: {fmt_price(r['undercut_reais'])}\n"
            f"Último: {fmt_price(r['last_seen_price'])} | Estado: {r['last_state']}\n"
            f"Catalog: {r['catalog_product_id'] or '—'}"
        )
    await tg_reply(update, "\n".join(lines))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await tg_reply(update, "Uso: /remove <MLB...>")
        return
    item_id = extract_item_id(context.args[0])
    if not item_id:
        await tg_reply(update, "ITEM_ID inválido.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM tracked_items WHERE item_id=?", (item_id,))
    changes = cur.rowcount
    conn.commit()
    conn.close()

    await tg_reply(update, "✅ Removido." if changes else "Não encontrei esse item no monitoramento.")


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await tg_reply(update, "Uso: /setprice <MLB...> <meu_preco>")
        return
    item_id = extract_item_id(context.args[0])
    if not item_id:
        await tg_reply(update, "ITEM_ID inválido.")
        return
    try:
        my_price = float(str(context.args[1]).replace(",", "."))
    except:
        await tg_reply(update, "Preço inválido.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE tracked_items SET my_price=?, updated_at=? WHERE item_id=?",
                (my_price, int(time.time()), item_id))
    conn.commit()
    changes = cur.rowcount
    conn.close()

    await tg_reply(update, "✅ Atualizado." if changes else "Não encontrei esse item no monitoramento.")


async def cmd_setundercut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await tg_reply(update, "Uso: /setundercut <MLB...> <reais>")
        return
    item_id = extract_item_id(context.args[0])
    if not item_id:
        await tg_reply(update, "ITEM_ID inválido.")
        return
    try:
        undercut = float(str(context.args[1]).replace(",", "."))
    except:
        await tg_reply(update, "Valor inválido.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE tracked_items SET undercut_reais=?, updated_at=? WHERE item_id=?",
                (undercut, int(time.time()), item_id))
    conn.commit()
    changes = cur.rowcount
    conn.close()

    await tg_reply(update, "✅ Atualizado." if changes else "Não encontrei esse item no monitoramento.")


async def cmd_setmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await tg_reply(update, "Uso: /setmode <MLB...> <listing|catalog>")
        return
    item_id = extract_item_id(context.args[0])
    mode = str(context.args[1]).lower().strip()
    if not item_id or mode not in ("listing", "catalog"):
        await tg_reply(update, "Uso: /setmode <MLB...> <listing|catalog>")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT catalog_product_id FROM tracked_items WHERE item_id=?", (item_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await tg_reply(update, "Não encontrei esse item no monitoramento.")
        return

    if mode == "catalog" and not row["catalog_product_id"]:
        conn.close()
        await tg_reply(update, "⚠️ Esse item não tem catalog_product_id. Use listing ou remova e adicione outro item.")
        return

    cur.execute("UPDATE tracked_items SET mode=?, updated_at=? WHERE item_id=?",
                (mode, int(time.time()), item_id))
    conn.commit()
    conn.close()
    await tg_reply(update, "✅ Modo atualizado.")


# =========================
# Monitor loop
# =========================
async def run_check(app):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tracked_items")
    rows = cur.fetchall()

    for r in rows:
        item_id = r["item_id"]
        my_price = float(r["my_price"])
        undercut = float(r["undercut_reais"])
        mode = (r["mode"] or "listing").lower()
        last_state = r["last_state"] or "OK"
        last_alert_price = r["last_alert_price"]
        my_seller_id = r["my_seller_id"]
        catalog_product_id = r["catalog_product_id"]

        now = int(time.time())

        title = None
        competitor_price = None
        competitor_item_id = None
        competitor_seller_id = None

        if mode == "listing":
            title, price, seller_id, _cat = ml_get_item(item_id)
            if price is None:
                continue
            competitor_price = price
            competitor_item_id = item_id
            competitor_seller_id = seller_id

        elif mode == "catalog":
            base_title, _base_price, seller_id, cat_id = ml_get_item(item_id)
            title = base_title or r["title"]
            my_seller_id = seller_id or my_seller_id
            catalog_product_id = cat_id or catalog_product_id

            if not catalog_product_id:
                cur.execute("""
                    UPDATE tracked_items
                    SET title=?, last_state=?, updated_at=?
                    WHERE item_id=?
                """, (title, "OK", now, item_id))
                conn.commit()
                time.sleep(1)
                continue

            results = ml_search_by_catalog(catalog_product_id, limit=50)

            best = None
            for it in results:
                try:
                    it_id = it.get("id")
                    it_price = float(it.get("price"))
                    it_seller = it.get("seller", {}).get("id")
                    it_seller = int(it_seller) if it_seller is not None else None
                except:
                    continue

                if my_seller_id is not None and it_seller == my_seller_id:
                    continue

                if best is None or it_price < best["price"]:
                    best = {"id": it_id, "price": it_price, "seller_id": it_seller}

            if not best:
                cur.execute("""
                    UPDATE tracked_items
                    SET title=?, last_state=?, last_seen_price=?, updated_at=?
                    WHERE item_id=?
                """, (title, "OK", None, now, item_id))
                conn.commit()
                time.sleep(1)
                continue

            competitor_price = best["price"]
            competitor_item_id = best["id"]
            competitor_seller_id = best["seller_id"]

        else:
            continue

        undercut_now = should_alert(my_price, undercut, competitor_price)
        state_now = "UNDERCUT" if undercut_now else "OK"

        # anti-spam
        alert = False
        if state_now == "UNDERCUT":
            if last_state != "UNDERCUT":
                alert = True
            else:
                if last_alert_price is None or abs(float(last_alert_price) - competitor_price) > 0.0001:
                    alert = True

        if alert:
            link = ml_item_link(competitor_item_id or item_id)
            msg = (
                "🔥 ALERTA (ML) — CONCORRENTE ABAIXO DO SEU PREÇO\n"
                f"Produto base: {title or item_id}\n"
                f"Modo: {mode}\n"
                f"Seu preço: {fmt_price(my_price)}\n"
                f"Concorrente: {fmt_price(competitor_price)}\n"
                f"Margem: {fmt_price(undercut)}\n"
                f"Item concorrente: {competitor_item_id}\n"
                f"Seller concorrente: {competitor_seller_id}\n"
                f"Link: {link}"
            )
            await tg_send(app, msg)

            cur.execute("""
                UPDATE tracked_items
                SET title=?, my_seller_id=?, catalog_product_id=?,
                    last_seen_price=?, last_alert_price=?, last_state=?, updated_at=?
                WHERE item_id=?
            """, (title, my_seller_id, catalog_product_id,
                  competitor_price, competitor_price, state_now, now, item_id))
        else:
            cur.execute("""
                UPDATE tracked_items
                SET title=?, my_seller_id=?, catalog_product_id=?,
                    last_seen_price=?, last_state=?, updated_at=?
                WHERE item_id=?
            """, (title, my_seller_id, catalog_product_id,
                  competitor_price, state_now, now, item_id))

        conn.commit()
        time.sleep(1)

    conn.close()


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise SystemExit("ERRO: TELEGRAM_BOT_TOKEN vazio no .env")

    init_db()

    # remove prints de debug se quiser
    print("ML Tracker rodando...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setprice", cmd_setprice))
    app.add_handler(CommandHandler("setundercut", cmd_setundercut))
    app.add_handler(CommandHandler("setmode", cmd_setmode))

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: app.create_task(run_check(app)), "interval", seconds=CHECK_INTERVAL_SECONDS)
    scheduler.start()

    app.run_polling()


if __name__ == "__main__":
    main()