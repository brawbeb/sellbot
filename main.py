import os
import json
import logging
import asyncio
from datetime import datetime
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
import redis.asyncio as redis

# ================== НАСТРОЙКА ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# ===============================================

# ================== ПЕРЕМЕННЫЕ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_IDS = [int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()]
REDIS_URL = os.environ.get("REDIS_URL")
# ================================================

redis_client = None

HELP_TEXT = """
🆘 **Помощь по боту**

👑 **Владельцы:**
/addseller <user_id> – добавить продавца
/removeseller <user_id> – удалить продавца

👤 **Продавец (блогер):**
👑 Мои товары – управление товарами
💳 Мои реквизиты – указать платёжные данные
📊 Статистика продаж – мои продажи

🧑 **Покупатель:**
🛍 Товары – купить товары

По вопросам: @karatitik
"""

PRODUCTS_PER_PAGE = 5

# ----- Redis -----
async def init_redis():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, max_connections=30)
    await redis_client.ping()
    print("✅ Redis подключён")

async def is_owner(user_id):
    return user_id in OWNER_IDS

async def is_seller(user_id):
    return await redis_client.sismember("sellers", str(user_id))

async def add_seller(user_id):
    await redis_client.sadd("sellers", str(user_id))

async def remove_seller(user_id):
    await redis_client.srem("sellers", str(user_id))

# ----- Платёжные реквизиты продавца -----
async def set_payment_details(seller_id, text):
    await redis_client.set(f"payment:{seller_id}", text)

async def get_payment_details(seller_id):
    return await redis_client.get(f"payment:{seller_id}")

# ----- Товары -----
async def add_product(seller_id, name, price, description):
    product_id = await redis_client.incr("global:product_id")
    product = {
        "id": product_id,
        "seller_id": seller_id,
        "name": name,
        "price": int(price),
        "description": description,
        "created": datetime.now().isoformat()
    }
    await redis_client.set(f"product:{product_id}", json.dumps(product))
    await redis_client.sadd(f"seller:{seller_id}:products", str(product_id))
    return product_id

async def get_product(product_id):
    data = await redis_client.get(f"product:{product_id}")
    return json.loads(data) if data else None

async def update_product(product_id, **fields):
    product = await get_product(product_id)
    if not product:
        return False
    product.update(fields)
    await redis_client.set(f"product:{product_id}", json.dumps(product))
    return True

async def delete_product(product_id):
    product = await get_product(product_id)
    if not product:
        return False
    await redis_client.delete(f"product:{product_id}")
    await redis_client.srem(f"seller:{product['seller_id']}:products", str(product_id))
    return True

async def get_seller_products(seller_id):
    product_ids = await redis_client.smembers(f"seller:{seller_id}:products")
    products = []
    for pid in product_ids:
        p = await get_product(int(pid))
        if p:
            products.append(p)
    return products

async def get_all_products():
    keys = await redis_client.keys("product:*")
    products = []
    for key in keys:
        p = json.loads(await redis_client.get(key))
        products.append(p)
    return products

# ----- Статистика продаж -----
async def record_purchase(product_id, buyer_id):
    product = await get_product(product_id)
    if not product:
        return False
    await redis_client.incr(f"stats:product:{product_id}:sales")
    await redis_client.incr(f"stats:seller:{product['seller_id']}:sales")
    return True

async def get_seller_stats(seller_id):
    sales = int(await redis_client.get(f"stats:seller:{seller_id}:sales") or 0)
    return sales

