import logging
import sqlite3
import datetime
import re
import os
import json
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

ADMIN_ID             = int(os.getenv("ADMIN_ID", "0"))
CHAVE_PIX            = os.getenv("CHAVE_PIX", "")
DB_PATH              = os.path.join(os.path.dirname(__file__), "controle.db")
ENTREGADOR_USERNAME  = "@jRDG7"

# Pedidos de clientes aguardando confirmação de pagamento (em memória)
pedidos_pendentes: dict = {}  # customer_chat_id -> order_data

PRODUTOS_INFO = {
    "ICE":     ("🍦 Ice Cream Cake",  140.0, "g"),
    "PAK":     ("🥐 Pak Nutella",      60.0, "g"),
    "CRUMBLE": ("🍪 Crumble",         180.0, "g"),
    "POD_I":   ("🪦 Pod THC Indica",  450.0, "un"),
    "POD_S":   ("🌿 Pod THC Sativa",  450.0, "un"),
}

# Estoque de referência para cálculo da barra visual (máximo esperado)
ESTOQUE_MAX_REF = {
    "ICE":     200.0,
    "PAK":     500.0,
    "CRUMBLE": 300.0,
    "POD_I":    20.0,
    "POD_S":    20.0,
}

def barra_estoque(qtd: float, max_ref: float, largura: int = 10) -> str:
    if qtd <= 0:
        return "░" * largura
    ratio  = min(1.0, qtd / max_ref)
    cheios = max(1, round(ratio * largura))
    vazios = largura - cheios
    return "▓" * cheios + "░" * vazios

def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID

def is_entregador(user) -> bool:
    if not user or not user.username:
        return False
    return f"@{user.username}".lower() == ENTREGADOR_USERNAME.lower()

FOTOS_PATH = os.path.join(os.path.dirname(__file__), "fotos.json")

def load_fotos() -> dict:
    if os.path.exists(FOTOS_PATH):
        with open(FOTOS_PATH) as f:
            return json.load(f)
    return {}

def save_foto(cod: str, file_id: str):
    fotos = load_fotos()
    fotos[cod] = file_id
    with open(FOTOS_PATH, "w") as f:
        json.dump(fotos, f)

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
        ("ICE",     "🍦 Ice Cream Cake",  375.0, 140.0),
        ("PAK",     "🥐 Pak Nutella",     170.0,  60.0),
        ("CRUMBLE", "🍪 Crumble",          83.0, 180.0),
        ("POD_I",   "🪦 Pod THC Indica",    3.0, 450.0),
        ("POD_S",   "🌿 Pod THC Sativa",    4.0, 450.0),
    ]
    c.executemany("INSERT OR IGNORE INTO produtos VALUES (?,?,?,?)", produtos)
    # Update display names
    for cod, nome, _, _ in produtos:
        c.execute("UPDATE produtos SET nome=? WHERE codigo=?", (nome, cod))
    # Migrate old POD stock into POD_I/POD_S if it exists with stock > 0
    c.execute("SELECT estoque FROM produtos WHERE codigo='POD'")
    old_pod = c.fetchone()
    if old_pod and old_pod[0] > 0:
        total = old_pod[0]
        sativa  = round(total / 2 + 0.5)
        indica  = total - sativa
        c.execute("UPDATE produtos SET estoque = estoque + ? WHERE codigo='POD_I'", (indica,))
        c.execute("UPDATE produtos SET estoque = estoque + ? WHERE codigo='POD_S'", (sativa,))
        c.execute("UPDATE produtos SET estoque=0 WHERE codigo='POD'")

    c.execute("INSERT OR IGNORE INTO config VALUES ('saldo_banco', 0)")
    c.execute("INSERT OR IGNORE INTO config VALUES ('divida_fornecedor', 74890)")
    c.execute("INSERT OR IGNORE INTO config VALUES ('loja_aberta', 1)")

    c.execute('''CREATE TABLE IF NOT EXISTS clientes (
                 chat_id INTEGER PRIMARY KEY,
                 username TEXT,
                 nome TEXT,
                 data_registro TEXT)''')

    # Migrações de colunas em pedidos
    c.execute("PRAGMA table_info(pedidos)")
    cols = [row[1] for row in c.fetchall()]
    if "endereco" not in cols:
        c.execute("ALTER TABLE pedidos ADD COLUMN endereco TEXT DEFAULT ''")
    if "customer_chat_id" not in cols:
        c.execute("ALTER TABLE pedidos ADD COLUMN customer_chat_id INTEGER DEFAULT 0")
    if "data_entrega" not in cols:
        c.execute("ALTER TABLE pedidos ADD COLUMN data_entrega TEXT DEFAULT ''")

    conn.commit()
    conn.close()

init_db()

# ====================== HELPERS ======================

CODIGOS = {"ICE", "PAK", "CRUMBLE", "POD_I", "POD_S"}
ALIAS   = {
    "I":    "ICE",
    "P":    "PAK",
    "C":    "CRUMBLE",
    "PI":   "POD_I",
    "PS":   "POD_S",
    "PODI": "POD_I",
    "PODS": "POD_S",
    "POD":  "POD_I",   # fallback legado
    "VP":   "POD_I",   # alias antigo
}

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
    if tipo == "entrada":
        c.execute("UPDATE config SET valor = valor + ? WHERE chave = 'saldo_banco'", (valor,))
    else:
        c.execute("UPDATE config SET valor = valor - ? WHERE chave = 'saldo_banco'", (valor,))
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

def loja_esta_aberta() -> bool:
    return bool(get_config("loja_aberta"))

def registrar_cliente(chat_id: int, username: str, nome: str):
    conn = get_db(); c = conn.cursor()
    data = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT OR REPLACE INTO clientes VALUES (?,?,?,?)",
              (chat_id, username or "", nome or "", data))
    conn.commit(); conn.close()

async def check_low_stock(bot):
    LIMITES = {"ICE": 50, "PAK": 30, "CRUMBLE": 20, "POD_I": 1, "POD_S": 1}
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT codigo, nome, estoque FROM produtos WHERE codigo IN ('ICE','PAK','CRUMBLE','POD_I','POD_S')")
    rows = c.fetchall(); conn.close()
    alertas = []
    for cod, nome, est in rows:
        if cod in LIMITES and est <= LIMITES[cod]:
            alertas.append(f"  {estoque_emoji(est)} {nome}: <b>{est:.1f}</b>")
    if alertas and ADMIN_ID:
        msg = "⚠️ <b>ALERTA — ESTOQUE BAIXO</b>\n━━━━━━━━━━━━━━\n\n" + "\n".join(alertas)
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Falha ao enviar alerta de estoque: {e}")

def build_relatorio_periodo(data_ini: str, data_fim: str, titulo: str) -> str:
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT pagamento, SUM(total) FROM pedidos WHERE data BETWEEN ? AND ? AND status='OK' GROUP BY pagamento",
              (data_ini, data_fim + " 23:59"))
    pagamentos = dict(c.fetchall())
    c.execute("SELECT SUM(total), COUNT(*) FROM pedidos WHERE data BETWEEN ? AND ? AND status='OK'",
              (data_ini, data_fim + " 23:59"))
    row = c.fetchone(); conn.close()
    total, qtd = (row[0] or 0), (row[1] or 0)
    rel  = f"📊 <b>{titulo}</b>\n━━━━━━━━━━━━━━\n\n"
    rel += f"🛒 Pedidos: <b>{qtd}</b>\n"
    rel += f"💰 Total: <b>R$ {total:.2f}</b>\n"
    rel += f"   📲 PIX: R$ {pagamentos.get('PIX', 0):.2f}\n"
    rel += f"   💵 Dinheiro: R$ {pagamentos.get('DINHEIRO', 0):.2f}\n"
    return rel

def build_estoque_text():
    hoje     = datetime.date.today().strftime("%Y-%m-%d")
    hoje_fmt = datetime.date.today().strftime("%d/%m/%Y")
    hora     = datetime.datetime.now().strftime("%H:%M")

    conn = get_db(); c = conn.cursor()
    c.execute("SELECT codigo, nome, estoque, preco_venda FROM produtos ORDER BY codigo")
    rows = c.fetchall()

    # Saídas do dia por produto
    c.execute("""
        SELECT i.produto, SUM(i.quantidade)
        FROM itens_pedido i JOIN pedidos p ON i.pedido_id = p.id
        WHERE p.data LIKE ? AND p.status != 'CANCELADO'
        GROUP BY i.produto
    """, (f"{hoje}%",))
    saidas_dia = dict(c.fetchall())

    # Total de pedidos e faturamento hoje
    c.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0) FROM pedidos WHERE data LIKE ? AND status!='CANCELADO'",
        (f"{hoje}%",)
    )
    qtd_ped, total_dia = c.fetchone()
    conn.close()

    texto  = f"📦 <b>ESTOQUE ATUAL</b>\n"
    texto += f"📅 {hoje_fmt}  ·  🕐 {hora}\n"
    texto += f"━━━━━━━━━━━━━━━━━━━━\n\n"

    valor_total = 0.0
    for cod, nome_db, qtd, preco in rows:
        if cod not in PRODUTOS_INFO:
            continue
        nome_emoji, _, unidade = PRODUTOS_INFO[cod]
        max_ref  = ESTOQUE_MAX_REF.get(cod, 100)
        barra    = barra_estoque(qtd, max_ref)
        saiu     = saidas_dia.get(cod, 0.0)
        v_est    = qtd * preco
        valor_total += v_est
        pct      = int(min(100, (qtd / max_ref) * 100)) if max_ref > 0 else 0

        if qtd <= 0:
            st = "❌ SEM ESTOQUE"
        elif qtd <= 20:
            st = "⚠️ BAIXO"
        else:
            st = "✅ OK"

        qtd_str  = f"{qtd:.0f}" if qtd == int(qtd) else f"{qtd:.1f}"
        saiu_str = f"{saiu:.0f}" if saiu == int(saiu) else f"{saiu:.1f}"

        texto += f"<b>{nome_emoji}</b>  {st}\n"
        texto += f"  <code>{barra}</code>  {pct}%\n"
        texto += f"  📊 Estoque: <b>{qtd_str} {unidade}</b>  ·  💰 R$ {preco:.0f}/{unidade}\n"
        texto += f"  📉 Saiu hoje: <b>{saiu_str} {unidade}</b>"
        if saiu > 0:
            texto += f"  ·  💵 R$ {saiu * preco:.0f}"
        texto += "\n\n"

    texto += f"━━━━━━━━━━━━━━━━━━━━\n"
    texto += f"🧾 Pedidos hoje: <b>{qtd_ped}</b>  ·  💰 Faturado: <b>R$ {total_dia:.0f}</b>\n"
    texto += f"📦 Valor em estoque: <b>R$ {valor_total:,.0f}</b>"
    return texto

