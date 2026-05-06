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

ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
CHAVE_PIX = os.getenv("CHAVE_PIX", "")
DB_PATH   = os.path.join(os.path.dirname(__file__), "controle.db")

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
        ("ICE",     "🍦 Ice o Lator", 375.0, 140.0),
        ("PAK",     "🥐 Pak",          170.0,  60.0),
        ("CRUMBLE", "🍪 Crumble",       83.0, 180.0),
        ("POD",     "🪦 Pod THC",        8.0, 450.0),
    ]
    c.executemany("INSERT OR IGNORE INTO produtos VALUES (?,?,?,?)", produtos)
    for cod, nome, _, _ in produtos:
        c.execute("UPDATE produtos SET nome=? WHERE codigo=? AND nome NOT LIKE ?",
                  (nome, cod, f"%{nome[-5:]}%"))

    c.execute("INSERT OR IGNORE INTO config VALUES ('saldo_banco', 0)")
    c.execute("INSERT OR IGNORE INTO config VALUES ('divida_fornecedor', 74890)")

    conn.commit()
    conn.close()

init_db()

# ====================== HELPERS ======================

CODIGOS = {"ICE", "PAK", "CRUMBLE", "POD"}
ALIAS   = {"I": "ICE", "P": "PAK", "C": "CRUMBLE", "VP": "POD"}

def estoque_emoji(qtd):
    if qtd <= 0:  return "❌"
    if qtd <= 20: return "⚠️"
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

def build_estoque_text():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos")
    rows = c.fetchall()
    conn.close()
    texto = "📦 <b>ESTOQUE ATUAL</b>\n━━━━━━━━━━━━━━\n\n"
    for cod, nome, qtd, preco in rows:
        texto += f"{estoque_emoji(qtd)} {nome}: <b>{qtd:.1f}</b> | R$ {preco:.2f}/un\n"
    return texto

def build_relatorio_text(hoje):
    hoje_fmt = datetime.datetime.strptime(hoje, "%Y-%m-%d").strftime("%d/%m/%Y")
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

    c.execute("SELECT SUM(CASE WHEN tipo='saida' THEN valor ELSE 0 END) FROM caixa WHERE data LIKE ?",
              (f"{hoje}%",))
    total_saidas = c.fetchone()[0] or 0

    saldo_banco = get_config("saldo_banco")
    divida      = get_config("divida_fornecedor")
    conn.close()

    rel  = "📊 <b>FECHAMENTO DO DIA</b>\n"
    rel += f"📅 {hoje_fmt}\n"
    rel += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    rel += f"📦 <b>Pedidos Realizados:</b> {qtd_pedidos or 0}\n"
    rel += f"💰 <b>Total Vendido:</b> R$ {total_vendido:.2f}\n\n"
    rel += "💳 <b>Forma de Pagamento:</b>\n"
    rel += f"   📲 PIX → R$ {pagamentos.get('PIX', 0):.2f}\n"
    rel += f"   💵 Dinheiro → R$ {pagamentos.get('DINHEIRO', 0):.2f}\n"
    if total_saidas > 0:
        rel += f"   💸 Saídas → R$ {total_saidas:.2f}\n"
        rel += f"   <b>Líquido: R$ {total_vendido - total_saidas:.2f}</b>\n"
    rel += "\n📦 <b>Saídas do Dia:</b>\n"
    for cod in ["ICE", "PAK", "CRUMBLE", "POD"]:
        rel += f"   • {cod}: <b>{saidas.get(cod, 0):.1f}</b>\n"
    rel += "\n📦 <b>Estoque Restante:</b>\n"
    for cod, nome, qtd in estoque_atual:
        rel += f"   {estoque_emoji(qtd)} {nome}: <b>{qtd:.1f}</b>\n"
    rel += f"\n🏦 Saldo Banco: R$ {saldo_banco:.2f}\n"
    rel += f"📉 Dívida Fornecedor: R$ {divida:.2f}\n"
    rel += "\n━━━━━━━━━━━━━━━━━━━━━━━\n"
    rel += "✅ Relatório gerado automaticamente"
    return rel

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Novo Pedido",      callback_data="novo_pedido")],
        [InlineKeyboardButton("📦 Ver Estoque",       callback_data="estoque")],
        [InlineKeyboardButton("💰 Caixa do Dia",      callback_data="caixa")],
        [InlineKeyboardButton("📊 Relatório do Dia",  callback_data="relatorio")],
    ])