# ----- Клавиатуры -----
def main_keyboard(user_id):
    keyboard = [[KeyboardButton("🛍 Товары")]]
    if asyncio.run_coroutine_threadsafe(is_seller(user_id), asyncio.get_event_loop()).result():
        keyboard.append([KeyboardButton("👑 Мои товары"), KeyboardButton("💳 Мои реквизиты")])
        keyboard.append([KeyboardButton("📊 Статистика продаж")])
    keyboard.append([KeyboardButton("🆘 Помощь")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ----- Команды -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🛍 Добро пожаловать в маркетплейс!\nИспользуйте кнопки для навигации.",
        reply_markup=main_keyboard(user_id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

# ----- Админ-команды (только владельцы) -----
async def add_seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав. Только владельцы могут назначать продавцов.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите ID пользователя. Пример: /addseller 123456789")
        return
    try:
        seller_id = int(context.args[0])
        await add_seller(seller_id)
        await update.message.reply_text(f"✅ Пользователь {seller_id} добавлен как продавец (блогер).")
    except:
        await update.message.reply_text("❌ Неверный ID.")

async def remove_seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав. Только владельцы могут удалять продавцов.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите ID. Пример: /removeseller 123456789")
        return
    try:
        seller_id = int(context.args[0])
        await remove_seller(seller_id)
        await update.message.reply_text(f"✅ Продавец {seller_id} удалён.")
    except:
        await update.message.reply_text("❌ Неверный ID.")

# ----- Управление товарами продавца -----
async def my_products_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не являетесь продавцом.")
        return
    products = await get_seller_products(user_id)
    if not products:
        await update.message.reply_text("У вас пока нет товаров. Нажмите «➕ Добавить товар».")
    else:
        text = "🏷 **Ваши товары:**\n"
        keyboard = []
        for p in products:
            text += f"• {p['name']} – {p['price']} RUB\n"
            keyboard.append([InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f"edit_prod_{p['id']}")])
        keyboard.append([InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_product'] = 'name'
    await query.edit_message_text("Введите **название** товара:", parse_mode='Markdown')

async def process_product_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    state = context.user_data.get('awaiting_product')
    if state == 'name':
        context.user_data['product_name'] = update.message.text
        context.user_data['awaiting_product'] = 'price'
        await update.message.reply_text("Введите **цену** товара (в RUB):")
    elif state == 'price':
        try:
            price = int(update.message.text)
            context.user_data['product_price'] = price
            context.user_data['awaiting_product'] = 'desc'
            await update.message.reply_text("Введите **описание** товара:")
        except:
            await update.message.reply_text("❌ Цена должна быть числом. Попробуйте ещё раз.")
    elif state == 'desc':
        name = context.user_data['product_name']
        price = context.user_data['product_price']
        desc = update.message.text
        product_id = await add_product(user_id, name, price, desc)
        await update.message.reply_text(f"✅ Товар «{name}» добавлен. ID: {product_id}")
        context.user_data.pop('awaiting_product', None)
        context.user_data.pop('product_name', None)
        context.user_data.pop('product_price', None)
        await my_products_button(update, context)

async def edit_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
    product = await get_product(product_id)
    if not product or product['seller_id'] != query.from_user.id:
        await query.edit_message_text("❌ Товар не найден или не принадлежит вам.")
        return
    context.user_data['edit_product_id'] = product_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Название", callback_data="edit_field_name")],
        [InlineKeyboardButton("💰 Цена", callback_data="edit_field_price")],
        [InlineKeyboardButton("📝 Описание", callback_data="edit_field_desc")],
        [InlineKeyboardButton("❌ Удалить товар", callback_data="delete_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_products")]
    ])
    await query.edit_message_text(f"Редактирование товара **{product['name']}**", parse_mode='Markdown', reply_markup=keyboard)

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("_")[2]
    context.user_data['edit_field'] = field
    if field == 'name':
        await query.edit_message_text("Введите новое название:")
    elif field == 'price':
        await query.edit_message_text("Введите новую цену (число):")
    elif field == 'desc':
        await query.edit_message_text("Введите новое описание:")
    context.user_data['awaiting_edit_value'] = True

async def process_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_edit_value'):
        return
    product_id = context.user_data.get('edit_product_id')
    field = context.user_data.get('edit_field')
    new_value = update.message.text
    product = await get_product(product_id)
    if not product:
        await update.message.reply_text("❌ Товар не найден.")
        context.user_data.clear()
        return
    if field == 'price':
        try:
            new_value = int(new_value)
        except:
            await update.message.reply_text("❌ Цена должна быть числом.")
            return
    if field == 'name':
        await update_product(product_id, name=new_value)
        await update.message.reply_text(f"✅ Название изменено на «{new_value}».")
    elif field == 'price':
        await update_product(product_id, price=new_value)
        await update.message.reply_text(f"✅ Цена изменена на {new_value} RUB.")
    elif field == 'desc':
        await update_product(product_id, description=new_value)
        await update.message.reply_text("✅ Описание обновлено.")
    context.user_data.pop('awaiting_edit_value', None)
    context.user_data.pop('edit_field', None)
    await edit_product_callback(update, context)

async def delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = context.user_data.get('edit_product_id')
    if not product_id:
        await query.edit_message_text("❌ Ошибка.")
        return
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    await delete_product(product_id)
    await query.edit_message_text(f"✅ Товар «{product['name']}» удалён.")
    context.user_data.clear()
    await my_products_button(update, context)

async def back_to_my_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await my_products_button(update, context)

# ----- Установка реквизитов продавцом -----
async def payment_details_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    current = await get_payment_details(user_id)
    text = "💳 **Ваши платёжные реквизиты**\n\n"
    if current:
        text += f"Текущие реквизиты:\n{current}\n\n"
    else:
        text += "Реквизиты не установлены.\n\n"
    text += "Введите новые реквизиты одной командой:\n`/setpayment <текст>`\n\nПример:\n`/setpayment Карта 1234 5678 9012 3456, Иван`"
    await update.message.reply_text(text, parse_mode='Markdown')

async def set_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите текст с реквизитами. Пример: /setpayment Карта 1234 5678 9012 3456")
        return
    details = ' '.join(context.args)
    await set_payment_details(user_id, details)
    await update.message.reply_text("✅ Ваши платёжные реквизиты сохранены. Покупатели будут видеть их при оформлении заказа.")

# ----- Каталог товаров -----
async def catalog_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await get_all_products()
    if not products:
        await update.message.reply_text("🛍 Пока нет товаров.")
        return
    page = context.user_data.get('catalog_page', 0)
    total = len(products)
    start = page * PRODUCTS_PER_PAGE
    end = start + PRODUCTS_PER_PAGE
    page_products = products[start:end]
    text = "🛍 **Каталог товаров:**\n\n"
    keyboard = []
    for p in page_products:
        text += f"**{p['name']}** – {p['price']} RUB (продавец: `{p['seller_id']}`)\n{p['description']}\n\n"
        keyboard.append([InlineKeyboardButton(f"Купить {p['name']}", callback_data=f"buy_{p['id']}")])
    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data="catalog_prev"))
    if end < total:
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data="catalog_next"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def catalog_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    page = context.user_data.get('catalog_page', 0)
    if data == "catalog_prev":
        page -= 1
    elif data == "catalog_next":
        page += 1
    context.user_data['catalog_page'] = page
    products = await get_all_products()
    if not products:
        await query.edit_message_text("🛍 Пока нет товаров.")
        return
    start = page * PRODUCTS_PER_PAGE
    end = start + PRODUCTS_PER_PAGE
    page_products = products[start:end]
    text = "🛍 **Каталог товаров:**\n\n"
    keyboard = []
    for p in page_products:
        text += f"**{p['name']}** – {p['price']} RUB (продавец: `{p['seller_id']}`)\n{p['description']}\n\n"
        keyboard.append([InlineKeyboardButton(f"Купить {p['name']}", callback_data=f"buy_{p['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data="catalog_prev"))
    if end < len(products):
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data="catalog_next"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ----- Покупка -----
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[1])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    seller_id = product['seller_id']
    payment_details = await get_payment_details(seller_id)
    if not payment_details:
        payment_details = "реквизиты не указаны. Свяжитесь с продавцом через поддержку."
    text = (
        f"🧾 **Оплата товара «{product['name']}»**\n"
        f"Сумма: {product['price']} RUB\n\n"
        f"💰 **Реквизиты продавца:**\n{payment_details}\n\n"
        f"После перевода нажмите кнопку «Я оплатил» и сообщите продавцу об оплате (можно в личные сообщения)."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_payment_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_catalog")]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)

async def confirm_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    buyer_id = query.from_user.id
    seller_id = product['seller_id']
    # Уведомляем продавца
    try:
        await context.bot.send_message(
            seller_id,
            f"💰 Пользователь {buyer_id} сообщил об оплате товара «{product['name']}».\n"
            f"Сумма: {product['price']} RUB.\n"
            f"Проверьте поступление средств и свяжитесь с покупателем для завершения сделки."
        )
    except:
        pass
    await query.edit_message_text("✅ Спасибо! Уведомление отправлено продавцу. Он свяжется с вами в ближайшее время.")
    await record_purchase(product_id, buyer_id)

async def back_to_catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['catalog_page'] = 0
    await catalog_button(update, context)

# ----- Статистика продавца -----
async def seller_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    sales = await get_seller_stats(user_id)
    text = f"📊 **Ваша статистика продаж**\nПродано товаров: {sales}"
    await update.message.reply_text(text, parse_mode='Markdown')

# ----- Кнопка "Назад" в главное меню -----
async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await start(update, context)
    await query.delete_message()

# ----- Веб-сервер для здоровья -----
async def health(request):
    return web.Response(text="OK")

# ----- Запуск -----
async def on_startup(app):
    await init_redis()
    print("✅ Бот запущен")

async def on_shutdown(app):
    if redis_client:
        await redis_client.aclose()

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get('/health', health)

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("addseller", add_seller_command))
    bot_app.add_handler(CommandHandler("removeseller", remove_seller_command))
    bot_app.add_handler(CommandHandler("setpayment", set_payment_command))

    # Кнопки главного меню
    bot_app.add_handler(MessageHandler(filters.Text("🛍 Товары"), catalog_button))
    bot_app.add_handler(MessageHandler(filters.Text("👑 Мои товары"), my_products_button))
    bot_app.add_handler(MessageHandler(filters.Text("💳 Мои реквизиты"), payment_details_button))
    bot_app.add_handler(MessageHandler(filters.Text("📊 Статистика продаж"), seller_stats_button))
    bot_app.add_handler(MessageHandler(filters.Text("🆘 Помощь"), help_command))

    # Колбэки
    bot_app.add_handler(CallbackQueryHandler(add_product_callback, pattern="^add_product$"))
    bot_app.add_handler(CallbackQueryHandler(edit_product_callback, pattern="^edit_prod_"))
    bot_app.add_handler(CallbackQueryHandler(edit_field_callback, pattern="^edit_field_"))
    bot_app.add_handler(CallbackQueryHandler(delete_product_callback, pattern="^delete_product$"))
    bot_app.add_handler(CallbackQueryHandler(back_to_my_products_callback, pattern="^back_to_my_products$"))
    bot_app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    bot_app.add_handler(CallbackQueryHandler(confirm_payment_callback, pattern="^confirm_payment_"))
    bot_app.add_handler(CallbackQueryHandler(back_to_catalog_callback, pattern="^back_to_catalog$"))
    bot_app.add_handler(CallbackQueryHandler(catalog_nav_callback, pattern="^catalog_(prev|next)$"))
    bot_app.add_handler(CallbackQueryHandler(back_to_main_callback, pattern="^back_to_main$"))

    # Обработка текстового ввода для добавления/редактирования товаров
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_product_input))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_value))

    # Запуск поллинга и веб-сервера
    port = int(os.environ.get('PORT', 10000))
    loop = asyncio.get_event_loop()
    loop.create_task(bot_app.run_polling())
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()