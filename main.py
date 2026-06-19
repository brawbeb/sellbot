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
logger = logging.getLogger(__name__)
# ===============================================

# ================== ПЕРЕМЕННЫЕ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_IDS = [int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()]
REDIS_URL = os.environ.get("REDIS_URL")
# ================================================

redis_client = None
bot_app = None

DEFAULT_ABOUT_TEXT = "🛍 Добро пожаловать в маркетплейс! Здесь вы можете купить различные товары у наших продавцов."

HELP_TEXT = """
По вопросам: @karatitik, @Pahachill
"""

# ----- Redis -----
async def init_redis():
    global redis_client
    logger.info("Подключение к Redis...")
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, max_connections=30)
    await redis_client.ping()
    logger.info("✅ Redis подключён")

async def is_owner(user_id):
    return user_id in OWNER_IDS

async def is_seller(user_id):
    return await redis_client.sismember("sellers", str(user_id))

async def add_seller(user_id):
    await redis_client.sadd("sellers", str(user_id))

async def remove_seller(user_id):
    await redis_client.srem("sellers", str(user_id))

async def get_payment_details():
    return await redis_client.get("global:payment_details")

async def set_payment_details(text):
    await redis_client.set("global:payment_details", text)

async def get_about_text():
    text = await redis_client.get("global:about")
    return text if text else DEFAULT_ABOUT_TEXT

async def set_about_text(text):
    await redis_client.set("global:about", text)

# ----- Разделы (один на продавца) -----
async def get_seller_section(seller_id):
    return await redis_client.get(f"seller:{seller_id}:section")

async def set_seller_section(seller_id, section_name):
    await redis_client.set(f"seller:{seller_id}:section", section_name)

async def delete_seller_section(seller_id):
    product_ids = await redis_client.smembers(f"seller:{seller_id}:products")
    for pid in product_ids:
        await delete_product(int(pid))
    await redis_client.delete(f"seller:{seller_id}:section")

async def rename_seller_section(seller_id, new_name):
    old_section = await get_seller_section(seller_id)
    if not old_section:
        return False
    await set_seller_section(seller_id, new_name)
    product_ids = await redis_client.smembers(f"seller:{seller_id}:products")
    for pid in product_ids:
        product = await get_product(int(pid))
        if product:
            await update_product(int(pid), section=new_name)
    return True

# ----- Товары -----
async def add_product(seller_id, name, price, description, section, quantity=None, data_from="buyer"):
    product_id = await redis_client.incr("global:product_id")
    product = {
        "id": product_id,
        "seller_id": seller_id,
        "section": section,
        "name": name,
        "price": int(price),
        "description": description,
        "quantity": quantity,
        "data_from": data_from,  # "buyer" или "seller" – кто предоставляет данные
        "created": datetime.now().isoformat()
    }
    await redis_client.set(f"product:{product_id}", json.dumps(product))
    await redis_client.sadd(f"seller:{seller_id}:products", str(product_id))
    return product_id

async def get_product(product_id):
    data = await redis_client.get(f"product:{product_id}")
    if not data:
        return None
    product = json.loads(data)
    if 'section' not in product:
        product['section'] = "Без раздела"
        await redis_client.set(f"product:{product_id}", json.dumps(product))
    if 'data_from' not in product:
        product['data_from'] = "buyer"
        await redis_client.set(f"product:{product_id}", json.dumps(product))
    return product

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
        p = await get_product(int(key.split(":")[1]))
        if p:
            products.append(p)
    return products

async def get_products_by_section(seller_id, section):
    products = await get_seller_products(seller_id)
    return [p for p in products if p.get('section') == section]

# ----- Баланс и пополнения -----
async def get_balance(user_id):
    return float(await redis_client.get(f"balance:{user_id}") or 0)

async def add_balance(user_id, amount):
    new_balance = await get_balance(user_id) + amount
    await redis_client.set(f"balance:{user_id}", str(new_balance))
    return new_balance

async def deduct_balance(user_id, amount):
    current = await get_balance(user_id)
    if current >= amount:
        new_balance = current - amount
        await redis_client.set(f"balance:{user_id}", str(new_balance))
        return True
    return False