# ====================== COMANDOS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🍪 <b>Cookie Control Pro</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sistema profissional de controle\n"
        "Escolha uma opção abaixo 👇",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

async def cmd_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_estoque_text(), parse_mode="HTML")

async def cmd_caixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoje = datetime.date.today().strftime("%Y-%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo", (f"{hoje}%",))
    rows = c.fetchall()
    c.execute("SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
              (f"{hoje}%",))
    saldo = c.fetchone()[0] or 0
    conn.close()
    msg = "💰 <b>CAIXA DO DIA</b>\n━━━━━━━━━━━━━━\n\n"
    for tipo, total in rows:
        msg += f"{'Entradas' if tipo == 'entrada' else 'Saídas'}: R$ {total:.2f}\n"
    msg += f"\n<b>Saldo: R$ {saldo:.2f}</b>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode ver o relatório.")
        return
    hoje = datetime.datetime.now().strftime("%Y-%m-%d")
    await update.message.reply_text(build_relatorio_text(hoje), parse_mode="HTML")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"✅ {nome} +{qtd:.1f}\n📦 Agora: {novo:.1f}", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ Uso: /add ICE 50")

async def cmd_saida(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_retirada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode registrar retiradas.")
        return
    try:
        cmd   = update.message.text.split()[0].lower().lstrip("/")
        socio = "RD" if cmd == "rd" else "Bart"
        valor = float(context.args[0].replace(",", "."))
        conn  = get_db()
        c     = conn.cursor()
        data  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO retiradas (socio, valor, data) VALUES (?,?,?)", (socio, valor, data))
        conn.commit()
        conn.close()
        registrar_caixa("saida", valor, f"Retirada {socio}")
        await update.message.reply_text(f"✅ {socio} retirou R$ {valor:.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /rd 500  ou  /bart 300")

async def cmd_banco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador.")
        return
    try:
        set_config("saldo_banco", float(context.args[0].replace(",", ".")))
        await update.message.reply_text(f"✅ Saldo Banco atualizado: R$ {get_config('saldo_banco'):.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /banco 12500")

async def cmd_fornecedor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador.")
        return
    try:
        set_config("divida_fornecedor", float(context.args[0].replace(",", ".")))
        await update.message.reply_text(f"✅ Dívida atualizada: R$ {get_config('divida_fornecedor'):.2f}")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: /fornecedor 74890")

# ====================== RESET ======================

async def reset_dia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode resetar.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Sim, limpar hoje", callback_data="resetdia_sim")],
        [InlineKeyboardButton("❌ Cancelar",          callback_data="cancelar_nao")],
    ])
    hoje = datetime.date.today().strftime("%d/%m/%Y")
    await update.message.reply_text(
        f"⚠️ <b>Limpar todos os pedidos de {hoje}?</b>\n\n"
        "Os registros de caixa do dia também serão removidos.\n"
        "<b>O estoque não será alterado.</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def reset_completo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Acesso negado.")
        return
    if not context.args or context.args[0].lower() != "confirmar":
        await update.message.reply_text(
            "⚠️ <b>CUIDADO!</b> Isso apaga TODOS os pedidos e caixa do histórico.\n\n"
            "Digite <code>/resetcompleto confirmar</code> para confirmar.\n"
            "O estoque não será alterado.",
            parse_mode="HTML"
        )
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM itens_pedido")
    c.execute("DELETE FROM pedidos")
    c.execute("DELETE FROM caixa")
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Reset completo realizado. Todo o histórico foi apagado.")

