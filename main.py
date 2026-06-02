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

async def get_payment_details(seller_id):
    return await redis_client.get(f"payment:{seller_id}")

async def set_payment_details(seller_id, text):
    await redis_client.set(f"payment:{seller_id}", text)

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
        "data_from": data_from,
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

# ----- Статистика -----
async def record_purchase(product_id, buyer_id, quantity=1):
    product = await get_product(product_id)
    if not product:
        return False
    await redis_client.incr(f"stats:product:{product_id}:sales", quantity)
    await redis_client.incr(f"stats:seller:{product['seller_id']}:sales", quantity)
    await redis_client.incr(f"stats:user:{buyer_id}:purchases", quantity)
    return True

async def get_seller_stats(seller_id):
    sales = int(await redis_client.get(f"stats:seller:{seller_id}:sales") or 0)
    return sales

async def get_user_stats(user_id):
    purchases = int(await redis_client.get(f"stats:user:{user_id}:purchases") or 0)
    return purchases

# ----- Вспомогательные -----
async def get_user_name(user_id):
    try:
        user = await bot_app.bot.get_chat(user_id)
        return user.username or str(user_id)
    except:
        return str(user_id)

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
            [KeyboardButton("ℹ️ О магазине"), KeyboardButton("👤 Профиль"), KeyboardButton("🆘 Помощь")]
        ]
    else:
        keyboard = [
            [KeyboardButton("📂 Все категории")],
            [KeyboardButton("🛒 Наличие товаров")],
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

# ----- Управление товарами продавца -----
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

async def manage_sections_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    section = await get_seller_section(user_id)
    if not section:
        text = "У вас пока нет раздела. Создайте раздел:"
        keyboard = [[InlineKeyboardButton("➕ Создать раздел", callback_data="add_section")]]
    else:
        text = f"📂 **Ваш раздел:** `{section}`\n\nВы можете изменить название или удалить раздел (все товары внутри будут удалены)."
        keyboard = [
            [InlineKeyboardButton("✏️ Изменить название", callback_data="rename_section")],
            [InlineKeyboardButton("❌ Удалить раздел", callback_data="delete_section")],
        ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_products")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def add_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data['awaiting_section_name'] = True
    await query.edit_message_text("Введите **название** нового раздела:")

async def process_section_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_section_name'):
        return
    user_id = update.effective_user.id
    section_name = update.message.text.strip()
    if not section_name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    await set_seller_section(user_id, section_name)
    await update.message.reply_text(f"✅ Раздел «{section_name}» создан.")
    context.user_data.pop('awaiting_section_name', None)
    await my_products_button(update, context)

async def rename_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data['awaiting_rename_section'] = True
    await query.edit_message_text("Введите **новое название** раздела:")

async def process_rename_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_rename_section'):
        return
    user_id = update.effective_user.id
    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    old_section = await get_seller_section(user_id)
    if not old_section:
        await update.message.reply_text("❌ Раздел не найден.")
        context.user_data.pop('awaiting_rename_section', None)
        return
    await rename_seller_section(user_id, new_name)
    await update.message.reply_text(f"✅ Раздел переименован из «{old_section}» в «{new_name}».")
    context.user_data.pop('awaiting_rename_section', None)
    await my_products_button(update, context)

async def delete_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    section = await get_seller_section(user_id)
    if not section:
        await query.edit_message_text("❌ Раздел не найден.")
        return
    await delete_seller_section(user_id)
    await query.edit_message_text(f"✅ Раздел «{section}» удалён вместе со всеми товарами.")
    await my_products_button(update, context)

async def add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    section = await get_seller_section(user_id)
    if not section:
        await query.edit_message_text("❌ Сначала создайте раздел через «Управление разделом».")
        return
    context.user_data['product_section'] = section
    context.user_data['awaiting_product'] = 'name'
    await query.edit_message_text("Введите **название** товара:")

async def process_product_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    state = context.user_data.get('awaiting_product')
    if state == 'name':
        context.user_data['product_name'] = update.message.text
        context.user_data['awaiting_product'] = 'price'
        await update.message.reply_text("Введите **цену** товара (только число):")
    elif state == 'price':
        try:
            price = int(update.message.text)
            context.user_data['product_price'] = price
            context.user_data['awaiting_product'] = 'quantity'
            await update.message.reply_text("Введите **количество** (например, «1 шт.», «3 шт.», «∞»):")
        except:
            await update.message.reply_text("❌ Цена должна быть числом. Попробуйте ещё раз.")
    elif state == 'quantity':
        quantity = update.message.text
        context.user_data['product_quantity'] = quantity
        context.user_data['awaiting_product'] = 'data_from'
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Покупатель должен предоставить данные", callback_data="data_from_buyer")],
            [InlineKeyboardButton("Продавец должен предоставить данные", callback_data="data_from_seller")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_product")]
        ])
        await update.message.reply_text("Кто должен предоставить дополнительные данные?", reply_markup=keyboard)
    elif state == 'desc':
        name = context.user_data['product_name']
        price = context.user_data['product_price']
        quantity = context.user_data['product_quantity']
        data_from = context.user_data.get('product_data_from', 'buyer')
        desc = update.message.text
        section = context.user_data.get('product_section')
        if not section:
            await update.message.reply_text("❌ Ошибка: не выбран раздел. Начните заново.")
            context.user_data.clear()
            return
        product_id = await add_product(user_id, name, price, desc, section, quantity, data_from)
        await update.message.reply_text(f"✅ Товар «{name}» добавлен в раздел «{section}». ID: {product_id}")
        context.user_data.clear()
        await my_products_button(update, context)

async def data_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data_from = query.data.split("_")[2]  # buyer или seller
    context.user_data['product_data_from'] = data_from
    context.user_data['awaiting_product'] = 'desc'
    await query.edit_message_text("Введите **описание** товара:")

async def cancel_add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Добавление товара отменено.")
    await my_products_button(update, context)

# ----- Редактирование товара -----
async def edit_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
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
        [InlineKeyboardButton("📦 Количество", callback_data="edit_field_quantity")],
        [InlineKeyboardButton("📝 Описание", callback_data="edit_field_desc")],
        [InlineKeyboardButton("👥 Кто предоставляет данные", callback_data="edit_field_data_from")],
        [InlineKeyboardButton("❌ Удалить товар", callback_data="delete_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_products")]
    ])
    await query.edit_message_text(f"Редактирование товара **{product['name']}**", parse_mode='Markdown', reply_markup=keyboard)

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    field = query.data.split("_")[2]
    context.user_data['edit_field'] = field
    if field == 'name':
        await query.edit_message_text("Введите новое название:")
    elif field == 'price':
        await query.edit_message_text("Введите новую цену (число):")
    elif field == 'quantity':
        await query.edit_message_text("Введите новое количество (например, «1 шт.», «3 шт.», «∞»):")
    elif field == 'desc':
        await query.edit_message_text("Введите новое описание:")
    elif field == 'data_from':
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Покупатель должен предоставить данные", callback_data="edit_data_from_buyer")],
            [InlineKeyboardButton("Продавец должен предоставить данные", callback_data="edit_data_from_seller")],
            [InlineKeyboardButton("🔙 Отмена", callback_data="cancel_edit")]
        ])
        await query.edit_message_text("Выберите, кто должен предоставить дополнительные данные:", reply_markup=keyboard)
    else:
        context.user_data['awaiting_edit_value'] = True

