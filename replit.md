# Cookie Stock Bot

A Telegram bot for tracking cookie flavor inventory — deduct stock by sending a message like `I 3`, check current levels with `/estoque`.

## Run & Operate

- `python bot/bot.py` — run the Telegram bot (managed via "Telegram Bot" workflow)
- Required env: `TELEGRAM_BOT_TOKEN` — Telegram bot token from @BotFather

## Stack

- Python 3.11
- python-telegram-bot (polling mode)
- JSON file for persistent storage (`bot/estoque.json`)

## Where things live

- `bot/bot.py` — main bot logic
- `bot/estoque.json` — stock data (auto-created on first run)

## Architecture decisions

- Polling mode (not webhook) for simplicity in the Replit environment
- Stock stored as a flat JSON file — no DB needed for this use case
- Each message handler reloads and saves the JSON on every operation to avoid data loss

## Product

Telegram bot for cannabis inventory + ordering:
- **ICE** = Ice o Lator | **PAK** = Pak | **CRUMBLE** = Crumble | **POD** = Pod THC

**Admin menu (owner only):**
- 📋 Novo Pedido / 📦 Estoque / 💰 Caixa / 📊 Relatório Hoje
- 🏪 Gestão de Estoque → ➕ Adicionar / ➖ Remover (guided input)
- 💸 Financeiro → Retirada RD/Bart, Saída Caixa, Saldo Banco, Dívida Fornecedor, Relatório por Data, Reset Dia/Completo
- ❌ Cancelar Último Pedido (with confirmation)

**Customer flow:** cart → PIX → photo comprovante → admin confirms → entregador notified

**Entregador (@jRDG7):** delivery notifications only — zero access to admin features

Commands still available: `/start`, `/relatorio [date]`, `/fechamento`, `/cancelar`, `/resetdia`, `/resetcompleto`, `/add`, `/remover`, `/saida`, `/rd`, `/bart`, `/banco`, `/fornecedor`

## User preferences

- Keep code in Python, not TypeScript
- Portuguese language in bot messages

## Gotchas

- Only one bot instance should run at a time — two instances cause a 409 Conflict error from Telegram
- Restart the "Telegram Bot" workflow after code changes

## Pointers

- See the `workflows` skill for managing the bot workflow
