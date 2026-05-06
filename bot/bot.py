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

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.path.join(os.path.dirname(__file__), "controle.db")

def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID

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

    c.execute('''CREATE TABLE IF NOT EXISTS pedidos (
                 id INTEGER PRIMARY KEY,
                 numero TEXT,
                 cliente TEXT,
                 total REAL,
                 taxa REAL DEFAULT 0,
                 pagamento TEXT DEFAULT 'PIX',
                 responsavel TEXT,
                 data TEXT,
                 status TEXT DEFAULT 'OK')''')

    c.execute('''CREATE TABLE IF NOT EXISTS itens_pedido (
                 pedido_id INTEGER,
                 produto TEXT,
                 quantidade REAL,
                 FOREIGN KEY(pedido_id) REFERENCES pedidos(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS caixa (
                 id INTEGER PRIMARY KEY,
                 tipo TEXT,
                 valor REAL,
                 descricao TEXT,
                 data TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS retiradas (
                 id INTEGER PRIMARY KEY,
                 socio TEXT,
                 valor REAL,
                 data TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS config (
                 chave TEXT PRIMARY KEY,
                 valor REAL)''')

    # Migrate old table names if they exist
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entregas'")
    if c.fetchone():
        c.execute('''INSERT OR IGNORE INTO pedidos (id, numero, cliente, total, taxa, responsavel, data, status)
                     SELECT id, numero, cliente, total, taxa, responsavel, data, status FROM entregas''')
        c.execute("DROP TABLE entregas")

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='itens_entrega'")
    if c.fetchone():
        c.execute('''INSERT OR IGNORE INTO itens_pedido (pedido_id, produto, quantidade)
                     SELECT entrega_id, produto, quantidade FROM itens_entrega''')
        c.execute("DROP TABLE itens_entrega")

    produtos = [
        ("ICE", "Ice o Lator", 375.0, 140.0),
        ("PAK", "Pak", 170.0, 60.0),
        ("CRUMBLE", "Crumble", 83.0, 180.0),
        ("POD", "Pod THC", 8.0, 450.0),
    ]
    c.executemany("INSERT OR IGNORE INTO produtos VALUES (?,?,?,?)", produtos)

    c.execute("INSERT OR IGNORE INTO config VALUES ('saldo_banco', 0)")
    c.execute("INSERT OR IGNORE INTO config VALUES ('divida_fornecedor', 74890)")

    conn.commit()
    conn.close()

init_db()

# ====================== HELPERS ======================

CODIGOS = {"ICE", "PAK", "CRUMBLE", "POD"}
ALIAS   = {"I": "ICE", "P": "PAK", "C": "CRUMBLE", "VP": "POD"}
NOMES   = {"ICE": "Ice o Lator", "PAK": "Pak", "CRUMBLE": "Crumble", "POD": "Pod THC"}

def estoque_emoji(qtd):
    if qtd <= 0:   return "❌"
    if qtd <= 20:  return "⚠️"
    return "✅"

def registrar_caixa(tipo, valor, descricao):
    conn = get_db()
    c = conn.cursor()
    data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO caixa (tipo, valor, descricao, data) VALUES (?,?,?,?)",
              (tipo, valor, descricao, data))
    conn.commit()
    conn.close()

def get_config(chave):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT valor FROM config WHERE chave=?", (chave,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def set_config(chave, valor):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE config SET valor=? WHERE chave=?", (valor, chave))
    conn.commit()
    conn.close()

# ====================== COMANDOS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_note = ""
    if ADMIN_ID == 0:
        admin_note = (
            f"\n\n🔑 <b>Seu ID Telegram:</b> <code>{uid}</code>\n"
            "Para ativar proteção admin, adicione <code>ADMIN_ID</code> nos Secrets do Replit."
        )
    await update.message.reply_text(
        "🍪 <b>Sistema de Controle de Pedidos</b>\n\n"
        "<b>Cole o pedido assim:</b>\n"
        "<code>pedido 01\n"
        "Poliana\n"
        "5G - PAK\n"
        "1G - ICE\n"
        "Total: R$ 440\n"
        "Dinheiro\n"
        "Responsavel: RD</code>\n\n"
        "<b>Comandos:</b>\n"
        "/menu → Menu com botões\n"
        "/estoque → Ver estoque\n"
        "/relatorio ou /fechamento → Relatório do dia\n"
        "/caixa → Ver caixa\n"
        "/add ICE 50 → Repor estoque\n"
        "/saida 50 Despesa → Registrar saída\n"
        "/rd 500 → Retirada RD\n"
        "/bart 300 → Retirada Bart\n"
        "/banco 12500 → Atualizar saldo banco\n"
        "/fornecedor 70000 → Atualizar dívida\n\n"
        "<b>Desconto rápido:</b> <code>ICE 3</code> ou <code>I 3</code>"
        + admin_note,
        parse_mode="HTML"
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 Ver Estoque",       callback_data="estoque")],
        [InlineKeyboardButton("💰 Ver Caixa",          callback_data="caixa")],
        [InlineKeyboardButton("📊 Relatório do Dia",  callback_data="relatorio_dia")],
    ]
    await update.message.reply_text("Escolha uma opção:",
                                    reply_markup=InlineKeyboardMarkup(keyboard))