async def edit_data_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data_from = query.data.split("_")[3]  # buyer или seller
    product_id = context.user_data.get('edit_product_id')
    if product_id:
        await update_product(product_id, data_from=data_from)
        await query.edit_message_text(f"✅ Настройка данных изменена: {'покупатель' if data_from == 'buyer' else 'продавец'} предоставляет данные.")
    else:
        await query.edit_message_text("❌ Ошибка.")
    await edit_product_callback(update, context)

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
    elif field == 'quantity':
        await update_product(product_id, quantity=new_value)
        await update.message.reply_text(f"✅ Количество изменено на {new_value}.")
    elif field == 'desc':
        await update_product(product_id, description=new_value)
        await update.message.reply_text("✅ Описание обновлено.")
    context.user_data.pop('awaiting_edit_value', None)
    context.user_data.pop('edit_field', None)
    await edit_product_callback(update, context)

async def delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
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
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await my_products_button(update, context)

# ----- Реквизиты -----
async def payment_details_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    current = await get_payment_details(user_id)
    if current:
        text = f"💳 **Ваши реквизиты:**\n{current}"
    else:
        text = "💳 **Реквизиты не установлены.**"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить реквизиты", callback_data="edit_payment")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)

async def edit_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data['awaiting_payment'] = True
    await query.edit_message_text("Введите новые платёжные реквизиты:")

async def process_payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_payment'):
        return
    user_id = update.effective_user.id
    details = update.message.text.strip()
    await set_payment_details(user_id, details)
    await update.message.reply_text("✅ Реквизиты сохранены.")
    context.user_data.pop('awaiting_payment', None)
    await payment_details_button(update, context)

# ----- Статистика -----
async def seller_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    sales = await get_seller_stats(user_id)
    await update.message.reply_text(f"📊 **Ваша статистика продаж**\nПродано товаров: {sales}", parse_mode='Markdown')