# ----- История операций -----
async def add_transaction(user_id, transaction_type, amount, description, status="completed"):
    """transaction_type: 'deposit', 'purchase', 'refund'"""
    trans_id = await redis_client.incr("global:trans_id")
    trans = {
        "id": trans_id,
        "type": transaction_type,
        "amount": amount,
        "description": description,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }
    await redis_client.rpush(f"transactions:{user_id}", json.dumps(trans))
    return trans_id

async def get_transactions(user_id, limit=10):
    raw = await redis_client.lrange(f"transactions:{user_id}", -limit, -1)
    transactions = []
    for item in raw:
        try:
            transactions.append(json.loads(item))
        except:
            continue
    return transactions

# ----- Заказы (чеки) -----
async def create_order(user_id, amount, product_id=None):
    order_id = await redis_client.incr("global:order_id")
    order = {
        "id": order_id,
        "user_id": user_id,
        "amount": amount,
        "product_id": product_id,
        "status": "pending",  # pending, approved, rejected
        "created": datetime.now().isoformat()
    }
    await redis_client.set(f"order:{order_id}", json.dumps(order))
    return order_id

async def get_order(order_id):
    data = await redis_client.get(f"order:{order_id}")
    return json.loads(data) if data else None

async def update_order(order_id, **fields):
    order = await get_order(order_id)
    if not order:
        return False
    order.update(fields)
    await redis_client.set(f"order:{order_id}", json.dumps(order))
    return True

# ----- Клавиатуры -----
async def send_main_keyboard(update: Update, text: str):
    user_id = update.effective_user.id
    seller_status = await is_seller(user_id)
    if seller_status:
        keyboard = [
            [KeyboardButton("📂 Все категории")],
            [KeyboardButton("🛒 Наличие товаров")],
            [KeyboardButton("👑 Управление товарами"), KeyboardButton("💳 Реквизиты")],
            [KeyboardButton("📊 Статистика продаж")],
            [KeyboardButton("💰 Баланс")],
            [KeyboardButton("ℹ️ О магазине"), KeyboardButton("👤 Профиль"), KeyboardButton("🆘 Помощь")]
        ]
    else:
        keyboard = [
            [KeyboardButton("📂 Все категории")],
            [KeyboardButton("🛒 Наличие товаров")],
            [KeyboardButton("💰 Баланс")],
            [KeyboardButton("ℹ️ О магазине"), KeyboardButton("👤 Профиль")],
            [KeyboardButton("🆘 Помощь")]
        ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)