def build_estoque_historico_text(data_str: str) -> str:
    """Estoque de um dia específico: início, saídas e saldo final."""
    data_dt   = datetime.datetime.strptime(data_str, "%Y-%m-%d").date()
    hoje      = datetime.date.today()
    data_fmt  = data_dt.strftime("%d/%m/%Y")
    dia_sem   = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"][data_dt.weekday()]
    eh_hoje   = (data_dt == hoje)

    conn = get_db(); c = conn.cursor()
    c.execute("SELECT codigo, estoque, preco_venda FROM produtos")
    estoque_atual = {cod: (est, preco) for cod, est, preco in c.fetchall()}

    # Saídas APÓS esse dia (para reconstruir estoque no início do dia)
    prox_dia = (data_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    c.execute("""
        SELECT i.produto, SUM(i.quantidade)
        FROM itens_pedido i JOIN pedidos p ON i.pedido_id = p.id
        WHERE p.data >= ? AND p.status != 'CANCELADO'
        GROUP BY i.produto
    """, (prox_dia,))
    saidas_apos = dict(c.fetchall())

    # Saídas desse dia
    c.execute("""
        SELECT i.produto, SUM(i.quantidade)
        FROM itens_pedido i JOIN pedidos p ON i.pedido_id = p.id
        WHERE p.data LIKE ? AND p.status != 'CANCELADO'
        GROUP BY i.produto
    """, (f"{data_str}%",))
    saidas_dia = dict(c.fetchall())

    # Pedidos e faturamento do dia
    c.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0), "
        "COALESCE(SUM(CASE WHEN pagamento='PIX' THEN total ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN pagamento='DINHEIRO' THEN total ELSE 0 END),0) "
        "FROM pedidos WHERE data LIKE ? AND status!='CANCELADO'",
        (f"{data_str}%",)
    )
    qtd_ped, total_dia, total_pix, total_din = c.fetchone()
    conn.close()

    titulo = f"📦 <b>ESTOQUE</b>  ·  {dia_sem} {data_fmt}"
    if eh_hoje:
        titulo += "  <i>(hoje)</i>"
    texto  = titulo + "\n"
    texto += f"━━━━━━━━━━━━━━━━━━━━\n\n"

    valor_saiu_total = 0.0
    for cod, (nome_emoji, _, unidade) in PRODUTOS_INFO.items():
        est_at, preco = estoque_atual.get(cod, (0, 0))
        saiu  = saidas_dia.get(cod, 0.0)
        apos  = saidas_apos.get(cod, 0.0)
        ini   = est_at + saiu + apos   # estoque no início do dia
        fim   = ini - saiu             # estoque no fim do dia

        max_ref  = ESTOQUE_MAX_REF.get(cod, 100)
        pct_ini  = int(min(100, (ini / max_ref) * 100)) if max_ref > 0 and ini > 0 else 0
        pct_fim  = int(min(100, (fim / max_ref) * 100)) if max_ref > 0 and fim > 0 else 0

        ini_str  = f"{ini:.0f}" if ini == int(ini) else f"{ini:.1f}"
        fim_str  = f"{fim:.0f}" if fim == int(fim) else f"{fim:.1f}"
        saiu_str = f"{saiu:.0f}" if saiu == int(saiu) else f"{saiu:.1f}"
        v_saiu   = saiu * preco
        valor_saiu_total += v_saiu

        if saiu > 0:
            barra_i = barra_estoque(ini, max_ref)
            barra_f = barra_estoque(fim, max_ref)
            texto += f"<b>{nome_emoji}</b>\n"
            texto += f"  Início  <code>{barra_i}</code> {pct_ini}%  {ini_str}{unidade}\n"
            texto += f"  Final   <code>{barra_f}</code> {pct_fim}%  {fim_str}{unidade}\n"
            texto += f"  📉 Saiu: <b>{saiu_str} {unidade}</b>  ·  💵 R$ {v_saiu:.0f}\n\n"
        else:
            barra_i = barra_estoque(ini, max_ref)
            st_icon = "❌" if ini <= 0 else ("⚠️" if ini <= 20 else "✅")
            texto += f"<b>{nome_emoji}</b>  {st_icon}\n"
            texto += f"  <code>{barra_i}</code>  {ini_str}{unidade}  ·  sem saída\n\n"

    texto += f"━━━━━━━━━━━━━━━━━━━━\n"
    if qtd_ped > 0:
        texto += f"🧾 <b>{qtd_ped} pedido(s)</b>  ·  💰 <b>R$ {total_dia:.0f}</b>\n"
        if total_pix > 0:
            texto += f"   📲 PIX: R$ {total_pix:.0f}"
        if total_din > 0:
            texto += f"  💵 Dinheiro: R$ {total_din:.0f}"
        if total_pix > 0 or total_din > 0:
            texto += "\n"
        texto += f"📦 Total saiu (valor): <b>R$ {valor_saiu_total:.0f}</b>"
    else:
        texto += f"🧾 Nenhum pedido nesse dia."
    return texto

def estoque_nav_keyboard(data_str: str, voltar_cb: str) -> InlineKeyboardMarkup:
    data_dt  = datetime.datetime.strptime(data_str, "%Y-%m-%d").date()
    hoje     = datetime.date.today()
    ant      = (data_dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    prox     = (data_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    data_fmt = data_dt.strftime("%d/%m")

    btn_prox = (
        InlineKeyboardButton("Próximo ▶", callback_data=f"estoque_dia_{prox}")
        if data_dt < hoje else
        InlineKeyboardButton("▶", callback_data="noop")
    )
    rows = [
        [
            InlineKeyboardButton("◀ Anterior", callback_data=f"estoque_dia_{ant}"),
            InlineKeyboardButton(f"📅 {data_fmt}", callback_data="noop"),
            btn_prox,
        ],
    ]
    # Atalhos rápidos
    atalhos = []
    if data_str != hoje.strftime("%Y-%m-%d"):
        atalhos.append(InlineKeyboardButton("📊 Hoje", callback_data="estoque"))
    ontem = (hoje - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if data_str != ontem:
        atalhos.append(InlineKeyboardButton("⏪ Ontem", callback_data=f"estoque_dia_{ontem}"))
    if atalhos:
        rows.append(atalhos)
    rows.append([InlineKeyboardButton("← Menu", callback_data=voltar_cb)])
    return InlineKeyboardMarkup(rows)

def build_pedidos_dia_text(hoje: str) -> str:
    hoje_fmt = datetime.datetime.strptime(hoje, "%Y-%m-%d").strftime("%d/%m/%Y")
    conn = get_db(); c = conn.cursor()
    c.execute("""
        SELECT id, numero, cliente, total, pagamento, data, endereco, data_entrega, status
        FROM pedidos
        WHERE data LIKE ? AND status != 'CANCELADO'
        ORDER BY id ASC
    """, (f"{hoje}%",))
    pedidos = c.fetchall()
    total_geral = 0
    n_entregue = 0
    linhas = []
    for ped_id, numero, cliente, total, pagamento, data_ped, endereco, data_entrega, status in pedidos:
        hora = data_ped[11:16] if len(data_ped) > 10 else "?"
        total_geral += total or 0
        c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (ped_id,))
        itens_rows = c.fetchall()
        itens_str = "  ".join(
            f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
            for p, q in itens_rows
        )
        end_str = f"\n    📍 {endereco}" if endereco and endereco not in ("", "Retirada") else ("  🏪 Retirada" if endereco == "Retirada" else "")
        ag_str  = f"  📅{datetime.datetime.strptime(data_entrega,'%Y-%m-%d').strftime('%d/%m')}" if data_entrega and data_entrega != hoje else ""
        pag_emoji = "📲" if pagamento == "PIX" else "💵"
        if status == "ENTREGUE":
            st_emoji = "✅"; n_entregue += 1
        else:
            st_emoji = "🚚"
        linhas.append(
            f"{st_emoji} <b>{hora}</b>  #{numero}  {pag_emoji} R$ {total:.0f}{ag_str}\n"
            f"    👤 {cliente}  |  {itens_str}{end_str}"
        )
    saldo_banco = get_config("saldo_banco")
    conn.close()
    if not pedidos:
        corpo = "\n<i>Nenhum pedido hoje.</i>\n"
    else:
        corpo = "\n" + "\n\n".join(linhas) + "\n"
    n_total = len(pedidos)
    n_pend  = n_total - n_entregue
    msg  = f"📋 <b>PEDIDOS DO DIA</b> — {hoje_fmt}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += corpo
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"🧾 <b>{n_total} pedido(s)</b>  ✅ {n_entregue} entregue(s)  🚚 {n_pend} pendente(s)\n"
    msg += f"💰 <b>R$ {total_geral:.0f}</b>  |  🏦 <b>Banco: R$ {saldo_banco:.2f}</b>"
    return msg

def build_admin_header() -> str:
    """Cabeçalho rico do painel admin com dados ao vivo."""
    hoje = datetime.date.today().strftime("%Y-%m-%d")
    amanha = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM pedidos WHERE data LIKE ? AND status NOT IN ('CANCELADO')", (f"{hoje}%",))
    qtd_hoje, total_hoje = c.fetchone()
    c.execute("SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND status='OK' AND responsavel='Loja-Bot'", (f"{hoje}%",))
    pend_entrega = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND status='ENTREGUE'", (f"{hoje}%",))
    entregues = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM pedidos WHERE data_entrega=? AND status='OK'", (amanha,))
    ag_amanha = c.fetchone()[0]
    conn.close()
    saldo = get_config("saldo_banco")
    n_pix = len(pedidos_pendentes)
    loja_status = "🟢 Aberta" if loja_esta_aberta() else "🔴 Fechada"
    hora_atual  = datetime.datetime.now().strftime("%H:%M")
    msg  = f"🍪 <b>COOKIE CONTROL PRO</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏪 {loja_status}  |  🕐 {hora_atual}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"📋 Hoje: <b>{qtd_hoje} pedido(s)</b>  💰 <b>R$ {total_hoje:.0f}</b>\n"
    msg += f"✅ Entregues: <b>{entregues}</b>  |  🚚 Pendentes: <b>{pend_entrega}</b>\n"
    if n_pix > 0:
        msg += f"⏳ PIX aguardando confirmação: <b>{n_pix}</b>\n"
    if ag_amanha > 0:
        msg += f"📅 Agendados p/ amanhã: <b>{ag_amanha}</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏦 Saldo Banco: <b>R$ {saldo:.2f}</b>"
    return msg

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
    for cod in ["ICE", "PAK", "CRUMBLE", "POD_I", "POD_S"]:
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
    loja_btn = "🔴 Fechar Loja" if loja_esta_aberta() else "🟢 Abrir Loja"
    n_pix  = len(pedidos_pendentes)
    pix_label = f"⏳ PIX Pendentes ({n_pix}) ❗" if n_pix > 0 else "⏳ PIX Pendentes"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pix_label,               callback_data="admin_pendentes"),
         InlineKeyboardButton("🚚 Entregas Pendentes", callback_data="admin_entregas_pendentes")],
        [InlineKeyboardButton("🗒️ Pedidos do Dia",     callback_data="pedidos_dia"),
         InlineKeyboardButton("📅 Agendados Amanhã",   callback_data="admin_agendados")],
        [InlineKeyboardButton("💰 Caixa do Dia",       callback_data="caixa"),
         InlineKeyboardButton("📊 Relatório Hoje",     callback_data="relatorio")],
        [InlineKeyboardButton("📦 Estoque",            callback_data="estoque"),
         InlineKeyboardButton("💸 Financeiro",         callback_data="admin_menu_financeiro")],
        [InlineKeyboardButton("🏪 Gestão de Estoque",  callback_data="admin_menu_estoque")],
        [InlineKeyboardButton("📢 Broadcast",          callback_data="admin_broadcast"),
         InlineKeyboardButton(loja_btn,                callback_data="admin_toggle_loja")],
        [InlineKeyboardButton("❌ Cancelar Último Pedido", callback_data="admin_cancelar")],
    ])

def admin_estoque_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Adicionar Estoque",  callback_data="admin_add"),
         InlineKeyboardButton("➖ Remover Estoque",    callback_data="admin_rem")],
        [InlineKeyboardButton("📸 Fotos dos Produtos", callback_data="admin_foto_produtos")],
        [InlineKeyboardButton("← Voltar ao Menu",      callback_data="admin_menu_principal")],
    ])