async def ver_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos")
    rows = c.fetchall()
    conn.close()
    msg = "📦 <b>ESTOQUE ATUAL</b>\n\n"
    for cod, nome, qtd, preco in rows:
        msg += f"{estoque_emoji(qtd)} <b>{cod}</b> - {nome}: {qtd:.1f} | R$ {preco:.2f}/un\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def ver_caixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoje = datetime.date.today().strftime("%Y-%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo", (f"{hoje}%",))
    rows = c.fetchall()
    c.execute("SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
              (f"{hoje}%",))
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
        cod = ALIAS.get(args[0].upper(), args[0].upper())
        qtd = float(args[1].replace(",", "."))
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
            f"✅ <b>{cod}</b> ({nome}) +{qtd:.1f}\n📦 Agora: {novo:.1f}", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ Uso: /add ICE 50")

async def registrar_saida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if not args:
            await update.message.reply_text("⚠️ Use: /saida 50 Descrição")
            return
        valor = float(args[0].replace(",", "."))
        descricao = " ".join(args[1:]) if len(args) > 1 else "Saída"
        registrar_caixa("saida", valor, descricao)
        await update.message.reply_text(f"💸 Saída registrada: R$ {valor:.2f}\n📝 {descricao}")
    except ValueError:
        await update.message.reply_text("⚠️ Uso: /saida 50 Descrição")

async def retirada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode registrar retiradas.")
        return
    try:
        cmd = update.message.text.split()[0].lower().lstrip("/")
        socio = "RD" if cmd == "rd" else "Bart"
        valor = float(context.args[0].replace(",", "."))
        conn = get_db()
        c = conn.cursor()
        data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO retiradas (socio, valor, data) VALUES (?,?,?)", (socio, valor, data))
        conn.commit()
        conn.close()
        registrar_caixa("saida", valor, f"Retirada {socio}")
        await update.message.reply_text(f"✅ {socio} retirou R$ {valor:.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /rd 500  ou  /bart 300")

async def atualizar_banco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador.")
        return
    try:
        valor = float(context.args[0].replace(",", "."))
        set_config("saldo_banco", valor)
        await update.message.reply_text(f"✅ Saldo Banco atualizado: R$ {valor:.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /banco 12500")

async def atualizar_fornecedor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador.")
        return
    try:
        valor = float(context.args[0].replace(",", "."))
        set_config("divida_fornecedor", valor)
        await update.message.reply_text(f"✅ Dívida Fornecedor atualizada: R$ {valor:.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /fornecedor 74890")