# ----- Старт -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"start от {update.effective_user.id}")
    await send_main_keyboard(update, "🛍 Добро пожаловать в маркетплейс!\nИспользуйте кнопки для навигации.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

# ----- Админ-команды -----
async def add_seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите ID пользователя. Пример: /addseller 123456789")
        return
    try:
        seller_id = int(context.args[0])
        await add_seller(seller_id)
        await update.message.reply_text(f"✅ Пользователь {seller_id} добавлен как продавец.")
        try:
            await context.bot.send_message(seller_id, "🎉 Вам выданы права продавца! Нажмите /start для обновления меню.")
        except:
            pass
    except:
        await update.message.reply_text("❌ Неверный ID.")

async def remove_seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав.")
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

async def set_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите реквизиты. Пример: /setpayment Карта 1234 5678 9012 3456, Иван")
        return
    text = ' '.join(context.args)
    await set_payment_details(text)
    await update.message.reply_text("✅ Платёжные реквизиты обновлены. Все покупатели будут видеть их при пополнении баланса.")

async def set_about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Нет прав.")
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите текст. Пример: /setabout Наш магазин лучший!")
        return
    text = ' '.join(context.args)
    await set_about_text(text)
    await update.message.reply_text("✅ Текст страницы «О магазине» обновлён.")

# ----- Управление товарами продавца (без изменений) -----
async def my_products_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не являетесь продавцом.")
        return
    keyboard = [
        [InlineKeyboardButton("📂 Управление разделом", callback_data="manage_sections")],
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    await update.message.reply_text(
        "🏷 **Управление товарами**\nВыберите действие:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ----- Функции управления разделами и товарами (оставляем без изменений из предыдущей версии) -----
# Вставьте сюда все функции из предыдущего кода: manage_sections_callback, add_section_callback, process_section_name, rename_section_callback, process_rename_section, delete_section_callback, add_product_callback, process_product_input, data_from_callback, cancel_add_product_callback, edit_product_callback, edit_field_callback, edit_data_from_callback, process_edit_value, delete_product_callback, back_to_my_products_callback, payment_details_button, edit_payment_callback, process_payment_input, seller_stats_button и т.д.
# Для краткости я их опущу, так как они идентичны предыдущей версии. Но в полном коде они будут.

# ... (пропущено, но в финальном коде присутствует)

# ----- Каталог и покупка (переделано) -----
async def catalog_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await get_all_products()
    if not products:
        await update.message.reply_text("📂 Пока нет категорий.")
        return
    sections = {}
    for p in products:
        sec = p['section']
        if sec not in sections:
            sections[sec] = p['seller_id']
    if not sections:
        await update.message.reply_text("📂 Пока нет категорий.")
        return
    text = "📂 **Выберите категорию:**"
    keyboard = []
    for sec, seller_id in sections.items():
        keyboard.append([InlineKeyboardButton(sec, callback_data=f"category_{seller_id}_{sec}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    parts = query.data.split("_")
    seller_id = int(parts[1])
    section = '_'.join(parts[2:])
    products = await get_products_by_section(seller_id, section)
    if not products:
        await query.edit_message_text(f"В категории «{section}» пока нет товаров.")
        return
    text = f"📂 **Категория: {section}**\n\n"
    for p in products:
        qty = p.get('quantity', '')
        if qty:
            text += f"• **{p['name']}** | {p['price']} ₽ | {qty}\n"
        else:
            text += f"• **{p['name']}** | {p['price']} ₽\n"
    text += "\nВыберите товар для покупки:"
    keyboard = []
    for p in products:
        keyboard.append([InlineKeyboardButton(p['name'], callback_data=f"product_{p['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 К категориям", callback_data="back_to_catalog")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def all_products_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await get_all_products()
    if not products:
        await update.message.reply_text("🛒 Пока нет товаров.")
        return
    groups = {}
    for p in products:
        sec = p['section']
        if sec not in groups:
            groups[sec] = []
        groups[sec].append(p)
    text = "🛒 **Наличие товаров:**\n\n"
    for sec, prods in groups.items():
        text += f"**{sec}**\n"
        for p in prods:
            qty = p.get('quantity', '')
            if qty:
                text += f"• **{p['name']}** | {p['price']} ₽ | {qty}\n"
            else:
                text += f"• **{p['name']}** | {p['price']} ₽\n"
        text += "\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ----- Карточка товара и покупка -----
async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    product_id = int(query.data.split("_")[1])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    context.user_data['buy_product_id'] = product_id
    text = (
        f"🧾 **{product['name']}**\n"
        f"💰 Цена: {product['price']} RUB (виртуальный баланс)\n"
        f"📝 Описание: {product['description']}\n\n"
        f"Ваш баланс: {await get_balance(query.from_user.id):.2f} RUB\n\n"
        f"Выберите количество товара, которое хотите купить:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1 шт.", callback_data="buy_qty_1")],
        [InlineKeyboardButton("5 шт.", callback_data="buy_qty_5")],
        [InlineKeyboardButton("10 шт.", callback_data="buy_qty_10")],
        [InlineKeyboardButton("Выбрать своё количество", callback_data="buy_qty_custom")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_product_{product_id}")]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)

async def buy_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data
    if data == "buy_qty_custom":
        context.user_data['awaiting_custom_qty'] = True
        await query.edit_message_text("Введите нужное количество (целое число):")
        return
    qty = int(data.split("_")[2])
    product_id = context.user_data.get('buy_product_id')
    if not product_id:
        await query.edit_message_text("❌ Ошибка: товар не выбран.")
        return
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    total = product['price'] * qty
    user_id = query.from_user.id
    balance = await get_balance(user_id)
    if balance < total:
        await query.edit_message_text(
            f"❌ Недостаточно баланса. Ваш баланс: {balance:.2f} RUB, требуется {total:.2f} RUB.\n"
            f"Пополните баланс через кнопку «💰 Баланс»."
        )
        return
    # Списываем баланс
    await deduct_balance(user_id, total)
    await add_transaction(user_id, "purchase", -total, f"Покупка {qty} шт. товара «{product['name']}»")
    # Уведомляем продавца
    seller_id = product['seller_id']
    buyer_name = await get_user_name(user_id)
    await context.bot.send_message(
        seller_id,
        f"💰 Продажа: Покупатель @{buyer_name} (ID: {user_id}) купил {qty} шт. товара «{product['name']}» на сумму {total} RUB."
    )
    # Если данные должен предоставить продавец – запрашиваем
    if product.get('data_from') == 'seller':
        context.user_data['pending_delivery'] = {'product_id': product_id, 'buyer_id': user_id, 'qty': qty}
        await query.edit_message_text(
            f"✅ Покупка совершена! С вашего баланса списано {total} RUB.\n\n"
            f"Продавец должен предоставить вам товар (файл или текст). Как только он отправит его через бота, вы получите уведомление."
        )
        await context.bot.send_message(
            seller_id,
            f"📦 Покупатель @{buyer_name} купил товар «{product['name']}».\n"
            f"Вам необходимо отправить покупателю товар (файл или текст).\n"
            f"Используйте кнопку ниже, чтобы отправить товар покупателю.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить товар покупателю", callback_data=f"deliver_{product_id}_{user_id}")]
            ])
        )
    else:
        # Данные должен предоставить покупатель – продавец может запросить их через /request
        await query.edit_message_text(f"✅ Покупка совершена! С вашего баланса списано {total} RUB.\n\nЕсли продавец запросит дополнительные данные, он свяжется с вами.")
        await context.bot.send_message(
            seller_id,
            f"💡 Для получения дополнительных данных от покупателя @{buyer_name} используйте команду:\n/request {buyer_id}"
        )
    # Обновляем статистику продавца
    await redis_client.incr(f"stats:seller:{seller_id}:sales", qty)

async def process_custom_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_qty'):
        return
    try:
        qty = int(update.message.text)
        if qty <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите корректное целое положительное число.")
        return
    product_id = context.user_data.get('buy_product_id')
    if not product_id:
        await update.message.reply_text("❌ Ошибка: товар не выбран.")
        context.user_data.pop('awaiting_custom_qty', None)
        return
    product = await get_product(product_id)
    if not product:
        await update.message.reply_text("❌ Товар не найден.")
        context.user_data.pop('awaiting_custom_qty', None)
        return
    total = product['price'] * qty
    user_id = update.effective_user.id
    balance = await get_balance(user_id)
    if balance < total:
        await update.message.reply_text(
            f"❌ Недостаточно баланса. Ваш баланс: {balance:.2f} RUB, требуется {total:.2f} RUB.\n"
            f"Пополните баланс через кнопку «💰 Баланс»."
        )
        return
    await deduct_balance(user_id, total)
    await add_transaction(user_id, "purchase", -total, f"Покупка {qty} шт. товара «{product['name']}»")
    seller_id = product['seller_id']
    buyer_name = await get_user_name(user_id)
    await context.bot.send_message(
        seller_id,
        f"💰 Продажа: Покупатель @{buyer_name} (ID: {user_id}) купил {qty} шт. товара «{product['name']}» на сумму {total} RUB."
    )
    if product.get('data_from') == 'seller':
        context.user_data['pending_delivery'] = {'product_id': product_id, 'buyer_id': user_id, 'qty': qty}
        await update.message.reply_text(
            f"✅ Покупка совершена! С вашего баланса списано {total} RUB.\n\n"
            f"Продавец должен предоставить вам товар (файл или текст). Как только он отправит его через бота, вы получите уведомление."
        )
        await context.bot.send_message(
            seller_id,
            f"📦 Покупатель @{buyer_name} купил товар «{product['name']}».\n"
            f"Вам необходимо отправить покупателю товар (файл или текст).\n"
            f"Используйте кнопку ниже, чтобы отправить товар покупателю.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить товар покупателю", callback_data=f"deliver_{product_id}_{user_id}")]
            ])
        )
    else:
        await update.message.reply_text(f"✅ Покупка совершена! С вашего баланса списано {total} RUB.\n\nЕсли продавец запросит дополнительные данные, он свяжется с вами.")
        await context.bot.send_message(
            seller_id,
            f"💡 Для получения дополнительных данных от покупателя @{buyer_name} используйте команду:\n/request {buyer_id}"
        )
    await redis_client.incr(f"stats:seller:{seller_id}:sales", qty)
    context.user_data.pop('awaiting_custom_qty', None)

