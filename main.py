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

DEFAULT_ABOUT_TEXT = "🛍 Добро пожаловать в наш маркетплейс! Здесь вы можете купить различные товары у наших продавцов."

HELP_TEXT = """
По вопросам: @karatitik, @Pahachill
"""

PRODUCTS_PER_PAGE = 5

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

# ----- Разделы (категории) -----
async def get_seller_sections(seller_id):
    return await redis_client.smembers(f"seller:{seller_id}:sections")

async def add_seller_section(seller_id, section_name):
    await redis_client.sadd(f"seller:{seller_id}:sections", section_name)

async def remove_seller_section(seller_id, section_name):
    await redis_client.srem(f"seller:{seller_id}:sections", section_name)
    # Удаляем товары этого раздела
    product_ids = await redis_client.smembers(f"seller:{seller_id}:products")
    for pid in product_ids:
        prod = await get_product(int(pid))
        if prod and prod.get('section') == section_name:
            await delete_product(int(pid))

# ----- Товары (с привязкой к разделу) -----
async def add_product(seller_id, name, price, description, section):
    product_id = await redis_client.incr("global:product_id")
    product = {
        "id": product_id,
        "seller_id": seller_id,
        "section": section,
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

async def get_products_by_section(seller_id, section):
    products = await get_seller_products(seller_id)
    return [p for p in products if p.get('section') == section]

# ----- Статистика продаж -----
async def record_purchase(product_id, buyer_id):
    product = await get_product(product_id)
    if not product:
        return False
    await redis_client.incr(f"stats:product:{product_id}:sales")
    await redis_client.incr(f"stats:seller:{product['seller_id']}:sales")
    await redis_client.incr(f"stats:user:{buyer_id}:purchases")
    return True

async def get_seller_stats(seller_id):
    sales = int(await redis_client.get(f"stats:seller:{seller_id}:sales") or 0)
    return sales

async def get_user_stats(user_id):
    purchases = int(await redis_client.get(f"stats:user:{user_id}:purchases") or 0)
    return purchases

# ----- Главные клавиатуры -----
async def send_main_keyboard(update: Update, text: str):
    user_id = update.effective_user.id
    seller_status = await is_seller(user_id)
    if seller_status:
        # Клавиатура для продавца
        keyboard = [
            [KeyboardButton("📂 Все категории")],
            [KeyboardButton("🛒 Наличие товаров")],
            [KeyboardButton("👑 Управление товарами"), KeyboardButton("💳 Реквизиты")],
            [KeyboardButton("📊 Статистика продаж")],
            [KeyboardButton("ℹ️ О магазине"), KeyboardButton("👤 Профиль"), KeyboardButton("🆘 Помощь")]
        ]
    else:
        # Клавиатура для обычного пользователя (по скриншоту)
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

# ----- Команды -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"start от {update.effective_user.id}")
    await send_main_keyboard(update, "🛍 Добро пожаловать в маркетплейс!\nИспользуйте кнопки для навигации.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

# ----- Админ-команды (только владельцы) -----
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
        await update.message.reply_text(f"✅ Пользователь {seller_id} добавлен как продавец (блогер).")
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
        [InlineKeyboardButton("📂 Управление разделами", callback_data="manage_sections")],
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
    await query.answer()
    user_id = query.from_user.id
    sections = await get_seller_sections(user_id)
    if not sections:
        text = "У вас пока нет разделов. Создайте первый раздел через кнопку ниже."
        keyboard = [[InlineKeyboardButton("➕ Создать раздел", callback_data="add_section")]]
    else:
        text = "📂 **Ваши разделы:**\nВыберите раздел для просмотра товаров или создайте новый."
        keyboard = []
        for sec in sections:
            keyboard.append([InlineKeyboardButton(f"📁 {sec}", callback_data=f"view_section_{sec}")])
        keyboard.append([InlineKeyboardButton("➕ Создать раздел", callback_data="add_section")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_products")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def add_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_section_name'] = True
    await query.edit_message_text("Введите **название** нового раздела (например, «Cookie», «Soft»):", parse_mode='Markdown')

async def process_section_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_section_name'):
        return
    user_id = update.effective_user.id
    section_name = update.message.text.strip()
    if not section_name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    await add_seller_section(user_id, section_name)
    await update.message.reply_text(f"✅ Раздел «{section_name}» создан.")
    context.user_data.pop('awaiting_section_name', None)
    await manage_sections_callback(update, context)

async def view_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    section = query.data.split("_", 2)[2]
    user_id = query.from_user.id
    products = await get_products_by_section(user_id, section)
    if not products:
        text = f"В разделе «{section}» пока нет товаров."
        keyboard = [[InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")]]
    else:
        text = f"🏷 **Товары раздела «{section}»:**\n"
        keyboard = []
        for p in products:
            text += f"• {p['name']} – {p['price']} RUB\n"
            keyboard.append([InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f"edit_prod_{p['id']}")])
        keyboard.append([InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")])
    keyboard.append([InlineKeyboardButton("🔙 К разделам", callback_data="manage_sections")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sections = await get_seller_sections(user_id)
    if not sections:
        await query.edit_message_text("❌ Сначала создайте хотя бы один раздел через «Управление разделами».")
        return
    context.user_data['awaiting_product'] = 'section'
    keyboard = []
    for sec in sections:
        keyboard.append([InlineKeyboardButton(sec, callback_data=f"product_section_{sec}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_my_products")])
    await query.edit_message_text("Выберите **раздел**, в который добавить товар:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def product_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    section = query.data.split("_", 2)[2]
    context.user_data['product_section'] = section
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
        section = context.user_data.get('product_section')
        if not section:
            await update.message.reply_text("❌ Ошибка: не выбран раздел. Начните заново.")
            context.user_data.clear()
            return
        product_id = await add_product(user_id, name, price, desc, section)
        await update.message.reply_text(f"✅ Товар «{name}» добавлен в раздел «{section}». ID: {product_id}")
        context.user_data.clear()
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
        [InlineKeyboardButton("📂 Раздел", callback_data="edit_field_section")],
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
    elif field == 'section':
        user_id = query.from_user.id
        sections = await get_seller_sections(user_id)
        if not sections:
            await query.edit_message_text("❌ У вас нет разделов. Создайте их через «Управление разделами».")
            return
        keyboard = []
        for sec in sections:
            keyboard.append([InlineKeyboardButton(sec, callback_data=f"change_section_{sec}")])
        keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="cancel_edit")])
        await query.edit_message_text("Выберите новый раздел:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['awaiting_section_change'] = True
    else:
        context.user_data['awaiting_edit_value'] = True

async def change_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_section = query.data.split("_", 2)[2]
    product_id = context.user_data.get('edit_product_id')
    if product_id:
        await update_product(product_id, section=new_section)
        await query.edit_message_text(f"✅ Раздел изменён на «{new_section}».")
    else:
        await query.edit_message_text("❌ Ошибка.")
    context.user_data.pop('awaiting_section_change', None)
    context.user_data.pop('edit_field', None)
    await edit_product_callback(update, context)

async def process_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_section_change'):
        return
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

# ----- Реквизиты продавца -----
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
    await query.answer()
    context.user_data['awaiting_payment'] = True
    await query.edit_message_text("Введите новые платёжные реквизиты (например, номер карты, кошелёк и т.д.):")

async def process_payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_payment'):
        return
    user_id = update.effective_user.id
    details = update.message.text.strip()
    await set_payment_details(user_id, details)
    await update.message.reply_text("✅ Реквизиты сохранены.")
    context.user_data.pop('awaiting_payment', None)
    await payment_details_button(update, context)

# ----- Статистика продавца -----
async def seller_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    sales = await get_seller_stats(user_id)
    await update.message.reply_text(f"📊 **Ваша статистика продаж**\nПродано товаров: {sales}", parse_mode='Markdown')

# ----- Каталог: все категории (разделы всех продавцов) -----
async def catalog_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await get_all_products()
    if not products:
        await update.message.reply_text("📂 Пока нет категорий.")
        return
    # Группируем по (seller_id, section)
    sections_map = {}
    for p in products:
        key = (p['seller_id'], p['section'])
        if key not in sections_map:
            sections_map[key] = {
                'seller_id': p['seller_id'],
                'section': p['section']
            }
    sections_list = list(sections_map.values())
    if not sections_list:
        await update.message.reply_text("📂 Пока нет категорий.")
        return
    page = context.user_data.get('catalog_page', 0)
    total = len(sections_list)
    start = page * PRODUCTS_PER_PAGE
    end = start + PRODUCTS_PER_PAGE
    page_sections = sections_list[start:end]
    text = "📂 **Все категории товаров:**\n\n"
    keyboard = []
    for sec in page_sections:
        text += f"📁 **{sec['section']}** (продавец: `{sec['seller_id']}`)\n"
        keyboard.append([InlineKeyboardButton(f"🔍 {sec['section']}", callback_data=f"catalog_section_{sec['seller_id']}_{sec['section']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data="catalog_prev"))
    if end < total:
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data="catalog_next"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def catalog_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    seller_id = int(parts[2])
    section = '_'.join(parts[3:])
    products = await get_products_by_section(seller_id, section)
    if not products:
        await query.edit_message_text(f"В разделе «{section}» пока нет товаров.")
        return
    text = f"🛍 **Товары в разделе «{section}» (продавец: {seller_id}):**\n\n"
    keyboard = []
    for p in products:
        text += f"• **{p['name']}** – {p['price']} RUB\n"
        keyboard.append([InlineKeyboardButton(f"🔍 {p['name']}", callback_data=f"product_detail_{p['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 К категориям", callback_data="back_to_catalog")])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    text = (
        f"🧾 **{product['name']}**\n"
        f"💰 Цена: {product['price']} RUB\n"
        f"📦 Раздел: {product['section']}\n\n"
        f"📝 Описание:\n{product['description']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Купить", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_section_{product['seller_id']}_{product['section']}")]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)

async def back_to_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    seller_id = int(parts[3])
    section = '_'.join(parts[4:])
    await catalog_section_callback(update, context)

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
    await catalog_button(update, context)

# ----- Наличие товаров (все товары всех продавцов) -----
async def all_products_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = await get_all_products()
    if not products:
        await update.message.reply_text("🛒 Пока нет товаров.")
        return
    # Пагинация по товарам
    page = context.user_data.get('all_products_page', 0)
    total = len(products)
    start = page * PRODUCTS_PER_PAGE
    end = start + PRODUCTS_PER_PAGE
    page_products = products[start:end]
    text = "🛒 **Все товары:**\n\n"
    keyboard = []
    for p in page_products:
        text += f"• **{p['name']}** – {p['price']} RUB (раздел: {p['section']}, продавец: {p['seller_id']})\n"
        keyboard.append([InlineKeyboardButton(f"🔍 {p['name']}", callback_data=f"product_detail_{p['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data="all_products_prev"))
    if end < total:
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data="all_products_next"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def all_products_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    page = context.user_data.get('all_products_page', 0)
    if data == "all_products_prev":
        page -= 1
    elif data == "all_products_next":
        page += 1
    context.user_data['all_products_page'] = page
    await all_products_button(update, context)

# ----- О магазине -----
async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_about_text()
    await update.message.reply_text(text, parse_mode='Markdown')

# ----- Профиль пользователя -----
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

# ----- Покупка -----
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[1])
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    payment_details = await get_payment_details(product['seller_id'])
    if not payment_details:
        payment_details = "реквизиты не указаны. Свяжитесь с продавцом через поддержку."
    text = (
        f"🧾 **Оплата товара «{product['name']}»**\n"
        f"Сумма: {product['price']} RUB\n\n"
        f"💰 **Реквизиты продавца:**\n{payment_details}\n\n"
        f"После перевода нажмите кнопку «Я оплатил» и сообщите продавцу об оплате."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_payment_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_product_{product_id}")]
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
    await record_purchase(product_id, buyer_id)
    try:
        await context.bot.send_message(
            seller_id,
            f"💰 Пользователь {buyer_id} сообщил об оплате товара «{product['name']}».\n"
            f"Сумма: {product['price']} RUB.\n"
            f"Проверьте поступление средств и свяжитесь с покупателем."
        )
    except:
        pass
    await query.edit_message_text("✅ Спасибо! Уведомление отправлено продавцу. Он свяжется с вами.")

async def back_to_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[2])
    await product_detail_callback(update, context)

async def back_to_catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['catalog_page'] = 0
    await catalog_button(update, context)

async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await send_main_keyboard(update, "Главное меню")

# ----- Веб-сервер -----
async def health(request):
    return web.Response(text="OK")

# ----- ГЛАВНАЯ ФУНКЦИЯ -----
async def main():
    logger.info("Запуск бота...")
    if REDIS_URL:
        await init_redis()
    else:
        logger.warning("REDIS_URL не задан! Данные не будут сохраняться.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addseller", add_seller_command))
    application.add_handler(CommandHandler("removeseller", remove_seller_command))
    application.add_handler(CommandHandler("setabout", set_about_command))

    # Кнопки главного меню (текстовые)
    application.add_handler(MessageHandler(filters.Text("📂 Все категории"), catalog_button))
    application.add_handler(MessageHandler(filters.Text("🛒 Наличие товаров"), all_products_button))
    application.add_handler(MessageHandler(filters.Text("ℹ️ О магазине"), about_button))
    application.add_handler(MessageHandler(filters.Text("👤 Профиль"), profile_button))
    application.add_handler(MessageHandler(filters.Text("🆘 Помощь"), help_command))
    # Кнопки для продавцов
    application.add_handler(MessageHandler(filters.Text("👑 Управление товарами"), my_products_button))
    application.add_handler(MessageHandler(filters.Text("💳 Реквизиты"), payment_details_button))
    application.add_handler(MessageHandler(filters.Text("📊 Статистика продаж"), seller_stats_button))

    # Колбэки
    application.add_handler(CallbackQueryHandler(manage_sections_callback, pattern="^manage_sections$"))
    application.add_handler(CallbackQueryHandler(add_section_callback, pattern="^add_section$"))
    application.add_handler(CallbackQueryHandler(view_section_callback, pattern="^view_section_"))
    application.add_handler(CallbackQueryHandler(product_section_callback, pattern="^product_section_"))
    application.add_handler(CallbackQueryHandler(add_product_callback, pattern="^add_product$"))
    application.add_handler(CallbackQueryHandler(edit_product_callback, pattern="^edit_prod_"))
    application.add_handler(CallbackQueryHandler(edit_field_callback, pattern="^edit_field_"))
    application.add_handler(CallbackQueryHandler(change_section_callback, pattern="^change_section_"))
    application.add_handler(CallbackQueryHandler(delete_product_callback, pattern="^delete_product$"))
    application.add_handler(CallbackQueryHandler(back_to_my_products_callback, pattern="^back_to_my_products$"))
    application.add_handler(CallbackQueryHandler(edit_payment_callback, pattern="^edit_payment$"))
    application.add_handler(CallbackQueryHandler(catalog_section_callback, pattern="^catalog_section_"))
    application.add_handler(CallbackQueryHandler(product_detail_callback, pattern="^product_detail_"))
    application.add_handler(CallbackQueryHandler(back_to_section_callback, pattern="^back_to_section_"))
    application.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(confirm_payment_callback, pattern="^confirm_payment_"))
    application.add_handler(CallbackQueryHandler(back_to_product_callback, pattern="^back_to_product_"))
    application.add_handler(CallbackQueryHandler(back_to_catalog_callback, pattern="^back_to_catalog$"))
    application.add_handler(CallbackQueryHandler(catalog_nav_callback, pattern="^catalog_(prev|next)$"))
    application.add_handler(CallbackQueryHandler(all_products_nav_callback, pattern="^all_products_(prev|next)$"))
    application.add_handler(CallbackQueryHandler(back_to_main_callback, pattern="^back_to_main$"))

    # Обработка текстового ввода
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_section_name))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_payment_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_product_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_value))

    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await asyncio.sleep(1)
    await application.start()
    await application.updater.start_polling()
    logger.info("✅ Бот запущен и получает обновления")

    # Веб-сервер
    app = web.Application()
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, host='0.0.0.0', port=port)
    await site.start()
    logger.info(f"✅ Веб-сервер запущен на порту {port}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())