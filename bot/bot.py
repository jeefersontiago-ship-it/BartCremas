import json
import os
import re
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

ARQUIVO = os.path.join(os.path.dirname(__file__), "estoque.json")

NOMES = {
    "I": "Ice o Lator",
    "P": "Pak",
    "C": "Crumble",
    "VP": "Pod THC",
}

def carregar():
    try:
        with open(ARQUIVO, "r") as f:
            return json.load(f)
    except Exception:
        return {"I": 400, "P": 190, "C": 0, "VP": 0}

def salvar(estoque):
    with open(ARQUIVO, "w") as f:
        json.dump(estoque, f, indent=2)

def aviso_estoque(estoque, sabores_afetados):
    avisos = []
    for k in sabores_afetados:
        if estoque.get(k, 0) <= 0:
            avisos.append(f"🚨 *{k}* ZERADO!")
        elif estoque.get(k, 0) <= 20:
            avisos.append(f"⚠️ *{k}* estoque baixo!")
    return "\n".join(avisos)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍪 *Bot de Estoque Ativo!*\n\n"
        "*Desconto rápido:*\n"
        "• `I 3` — desconta 3 unidades de Ice o Lator\n"
        "• `P 2` — desconta 2 de Pak\n"
        "• `C 5` — desconta 5 de Crumble\n"
        "• `VP 1` — desconta 1 de Pod THC\n\n"
        "*Resumo de pedidos:*\n"
        "• `/pedidos` — cole o texto do pedido abaixo do comando\n"
        "  Ex: `/pedidos 3 - I, 2 - P R$ 150,00`\n\n"
        "*Outros comandos:*\n"
        "• `/estoque` — ver estoque atual\n"
        "• `/add I 50` — repor estoque\n",
        parse_mode="Markdown"
    )

async def ver_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    estoque = carregar()
    msg = "📦 *ESTOQUE ATUAL:*\n\n"
    for k, v in estoque.items():
        nome = NOMES.get(k, k)
        emoji = "✅" if v > 20 else ("⚠️" if v > 0 else "❌")
        msg += f"{emoji} *{k}* ({nome}): `{v}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def adicionar_estoque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("⚠️ Use: /add I 50")
            return
        sabor = args[0].upper()
        qtd = int(args[1])
        if sabor not in NOMES:
            await update.message.reply_text(f"❌ Sabor inválido. Use: {', '.join(NOMES.keys())}")
            return
        estoque = carregar()
        estoque[sabor] += qtd
        salvar(estoque)
        nome = NOMES[sabor]
        await update.message.reply_text(
            f"✅ *{sabor}* ({nome}) +{qtd}\n📦 Agora: `{estoque[sabor]}`",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("⚠️ Quantidade inválida. Use: /add I 50")

async def pedidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = update.message.text

        contagem = {k: 0 for k in NOMES}
        total_dinheiro = 0.0

        itens = re.findall(r'(\d+)\s*[-–]\s*(VP|I|P|C)', texto.upper())
        for qtd, sabor in itens:
            contagem[sabor] += int(qtd)

        valores = re.findall(r'R?\$?\s*([\d\.]+)', texto.replace(",", "."))
        for v in valores:
            try:
                total_dinheiro += float(v)
            except Exception:
                pass

        if not any(contagem.values()) and total_dinheiro == 0:
            await update.message.reply_text(
                "⚠️ Nenhum item encontrado.\n"
                "Use o formato: `3 - I, 2 - P R$ 150,00`",
                parse_mode="Markdown"
            )
            return

        estoque = carregar()
        sabores_afetados = [k for k, v in contagem.items() if v > 0]
        for k in sabores_afetados:
            estoque[k] -= contagem[k]
        salvar(estoque)

        resposta = "📊 *RESUMO DO PEDIDO*\n\n"

        resposta += "🍪 *UNIDADES DESCONTADAS:*\n"
        for k, v in contagem.items():
            if v > 0:
                nome = NOMES.get(k, k)
                resposta += f"  *{k}* ({nome}): `{v}`\n"

        if total_dinheiro > 0:
            resposta += f"\n💰 *FATURAMENTO:* `R$ {total_dinheiro:.2f}`\n"

        resposta += "\n📦 *ESTOQUE ATUAL:*\n"
        for k, v in estoque.items():
            emoji = "✅" if v > 20 else ("⚠️" if v > 0 else "❌")
            resposta += f"  {emoji} *{k}*: `{v}`\n"

        avisos = aviso_estoque(estoque, sabores_afetados)
        if avisos:
            resposta += f"\n{avisos}"

        await update.message.reply_text(resposta, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Erro em pedidos: {e}")
        await update.message.reply_text("❌ Erro ao processar pedido.")

async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = update.message.text.upper().split()
        if len(texto) < 2:
            return
        sabor = texto[0]
        qtd = int(texto[1])

        if sabor not in NOMES:
            return

        if qtd <= 0:
            await update.message.reply_text("⚠️ Quantidade deve ser maior que zero.")
            return

        estoque = carregar()
        estoque[sabor] -= qtd
        salvar(estoque)

        nome = NOMES[sabor]
        aviso = ""
        if estoque[sabor] <= 0:
            aviso = "\n🚨 *ESTOQUE ZERADO!*"
        elif estoque[sabor] <= 20:
            aviso = "\n⚠️ *Estoque baixo!*"

        await update.message.reply_text(
            f"✅ *{sabor}* ({nome}) -{qtd}\n📦 Agora: `{estoque[sabor]}`{aviso}",
            parse_mode="Markdown"
        )

    except ValueError:
        pass
    except Exception as e:
        logging.error(f"Erro ao registrar: {e}")
        await update.message.reply_text("❌ Erro inesperado.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não definido!")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("estoque", ver_estoque))
    app.add_handler(CommandHandler("add", adicionar_estoque))
    app.add_handler(CommandHandler("pedidos", pedidos))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