def admin_financeiro_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Retirada RD",        callback_data="admin_rd_input"),
         InlineKeyboardButton("👤 Retirada Bart",      callback_data="admin_bart_input")],
        [InlineKeyboardButton("💸 Saída de Caixa",     callback_data="admin_saida_input"),
         InlineKeyboardButton("🏦 Saldo Banco",        callback_data="admin_banco_input")],
        [InlineKeyboardButton("🏭 Dívida Fornecedor",  callback_data="admin_fornecedor_input")],
        [InlineKeyboardButton("📅 Relatório por Data",    callback_data="admin_relatorio_data")],
        [InlineKeyboardButton("📅 Relatório Semanal",   callback_data="relatorio_semanal"),
         InlineKeyboardButton("📅 Relatório Mensal",    callback_data="relatorio_mensal")],
        [InlineKeyboardButton("🗑️ Reset Dia",          callback_data="resetdia_btn"),
         InlineKeyboardButton("🗑️ Reset Completo",     callback_data="admin_reset_completo")],
        [InlineKeyboardButton("← Voltar ao Menu",      callback_data="admin_menu_principal")],
    ])

def customer_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒  pedir agora",     callback_data="loja_iniciar")],
        [InlineKeyboardButton("📦  ver cardápio",    callback_data="loja_produtos")],
        [InlineKeyboardButton("📋  meu pedido",      callback_data="loja_status")],
    ])

def socio_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚚 Minhas Entregas Pendentes", callback_data="socio_entregas_pendentes")],
        [InlineKeyboardButton("📋 Todos os Pedidos do Dia",   callback_data="socio_pedidos")],
        [InlineKeyboardButton("📦 Estoque",                   callback_data="estoque"),
         InlineKeyboardButton("💰 Caixa do Dia",             callback_data="caixa")],
        [InlineKeyboardButton("📊 Relatório Hoje",            callback_data="relatorio")],
        [InlineKeyboardButton("🛍️  Cardápio",                 callback_data="loja_produtos")],
    ])

def build_cart_text(carrinho: dict, prefixo: str = "") -> str:
    subtotal = sum(carrinho.get(cod, 0) * PRODUTOS_INFO[cod][1] for cod in PRODUTOS_INFO)
    taxa     = 10.0 if 0 < subtotal < 500 else 0.0
    total    = subtotal + taxa

    linhas = prefixo + "🛒  <b>CARRINHO</b>\n━━━━━━━━━━━━━━━━━━\n\n"
    tem_item = False
    for cod, (nome, preco, unidade) in PRODUTOS_INFO.items():
        qtd = carrinho.get(cod, 0)
        if qtd > 0:
            tem_item = True
            linhas += f"  {nome}  ×{qtd}   <b>R$ {qtd * preco:.0f}</b>\n"
    if not tem_item:
        linhas += "  <i>nenhum item ainda — use ➕</i>\n"
    linhas += "\n━━━━━━━━━━━━━━━━━━\n"
    if taxa > 0:
        linhas += f"  entrega: R$ {taxa:.0f}\n"
    linhas += f"💸  <b>Total: R$ {total:.0f}</b>"
    return linhas

def build_cart_keyboard(carrinho: dict) -> InlineKeyboardMarkup:
    rows = []
    for cod, (nome, preco, unidade) in PRODUTOS_INFO.items():
        qtd    = carrinho.get(cod, 0)
        # linha 1: nome completo em largura total
        nome_label = f"{'🔥 ' if qtd > 0 else ''}{nome}  — R$ {preco:.0f}/{unidade}"
        rows.append([InlineKeyboardButton(nome_label, callback_data="noop")])
        # linha 2: controles
        qtd_label = f"🔥 {qtd}" if qtd > 0 else "0"
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"loja_rem_{cod}"),
            InlineKeyboardButton(qtd_label, callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"loja_add_{cod}"),
        ])
    rows.append([InlineKeyboardButton("⚡ fechar pedido", callback_data="loja_confirmar")])
    rows.append([InlineKeyboardButton("✖ cancelar",       callback_data="loja_cancelar")])
    return InlineKeyboardMarkup(rows)

# ====================== COMANDOS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    if is_admin(user.id):
        await update.message.reply_text(
            build_admin_header(),
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    elif is_entregador(user):
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND status='OK' AND responsavel='Loja-Bot'", (f"{hoje}%",))
        pend = c.fetchone()[0]
        c.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM pedidos WHERE data LIKE ? AND status IN ('OK','ENTREGUE')", (f"{hoje}%",))
        qtd, total = c.fetchone()
        conn.close()
        hora = datetime.datetime.now().strftime("%H:%M")
        await update.message.reply_text(
            f"🛵  <b>PAINEL DO SÓCIO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {hora}  |  📋 {qtd} pedido(s)  |  💰 R$ {total:.0f}\n"
            f"🚚 Pendentes de entrega: <b>{pend}</b>",
            parse_mode="HTML",
            reply_markup=socio_keyboard()
        )
    else:
        registrar_cliente(user.id, user.username or "", user.first_name or "")
        await update.message.reply_text(
            "🖤  <b>STORE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "entrega a partir das 19:30",
            parse_mode="HTML",
            reply_markup=customer_keyboard()
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
    if context.args:
        raw = context.args[0].strip()
        try:
            ano = datetime.datetime.now().year
            if len(raw) <= 5:          # DD/MM
                data = datetime.datetime.strptime(f"{raw}/{ano}", "%d/%m/%Y")
            else:                       # DD/MM/AAAA
                data = datetime.datetime.strptime(raw, "%d/%m/%Y")
            data_str = data.strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("⚠️ Data inválida. Use: /relatorio 05/05")
            return
    else:
        data_str = datetime.datetime.now().strftime("%Y-%m-%d")
    await update.message.reply_text(build_relatorio_text(data_str), parse_mode="HTML")

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

async def cmd_remover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Apenas o administrador pode remover estoque.")
        return
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("⚠️ Uso: /remover ICE 10")
            return
        cod = ALIAS.get(args[0].upper(), args[0].upper())
        qtd = float(args[1].replace(",", "."))
        if cod not in CODIGOS:
            await update.message.reply_text(f"❌ Código inválido. Use: {', '.join(CODIGOS)}")
            return
        if qtd <= 0:
            await update.message.reply_text("❌ A quantidade deve ser maior que zero.")
            return
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT estoque, nome FROM produtos WHERE codigo = ?", (cod,))
        row = c.fetchone()
        atual, nome = row
        if qtd > atual:
            conn.close()
            await update.message.reply_text(
                f"❌ Estoque insuficiente.\n{nome} tem apenas {atual:.1f} em estoque.")
            return
        c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, cod))
        c.execute("SELECT estoque FROM produtos WHERE codigo = ?", (cod,))
        novo = c.fetchone()[0]
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ {nome} −{qtd:.1f}\n📦 Agora: {novo:.1f}", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ Uso: /remover ICE 10")

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
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*(ICE|PAK|CRUMBLE|POD_I|POD_S|PODI|PODS|POD)', line)
        if match:
            qtd  = float(match.group(1).replace(",", "."))
            prod = match.group(2)
            prod = ALIAS.get(prod, prod)   # normaliza PODI/PODS/POD → POD_I/POD_S
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

# ====================== COMPROVANTE PIX (CLIENTE) ======================