# ====================== CANCELAR ÚLTIMO PEDIDO ======================

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode cancelar pedidos.")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, numero, cliente, total, pagamento, data FROM pedidos "
        "WHERE status='OK' ORDER BY id DESC LIMIT 1"
    )
    row = c.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("ℹ️ Nenhum pedido para cancelar.")
        return

    pedido_id, numero, cliente, total, pagamento, data = row
    pag_emoji = "📲" if pagamento == "PIX" else "💵"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sim, cancelar", callback_data=f"cancelar_sim_{pedido_id}")],
        [InlineKeyboardButton("❌ Não, manter",   callback_data="cancelar_nao")],
    ])
    await update.message.reply_text(
        "⚠️ <b>Cancelar último pedido?</b>\n\n"
        f"🔢 Pedido: <b>#{numero}</b>\n"
        f"👤 Cliente: {cliente}\n"
        f"💰 Total: R$ {total:.2f} {pag_emoji}\n"
        f"🕐 Data: {data}\n\n"
        "O estoque será restaurado automaticamente.",
        parse_mode="HTML",
        reply_markup=keyboard
    )

# ====================== FLUXO GUIADO ======================

async def calcular_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str):
    conn = get_db()
    c = conn.cursor()
    itens    = {}
    subtotal = 0.0

    for line in texto.upper().split("\n"):
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*(ICE|PAK|CRUMBLE|POD)', line)
        if match:
            qtd  = float(match.group(1).replace(",", "."))
            prod = match.group(2)
            c.execute("SELECT preco_venda FROM produtos WHERE codigo=?", (prod,))
            row = c.fetchone()
            if row:
                itens[prod] = itens.get(prod, 0) + qtd
                subtotal   += qtd * row[0]

    conn.close()

    if not itens:
        await update.message.reply_text(
            "⚠️ Nenhum item reconhecido.\nFormato: <code>5 PAK\n2 ICE</code>",
            parse_mode="HTML")
        return

    taxa       = 10.0 if subtotal < 500 else 0.0
    total      = subtotal + taxa

    context.user_data["itens"]    = itens
    context.user_data["subtotal"] = subtotal
    context.user_data["taxa"]     = taxa
    context.user_data["total"]    = total
    context.user_data["estado"]   = "confirmar"

    msg  = "📋 <b>RESUMO DO PEDIDO</b>\n\n"
    msg += f"👤 Cliente: <b>{context.user_data['cliente']}</b>\n\n"
    for p, q in itens.items():
        msg += f"   • {q:.1f} × {p}\n"
    msg += f"\n💰 Subtotal: R$ {subtotal:.2f}"
    if taxa > 0:
        msg += f"\n📌 Taxa de entrega: R$ {taxa:.2f}"
    msg += f"\n\n💎 <b>Total: R$ {total:.2f}</b>\n\n"
    msg += "Responda <b>Sim</b> para confirmar ou <b>Não</b> para cancelar."
    await update.message.reply_text(msg, parse_mode="HTML")

async def salvar_pedido_guiado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn   = get_db()
    c      = conn.cursor()
    data   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    numero = datetime.datetime.now().strftime("%d%H%M")
    cliente = context.user_data["cliente"]
    total   = context.user_data["total"]
    taxa    = context.user_data["taxa"]
    itens   = context.user_data["itens"]

    c.execute(
        "INSERT INTO pedidos (numero, cliente, total, taxa, pagamento, responsavel, data, status) VALUES (?,?,?,?,?,?,?,?)",
        (numero, cliente, total, taxa, "PIX", "Bot", data, "OK"))
    pedido_id = c.lastrowid
    for prod, qtd in itens.items():
        c.execute("INSERT INTO itens_pedido VALUES (?,?,?)", (pedido_id, prod, qtd))
        c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, prod))
    conn.commit()
    conn.close()

    registrar_caixa("entrada", total, f"Pedido #{numero} - {cliente}")

    await update.message.reply_text(
        f"✅ <b>Pedido #{numero} confirmado com sucesso!</b>\n\n"
        f"👤 Cliente: {cliente}\n"
        f"💰 Total: R$ {total:.2f}\n\n"
        "🚚 Seu pedido foi confirmado.\n"
        "Em breve o entregador entrará em contato para realizar a entrega.\n\n"
        "⏰ Lembrando que o horário de entrega é após as 19:30.",
        parse_mode="HTML"
    )
    context.user_data.clear()

