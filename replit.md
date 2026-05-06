# Cookie Stock Bot

Telegram bot (@bartcontrole_bot) para controle de estoque e pedidos de cannabis — clientes pedem pelo bot, admin confirma, entregador é notificado.

## Run & Operate

- `python bot/bot.py` — roda o bot (workflow "Telegram Bot")
- Env vars obrigatórias: `TELEGRAM_BOT_TOKEN`, `ADMIN_ID`, `CHAVE_PIX`, `SESSION_SECRET`

## Stack

- Python 3.11
- python-telegram-bot (polling mode)
- SQLite (`bot/controle.db`) para persistência

## Where things live

- `bot/bot.py` — toda a lógica do bot (~2000 linhas)
- `bot/controle.db` — banco SQLite; tabelas: `produtos`, `pedidos`, `itens_pedido`, `caixa`, `retiradas`, `config`, `clientes`
- `bot/fotos.json` — file_ids do Telegram para as 5 fotos de produto
- `bot/fotos_local/` — fotos locais (ICE.jpg, PAK.jpg, CRUMBLE.jpg, POD_I.png, POD_S.jpg)

## Architecture decisions

- Polling mode (não webhook) para simplicidade no Replit
- JSON flat para fotos, SQLite para tudo mais — sem dependências externas
- `pedidos_pendentes` é um dict em memória: temporário enquanto aguarda confirmação do admin
- Colunas adicionadas via `ALTER TABLE` em `init_db()` para migrações sem perda de dados
- Broadcast lê `clientes.chat_id` — populado no /start de cada cliente

## Product

**Produtos:** ICE (Ice Cream Cake) | PAK (Pak Nutella) | CRUMBLE | POD_I (Pod Indica) | POD_S (Pod Sativa)

**Fluxo do cliente:**
1. /start → store → 🛒 pedir agora (bloqueado se loja fechada)
2. Selecionar itens no carrinho → fechar pedido
3. Informar endereço de entrega (ou "Vou retirar")
4. Escolher PIX ou Dinheiro
5. PIX: enviar comprovante (expira em 20 min) → admin confirma → entregador notificado
6. Dinheiro: informar troco → confirmar → admin + entregador notificados
7. Entregador clica "✅ Marcar como Entregue" → cliente recebe confirmação

**Admin (ADMIN_ID):**
- 📋 Novo Pedido / 📦 Estoque / 💰 Caixa / 📊 Relatório Hoje
- 🏪 Gestão de Estoque → Adicionar / Remover / Fotos dos Produtos
- 💸 Financeiro → Retiradas, Saída Caixa, Saldo Banco, Dívida Fornecedor, Relatório por Data, Semanal, Mensal, Reset
- 📢 Broadcast → mensagem para todos os clientes cadastrados
- 🟢/🔴 Abrir/Fechar Loja (bloqueia novos pedidos quando fechada)
- ❌ Cancelar Último Pedido

**Entregador (@jRDG7):**
- Recebe notificação com endereço e itens ao confirmar pedido
- Botão "✅ Marcar como Entregue" em cada entrega
- Painel do Sócio: estoque, caixa, relatório, pedidos do dia, cardápio

**Cliente:**
- 📋 Meu Pedido → status e endereço do último pedido
- Notificado quando pagamento confirmado e quando entregue

## User preferences

- Manter código em Python
- Mensagens em português
- Sem dependências externas desnecessárias

## Gotchas

- Apenas uma instância do bot por vez — dois processos = erro 409
- Reiniciar o workflow "Telegram Bot" após qualquer mudança de código
- `itens_pedido.produto` é o nome correto da coluna (não `produto_codigo`)
- Alerta de estoque baixo disparado automaticamente após cada pedido confirmado

## Pointers

- Ver skill `workflows` para gerenciar o bot
- Ver skill `deployment` para deploy 24/7