async def handle_comprovante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estado = context.user_data.get("estado", "")

    # ---- Admin: salvar foto de produto ----
    if isinstance(estado, str) and estado.startswith("admin_foto_"):
        if not is_admin(update.effective_user.id):
            return
        cod     = estado[len("admin_foto_"):]
        file_id = update.message.photo[-1].file_id
        save_foto(cod, file_id)
        nome = PRODUTOS_INFO.get(cod, (cod,))[0]
        context.user_data["estado"] = None
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Enviar outra foto", callback_data="admin_foto_produtos")],
            [InlineKeyboardButton("← Gestão de Estoque",  callback_data="admin_menu_estoque")],
        ])
        await update.message.reply_text(
            f"✅ Foto de <b>{nome}</b> salva com sucesso!",
            parse_mode="HTML", reply_markup=kb)
        return

    # ---- Cliente: comprovante de pagamento ----
    if estado != "cliente_comprovante":
        return
    pedido = context.user_data.get("pedido_pendente")
    if not pedido:
        return
    # Verificar expiração (20 minutos)
    tempo_str = context.user_data.get("tempo_comprovante")
    if tempo_str:
        tempo = datetime.datetime.fromisoformat(tempo_str)
        if (datetime.datetime.now() - tempo).total_seconds() > 1200:
            context.user_data.clear()
            await update.message.reply_text(
                "⏱️ <b>Pedido expirado!</b>\n\n"
                "Você demorou mais de 20 minutos para enviar o comprovante.\n"
                "Use /start para fazer um novo pedido.",
                parse_mode="HTML"
            )
            return

    customer_id = update.effective_chat.id
    pedidos_pendentes[customer_id] = pedido

    user = update.effective_user
    contato = f"@{user.username}" if user.username else f"<a href='tg://user?id={customer_id}'>{user.first_name}</a>"

    itens_str = "\n".join(
        f"   • {q} {PRODUTOS_INFO[p][2]} × {PRODUTOS_INFO[p][0]}"
        for p, q in pedido["itens"].items() if q > 0
    )
    taxa_str = f"\n📌 Taxa entrega: R$ {pedido['taxa']:.2f}" if pedido["taxa"] > 0 else ""

    msg = (
        f"🔔 <b>NOVO PEDIDO — AGUARDANDO CONFIRMAÇÃO</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Cliente: {pedido['nome_cliente']} ({contato})\n\n"
        f"{itens_str}\n"
        f"💰 Total: R$ {pedido['total']:.2f}{taxa_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Comprovante PIX acima 👆"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar Pagamento", callback_data=f"pgto_ok_{customer_id}")],
        [InlineKeyboardButton("❌ Recusar Pagamento",   callback_data=f"pgto_rec_{customer_id}")],
    ])

    photo = update.message.photo[-1].file_id
    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo,
        caption=msg,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    # Notificar entregador que um pedido entrou (aguardando confirmação)
    try:
        await context.bot.send_message(
            chat_id=ENTREGADOR_USERNAME,
            text=(
                f"📥 <b>PEDIDO RECEBIDO</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"👤 Cliente: {pedido['nome_cliente']}\n"
                f"📱 Contato: {contato}\n\n"
                f"{itens_str}\n\n"
                f"💰 Total: R$ {pedido['total']:.2f}\n\n"
                f"⏳ Aguardando confirmação do pagamento..."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Não foi possível notificar entregador (entrada): {e}")

    await update.message.reply_text(
        "✅ <b>Comprovante recebido!</b>\n\n"
        "Aguarde a confirmação do pagamento.\n"
        "Você será avisado assim que confirmado. 🙏",
        parse_mode="HTML"
    )
    context.user_data["estado"] = "aguardando_confirmacao"

# ====================== CALLBACK BUTTONS ======================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "novo_pedido":
        context.user_data.clear()
        context.user_data["estado"] = "esperando_cliente"
        await query.edit_message_text("👤 Digite o nome do cliente:")

    elif query.data == "estoque":
        voltar = "admin_menu_principal" if is_admin(query.from_user.id) else "socio_menu_principal"
        hoje_str = datetime.date.today().strftime("%Y-%m-%d")
        await query.edit_message_text(
            build_estoque_historico_text(hoje_str),
            parse_mode="HTML",
            reply_markup=estoque_nav_keyboard(hoje_str, voltar)
        )

    elif query.data.startswith("estoque_dia_"):
        data_str = query.data[len("estoque_dia_"):]
        voltar   = "admin_menu_principal" if is_admin(query.from_user.id) else "socio_menu_principal"
        try:
            datetime.datetime.strptime(data_str, "%Y-%m-%d")
        except ValueError:
            await query.answer("Data inválida.", show_alert=True); return
        await query.edit_message_text(
            build_estoque_historico_text(data_str),
            parse_mode="HTML",
            reply_markup=estoque_nav_keyboard(data_str, voltar)
        )

    elif query.data == "pedidos_dia":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]])
        await query.edit_message_text(
            build_pedidos_dia_text(hoje),
            parse_mode="HTML",
            reply_markup=kb_vol
        )

    elif query.data == "caixa":
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT tipo, SUM(valor) FROM caixa WHERE data LIKE ? GROUP BY tipo", (f"{hoje}%",))
        rows = c.fetchall()
        c.execute("SELECT SUM(CASE WHEN tipo='entrada' THEN valor ELSE -valor END) FROM caixa WHERE data LIKE ?",
                  (f"{hoje}%",))
        saldo_dia = c.fetchone()[0] or 0
        conn.close()
        saldo_banco = get_config("saldo_banco")
        msg = "💰 <b>CAIXA DO DIA</b>\n━━━━━━━━━━━━━━\n\n"
        for tipo, total in rows:
            emoji = "📥" if tipo == "entrada" else "📤"
            msg += f"{emoji} {'Entradas' if tipo == 'entrada' else 'Saídas'}: R$ {total:.2f}\n"
        msg += f"\n💵 <b>Saldo do Dia: R$ {saldo_dia:.2f}</b>\n"
        msg += f"🏦 <b>Saldo Banco: R$ {saldo_banco:.2f}</b>"
        voltar = "admin_menu_principal" if is_admin(query.from_user.id) else "socio_menu_principal"
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data=voltar)]])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data == "relatorio":
        if not is_admin(query.from_user.id) and not is_entregador(query.from_user):
            await query.edit_message_text("❌ Acesso negado.")
            return
        hoje = datetime.datetime.now().strftime("%Y-%m-%d")
        voltar = "admin_menu_principal" if is_admin(query.from_user.id) else "socio_menu_principal"
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data=voltar)]])
        await query.edit_message_text(build_relatorio_text(hoje), parse_mode="HTML", reply_markup=kb_vol)

    # ====================== MENU ADMIN ======================

    elif query.data == "socio_menu_principal":
        if not is_entregador(query.from_user):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND status='OK' AND responsavel='Loja-Bot'", (f"{hoje}%",))
        pend = c.fetchone()[0]
        c.execute("SELECT COUNT(*), COALESCE(SUM(total),0) FROM pedidos WHERE data LIKE ? AND status IN ('OK','ENTREGUE')", (f"{hoje}%",))
        qtd, total = c.fetchone()
        c.execute("SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND status='ENTREGUE'", (f"{hoje}%",))
        entregues = c.fetchone()[0]
        conn.close()
        hora = datetime.datetime.now().strftime("%H:%M")
        await query.edit_message_text(
            f"🛵  <b>PAINEL DO SÓCIO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {hora}  |  📋 {qtd} pedido(s)  |  💰 R$ {total:.0f}\n"
            f"✅ Entregues: <b>{entregues}</b>  |  🚚 Pendentes: <b>{pend}</b>",
            parse_mode="HTML", reply_markup=socio_keyboard())

    elif query.data == "socio_pedidos":
        if not is_entregador(query.from_user):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT id, numero, cliente, total, pagamento, data, endereco, status
                     FROM pedidos WHERE status IN ('OK','ENTREGUE') AND data LIKE ?
                     ORDER BY id ASC""", (f"{hoje}%",))
        pedidos = c.fetchall()
        if not pedidos:
            msg = "📋 <b>PEDIDOS DO DIA</b>\n━━━━━━━━━━━━━━\n\nNenhum pedido hoje ainda."
        else:
            msg = f"📋 <b>PEDIDOS DO DIA</b> ({len(pedidos)})\n━━━━━━━━━━━━━━\n\n"
            for ped_id, num, cli, tot, pag, data, endereco, status in pedidos:
                hora = data[11:16] if len(data) > 10 else ""
                c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (ped_id,))
                itens_rows = c.fetchall()
                itens_str = "  ".join(
                    f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
                    for p, q in itens_rows
                )
                st_emoji = "✅" if status == "ENTREGUE" else "🚚"
                pag_emoji = "📲" if pag == "PIX" else "💵"
                end_str = f"\n  📍 {endereco}" if endereco and endereco not in ("","Retirada") else ("  🏪 Retirada" if endereco == "Retirada" else "")
                msg += f"{st_emoji} <b>#{num}</b>  🕐 {hora}  {pag_emoji} R$ {tot:.0f}\n  👤 {cli}{end_str}\n  {itens_str}\n\n"
        conn.close()
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="socio_menu_principal")]])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data == "admin_menu_principal":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        await query.edit_message_text(
            build_admin_header(), parse_mode="HTML", reply_markup=main_keyboard())

    elif query.data == "admin_pendentes":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]])
        if not pedidos_pendentes:
            await query.edit_message_text(
                "⏳ <b>PIX PENDENTES</b>\n━━━━━━━━━━━━━━\n\n✅ Nenhum comprovante aguardando confirmação.",
                parse_mode="HTML", reply_markup=kb_vol)
            return
        msg = f"⏳ <b>PIX PENDENTES ({len(pedidos_pendentes)})</b>\n━━━━━━━━━━━━━━\n\n"
        for cid, ped in pedidos_pendentes.items():
            itens_str = "  ".join(
                f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
                for p, q in ped["itens"].items() if q > 0
            )
            contato = ped.get("customer_contact", f"ID:{cid}")
            end_str = f"\n  📍 {ped['endereco']}" if ped.get("endereco") and ped["endereco"] not in ("","Retirada") else ("  🏪 Retirada" if ped.get("endereco") == "Retirada" else "")
            msg += (
                f"👤 <b>{ped['nome_cliente']}</b> ({contato}){end_str}\n"
                f"  {itens_str}\n"
                f"  💰 R$ {ped['total']:.0f}\n\n"
            )
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data == "admin_entregas_pendentes":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT id, numero, cliente, total, pagamento, data, endereco
                     FROM pedidos WHERE data LIKE ? AND status='OK' AND responsavel='Loja-Bot'
                     ORDER BY id ASC""", (f"{hoje}%",))
        pendentes = c.fetchall()
        linhas = []
        for ped_id, numero, cliente, total, pag, data_ped, endereco in pendentes:
            hora = data_ped[11:16] if len(data_ped) > 10 else "?"
            c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (ped_id,))
            itens_rows = c.fetchall()
            itens_str = "  ".join(
                f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
                for p, q in itens_rows
            )
            end_str = f"\n  📍 {endereco}" if endereco and endereco not in ("","Retirada") else ("  🏪 Retirada" if endereco == "Retirada" else "")
            pag_emoji = "📲" if pag == "PIX" else "💵"
            linhas.append(f"🚚 <b>#{numero}</b>  🕐 {hora}  {pag_emoji} R$ {total:.0f}\n  👤 {cliente}{end_str}\n  {itens_str}")
        conn.close()
        if not pendentes:
            msg = "🚚 <b>ENTREGAS PENDENTES</b>\n━━━━━━━━━━━━━━\n\n✅ Nenhuma entrega pendente agora."
        else:
            msg = f"🚚 <b>ENTREGAS PENDENTES ({len(pendentes)})</b>\n━━━━━━━━━━━━━━\n\n" + "\n\n".join(linhas)
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data == "admin_agendados":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        amanha = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        amanha_fmt = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%d/%m")
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT id, numero, cliente, total, pagamento, endereco
                     FROM pedidos WHERE data_entrega=? AND status='OK'
                     ORDER BY id ASC""", (amanha,))
        agendados = c.fetchall()
        linhas = []
        for ped_id, numero, cliente, total, pag, endereco in agendados:
            c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (ped_id,))
            itens_rows = c.fetchall()
            itens_str = "  ".join(
                f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
                for p, q in itens_rows
            )
            end_str = f"\n  📍 {endereco}" if endereco and endereco not in ("","Retirada") else ("  🏪 Retirada" if endereco == "Retirada" else "")
            pag_emoji = "📲" if pag == "PIX" else "💵"
            linhas.append(f"📦 <b>#{numero}</b>  {pag_emoji} R$ {total:.0f}\n  👤 {cliente}{end_str}\n  {itens_str}")
        conn.close()
        if not agendados:
            msg = f"📅 <b>AGENDADOS PARA {amanha_fmt}</b>\n━━━━━━━━━━━━━━\n\nNenhum pedido agendado para amanhã."
        else:
            msg = f"📅 <b>AGENDADOS PARA {amanha_fmt} ({len(agendados)})</b>\n━━━━━━━━━━━━━━\n\n" + "\n\n".join(linhas)
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data == "admin_menu_estoque":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        await query.edit_message_text(
            "🏪 <b>GESTÃO DE ESTOQUE</b>\n━━━━━━━━━━━━━━━━━━\n"
            + build_estoque_text(),
            parse_mode="HTML", reply_markup=admin_estoque_keyboard())

    elif query.data == "admin_menu_financeiro":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        saldo   = get_config("saldo_banco")
        divida  = get_config("divida_fornecedor")
        await query.edit_message_text(
            f"💸 <b>FINANCEIRO</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Banco: <b>R$ {saldo:.2f}</b>\n"
            f"🏭 Fornecedor: <b>R$ {divida:.2f}</b>",
            parse_mode="HTML", reply_markup=admin_financeiro_keyboard())

    elif query.data == "admin_cancelar":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT id, numero, cliente, total, pagamento, data FROM pedidos WHERE status='OK' ORDER BY id DESC LIMIT 1")
        row = c.fetchone(); conn.close()
        if not row:
            await query.edit_message_text("ℹ️ Nenhum pedido para cancelar.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]]))
            return
        pedido_id, numero, cliente, total, pagamento, data = row
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sim, cancelar", callback_data=f"cancelar_sim_{pedido_id}")],
            [InlineKeyboardButton("❌ Não, manter",   callback_data="admin_menu_principal")],
        ])
        await query.edit_message_text(
            f"⚠️ <b>Cancelar último pedido?</b>\n\n"
            f"🔢 #{numero}  👤 {cliente}\n💰 R$ {total:.2f}  🕐 {data}",
            parse_mode="HTML", reply_markup=kb)

    elif query.data == "admin_foto_produtos":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        fotos = load_fotos()
        rows  = []
        for cod, (nome, _, _) in PRODUTOS_INFO.items():
            tem = "✅ " if cod in fotos else "📸 "
            rows.append([InlineKeyboardButton(f"{tem}{nome}", callback_data=f"admin_foto_pick_{cod}")])
        rows.append([InlineKeyboardButton("← Voltar", callback_data="admin_menu_estoque")])
        await query.edit_message_text(
            "📸 <b>Fotos dos Produtos</b>\n━━━━━━━━━━━━━━━━━━\n"
            "✅ = foto cadastrada   📸 = sem foto\n\n"
            "Toque no produto para enviar/trocar a foto:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows))

    elif query.data.startswith("admin_foto_pick_"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        cod  = query.data[len("admin_foto_pick_"):]
        nome = PRODUTOS_INFO.get(cod, (cod,))[0]
        context.user_data["estado"] = f"admin_foto_{cod}"
        await query.edit_message_text(
            f"📸 <b>{nome}</b>\n\nEnvie a foto deste produto agora:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✖ Cancelar", callback_data="admin_foto_produtos")
            ]]))

    # --- Inputs guiados ---
    elif query.data in ("admin_add", "admin_rem", "admin_rd_input", "admin_bart_input",
                        "admin_saida_input", "admin_banco_input", "admin_fornecedor_input",
                        "admin_relatorio_data"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        prompts = {
            "admin_add":             ("admin_add",        "➕ <b>Adicionar Estoque</b>\n\nDigite o produto e quantidade:\n<code>ICE 50</code>  ou  <code>PAK 20</code>"),
            "admin_rem":             ("admin_rem",        "➖ <b>Remover Estoque</b>\n\nDigite o produto e quantidade:\n<code>ICE 10</code>  ou  <code>PAK 5</code>"),
            "admin_rd_input":        ("admin_rd",         "👤 <b>Retirada RD</b>\n\nDigite o valor:\n<code>500</code>"),
            "admin_bart_input":      ("admin_bart",       "👤 <b>Retirada Bart</b>\n\nDigite o valor:\n<code>500</code>"),
            "admin_saida_input":     ("admin_saida",      "💸 <b>Saída de Caixa</b>\n\nDigite o valor e descrição:\n<code>50 Gasolina</code>"),
            "admin_banco_input":     ("admin_banco",      "🏦 <b>Saldo Banco</b>\n\nDigite o novo saldo:\n<code>3400</code>"),
            "admin_fornecedor_input":("admin_fornecedor", "🏭 <b>Dívida Fornecedor</b>\n\nDigite o valor da dívida:\n<code>74890</code>"),
            "admin_relatorio_data":  ("admin_rel_data",   "📅 <b>Relatório por Data</b>\n\nDigite a data:\n<code>05/05</code>  ou  <code>05/05/2025</code>"),
        }
        estado, prompt = prompts[query.data]
        context.user_data["estado"] = estado
        kb_cancel = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancelar", callback_data="admin_menu_principal")]])
        await query.edit_message_text(prompt, parse_mode="HTML", reply_markup=kb_cancel)

    elif query.data == "resetdia_btn":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%d/%m/%Y")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Sim, limpar hoje", callback_data="resetdia_sim")],
            [InlineKeyboardButton("← Voltar",            callback_data="admin_menu_financeiro")],
        ])
        await query.edit_message_text(
            f"⚠️ <b>Limpar todos os pedidos de {hoje}?</b>\n\nO estoque não será alterado.",
            parse_mode="HTML", reply_markup=kb)

    elif query.data == "admin_reset_completo":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ SIM, APAGAR TUDO", callback_data="admin_reset_completo_sim")],
            [InlineKeyboardButton("← Voltar",             callback_data="admin_menu_financeiro")],
        ])
        await query.edit_message_text(
            "🚨 <b>RESET COMPLETO</b>\n\nIsso apaga TODO o histórico de pedidos e caixa.\nEstoque não é alterado.\n\n<b>Tem certeza absoluta?</b>",
            parse_mode="HTML", reply_markup=kb)

    elif query.data == "admin_reset_completo_sim":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        conn = get_db(); c = conn.cursor()
        c.execute("DELETE FROM itens_pedido")
        c.execute("DELETE FROM pedidos")
        c.execute("DELETE FROM caixa")
        conn.commit(); conn.close()
        await query.edit_message_text(
            "🗑️ Reset completo realizado. Todo o histórico apagado.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]]))

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

    # ====================== LOJA (CLIENTE) ======================

    elif query.data == "noop":
        pass  # quantity display buttons — do nothing

    elif query.data == "loja_iniciar":
        amanha     = datetime.date.today() + datetime.timedelta(days=1)
        amanha_str = amanha.strftime("%d/%m")
        kb_agendar = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📅 Agendar para amanhã ({amanha_str})", callback_data="loja_agendar")],
            [InlineKeyboardButton("← Voltar", callback_data="loja_menu")],
        ])
        if not loja_esta_aberta():
            await query.edit_message_text(
                "🔴 <b>Estamos fechados agora.</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "🕐 Horário: <b>08:00 às 19:00</b>\n\n"
                "Mas você pode deixar um pedido agendado\n"
                "para entrega amanhã!",
                parse_mode="HTML",
                reply_markup=kb_agendar
            )
            return
        # Limite de 10 pedidos por dia (dias de semana)
        hoje = datetime.date.today()
        if hoje.weekday() < 5:  # 0=seg … 4=sex
            conn_lim = get_db(); c_lim = conn_lim.cursor()
            c_lim.execute(
                "SELECT COUNT(*) FROM pedidos WHERE data LIKE ? AND responsavel='Loja-Bot' AND status!='CANCELADO'",
                (hoje.strftime("%Y-%m-%d") + "%",)
            )
            qtd_hoje = c_lim.fetchone()[0]; conn_lim.close()
            if qtd_hoje >= 10:
                await query.edit_message_text(
                    "🚫 <b>Limite de pedidos atingido!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "Aceitamos no máximo <b>10 pedidos por dia</b>.\n\n"
                    "Mas você pode deixar um pedido agendado\n"
                    "para entrega amanhã!",
                    parse_mode="HTML",
                    reply_markup=kb_agendar
                )
                return
        context.user_data.clear()
        context.user_data["carrinho"] = {c: 0 for c in PRODUTOS_INFO}
        context.user_data["estado"]   = "cliente_carrinho"
        carrinho = context.user_data["carrinho"]
        await query.edit_message_text(
            build_cart_text(carrinho),
            parse_mode="HTML",
            reply_markup=build_cart_keyboard(carrinho)
        )

    elif query.data == "loja_agendar":
        amanha     = datetime.date.today() + datetime.timedelta(days=1)
        amanha_str = amanha.strftime("%d/%m")
        context.user_data.clear()
        context.user_data["carrinho"]     = {c: 0 for c in PRODUTOS_INFO}
        context.user_data["estado"]       = "cliente_carrinho"
        context.user_data["agendado"]     = True
        context.user_data["data_entrega"] = amanha.strftime("%Y-%m-%d")
        carrinho = context.user_data["carrinho"]
        prefixo  = f"📅 <b>AGENDADO — entrega {amanha_str}</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        await query.edit_message_text(
            build_cart_text(carrinho, prefixo),
            parse_mode="HTML",
            reply_markup=build_cart_keyboard(carrinho)
        )

    elif query.data == "loja_produtos":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT codigo, nome, preco_venda, estoque FROM produtos ORDER BY codigo")
        rows = c.fetchall()
        conn.close()
        fotos   = load_fotos()
        media   = []
        sem_foto = ""
        for cod, nome, preco, estoque in rows:
            if cod not in PRODUTOS_INFO:
                continue
            unidade = PRODUTOS_INFO[cod][2]
            status  = "🟢" if estoque > 0 else "🔴 esgotado"
            if cod in fotos:
                caption = f"{status}  <b>{nome}</b>\n💰 R$ {preco:.0f}/{unidade}"
                media.append(InputMediaPhoto(fotos[cod], caption=caption, parse_mode="HTML"))
            else:
                sem_foto += f"{status}  {nome}\n    R$ {preco:.0f}/{unidade}\n\n"
        entrega = is_entregador(query.from_user)
        if entrega:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Atualizar",  callback_data="loja_produtos")],
                [InlineKeyboardButton("← Menu",        callback_data="socio_menu_principal")],
            ])
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒  pedir agora", callback_data="loja_iniciar")],
                [InlineKeyboardButton("← voltar",        callback_data="loja_menu")],
            ])
        if media:
            await context.bot.send_media_group(chat_id=query.message.chat_id, media=media)
        header = "📦  <b>CARDÁPIO</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        await query.edit_message_text(
            header + sem_foto if sem_foto else header.rstrip(),
            parse_mode="HTML", reply_markup=kb)

    elif query.data.startswith("loja_add_") or query.data.startswith("loja_rem_"):
        parts    = query.data.split("_")
        action   = parts[1]
        cod      = "_".join(parts[2:])   # reconstrói POD_I, POD_S corretamente
        carrinho = context.user_data.get("carrinho", {c: 0 for c in PRODUTOS_INFO})
        if action == "add":
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("SELECT estoque FROM produtos WHERE codigo=?", (cod,))
            estoque = cur.fetchone()[0]
            conn.close()
            if carrinho.get(cod, 0) >= int(estoque):
                await query.answer("sem estoque", show_alert=True)
                return
            carrinho[cod] = carrinho.get(cod, 0) + 1
        else:
            carrinho[cod] = max(0, carrinho.get(cod, 0) - 1)
        context.user_data["carrinho"] = carrinho
        try:
            _ag = context.user_data.get("agendado")
            _de = context.user_data.get("data_entrega", "")
            _pref = ""
            if _ag and _de:
                try:
                    _pref = f"📅 <b>AGENDADO — entrega {datetime.datetime.strptime(_de,'%Y-%m-%d').strftime('%d/%m')}</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                except Exception:
                    pass
            await query.edit_message_text(
                build_cart_text(carrinho, _pref),
                parse_mode="HTML",
                reply_markup=build_cart_keyboard(carrinho)
            )
        except Exception:
            pass

    elif query.data == "loja_confirmar":
        carrinho = context.user_data.get("carrinho", {})
        itens    = {k: v for k, v in carrinho.items() if v > 0}
        if not itens:
            await query.answer("adiciona pelo menos um item", show_alert=True)
            return
        subtotal = sum(itens[cod] * PRODUTOS_INFO[cod][1] for cod in itens)
        taxa     = 10.0 if subtotal < 500 else 0.0
        total    = subtotal + taxa
        user     = query.from_user
        context.user_data["pedido_pendente"] = {
            "itens":            itens,
            "subtotal":         subtotal,
            "taxa":             taxa,
            "total":            total,
            "nome_cliente":     user.first_name or "Cliente",
            "customer_contact": f"@{user.username}" if user.username else f"ID:{user.id}",
            "customer_chat_id": user.id,
            "endereco":         "",
            "agendado":         context.user_data.get("agendado", False),
            "data_entrega":     context.user_data.get("data_entrega", ""),
        }
        itens_str = "\n".join(
            f"  {PRODUTOS_INFO[p][0]}  ×{q}   R$ {q * PRODUTOS_INFO[p][1]:.0f}"
            for p, q in itens.items()
        )
        taxa_str = f"\n  entrega: R$ {taxa:.0f}" if taxa > 0 else ""
        context.user_data["itens_str_cache"] = itens_str
        context.user_data["taxa_str_cache"]  = taxa_str
        context.user_data["estado"] = "cliente_nome"
        agendado_banner = ""
        if context.user_data.get("agendado"):
            dt_raw = context.user_data.get("data_entrega", "")
            try:
                dt_fmt = datetime.datetime.strptime(dt_raw, "%Y-%m-%d").strftime("%d/%m")
            except Exception:
                dt_fmt = "amanhã"
            agendado_banner = f"📅 <b>AGENDADO — entrega {dt_fmt}</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        msg = (
            f"{agendado_banner}"
            f"🖤  <b>PEDIDO FECHADO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{itens_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{taxa_str}\n"
            f"💸  <b>Total: R$ {total:.0f}</b>\n\n"
            f"👤 <b>Qual o seu nome completo?</b>\n"
            f"<i>(será usado para identificar seu pedido)</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("← voltar ao carrinho", callback_data="loja_voltar_carrinho")],
        ])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)

    elif query.data == "loja_pagar_pix":
        pedido    = context.user_data.get("pedido_pendente")
        if not pedido:
            await query.answer("sessão expirada, use /start", show_alert=True); return
        itens_str = context.user_data.get("itens_str_cache", "")
        taxa_str  = context.user_data.get("taxa_str_cache", "")
        context.user_data["estado"] = "cliente_comprovante"
        context.user_data["tempo_comprovante"] = datetime.datetime.now().isoformat()
        endereco  = pedido.get("endereco", "")
        end_str   = f"\n📍 <b>Entrega em:</b> {endereco}" if endereco and endereco != "Retirada" else ("\n🏪 <b>Retirada</b>" if endereco == "Retirada" else "")
        msg = (
            f"🖤  <b>PEDIDO FECHADO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{itens_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{taxa_str}\n"
            f"💸  <b>Total: R$ {pedido['total']:.0f}</b>{end_str}\n\n"
            f"⚡  <b>Pague via PIX e mande o comprovante aqui como foto 👇</b>"
        )
        kb_pix = InlineKeyboardMarkup([
            [InlineKeyboardButton("← voltar ao carrinho", callback_data="loja_voltar_carrinho")],
            [InlineKeyboardButton("✖ cancelar pedido",    callback_data="loja_cancelar")],
        ])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_pix)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"<code>{CHAVE_PIX}</code>",
            parse_mode="HTML"
        )

    elif query.data == "loja_pagar_dinheiro":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await query.answer("sessão expirada, use /start", show_alert=True); return
        context.user_data["estado"] = "cliente_troco"
        msg = (
            f"💵 <b>PAGAMENTO EM DINHEIRO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"💸 Total: <b>R$ {pedido['total']:.0f}</b>\n\n"
            f"Com quanto vai pagar?\n"
            f"<i>Digite o valor ou clique em «Valor exato»</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Pagar valor exato", callback_data="loja_troco_exato")],
            [InlineKeyboardButton("← Voltar",             callback_data="loja_confirmar")],
        ])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)

    elif query.data == "loja_confirmar_dinheiro":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await query.answer("sessão expirada, use /start", show_alert=True); return
        user      = query.from_user
        contato   = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
        itens_str = context.user_data.get("itens_str_cache", "")
        data_now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        numero    = datetime.datetime.now().strftime("%d%H%M")
        conn = get_db(); c = conn.cursor()
        c.execute(
            "INSERT INTO pedidos (numero, cliente, total, taxa, pagamento, responsavel, data, status) VALUES (?,?,?,?,?,?,?,?)",
            (numero, pedido["nome_cliente"], pedido["total"], pedido["taxa"], "DINHEIRO", "Loja-Bot", data_now, "OK")
        )
        pedido_id = c.lastrowid
        endereco  = pedido.get("endereco", "")
        cid       = pedido.get("customer_chat_id", user.id)
        troco     = context.user_data.get("troco", 0)
        for prod, qtd in pedido["itens"].items():
            if qtd > 0:
                c.execute("INSERT INTO itens_pedido (pedido_id, produto, quantidade) VALUES (?,?,?)", (pedido_id, prod, qtd))
                c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, prod))
        agendado     = pedido.get("agendado", False)
        data_entrega = pedido.get("data_entrega", "")
        try:
            dt_fmt = datetime.datetime.strptime(data_entrega, "%Y-%m-%d").strftime("%d/%m") if data_entrega else ""
        except Exception:
            dt_fmt = ""
        c.execute("INSERT INTO caixa (tipo, valor, descricao, data) VALUES ('entrada',?,?,?)",
                  (pedido["total"], f"Pedido #{numero} DINHEIRO", data_now))
        c.execute("UPDATE pedidos SET endereco=?, customer_chat_id=?, data_entrega=? WHERE id=?",
                  (endereco, cid, data_entrega, pedido_id))
        conn.commit(); conn.close()
        await check_low_stock(context.bot)
        context.user_data.clear()
        end_str   = f"\n📍 <b>Endereço:</b> {endereco}" if endereco and endereco != "Retirada" else ("\n🏪 <b>Retirada</b>" if endereco == "Retirada" else "")
        troco_str = f"\n💸 Troco: R$ {troco:.0f}" if troco > 0 else ""
        ag_str    = f"\n📅 <b>Entrega agendada:</b> {dt_fmt}" if agendado and dt_fmt else ""
        titulo_din = "📅 PEDIDO AGENDADO — DINHEIRO" if agendado else "💵 NOVO PEDIDO — DINHEIRO"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"{titulo_din}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 Cliente: {pedido['nome_cliente']} ({contato}){end_str}{ag_str}\n\n"
                f"{itens_str}\n"
                f"💰 Total: R$ {pedido['total']:.2f}{troco_str}  [paga na entrega]\n"
                f"━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                chat_id=ENTREGADOR_USERNAME,
                text=(
                    f"{'📅 ENTREGA AGENDADA!' if agendado else '📥 PEDIDO — DINHEIRO'}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 Cliente: {pedido['nome_cliente']}\n"
                    f"📱 Contato: {contato}{end_str}{ag_str}\n\n"
                    f"{itens_str}\n\n"
                    f"💰 Total: R$ {pedido['total']:.2f}{troco_str}  [paga na entrega 💵]"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Marcar como Entregue", callback_data=f"entregue_{cid}_{numero}")
                ]])
            )
        except Exception as e:
            logging.warning(f"Não foi possível notificar entregador (dinheiro): {e}")
        if agendado and dt_fmt:
            msg_confirm = (
                f"📅 <b>Pedido agendado para {dt_fmt}!</b>\n\n"
                f"💵 Pagamento em dinheiro na entrega.\n"
                f"O entregador entrará em contato no dia da entrega. 🛵"
            )
        else:
            msg_confirm = (
                "✅ <b>Pedido confirmado!</b>\n\n"
                "💵 Pagamento em dinheiro na entrega.\n"
                "Em breve o entregador entrará em contato. 🛵"
            )
        await query.edit_message_text(msg_confirm, parse_mode="HTML")

    elif query.data == "loja_retirar":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await query.answer("sessão expirada, use /start", show_alert=True); return
        pedido["endereco"] = "Retirada"
        context.user_data["estado"] = None
        msg = (
            f"🏪 <b>Retirada na loja</b>\n\n"
            f"💸 Total: <b>R$ {pedido['total']:.0f}</b>\n\n"
            f"Como vai pagar?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 PIX",      callback_data="loja_pagar_pix"),
             InlineKeyboardButton("💵 Dinheiro", callback_data="loja_pagar_dinheiro")],
            [InlineKeyboardButton("← Voltar", callback_data="loja_confirmar")],
        ])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)

    elif query.data == "loja_troco_exato":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await query.answer("sessão expirada, use /start", show_alert=True); return
        context.user_data["troco"] = 0
        context.user_data["estado"] = None
        itens_str = context.user_data.get("itens_str_cache", "")
        taxa_str  = context.user_data.get("taxa_str_cache", "")
        endereco  = pedido.get("endereco", "")
        end_str   = f"\n📍 {endereco}" if endereco and endereco != "Retirada" else ("\n🏪 Retirada" if endereco == "Retirada" else "")
        msg = (
            f"💵 <b>CONFIRMAR PEDIDO — DINHEIRO</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{itens_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{taxa_str}\n"
            f"💸 Total: <b>R$ {pedido['total']:.0f}</b>{end_str}\n\n"
            f"Confirma o pedido em dinheiro?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar pedido", callback_data="loja_confirmar_dinheiro")],
            [InlineKeyboardButton("← Voltar",           callback_data="loja_pagar_dinheiro")],
        ])
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)

    elif query.data == "loja_status":
        cid = query.from_user.id
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT numero, total, pagamento, status, data, endereco FROM pedidos WHERE customer_chat_id=? ORDER BY id DESC LIMIT 1", (cid,))
        row = c.fetchone(); conn.close()
        kb_vol = InlineKeyboardMarkup([[InlineKeyboardButton("← Voltar", callback_data="loja_menu")]])
        if not row:
            await query.edit_message_text("📋 Nenhum pedido encontrado.\n\nFaça seu primeiro pedido! 🛒", reply_markup=kb_vol)
            return
        numero, total, pag, status, data, endereco = row
        pag_emoji    = "📲" if pag == "PIX" else "💵"
        status_texto = "✅ Confirmado" if status == "OK" else ("❌ Cancelado" if status == "CANCELADO" else status)
        hora = data[11:16] if data and len(data) > 10 else ""
        end_str = f"\n📍 {endereco}" if endereco else ""
        await query.edit_message_text(
            f"📋 <b>Último Pedido</b>\n━━━━━━━━━━━━━━\n\n"
            f"🔢 Pedido #{numero}\n"
            f"📊 Status: {status_texto}\n"
            f"💰 R$ {total:.0f}  {pag_emoji} {pag}\n"
            f"🕐 {hora}{end_str}",
            parse_mode="HTML", reply_markup=kb_vol
        )

    elif query.data == "socio_entregas_pendentes":
        if not is_entregador(query.from_user):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT id, numero, cliente, total, pagamento, data, endereco, customer_chat_id
                     FROM pedidos WHERE data LIKE ? AND status='OK' AND responsavel='Loja-Bot'
                     ORDER BY id ASC""", (f"{hoje}%",))
        pendentes = c.fetchall()
        linhas = []
        kb_rows = []
        for ped_id, numero, cliente, total, pag, data_ped, endereco, cid in pendentes:
            hora = data_ped[11:16] if len(data_ped) > 10 else "?"
            c.execute("SELECT produto, quantidade FROM itens_pedido WHERE pedido_id=?", (ped_id,))
            itens_rows = c.fetchall()
            itens_str = "  ".join(
                f"{PRODUTOS_INFO[p][0]}×{int(q)}" if p in PRODUTOS_INFO else f"{p}×{int(q)}"
                for p, q in itens_rows
            )
            end_str = f"\n  📍 {endereco}" if endereco and endereco not in ("","Retirada") else ("  🏪 Retirada" if endereco == "Retirada" else "")
            pag_emoji = "📲" if pag == "PIX" else "💵"
            linhas.append(f"🚚 <b>#{numero}</b>  🕐 {hora}  {pag_emoji} R$ {total:.0f}\n  👤 {cliente}{end_str}\n  {itens_str}")
            kb_rows.append([InlineKeyboardButton(f"✅ Entregue #{numero}", callback_data=f"entregue_{cid}_{numero}")])
        conn.close()
        kb_rows.append([InlineKeyboardButton("← Menu", callback_data="socio_menu_principal")])
        kb_vol = InlineKeyboardMarkup(kb_rows)
        if not pendentes:
            msg = "🚚 <b>MINHAS ENTREGAS PENDENTES</b>\n━━━━━━━━━━━━━━\n\n✅ Tudo entregue! Nenhuma pendência."
        else:
            msg = f"🚚 <b>ENTREGAS PENDENTES ({len(pendentes)})</b>\n━━━━━━━━━━━━━━\n\n" + "\n\n".join(linhas)
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_vol)

    elif query.data.startswith("entregue_"):
        # entregue_{customer_chat_id}_{numero}
        partes     = query.data.split("_", 2)
        cid_str    = partes[1]
        num_pedido = partes[2] if len(partes) > 2 else "?"
        # Atualizar status no banco
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE pedidos SET status='ENTREGUE' WHERE numero=?", (num_pedido,))
        conn.commit(); conn.close()
        try:
            cid = int(cid_str)
            await context.bot.send_message(
                chat_id=cid,
                text=(
                    "✅ <b>Seu pedido foi entregue!</b>\n\n"
                    "Obrigado pela preferência! 🖤\n"
                    "/start para fazer outro pedido."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Não foi possível notificar cliente sobre entrega: {e}")
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"✅ <b>Entrega confirmada!</b>\nPedido #{num_pedido} marcado como entregue pelo entregador.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer(f"✅ Pedido #{num_pedido} marcado como entregue!", show_alert=True)

    elif query.data in ("relatorio_semanal", "relatorio_mensal"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        hoje  = datetime.date.today()
        if query.data == "relatorio_semanal":
            inicio = hoje - datetime.timedelta(days=7)
            titulo = "RELATÓRIO — ÚLTIMOS 7 DIAS"
        else:
            inicio = hoje.replace(day=1)
            titulo = f"RELATÓRIO — {hoje.strftime('%B/%Y').upper()}"
        rel = build_relatorio_periodo(str(inicio), str(hoje), titulo)
        kb  = InlineKeyboardMarkup([[InlineKeyboardButton("← Financeiro", callback_data="admin_menu_financeiro")]])
        await query.edit_message_text(rel, parse_mode="HTML", reply_markup=kb)

    elif query.data == "admin_broadcast":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        context.user_data["estado"] = "admin_broadcast"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Cancelar", callback_data="admin_menu_principal")]])
        await query.edit_message_text(
            "📢 <b>BROADCAST</b>\n━━━━━━━━━━━━━━\n\n"
            "Envie a mensagem que deseja mandar para <b>todos os clientes</b>.\n\n"
            "<i>Pode usar texto, emojis, etc. Apenas texto por enquanto.</i>",
            parse_mode="HTML", reply_markup=kb
        )

    elif query.data == "admin_toggle_loja":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True); return
        aberta = loja_esta_aberta()
        set_config("loja_aberta", 0 if aberta else 1)
        status = "🔴 Loja FECHADA" if aberta else "🟢 Loja ABERTA"
        await query.answer(f"{status}", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=main_keyboard())

    elif query.data == "loja_voltar_carrinho":
        carrinho = context.user_data.get("carrinho", {c: 0 for c in PRODUTOS_INFO})
        context.user_data["estado"] = "cliente_carrinho"
        context.user_data.pop("pedido_pendente", None)
        await query.edit_message_text(
            build_cart_text(carrinho),
            parse_mode="HTML",
            reply_markup=build_cart_keyboard(carrinho)
        )

    elif query.data == "loja_menu":
        context.user_data.clear()
        if is_entregador(query.from_user):
            await query.edit_message_text(
                "🛵  <b>PAINEL DO SÓCIO</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Consultas disponíveis:",
                parse_mode="HTML",
                reply_markup=socio_keyboard()
            )
        else:
            await query.edit_message_text(
                "🖤  <b>STORE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "entrega a partir das 19:30",
                parse_mode="HTML",
                reply_markup=customer_keyboard()
            )

    elif query.data == "loja_cancelar":
        context.user_data.clear()
        await query.edit_message_text("pedido cancelado.\n\n/start pra voltar.")

    # ====================== CONFIRMAÇÃO ADMIN ======================

    elif query.data.startswith("pgto_ok_"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True)
            return
        customer_id = int(query.data.split("_")[-1])
        pedido      = pedidos_pendentes.pop(customer_id, None)
        if not pedido:
            await query.edit_message_caption("⚠️ Pedido não encontrado ou já processado.")
            return
        # Salvar no banco
        conn     = get_db()
        c        = conn.cursor()
        data_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        numero   = datetime.datetime.now().strftime("%d%H%M")
        agendado     = pedido.get("agendado", False)
        data_entrega = pedido.get("data_entrega", "")
        try:
            dt_fmt = datetime.datetime.strptime(data_entrega, "%Y-%m-%d").strftime("%d/%m") if data_entrega else ""
        except Exception:
            dt_fmt = ""
        c.execute(
            "INSERT INTO pedidos (numero, cliente, total, taxa, pagamento, responsavel, data, status, endereco, customer_chat_id, data_entrega) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (numero, pedido["nome_cliente"], pedido["total"], pedido["taxa"], "PIX", "Loja-Bot", data_now, "OK",
             pedido.get("endereco", ""), pedido.get("customer_chat_id", customer_id), data_entrega)
        )
        pedido_id = c.lastrowid
        for prod, qtd in pedido["itens"].items():
            if qtd > 0:
                c.execute("INSERT INTO itens_pedido VALUES (?,?,?)", (pedido_id, prod, qtd))
                c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, prod))
        conn.commit()
        conn.close()
        registrar_caixa("entrada", pedido["total"], f"Pedido #{numero} - {pedido['nome_cliente']}")
        await check_low_stock(context.bot)
        # Montar strings comuns
        itens_entrega = "\n".join(
            f"• {q} {PRODUTOS_INFO[p][2]} {PRODUTOS_INFO[p][0]}"
            for p, q in pedido["itens"].items() if q > 0
        )
        endereco = pedido.get("endereco", "")
        end_str  = f"\n📍 <b>Endereço:</b> {endereco}" if endereco and endereco != "Retirada" else ("\n🏪 <b>Retirada</b>" if endereco == "Retirada" else "")
        cid      = pedido.get("customer_chat_id", customer_id)
        ag_str   = f"\n📅 <b>Entrega agendada:</b> {dt_fmt}" if agendado and dt_fmt else ""
        titulo_entregador = "📅 ENTREGA AGENDADA!" if agendado else "🚚 NOVA ENTREGA!"
        try:
            await context.bot.send_message(
                chat_id=ENTREGADOR_USERNAME,
                text=(
                    f"{titulo_entregador}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 <b>Cliente:</b> {pedido['nome_cliente']}\n"
                    f"📱 <b>Contato:</b> {pedido['customer_contact']}"
                    f"{end_str}{ag_str}\n\n"
                    f"{itens_entrega}\n\n"
                    f"💰 Total: R$ {pedido['total']:.2f} (PIX ✅ confirmado)"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Marcar como Entregue", callback_data=f"entregue_{cid}_{numero}")
                ]])
            )
        except Exception as e:
            logging.warning(f"Não foi possível notificar entregador: {e}")
        # Notificar cliente
        if agendado and dt_fmt:
            msg_cliente = (
                f"📅 <b>Pedido agendado para {dt_fmt}!</b>\n\n"
                f"✅ PIX confirmado. Seu pedido está reservado!\n\n"
                f"📱 <b>Entregador:</b> {ENTREGADOR_USERNAME}\n"
                f"Ele entrará em contato no dia da entrega."
            )
        else:
            msg_cliente = (
                "✅ <b>Pagamento confirmado!</b>\n\n"
                "🚚 Seu pedido foi aceito!\n\n"
                f"📱 <b>Contato do entregador:</b> {ENTREGADOR_USERNAME}\n"
                "Entre em contato com ele para combinar a entrega."
            )
        await context.bot.send_message(chat_id=customer_id, text=msg_cliente, parse_mode="HTML")
        await query.edit_message_caption(
            f"✅ <b>Pedido #{numero} confirmado!</b>\n"
            f"👤 {pedido['nome_cliente']} — R$ {pedido['total']:.2f}\n"
            "Estoque debitado. Entregador notificado. ✅",
            parse_mode="HTML"
        )

    elif query.data.startswith("pgto_rec_"):
        if not is_admin(query.from_user.id):
            await query.answer("❌ Acesso negado.", show_alert=True)
            return
        customer_id = int(query.data.split("_")[-1])
        pedidos_pendentes.pop(customer_id, None)
        await context.bot.send_message(
            chat_id=customer_id,
            text=(
                "❌ <b>Pagamento não identificado.</b>\n\n"
                "Não conseguimos confirmar o seu PIX.\n"
                "Verifique e tente novamente ou entre em contato conosco."
            ),
            parse_mode="HTML"
        )
        await query.edit_message_caption("❌ Pagamento recusado. Cliente notificado.")

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
            "<code>5 PAK\n2 ICE\n1 POD_I\n1 POD_S</code>",
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

    # --- Nome completo do cliente ---
    if estado == "cliente_nome":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await update.message.reply_text("Sessão expirada. Use /start para recomeçar.")
            return
        nome = texto.strip()
        if len(nome) < 3:
            await update.message.reply_text(
                "✏️ Por favor, informe seu nome completo (mínimo 3 letras)."
            )
            return
        pedido["nome_cliente"] = nome
        context.user_data["estado"] = "cliente_endereco"
        msg = (
            f"✅ Nome salvo: <b>{nome}</b>\n\n"
            f"📍 <b>Qual o seu endereço de entrega?</b>\n"
            f"<i>(rua, número, bairro)</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏪 Vou retirar", callback_data="loja_retirar")],
        ])
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        return

    # --- Endereço de entrega (fluxo loja) ---
    if estado == "cliente_endereco":
        endereco = texto
        pedido   = context.user_data.get("pedido_pendente")
        if not pedido:
            await update.message.reply_text("Sessão expirada. Use /start para recomeçar.")
            return
        pedido["endereco"] = endereco
        context.user_data["estado"] = None
        msg = (
            f"📍 <b>Endereço salvo:</b> <i>{endereco}</i>\n\n"
            f"💸 Total: <b>R$ {pedido['total']:.0f}</b>\n\n"
            f"Como vai pagar?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 PIX",      callback_data="loja_pagar_pix"),
             InlineKeyboardButton("💵 Dinheiro", callback_data="loja_pagar_dinheiro")],
            [InlineKeyboardButton("← Voltar ao carrinho", callback_data="loja_voltar_carrinho")],
        ])
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        return

    # --- Troco para dinheiro ---
    if estado == "cliente_troco":
        pedido = context.user_data.get("pedido_pendente")
        if not pedido:
            await update.message.reply_text("Sessão expirada. Use /start para recomeçar.")
            return
        try:
            valor = float(texto.replace(",", ".").replace("R$", "").replace(" ", ""))
            total = pedido.get("total", 0)
            if valor < total:
                await update.message.reply_text(
                    f"❌ Valor insuficiente. O total é <b>R$ {total:.0f}</b>.\nDigite um valor maior.",
                    parse_mode="HTML"
                )
                return
            troco = valor - total
            context.user_data["troco"]  = troco
            context.user_data["estado"] = None
            itens_str = context.user_data.get("itens_str_cache", "")
            taxa_str  = context.user_data.get("taxa_str_cache", "")
            endereco  = pedido.get("endereco", "")
            end_str   = f"\n📍 {endereco}" if endereco and endereco != "Retirada" else ("\n🏪 Retirada" if endereco == "Retirada" else "")
            troco_str = f"\n💸 Troco: <b>R$ {troco:.0f}</b>" if troco > 0 else ""
            msg = (
                f"💵 <b>CONFIRMAR PEDIDO — DINHEIRO</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"{itens_str}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{taxa_str}\n"
                f"💸 Total: <b>R$ {total:.0f}</b>{troco_str}{end_str}\n\n"
                f"Confirma o pedido?"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar pedido", callback_data="loja_confirmar_dinheiro")],
                [InlineKeyboardButton("← Voltar",           callback_data="loja_pagar_dinheiro")],
            ])
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        except ValueError:
            await update.message.reply_text("❌ Valor inválido. Ex: <code>100</code>", parse_mode="HTML")
        return

    # ====================== ESTADOS ADMIN GUIADOS ======================

    ADMIN_ESTADOS = ("admin_add", "admin_rem", "admin_rd", "admin_bart",
                     "admin_saida", "admin_banco", "admin_fornecedor", "admin_rel_data", "admin_broadcast")

    if estado in ADMIN_ESTADOS:
        if not is_admin(update.effective_user.id):
            context.user_data.clear()
            return

        kb_menu = InlineKeyboardMarkup([[InlineKeyboardButton("← Menu", callback_data="admin_menu_principal")]])

        if estado == "admin_add":
            partes = texto.upper().split()
            if len(partes) == 2:
                cod = ALIAS.get(partes[0], partes[0])
                if cod in CODIGOS:
                    try:
                        qtd = float(partes[1].replace(",", "."))
                        conn = get_db(); c = conn.cursor()
                        c.execute("UPDATE produtos SET estoque = estoque + ? WHERE codigo = ?", (qtd, cod))
                        c.execute("SELECT estoque, nome FROM produtos WHERE codigo = ?", (cod,))
                        novo, nome = c.fetchone(); conn.commit(); conn.close()
                        context.user_data["estado"] = None
                        await update.message.reply_text(
                            f"✅ <b>{nome}</b> +{qtd:.1f}\n📦 Estoque agora: <b>{novo:.1f}</b>",
                            parse_mode="HTML", reply_markup=kb_menu)
                        return
                    except ValueError: pass
            await update.message.reply_text("❌ Formato inválido. Ex: <code>ICE 50</code>", parse_mode="HTML")

        elif estado == "admin_rem":
            partes = texto.upper().split()
            if len(partes) == 2:
                cod = ALIAS.get(partes[0], partes[0])
                if cod in CODIGOS:
                    try:
                        qtd = float(partes[1].replace(",", "."))
                        conn = get_db(); c = conn.cursor()
                        c.execute("UPDATE produtos SET estoque = estoque - ? WHERE codigo = ?", (qtd, cod))
                        c.execute("SELECT estoque, nome FROM produtos WHERE codigo = ?", (cod,))
                        novo, nome = c.fetchone(); conn.commit(); conn.close()
                        aviso = "\n🚨 <b>ESTOQUE ZERADO!</b>" if novo <= 0 else ("\n⚠️ <b>Estoque baixo!</b>" if novo <= 20 else "")
                        context.user_data["estado"] = None
                        await update.message.reply_text(
                            f"✅ <b>{nome}</b> -{qtd:.1f}\n📦 Estoque agora: <b>{novo:.1f}</b>{aviso}",
                            parse_mode="HTML", reply_markup=kb_menu)
                        return
                    except ValueError: pass
            await update.message.reply_text("❌ Formato inválido. Ex: <code>ICE 10</code>", parse_mode="HTML")

        elif estado == "admin_rd":
            try:
                valor = float(texto.replace(",", ".").replace("R$", "").strip())
                registrar_caixa("saida", valor, "Retirada RD")
                conn = get_db(); c = conn.cursor()
                c.execute("INSERT INTO retiradas (responsavel, valor, data) VALUES (?,?,?)",
                          ("RD", valor, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit(); conn.close()
                context.user_data["estado"] = None
                await update.message.reply_text(
                    f"✅ <b>Retirada RD</b> registrada: R$ {valor:.2f}", parse_mode="HTML", reply_markup=kb_menu)
            except ValueError:
                await update.message.reply_text("❌ Valor inválido. Ex: <code>500</code>", parse_mode="HTML")

        elif estado == "admin_bart":
            try:
                valor = float(texto.replace(",", ".").replace("R$", "").strip())
                registrar_caixa("saida", valor, "Retirada Bart")
                conn = get_db(); c = conn.cursor()
                c.execute("INSERT INTO retiradas (responsavel, valor, data) VALUES (?,?,?)",
                          ("Bart", valor, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit(); conn.close()
                context.user_data["estado"] = None
                await update.message.reply_text(
                    f"✅ <b>Retirada Bart</b> registrada: R$ {valor:.2f}", parse_mode="HTML", reply_markup=kb_menu)
            except ValueError:
                await update.message.reply_text("❌ Valor inválido. Ex: <code>500</code>", parse_mode="HTML")

        elif estado == "admin_saida":
            partes = texto.split(None, 1)
            try:
                valor = float(partes[0].replace(",", ".").replace("R$", ""))
                desc  = partes[1].strip() if len(partes) > 1 else "Saída"
                registrar_caixa("saida", valor, desc)
                context.user_data["estado"] = None
                await update.message.reply_text(
                    f"✅ <b>Saída</b> registrada: R$ {valor:.2f}\n📝 {desc}", parse_mode="HTML", reply_markup=kb_menu)
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Formato inválido. Ex: <code>50 Gasolina</code>", parse_mode="HTML")

        elif estado == "admin_banco":
            try:
                valor = float(texto.replace(",", ".").replace("R$", "").strip())
                set_config("saldo_banco", valor)
                context.user_data["estado"] = None
                await update.message.reply_text(
                    f"✅ <b>Saldo Banco</b> atualizado: R$ {valor:.2f}", parse_mode="HTML", reply_markup=kb_menu)
            except ValueError:
                await update.message.reply_text("❌ Valor inválido. Ex: <code>3400</code>", parse_mode="HTML")

        elif estado == "admin_fornecedor":
            try:
                valor = float(texto.replace(",", ".").replace("R$", "").strip())
                set_config("divida_fornecedor", valor)
                context.user_data["estado"] = None
                await update.message.reply_text(
                    f"✅ <b>Dívida Fornecedor</b> atualizada: R$ {valor:.2f}", parse_mode="HTML", reply_markup=kb_menu)
            except ValueError:
                await update.message.reply_text("❌ Valor inválido. Ex: <code>74890</code>", parse_mode="HTML")

        elif estado == "admin_rel_data":
            try:
                raw = texto.strip().replace("/", "-")
                partes = raw.split("-")
                ano = partes[2] if len(partes) == 3 else datetime.date.today().strftime("%Y")
                data_fmt = f"{ano}-{partes[1].zfill(2)}-{partes[0].zfill(2)}"
                rel = build_relatorio_text(data_fmt)
                context.user_data["estado"] = None
                await update.message.reply_text(rel, parse_mode="HTML", reply_markup=kb_menu)
            except Exception:
                await update.message.reply_text("❌ Data inválida. Ex: <code>05/05</code>", parse_mode="HTML")

        elif estado == "admin_broadcast":
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT chat_id FROM clientes")
            todos = [row[0] for row in c.fetchall()]; conn.close()
            context.user_data["estado"] = None
            enviados = 0; falhos = 0
            for cid in todos:
                try:
                    await context.bot.send_message(
                        chat_id=cid,
                        text=f"📢 <b>Aviso da loja:</b>\n\n{texto}",
                        parse_mode="HTML"
                    )
                    enviados += 1
                except Exception:
                    falhos += 1
            await update.message.reply_text(
                f"✅ Broadcast enviado!\n"
                f"📨 Enviado: <b>{enviados}</b>\n"
                f"❌ Falhou: <b>{falhos}</b>",
                parse_mode="HTML", reply_markup=kb_menu
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
            for prod in ["ICE", "PAK", "CRUMBLE", "POD_I", "POD_S"]:
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

# ====================== JOBS AGENDADOS ======================

async def job_abrir_loja(context: ContextTypes.DEFAULT_TYPE):
    """Abre a loja automaticamente no horário definido."""
    set_config("loja_aberta", 1)
    await check_low_stock(context.bot)
    if ADMIN_ID:
        estoque = build_estoque_text()
        # Pedidos agendados para hoje
        hoje_str = datetime.date.today().strftime("%Y-%m-%d")
        conn = get_db(); c = conn.cursor()
        c.execute(
            "SELECT numero, cliente, total, pagamento, endereco FROM pedidos "
            "WHERE data_entrega=? AND responsavel='Loja-Bot' AND status='OK'",
            (hoje_str,)
        )
        agendados = c.fetchall(); conn.close()
        ag_bloco = ""
        if agendados:
            linhas = "\n".join(
                f"  #{n} {cl} — R${tot:.0f} ({pag})"
                + (f"\n    📍 {end}" if end else "")
                for n, cl, tot, pag, end in agendados
            )
            ag_bloco = f"\n\n📅 <b>Pedidos agendados para hoje ({len(agendados)}):</b>\n{linhas}"
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🟢 <b>LOJA ABERTA</b>\n"
                    "━━━━━━━━━━━━━━\n\n"
                    "Abertura automática às 08:00.\n\n"
                    + estoque + ag_bloco
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Falha ao notificar abertura: {e}")

async def job_fechar_loja(context: ContextTypes.DEFAULT_TYPE):
    """Fecha a loja e envia relatório diário automaticamente."""
    set_config("loja_aberta", 0)
    if ADMIN_ID:
        hoje = datetime.date.today().strftime("%Y-%m-%d")
        rel  = build_relatorio_text(hoje)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🔴 <b>LOJA FECHADA</b>\n"
                    "━━━━━━━━━━━━━━\n\n"
                    "Fechamento automático às 19:00.\n\n"
                    + rel
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Falha ao notificar fechamento: {e}")

async def job_verificar_estoque(context: ContextTypes.DEFAULT_TYPE):
    """Verificação de estoque toda manhã às 10h."""
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="📦 <b>Checagem de estoque — manhã</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    await check_low_stock(context.bot)

# ====================== MAIN ======================

FOTOS_LOCAL_DIR = os.path.join(os.path.dirname(__file__), "fotos_local")

async def upload_local_fotos(app):
    """Sobe fotos locais ao Telegram na inicialização e guarda os file_ids."""
    if not os.path.exists(FOTOS_LOCAL_DIR):
        return
    fotos = load_fotos()
    for cod in PRODUTOS_INFO:
        if cod in fotos:
            continue
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = os.path.join(FOTOS_LOCAL_DIR, f"{cod}.{ext}")
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        msg = await app.bot.send_photo(
                            chat_id=ADMIN_ID, photo=f,
                            disable_notification=True)
                    save_foto(cod, msg.photo[-1].file_id)
                    await app.bot.delete_message(
                        chat_id=ADMIN_ID, message_id=msg.message_id)
                    logging.info(f"Foto local '{cod}' registrada.")
                except Exception as e:
                    logging.warning(f"Falha ao enviar foto local de {cod}: {e}")
                break

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não definido!")

    app = ApplicationBuilder().token(token).post_init(upload_local_fotos).build()

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
    app.add_handler(CommandHandler("remover",       cmd_remover))
    app.add_handler(CommandHandler("cancelar",      cmd_cancelar))
    app.add_handler(CommandHandler("resetdia",      reset_dia))
    app.add_handler(CommandHandler("resetcompleto", reset_completo))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_comprovante))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem))

    # ── Agendamentos automáticos (fuso horário Brasília) ──
    BR_TZ = ZoneInfo("America/Sao_Paulo")
    jq    = app.job_queue
    # Abre loja às 08:00 e checa estoque
    jq.run_daily(job_abrir_loja,        datetime.time( 8,  0, 0, tzinfo=BR_TZ))
    # Fecha loja às 19:00 e envia relatório do dia
    jq.run_daily(job_fechar_loja,       datetime.time(19,  0, 0, tzinfo=BR_TZ))
    # Checagem de estoque às 07:30 (antes da abertura)
    jq.run_daily(job_verificar_estoque, datetime.time( 7, 30, 0, tzinfo=BR_TZ))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