# ====================== CALLBACK BUTTONS ======================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "novo_pedido":
        context.user_data.clear()
        context.user_data["estado"] = "esperando_cliente"
        await query.edit_message_text("👤 Digite o nome do cliente:")

    elif query.data == "estoque":
        await query.edit_message_text(build_estoque_text(), parse_mode="HTML")

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
        msg = "💰 <b>CAIXA DO DIA</b>\n━━━━━━━━━━━━━━\n\n"
        for tipo, total in rows:
            msg += f"{'Entradas' if tipo == 'entrada' else 'Saídas'}: R$ {total:.2f}\n"
        msg += f"\n<b>Saldo: R$ {saldo:.2f}</b>"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif query.data == "relatorio":
        if not is_admin(query.from_user.id):
            await query.edit_message_text("❌ Acesso negado. Apenas o administrador.")
            return
        hoje = datetime.datetime.now().strftime("%Y-%m-%d")
        await query.edit_message_text(build_relatorio_text(hoje), parse_mode="HTML")

    elif query.data.startswith("cancelar_sim_"):
        if not is_admin(query.from_user.id):
            await query.edit_message_text("❌ Acesso negado.")
            return
        pedido_id = int(query.data.split("_")[-1])
        conn = get_db()
        c = conn.cursor()
        # Restore stock for each item
        c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (pedido_id,))
        itens = c.fetchall()
        for prod, qtd in itens:
            c.execute("UPDATE produtos SET estoque = estoque + ? WHERE codigo = ?", (qtd, prod))
        # Get order info for caixa reversal
        c.execute("SELECT numero, cliente, total FROM pedidos WHERE id=?", (pedido_id,))
        row = c.fetchone()
        # Mark as cancelled instead of deleting (keeps history)
        c.execute("UPDATE pedidos SET status='CANCELADO' WHERE id=?", (pedido_id,))
        conn.commit()
        conn.close()
        # Reverse the caixa entry
        if row:
            numero, cliente, total = row
            registrar_caixa("saida", total, f"Cancelamento pedido #{numero} - {cliente}")
        itens_str = "\n".join(f"   • {p}: +{q:.1f} (restaurado)" for p, q in itens)
        await query.edit_message_text(
            f"✅ <b>Pedido #{row[0] if row else pedido_id} cancelado!</b>\n\n"
            f"📦 <b>Estoque restaurado:</b>\n{itens_str}",
            parse_mode="HTML"
        )

    elif query.data == "cancelar_nao":
        await query.edit_message_text("👍 Nenhuma alteração feita.")

    elif query.data == "resetdia_sim":
        if not is_admin(query.from_user.id):
            await query.edit_message_text("❌ Acesso negado.")
            return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM itens_pedido WHERE pedido_id IN (SELECT id FROM pedidos WHERE data LIKE ?)",
                  (f"{hoje}%",))
        c.execute("DELETE FROM pedidos WHERE data LIKE ?", (f"{hoje}%",))
        c.execute("DELETE FROM caixa WHERE data LIKE ?", (f"{hoje}%",))
        conn.commit()
        conn.close()
        hoje_fmt = datetime.date.today().strftime("%d/%m/%Y")
        await query.edit_message_text(
            f"🗑️ Pedidos e caixa de <b>{hoje_fmt}</b> apagados.\n"
            "O estoque não foi alterado.",
            parse_mode="HTML"
        )