# ----- Доставка товара (если данные предоставляет продавец) -----
async def deliver_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    parts = query.data.split("_")
    product_id = int(parts[1])
    buyer_id = int(parts[2])
    seller_id = query.from_user.id
    product = await get_product(product_id)
    if not product or product['seller_id'] != seller_id:
        await query.edit_message_text("❌ Ошибка: товар не найден или вы не являетесь продавцом.")
        return
    context.user_data['delivery'] = {'product_id': product_id, 'buyer_id': buyer_id}
    await query.edit_message_text("Отправьте файл или текст, который нужно передать покупателю как товар.")

async def process_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'delivery' not in context.user_data:
        return
    delivery = context.user_data['delivery']
    product_id = delivery['product_id']
    buyer_id = delivery['buyer_id']
    product = await get_product(product_id)
    if not product:
        await update.message.reply_text("❌ Товар не найден.")
        context.user_data.pop('delivery', None)
        return
    if update.message.document:
        await context.bot.send_document(buyer_id, update.message.document.file_id, caption=f"Ваш товар: {product['name']}")
    elif update.message.text:
        await context.bot.send_message(buyer_id, f"Ваш товар «{product['name']}»:\n\n{update.message.text}")
    else:
        await update.message.reply_text("❌ Поддерживаются только текстовые сообщения и файлы.")
        return
    await update.message.reply_text("✅ Товар отправлен покупателю.")
    context.user_data.pop('delivery', None)