async def relatorio_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode ver o relatório.")
        return

    hoje = datetime.datetime.now().strftime("%Y-%m-%d")
    hoje_fmt = datetime.datetime.now().strftime("%d/%m/%Y")
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT SUM(total), COUNT(*) FROM pedidos WHERE data LIKE ?", (f"{hoje}%",))
    total_vendido, qtd_pedidos = c.fetchone()
    total_vendido = total_vendido or 0

    c.execute("SELECT pagamento, SUM(total) FROM pedidos WHERE data LIKE ? GROUP BY pagamento",
              (f"{hoje}%",))
    pagamentos = dict(c.fetchall())

    c.execute("""SELECT i.produto, SUM(i.quantidade)
                 FROM itens_pedido i JOIN pedidos p ON i.pedido_id = p.id
                 WHERE p.data LIKE ? GROUP BY i.produto""", (f"{hoje}%",))
    saidas = dict(c.fetchall())

    c.execute("SELECT codigo, nome, estoque FROM produtos")
    estoque_atual = c.fetchall()

    c.execute("SELECT SUM(valor) FROM retiradas WHERE data LIKE ?", (f"{hoje}%",))
    total_retiradas = c.fetchone()[0] or 0

    c.execute("SELECT SUM(CASE WHEN tipo='saida' THEN valor ELSE 0 END) FROM caixa WHERE data LIKE ?",
              (f"{hoje}%",))
    total_saidas = c.fetchone()[0] or 0

    saldo_banco = get_config("saldo_banco")
    divida = get_config("divida_fornecedor")

    conn.close()

    rel  = f"📦 <b>RELATÓRIO — {hoje_fmt}</b>\n"
    rel += "━━━━━━━━━━━━━━━━━━\n\n"
    rel += f"Pedidos: {qtd_pedidos or 0}\n"
    rel += f"💰 Total Vendido: R$ {total_vendido:.2f}\n"
    rel += f"📲 PIX: R$ {pagamentos.get('PIX', 0):.2f}\n"
    rel += f"💵 Dinheiro: R$ {pagamentos.get('DINHEIRO', 0):.2f}\n"
    if total_saidas > 0:
        rel += f"💸 Saídas/Retiradas: R$ {total_saidas:.2f}\n"
    rel += f"<b>Líquido: R$ {total_vendido - total_saidas:.2f}</b>\n\n"

    rel += "📦 <b>SAÍDAS DO DIA</b>\n"
    for cod in ["ICE", "PAK", "CRUMBLE", "POD"]:
        rel += f"{cod}: {saidas.get(cod, 0):.1f}\n"

    rel += "\n📦 <b>ESTOQUE RESTANTE</b>\n"
    for cod, nome, qtd in estoque_atual:
        rel += f"{estoque_emoji(qtd)} {cod} ({nome}): {qtd:.1f}\n"

    rel += f"\n🏦 Saldo Banco: R$ {saldo_banco:.2f}\n"
    rel += f"📉 Dívida Fornecedor: R$ {divida:.2f}\n"
    rel += "━━━━━━━━━━━━━━━━━━"
    await update.message.reply_text(rel, parse_mode="HTML")

# ====================== CALLBACK BUTTONS ======================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "estoque":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos")
        rows = c.fetchall()
        conn.close()
        msg = "📦 <b>ESTOQUE ATUAL</b>\n\n"
        for cod, nome, qtd, preco in rows:
            msg += f"{estoque_emoji(qtd)} <b>{cod}</b> - {nome}: {qtd:.1f} | R$ {preco:.2f}/un\n"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif query.data == "caixa":
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo", (f"{hoje}%",))
        rows = c.fetchall()
        c.execute("SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
                  (f"{hoje}%",))
        saldo = c.fetchone()[0] or 0
        conn.close()
        msg = "💰 <b>CAIXA DO DIA</b>\n\n"
        for tipo, total in rows:
            label = "Entradas" if tipo == "entrada" else "Saídas"
            msg += f"{label}: R$ {total:.2f}\n"
        msg += f"\n<b>Saldo: R$ {saldo:.2f}</b>"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif query.data == "relatorio_dia":
        if not is_admin(query.from_user.id):
            await query.edit_message_text("❌ Apenas o administrador pode ver o relatório.")
            return
        hoje = datetime.datetime.now().strftime("%Y-%m-%d")
        hoje_fmt = datetime.datetime.now().strftime("%d/%m/%Y")
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT numero, cliente, total, pagamento, responsavel FROM pedidos WHERE data LIKE ? ORDER BY id DESC",
                  (f"{hoje}%",))
        pedidos = c.fetchall()
        c.execute("SELECT SUM(total) FROM pedidos WHERE data LIKE ?", (f"{hoje}%",))
        total = c.fetchone()[0] or 0
        conn.close()
        msg = f"📊 <b>RELATÓRIO — {hoje_fmt}</b>\n\n"
        if pedidos:
            msg += f"<b>Pedidos ({len(pedidos)}):</b>\n"
            for num, cli, tot, pag, resp in pedidos:
                pag_emoji = "📲" if pag == "PIX" else "💵"
                msg += f"  #{num} {cli} — R$ {tot:.2f} {pag_emoji} ({resp})\n"
        else:
            msg += "Nenhum pedido hoje.\n"
        msg += f"\n💰 <b>Total: R$ {total:.2f}</b>"
        await query.edit_message_text(msg, parse_mode="HTML")