# ====================== MESSAGE HANDLER ======================

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto  = update.message.text.strip()
    estado = context.user_data.get("estado")

    # --- Fluxo guiado (Novo Pedido via botão) ---
    if estado == "esperando_cliente":
        context.user_data["cliente"] = texto
        context.user_data["estado"]  = "esperando_itens"
        await update.message.reply_text(
            "🛒 Envie os itens (um por linha):\n\n"
            "<code>5 PAK\n2 ICE\n1 POD</code>",
            parse_mode="HTML"
        )
        return

    if estado == "esperando_itens":
        await calcular_pedido(update, context, texto)
        return

    if estado == "confirmar":
        if texto.lower() in ("sim", "ok", "s", "confirmar", "yes"):
            await salvar_pedido_guiado(update, context)
        else:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Pedido cancelado.\n\n"
                "Use /start para voltar ao menu.",
            )
        return

    # --- Desconto rápido: "ICE 3" ou "I 3" (uma linha) ---
    lines = [l.strip() for l in texto.split("\n") if l.strip()]
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
                    if novo <= 0:    aviso = "\n🚨 <b>ESTOQUE ZERADO!</b>"
                    elif novo <= 20: aviso = "\n⚠️ <b>Estoque baixo!</b>"
                    await update.message.reply_text(
                        f"✅ {nome} -{qtd:.1f}\n📦 Agora: {novo:.1f}{aviso}",
                        parse_mode="HTML")
                except ValueError:
                    pass
        return

    # --- Parser de pedido colado (requer "pedido" ou "total") ---
    if not re.search(r'pedido|total', texto, re.IGNORECASE):
        return

    try:
        numero_match = re.search(r'pedido\s*(\d+)', lines[0], re.IGNORECASE)
        numero      = numero_match.group(1) if numero_match else datetime.datetime.now().strftime("%d%H%M")
        cliente     = lines[1] if len(lines) > 1 else "Desconhecido"
        itens       = {}
        total       = 0.0
        taxa        = 0.0
        pagamento   = "PIX"
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
        itens_str = "\n".join(f"   • {k}: {v:.1f}" for k, v in itens.items()) or "   (nenhum item)"
        await update.message.reply_text(
            f"✅ <b>Pedido #{numero} registrado!</b>\n"
            f"👤 {cliente}\n{itens_str}\n"
            f"💰 R$ {total:.2f} {pag_emoji} {pagamento}\n"
            f"👷 {responsavel}",
            parse_mode="HTML")

    except Exception as e:
        logging.error(f"Erro ao processar pedido: {e}")
        await update.message.reply_text(
            f"❌ Erro ao processar. Verifique o formato.\n\n<code>{e}</code>",
            parse_mode="HTML")

# ====================== MAIN ======================

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não definido!")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("menu",       start))
    app.add_handler(CommandHandler("estoque",    cmd_estoque))
    app.add_handler(CommandHandler("caixa",      cmd_caixa))
    app.add_handler(CommandHandler("add",        cmd_add))
    app.add_handler(CommandHandler("saida",      cmd_saida))
    app.add_handler(CommandHandler("rd",         cmd_retirada))
    app.add_handler(CommandHandler("bart",       cmd_retirada))
    app.add_handler(CommandHandler("banco",      cmd_banco))
    app.add_handler(CommandHandler("fornecedor", cmd_fornecedor))
    app.add_handler(CommandHandler("relatorio",  cmd_relatorio))
    app.add_handler(CommandHandler("fechamento", cmd_relatorio))
    app.add_handler(CommandHandler("cancelar",      cmd_cancelar))
    app.add_handler(CommandHandler("resetdia",      reset_dia))
    app.add_handler(CommandHandler("resetcompleto", reset_completo))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