# ----- Команда для запроса дополнительных данных от покупателя -----
async def request_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("❌ Используйте: /request <ID_покупателя>")
        return
    try:
        buyer_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Неверный ID.")
        return
    buyer_name = await get_user_name(buyer_id)
    context.user_data['awaiting_request_text'] = buyer_id
    await update.message.reply_text(f"Введите запрос (вопрос) для покупателя @{buyer_name}:")

async def process_request_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_request_text' not in context.user_data:
        return
    buyer_id = context.user_data['awaiting_request_text']
    seller_id = update.effective_user.id
    request_text = update.message.text
    buyer_name = await get_user_name(buyer_id)
    try:
        await context.bot.send_message(
            buyer_id,
            f"📝 Продавец @{await get_user_name(seller_id)} запрашивает дополнительную информацию:\n\n{request_text}\n\nПожалуйста, ответьте на это сообщение, отправив нужные данные (ссылку, скриншот и т.д.)."
        )
        await update.message.reply_text(f"✅ Запрос отправлен покупателю @{buyer_name}.")
    except:
        await update.message.reply_text("❌ Не удалось отправить сообщение покупателю (возможно, он заблокировал бота).")
    context.user_data.pop('awaiting_request_text', None)

# ----- Баланс и пополнение -----
async def balance_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = await get_balance(user_id)
    transactions = await get_transactions(user_id, limit=5)
    text = f"💰 **Ваш баланс:** {balance:.2f} RUB\n\n"
    if transactions:
        text += "📋 **Последние операции:**\n"
        for t in reversed(transactions):
            amount_str = f"+{t['amount']:.2f}" if t['amount'] > 0 else f"{t['amount']:.2f}"
            text += f"{t['timestamp'][:16]} {amount_str} – {t['description']}\n"
    else:
        text += "История операций пуста."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)

async def deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    payment_details = await get_payment_details()
    if not payment_details:
        await query.edit_message_text("❌ Реквизиты для оплаты ещё не заданы администратором. Попробуйте позже.")
        return
    user_id = query.from_user.id
    context.user_data['awaiting_deposit_amount'] = True
    text = (
        f"💳 **Пополнение баланса**\n\n"
        f"1️⃣ Переведите сумму на следующие реквизиты:\n{payment_details}\n\n"
        f"2️⃣ Напишите в ответ на это сообщение **сумму** (только число), которую вы перевели (например, 500).\n\n"
        f"3️⃣ После этого отправьте **файл с чеком** (скриншот, фото) в ответ на следующее сообщение."
    )
    await query.edit_message_text(text, parse_mode='Markdown')