# ====================== MESSAGE HANDLER ======================

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    lines = [line.strip() for line in texto.split("\n") if line.strip()]

    # --- Desconto rápido: "ICE 3" ou "I 3" (uma linha) ---
    if len(lines) == 1:
        partes = texto.upper().split()
        if len(partes) == 2:
            cod = ALIAS.get(partes[0], partes[0])
            if cod in CODIGOS:
                try:
                    qtd = float(partes[1].replace(",", "."))
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
                    if novo <= 0:   aviso = "\n🚨 <b>ESTOQUE ZERADO!</b>"
                    elif novo <= 20: aviso = "\n⚠️ <b>Estoque baixo!</b>"
                    await update.message.reply_text(
                        f"✅ <b>{cod}</b> ({nome}) -{qtd:.1f}\n📦 Agora: {novo:.1f}{aviso}",
                        parse_mode="HTML")
                except ValueError:
                    pass
        return  # ignora mensagens de uma linha que não são desconto rápido

    # --- Parser de pedido completo (requer "pedido" ou "total") ---
    if not re.search(r'pedido|total', texto, re.IGNORECASE):
        return

    try:
        numero_match = re.search(r'pedido\s*(\d+)', lines[0], re.IGNORECASE)
        numero = numero_match.group(1) if numero_match else datetime.datetime.now().strftime("%d%H%M")

        cliente = lines[1] if len(lines) > 1 else "Desconhecido"

        itens = {}
        total = 0.0
        taxa  = 0.0
        pagamento  = "PIX"
        responsavel = "Não informado"

        for line in lines:
            line_u = line.upper()

            for prod in ["ICE", "PAK", "CRUMBLE", "POD"]:
                if prod in line_u:
                    q = re.search(r'(\d+(?:[.,]\d+)?)', line.replace(",", "."))
                    if q:
                        itens[prod] = itens.get(prod, 0) + float(q.group(1))

            if any(x in line_u for x in ["TOTAL", "R$"]):
                v = re.search(r'(\d+(?:[.,]\d+)?)', line.replace(",", "."))
                if v: total = float(v.group(1))

            if "TAXA" in line_u:
                v = re.search(r'(\d+(?:[.,]\d+)?)', line.replace(",", "."))
                if v: taxa = float(v.group(1))

            if any(x in line_u for x in ["DINHEIRO", "GRANA", "ESPECIE", "ESPÉCIE"]):
                pagamento = "DINHEIRO"
            elif "PIX" in line_u:
                pagamento = "PIX"

            if "RESPONSAVEL" in line_u or "RESPONSÁVEL" in line_u:
                responsavel = line.split(":", 1)[-1].strip()
            elif any(x in line_u for x in ["RD", "BART"]) and ":" in line:
                responsavel = line.split(":", 1)[-1].strip()

        if total <= 0:
            await update.message.reply_text(
                "❌ Informe o Total.\nEx: <code>Total: R$ 440</code>", parse_mode="HTML")
            return

        conn = get_db()
        c = conn.cursor()
        data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute(
            "INSERT INTO pedidos (numero, cliente, total, taxa, pagamento, responsavel, data, status) VALUES (?,?,?,?,?,?,?,?)",
            (numero, cliente, total, taxa, pagamento, responsavel, data, "OK"))
        pedido_id = c.lastrowid
        for prod, qtd in itens.items():
            c.execute("INSERT INTO itens_pedido VALUES (?,?,?)", (pedido_id, prod, qtd))
            c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, prod))
        conn.commit()
        conn.close()

        registrar_caixa("entrada", total, f"Pedido #{numero} - {cliente}")

        pag_emoji = "📲" if pagamento == "PIX" else "💵"
        itens_str = "\n".join(f"  {k}: {v:.1f}" for k, v in itens.items()) or "  (nenhum item)"
        await update.message.reply_text(
            f"✅ <b>Pedido #{numero} registrado!</b>\n"
            f"👤 {cliente}\n{itens_str}\n"
            f"💰 R$ {total:.2f} {pag_emoji} {pagamento}\n"
            f"👷 {responsavel}",
            parse_mode="HTML")

    except Exception as e:
        logging.error(f"Erro ao processar pedido: {e}")
        await update.message.reply_text(
            f"❌ Erro ao processar. Verifique o formato.\n\n<code>{e}</code>", parse_mode="HTML")

# ====================== MAIN ======================

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não definido!")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("menu",       menu))
    app.add_handler(CommandHandler("estoque",    ver_estoque))
    app.add_handler(CommandHandler("caixa",      ver_caixa))
    app.add_handler(CommandHandler("add",        adicionar_estoque))
    app.add_handler(CommandHandler("saida",      registrar_saida))
    app.add_handler(CommandHandler("rd",         retirada))
    app.add_handler(CommandHandler("bart",       retirada))
    app.add_handler(CommandHandler("banco",      atualizar_banco))
    app.add_handler(CommandHandler("fornecedor", atualizar_fornecedor))
    app.add_handler(CommandHandler("relatorio",  relatorio_dia))
    app.add_handler(CommandHandler("fechamento", relatorio_dia))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