# ----- Каталог -----
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

# ----- Наличие товаров -----
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
        f"💰 Цена: {product['price']} ₽\n"
        f"📝 Описание: {product['description']}\n\n"
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
    payment_details = await get_payment_details(product['seller_id'])
    if not payment_details:
        payment_details = "реквизиты не указаны. Свяжитесь с продавцом через поддержку."
    total = product['price'] * qty
    text = (
        f"🧾 **Оплата товара «{product['name']}»**\n"
        f"Количество: {qty} шт.\n"
        f"Сумма: {total} RUB\n\n"
        f"💰 **Реквизиты продавца:**\n{payment_details}\n\n"
        f"После перевода нажмите кнопку «Я оплатил»."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_payment_{product_id}_{qty}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"product_{product_id}")]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)

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
    payment_details = await get_payment_details(product['seller_id'])
    if not payment_details:
        payment_details = "реквизиты не указаны. Свяжитесь с продавцом через поддержку."
    total = product['price'] * qty
    text = (
        f"🧾 **Оплата товара «{product['name']}»**\n"
        f"Количество: {qty} шт.\n"
        f"Сумма: {total} RUB\n\n"
        f"💰 **Реквизиты продавца:**\n{payment_details}\n\n"
        f"После перевода нажмите кнопку «Я оплатил»."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_payment_{product_id}_{qty}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"product_{product_id}")]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)
    context.user_data.pop('awaiting_custom_qty', None)

async def confirm_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    parts = query.data.split("_")
    product_id = int(parts[2])
    qty = int(parts[3])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    buyer_id = query.from_user.id
    seller_id = product['seller_id']
    buyer_name = await get_user_name(buyer_id)
    await record_purchase(product_id, buyer_id, qty)

    await context.bot.send_message(
        seller_id,
        f"💰 Покупатель @{buyer_name} (ID: {buyer_id}) сообщил об оплате товара «{product['name']}».\n"
        f"Количество: {qty} шт.\nСумма: {product['price'] * qty} RUB.\n"
        f"Проверьте поступление средств."
    )

    if product.get('data_from') == 'seller':
        context.user_data['pending_delivery'] = {'product_id': product_id, 'buyer_id': buyer_id, 'qty': qty}
        await query.edit_message_text(
            f"✅ Спасибо! Уведомление отправлено продавцу.\n\n"
            f"Продавец должен предоставить вам товар (файл или текст). Как только он отправит его через бота, вы получите уведомление."
        )
        await context.bot.send_message(
            seller_id,
            f"📦 Покупатель @{buyer_name} оплатил товар «{product['name']}».\n"
            f"Вам необходимо отправить покупателю товар (файл или текст).\n"
            f"Используйте кнопку ниже, чтобы отправить товар покупателю.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить товар покупателю", callback_data=f"deliver_{product_id}_{buyer_id}")]
            ])
        )
    else:
        await query.edit_message_text("✅ Спасибо! Уведомление отправлено продавцу. Если продавец запросит дополнительные данные, он свяжется с вами.")
        await context.bot.send_message(
            seller_id,
            f"💡 Для получения дополнительных данных от покупателя @{buyer_name} используйте команду:\n/request {buyer_id}"
        )

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
    await update.message.reply_text(f"Введите запрос (вопрос) для покупателя @{buyer_name} (например, отправьте ссылку на профиль Steam):")

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

# ----- О магазине, Профиль -----
async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_about_text()
    await update.message.reply_text(text, parse_mode='Markdown')

async def profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    purchases = await get_user_stats(user_id)
    seller_status = await is_seller(user_id)
    status = "Продавец" if seller_status else "Покупатель"
    text = (
        f"👤 **Ваш профиль**\n"
        f"ID: `{user_id}`\n"
        f"Статус: {status}\n"
        f"Куплено товаров: {purchases}\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await send_main_keyboard(update, "Главное меню")

async def back_to_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    product_id = int(query.data.split("_")[3])
    await product_detail_callback(update, context)

async def back_to_catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await catalog_button(update, context)

async def cancel_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await edit_product_callback(update, context)

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
    application.add_handler(CommandHandler("setabout", set_about_command))
    application.add_handler(CommandHandler("request", request_data_command))

    application.add_handler(MessageHandler(filters.Text("📂 Все категории"), catalog_button))
    application.add_handler(MessageHandler(filters.Text("🛒 Наличие товаров"), all_products_button))
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
    application.add_handler(CallbackQueryHandler(confirm_payment_callback, pattern="^confirm_payment_"))
    application.add_handler(CallbackQueryHandler(deliver_callback, pattern="^deliver_"))
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