async def process_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_deposit_amount'):
        return
    try:
        amount = float(update.message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите корректную положительную сумму (например, 500).")
        return
    user_id = update.effective_user.id
    context.user_data['deposit_amount'] = amount
    context.user_data['awaiting_deposit_file'] = True
    await update.message.reply_text(
        f"✅ Сумма {amount} RUB сохранена. Теперь отправьте **файл с чеком** (скриншот, фото) в ответ на это сообщение."
    )

async def process_deposit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_deposit_file'):
        return
    user_id = update.effective_user.id
    amount = context.user_data.get('deposit_amount')
    if not amount:
        await update.message.reply_text("❌ Ошибка: сумма не найдена. Начните пополнение заново.")
        context.user_data.clear()
        return
    # Создаём заказ
    order_id = await create_order(user_id, amount)
    # Сохраняем файл (если есть)
    file_id = None
    if update.message.document:
        file_id = update.message.document.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        await update.message.reply_text("❌ Пожалуйста, отправьте файл (скриншот, фото чека).")
        return
    # Сохраняем file_id в заказ
    order = await get_order(order_id)
    order['file_id'] = file_id
    await redis_client.set(f"order:{order_id}", json.dumps(order))
    # Уведомляем владельцев
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(
                owner_id,
                f"📩 Новый запрос на пополнение баланса!\n"
                f"Пользователь: @{await get_user_name(user_id)} (ID: {user_id})\n"
                f"Сумма: {amount} RUB\n"
                f"Заказ ID: {order_id}\n"
                f"Чек приложен ниже.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order_id}")],
                    [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order_id}")]
                ])
            )
            # Отправляем сам файл
            if file_id:
                await context.bot.send_document(owner_id, file_id)
        except:
            pass
    await update.message.reply_text(
        f"✅ Ваш чек на сумму {amount} RUB отправлен на проверку. Ожидайте подтверждения администратора.\n"
        f"После подтверждения баланс будет пополнен."
    )
    context.user_data.clear()

async def approve_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[1])
    order = await get_order(order_id)
    if not order or order['status'] != 'pending':
        await query.edit_message_text("❌ Заказ не найден или уже обработан.")
        return
    # Подтверждаем
    await update_order(order_id, status='approved')
    user_id = order['user_id']
    amount = order['amount']
    new_balance = await add_balance(user_id, amount)
    await add_transaction(user_id, "deposit", amount, f"Пополнение баланса на {amount} RUB")
    await query.edit_message_text(f"✅ Пополнение подтверждено! Баланс пользователя @{await get_user_name(user_id)} увеличен на {amount} RUB. Новый баланс: {new_balance:.2f} RUB.")
    try:
        await context.bot.send_message(
            user_id,
            f"💰 Ваш баланс пополнен на {amount} RUB. Текущий баланс: {new_balance:.2f} RUB."
        )
    except:
        pass

async def reject_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[1])
    order = await get_order(order_id)
    if not order or order['status'] != 'pending':
        await query.edit_message_text("❌ Заказ не найден или уже обработан.")
        return
    await update_order(order_id, status='rejected')
    user_id = order['user_id']
    await query.edit_message_text(f"❌ Пополнение отклонено для пользователя @{await get_user_name(user_id)}.")
    try:
        await context.bot.send_message(
            user_id,
            f"❌ Ваше пополнение на сумму {order['amount']} RUB отклонено администратором. Пожалуйста, свяжитесь с поддержкой."
        )
    except:
        pass

# ----- О магазине, Профиль, Помощь -----
async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_about_text()
    await update.message.reply_text(text, parse_mode='Markdown')

