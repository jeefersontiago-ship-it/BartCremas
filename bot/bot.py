import json
import os
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍪 *Bot de Estoque Ativo!*\n\n"
        "*Como usar:*\n"
        "• `I 3` — desconta 3 unidades de Ice o Lator\n"
        "• `P 2` — desconta 2 unidades de Pak\n"
        "• `C 5` — desconta 5 unidades de Crumble\n"
        "• `VP 1` — desconta 1 unidade de Pod THC\n\n"
        "*Sabores disponíveis:* I, P, C, VP\n\n"
        "Use /estoque para ver o estoque atual.",
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

async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        texto = update.message.text.upper().split()
        if len(texto) < 2:
            await update.message.reply_text("⚠️ Use: I 3")
            return
        sabor = texto[0]
        qtd = int(texto[1])

        if sabor not in NOMES:
            await update.message.reply_text(
                f"❌ Sabor inválido.\nUse: {', '.join(NOMES.keys())}"
            )
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
        await update.message.reply_text("⚠️ Use: I 3")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, registrar))

    logging.info("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
