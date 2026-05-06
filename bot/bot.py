import logging
import sqlite3
import datetime
import re
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DB_PATH = os.path.join(os.path.dirname(__file__), "controle.db")

# ====================== BANCO ======================

def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS produtos (
                 codigo TEXT PRIMARY KEY,
                 nome TEXT,
                 estoque REAL DEFAULT 0,
                 preco_venda REAL)''')

    c.execute('''CREATE TABLE IF NOT EXISTS entregas (
                 id INTEGER PRIMARY KEY,
                 numero TEXT,
                 cliente TEXT,
                 total REAL,
                 taxa REAL DEFAULT 0,
                 responsavel TEXT,
                 data TEXT,
                 status TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS itens_entrega (
                 entrega_id INTEGER,
                 produto TEXT,
                 quantidade REAL,
                 FOREIGN KEY(entrega_id) REFERENCES entregas(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS caixa (
                 id INTEGER PRIMARY KEY,
                 tipo TEXT,
                 valor REAL,
                 descricao TEXT,
                 data TEXT)''')

    produtos = [
        ("ICE", "Ice o Lator", 375, 140),
        ("PAK", "Pak", 170, 60),
        ("CRUMBLE", "Crumble", 83, 180),
        ("POD", "Pod THC", 8, 450),
    ]
    c.executemany("INSERT OR IGNORE INTO produtos VALUES (?,?,?,?)", produtos)
    conn.commit()
    conn.close()

init_db()

# ====================== HELPERS ======================

CODIGOS = {"ICE", "PAK", "CRUMBLE", "POD"}

# Legacy short codes → DB codes
ALIAS = {"I": "ICE", "P": "PAK", "C": "CRUMBLE", "VP": "POD"}

NOMES = {
    "ICE": "Ice o Lator",
    "PAK": "Pak",
    "CRUMBLE": "Crumble",
    "POD": "Pod THC",
}

def registrar_caixa(tipo, valor, descricao):
    conn = get_db()
    c = conn.cursor()
    data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO caixa (tipo, valor, descricao, data) VALUES (?,?,?,?)",
        (tipo, valor, descricao, data)
    )
    conn.commit()
    conn.close()

def estoque_emoji(qtd):
    if qtd <= 0:
        return "❌"
    elif qtd <= 20:
        return "⚠️"
    return "✅"

# ====================== COMANDOS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍪 <b>Sistema de Controle de Entregas</b>\n\n"
        "<b>Comandos:</b>\n"
        "/menu → Menu com botões\n"
        "/entrega → Registrar entrega\n"
        "/estoque → Ver estoque atual\n"
        "/caixa → Ver saldo do caixa\n"
        "/add ICE 50 → Repor estoque\n\n"
        "<b>Desconto rápido (mensagem):</b>\n"
        "<code>ICE 3</code> ou <code>I 3</code> → desconta 3 unidades",
        parse_mode="HTML"
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 Ver Estoque", callback_data="estoque")],
        [InlineKeyboardButton("📋 Registrar Entrega", callback_data="nova_entrega")],
        [InlineKeyboardButton("💰 Ver Caixa", callback_data="caixa")],
        [InlineKeyboardButton("📊 Relatório do Dia", callback_data="relatorio_dia")],
    ]
    await update.message.reply_text(
        "Escolha uma opção:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def ver_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos")
    rows = c.fetchall()
    conn.close()

    msg = "📦 <b>ESTOQUE ATUAL</b>\n\n"
    for cod, nome, qtd, preco in rows:
        emoji = estoque_emoji(qtd)
        msg += f"{emoji} <b>{cod}</b> - {nome}\n   Qtd: {qtd:.1f} | R$ {preco:.2f}/un\n\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def ver_caixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    hoje = datetime.date.today().strftime("%Y-%m-%d")
    c.execute(
        "SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo",
        (f"{hoje}%",)
    )
    rows = c.fetchall()
    c.execute(
        "SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
        (f"{hoje}%",)
    )
    saldo = c.fetchone()[0] or 0
    conn.close()

    msg = "💰 <b>CAIXA DO DIA</b>\n\n"
    for tipo, total in rows:
        label = "Entradas" if tipo == "entrada" else "Saídas"
        msg += f"{label}: R$ {total:.2f}\n"
    msg += f"\n<b>Saldo: R$ {saldo:.2f}</b>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def adicionar_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("⚠️ Use: /add ICE 50")
            return
        cod = args[0].upper()
        cod = ALIAS.get(cod, cod)
        qtd = float(args[1])
        if cod not in CODIGOS:
            await update.message.reply_text(f"❌ Código inválido. Use: {', '.join(CODIGOS)}")
            return
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE produtos SET estoque = estoque + ? WHERE codigo = ?", (qtd, cod))
        c.execute("SELECT estoque, nome FROM produtos WHERE codigo = ?", (cod,))
        novo, nome = c.fetchone()
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ <b>{cod}</b> ({nome}) +{qtd:.1f}\n📦 Agora: {novo:.1f}",
            parse_mode="HTML"
        )
    except ValueError:
        await update.message.reply_text("⚠️ Quantidade inválida. Use: /add ICE 50")

async def registrar_entrega_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Envie os dados da entrega no seguinte formato:\n\n"
        "<code>#012\n"
        "Cliente: Nome Sobrenome\n"
        "ICE: 2\n"
        "PAK: 1\n"
        "Taxa: 10\n"
        "Total: 450\n"
        "Responsavel: RD</code>",
        parse_mode="HTML"
    )
    context.user_data["esperando_entrega"] = True

# ====================== CALLBACK BUTTONS ======================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "estoque":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos")
        rows = c.fetchall()
        conn.close()
        msg = "📦 <b>ESTOQUE ATUAL</b>\n\n"
        for cod, nome, qtd, preco in rows:
            emoji = estoque_emoji(qtd)
            msg += f"{emoji} <b>{cod}</b> - {nome}\n   Qtd: {qtd:.1f} | R$ {preco:.2f}/un\n\n"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif data == "nova_entrega":
        await query.edit_message_text(
            "✅ Envie os dados da entrega no seguinte formato:\n\n"
            "<code>#012\n"
            "Cliente: Nome Sobrenome\n"
            "ICE: 2\n"
            "PAK: 1\n"
            "Taxa: 10\n"
            "Total: 450\n"
            "Responsavel: RD</code>",
            parse_mode="HTML"
        )
        context.user_data["esperando_entrega"] = True

    elif data == "caixa":
        conn = get_db()
        c = conn.cursor()
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        c.execute(
            "SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo",
            (f"{hoje}%",)
        )
        rows = c.fetchall()
        c.execute(
            "SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
            (f"{hoje}%",)
        )
        saldo = c.fetchone()[0] or 0
        conn.close()
        msg = "💰 <b>CAIXA DO DIA</b>\n\n"
        for tipo, total in rows:
            label = "Entradas" if tipo == "entrada" else "Saídas"
            msg += f"{label}: R$ {total:.2f}\n"
        msg += f"\n<b>Saldo: R$ {saldo:.2f}</b>"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif data == "relatorio_dia":
        conn = get_db()
        c = conn.cursor()
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        c.execute(
            "SELECT numero, cliente, total, responsavel FROM entregas WHERE data LIKE ? ORDER BY id DESC",
            (f"{hoje}%",)
        )
        entregas = c.fetchall()
        c.execute(
            "SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
            (f"{hoje}%",)
        )
        saldo = c.fetchone()[0] or 0
        conn.close()

        msg = f"📊 <b>RELATÓRIO — {hoje}</b>\n\n"
        if entregas:
            msg += f"<b>Entregas ({len(entregas)}):</b>\n"
            for num, cli, tot, resp in entregas:
                msg += f"  #{num} {cli} — R$ {tot:.2f} ({resp})\n"
        else:
            msg += "Nenhuma entrega hoje.\n"
        msg += f"\n💰 <b>Faturamento: R$ {saldo:.2f}</b>"
        await query.edit_message_text(msg, parse_mode="HTML")

# ====================== MESSAGE HANDLER ======================

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text

    # --- Aguardando entrega formatada ---
    if context.user_data.get("esperando_entrega"):
        try:
            lines = [line.strip() for line in texto.split("\n") if line.strip()]
            numero = lines[0].replace("#", "").strip()
            cliente = ""
            itens = {}
            taxa = 0.0
            total = 0.0
            responsavel = "Desconhecido"

            for line in lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().upper()
                    value = value.strip()
                    if key in CODIGOS:
                        itens[key] = float(value)
                    elif key in ALIAS:
                        itens[ALIAS[key]] = float(value)
                    elif key == "CLIENTE":
                        cliente = value
                    elif key == "TAXA":
                        taxa = float(value)
                    elif key == "TOTAL":
                        total = float(value)
                    elif key in ("RESPONSAVEL", "RESPONSÁVEL"):
                        responsavel = value

            conn = get_db()
            c = conn.cursor()
            data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            c.execute(
                "INSERT INTO entregas (numero, cliente, total, taxa, responsavel, data, status) VALUES (?,?,?,?,?,?,?)",
                (numero, cliente, total, taxa, responsavel, data, "OK")
            )
            entrega_id = c.lastrowid
            for prod, qtd in itens.items():
                c.execute(
                    "INSERT INTO itens_entrega (entrega_id, produto, quantidade) VALUES (?,?,?)",
                    (entrega_id, prod, qtd)
                )
                c.execute(
                    "UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?",
                    (qtd, prod)
                )
            conn.commit()
            conn.close()

            registrar_caixa("entrada", total, f"Entrega #{numero} - {cliente}")

            itens_str = ", ".join(f"{k}: {v:.1f}" for k, v in itens.items())
            await update.message.reply_text(
                f"✅ <b>Entrega #{numero} registrada!</b>\n"
                f"Cliente: {cliente}\n"
                f"Itens: {itens_str}\n"
                f"Taxa: R$ {taxa:.2f} | Total: R$ {total:.2f}\n"
                f"Responsável: {responsavel}",
                parse_mode="HTML"
            )
            context.user_data["esperando_entrega"] = False
            return
        except Exception as e:
            logging.error(f"Erro ao processar entrega: {e}")
            await update.message.reply_text(
                f"❌ Erro ao processar entrega. Verifique o formato.\n\n<code>{e}</code>",
                parse_mode="HTML"
            )
            return

    # --- Desconto rápido: "ICE 3" ou "I 3" ---
    partes = texto.upper().split()
    if len(partes) == 2:
        cod = partes[0]
        cod = ALIAS.get(cod, cod)
        if cod in CODIGOS:
            try:
                qtd = float(partes[1])
                if qtd <= 0:
                    await update.message.reply_text("⚠️ Quantidade deve ser maior que zero.")
                    return
                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, cod))
                c.execute("SELECT estoque, nome FROM produtos WHERE codigo = ?", (cod,))
                novo, nome = c.fetchone()
                conn.commit()
                conn.close()

                aviso = ""
                if novo <= 0:
                    aviso = "\n🚨 <b>ESTOQUE ZERADO!</b>"
                elif novo <= 20:
                    aviso = "\n⚠️ <b>Estoque baixo!</b>"

                await update.message.reply_text(
                    f"✅ <b>{cod}</b> ({nome}) -{qtd:.1f}\n📦 Agora: {novo:.1f}{aviso}",
                    parse_mode="HTML"
                )
                return
            except ValueError:
                pass

# ====================== MAIN ======================

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não definido!")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("estoque", ver_estoque))
    app.add_handler(CommandHandler("caixa", ver_caixa))
    app.add_handler(CommandHandler("add", adicionar_estoque))
    app.add_handler(CommandHandler("entrega", registrar_entrega_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