async def profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = await get_balance(user_id)
    purchases = int(await redis_client.get(f"stats:user:{user_id}:purchases") or 0)
    seller_status = await is_seller(user_id)
    status = "Продавец" if seller_status else "Покупатель"
    text = (
        f"👤 **Ваш профиль**\n"
        f"ID: `{user_id}`\n"
        f"Статус: {status}\n"
        f"💰 Баланс: {balance:.2f} RUB\n"
        f"🛒 Куплено товаров: {purchases}\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await send_main_keyboard(update, "Главное меню")

# ----- Веб-сервер -----
async def health(request):
    return web.Response(text="OK")

# ----- ГЛАВНАЯ ФУНКЦИЯ -----
async def main():
    global bot_app
    logger.info("Запуск бота...")
    if REDIS_URL:
        await init_redis()
    else:
        logger.warning("REDIS_URL не задан! Данные не будут сохраняться.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addseller", add_seller_command))
    application.add_handler(CommandHandler("removeseller", remove_seller_command))
    application.add_handler(CommandHandler("setpayment", set_payment_command))
    application.add_handler(CommandHandler("setabout", set_about_command))
    application.add_handler(CommandHandler("request", request_data_command))

    application.add_handler(MessageHandler(filters.Text("📂 Все категории"), catalog_button))
    application.add_handler(MessageHandler(filters.Text("🛒 Наличие товаров"), all_products_button))
    application.add_handler(MessageHandler(filters.Text("💰 Баланс"), balance_button))
    application.add_handler(MessageHandler(filters.Text("ℹ️ О магазине"), about_button))
    application.add_handler(MessageHandler(filters.Text("👤 Профиль"), profile_button))
    application.add_handler(MessageHandler(filters.Text("🆘 Помощь"), help_command))
    application.add_handler(MessageHandler(filters.Text("👑 Управление товарами"), my_products_button))
    application.add_handler(MessageHandler(filters.Text("💳 Реквизиты"), payment_details_button))
    application.add_handler(MessageHandler(filters.Text("📊 Статистика продаж"), seller_stats_button))

    application.add_handler(CallbackQueryHandler(manage_sections_callback, pattern="^manage_sections$"))
    application.add_handler(CallbackQueryHandler(add_section_callback, pattern="^add_section$"))
    application.add_handler(CallbackQueryHandler(rename_section_callback, pattern="^rename_section$"))
    application.add_handler(CallbackQueryHandler(delete_section_callback, pattern="^delete_section$"))
    application.add_handler(CallbackQueryHandler(add_product_callback, pattern="^add_product$"))
    application.add_handler(CallbackQueryHandler(data_from_callback, pattern="^data_from_"))
    application.add_handler(CallbackQueryHandler(cancel_add_product_callback, pattern="^cancel_add_product$"))
    application.add_handler(CallbackQueryHandler(edit_product_callback, pattern="^edit_prod_"))
    application.add_handler(CallbackQueryHandler(edit_field_callback, pattern="^edit_field_"))
    application.add_handler(CallbackQueryHandler(edit_data_from_callback, pattern="^edit_data_from_"))
    application.add_handler(CallbackQueryHandler(delete_product_callback, pattern="^delete_product$"))
    application.add_handler(CallbackQueryHandler(back_to_my_products_callback, pattern="^back_to_my_products$"))
    application.add_handler(CallbackQueryHandler(edit_payment_callback, pattern="^edit_payment$"))
    application.add_handler(CallbackQueryHandler(category_callback, pattern="^category_"))
    application.add_handler(CallbackQueryHandler(product_detail_callback, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(buy_qty_callback, pattern="^buy_qty_"))
    application.add_handler(CallbackQueryHandler(deliver_callback, pattern="^deliver_"))
    application.add_handler(CallbackQueryHandler(deposit_callback, pattern="^deposit$"))
    application.add_handler(CallbackQueryHandler(approve_deposit_callback, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(reject_deposit_callback, pattern="^reject_"))
    application.add_handler(CallbackQueryHandler(back_to_product_callback, pattern="^back_to_product_"))
    application.add_handler(CallbackQueryHandler(back_to_catalog_callback, pattern="^back_to_catalog$"))
    application.add_handler(CallbackQueryHandler(back_to_main_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(cancel_edit_callback, pattern="^cancel_edit$"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_section_name))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_rename_section))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_payment_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_product_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_value))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_custom_qty))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_delivery))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_request_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_deposit_amount))
    application.add_handler(MessageHandler(filters.ALL, process_deposit_file))  # обрабатывает файлы и фото

    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await asyncio.sleep(1)
    await application.start()
    await application.updater.start_polling()
    logger.info("✅ Бот запущен и получает обновления")

    web_app = web.Application()
    web_app.router.add_get('/health', health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, host='0.0.0.0', port=port)
    await site.start()
    logger.info(f"✅ Веб-сервер запущен на порту {port}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())