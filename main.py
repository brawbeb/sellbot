import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
import redis.asyncio as redis

# ================== НАСТРОЙКА ЛОГИРОВАНИЯ ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
# ===========================================================

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

# ================== REDIS ==================
async def init_redis():
    global redis_client
    for attempt in range(5):
        try:
            redis_client = redis.from_url(
                REDIS_URL,
                max_connections=30,
                socket_timeout=10,
                socket_connect_timeout=10,
                retry_on_timeout=True,
                decode_responses=True
            )
            await redis_client.ping()
            try:
                await redis_client.execute_command("CLIENT KILL TYPE normal")
                print("🧹 Старые клиенты Redis отключены при старте")
            except:
                pass
            print("✅ Redis подключён")
            return
        except Exception as e:
            print(f"Попытка {attempt+1} подключения к Redis не удалась: {e}")
            if attempt < 4:
                await asyncio.sleep(3)
            else:
                raise

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
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

# ================== РАЗДЕЛЫ ==================
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

# ================== ТОВАРЫ ==================
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
    logger.info(f"✅ Товар добавлен: id={product_id}, name={name}, section={section}, seller={seller_id}")
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

# ================== ЗАКАЗЫ И ОПЛАТА ==================
async def create_order(user_id, product_id, quantity, total_price):
    order_id = await redis_client.incr("global:order_id")
    order = {
        "id": order_id,
        "user_id": user_id,
        "product_id": product_id,
        "quantity": quantity,
        "total_price": total_price,
        "status": "pending",
        "created": datetime.now().isoformat(),
        "expires": (datetime.now() + timedelta(minutes=30)).isoformat()
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

# ================== ВСПОМОГАТЕЛЬНЫЕ ДЛЯ БОТА ==================
async def get_user_name(user_id):
    try:
        user = await bot_app.bot.get_chat(user_id)
        return user.username or str(user_id)
    except:
        return str(user_id)

async def send_main_keyboard(update: Update, text: str):
    user_id = update.effective_user.id
    seller_status = await is_seller(user_id)
    if seller_status:
        keyboard = [
            [KeyboardButton("📂 Все категории")],
            [KeyboardButton("🛒 Наличие товаров")],
            [KeyboardButton("👑 Управление товарами")],
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

# ================== КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"start от {update.effective_user.id}")
    await send_main_keyboard(update, "🛍 Добро пожаловать в маркетплейс!\nИспользуйте кнопки для навигации.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

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
        await update.message.reply_text("❌ Укажите номер карты. Пример: /setpayment 2202208177548824")
        return
    card_number = context.args[0].strip()
    await set_payment_details(card_number)
    await update.message.reply_text(f"✅ Номер карты для оплаты обновлён: {card_number}")

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

# ================== УПРАВЛЕНИЕ ТОВАРАМИ ==================
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
        "🏷 Управление товарами\nВыберите действие:",
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
        text = f"📂 Ваш раздел: `{section}`\n\nВы можете изменить название или удалить раздел (все товары внутри будут удалены)."
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
    await query.edit_message_text("Введите название нового раздела:")

async def rename_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data['awaiting_rename_section'] = True
    await query.edit_message_text("Введите новое название раздела:")

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
    keyboard = [
        [InlineKeyboardButton("📂 Управление разделом", callback_data="manage_sections")],
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        "🏷 Управление товарами\nВыберите действие:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    logger.info(f"add_product_callback вызвана пользователем {query.from_user.id}")
    user_id = query.from_user.id
    section = await get_seller_section(user_id)
    if not section:
        await query.edit_message_text("❌ Сначала создайте раздел через «Управление разделом».")
        return
    context.user_data['product_section'] = section
    context.user_data['awaiting_product'] = 'name'
    logger.info(f"Установлен awaiting_product='name' для пользователя {user_id}")
    await query.edit_message_text("Введите название товара:")

async def data_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data_from = query.data.split("_")[2]
    logger.info(f"data_from_callback: выбрано {data_from}")
    context.user_data['product_data_from'] = data_from
    context.user_data['awaiting_product'] = 'desc'
    await query.edit_message_text("Введите описание товара:")

async def cancel_add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Добавление товара отменено.")
    keyboard = [
        [InlineKeyboardButton("📂 Управление разделом", callback_data="manage_sections")],
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        "🏷 Управление товарами\nВыберите действие:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
    data_from = query.data.split("_")[3]
    product_id = context.user_data.get('edit_product_id')
    if product_id:
        await update_product(product_id, data_from=data_from)
        await query.edit_message_text(f"✅ Настройка данных изменена: {'покупатель' if data_from == 'buyer' else 'продавец'} предоставляет данные.")
    else:
        await query.edit_message_text("❌ Ошибка.")
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
    keyboard = [
        [InlineKeyboardButton("📂 Управление разделом", callback_data="manage_sections")],
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        "🏷 Управление товарами\nВыберите действие:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cancel_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await edit_product_callback(update, context)

# ================== СТАТИСТИКА ПРОДАВЦА ==================
async def seller_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    sales = int(await redis_client.get(f"stats:seller:{user_id}:sales") or 0)
    await update.message.reply_text(f"📊 Ваша статистика продаж\nПродано товаров: {sales}", parse_mode='Markdown')

# ================== КАТАЛОГ ==================
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
    text = "📂 Выберите категорию:"
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
    text = f"📂 Категория: {section}\n\n"
    for p in products:
        qty = p.get('quantity', '')
        if qty:
            text += f"• {p['name']} | {p['price']} ₽ | {qty}\n"
        else:
            text += f"• {p['name']} | {p['price']} ₽\n"
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
    text = "🛒 Наличие товаров:\n\n"
    for sec, prods in groups.items():
        text += f"{sec}\n"
        for p in prods:
            qty = p.get('quantity', '')
            if qty:
                text += f"• {p['name']} | {p['price']} ₽ | {qty}\n"
            else:
                text += f"• {p['name']} | {p['price']} ₽\n"
        text += "\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ================== КАРТОЧКА ТОВАРА И ПОКУПКА ==================
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
        f"🧾 {product['name']}\n"
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
    total_price = product['price'] * qty
    user_id = query.from_user.id
    order_id = await create_order(user_id, product_id, qty, total_price)
    payment_card = await get_payment_details()
    if not payment_card:
        await query.edit_message_text("❌ Реквизиты для оплаты ещё не заданы администратором. Попробуйте позже.")
        return
    text = (
        f"✅ Для оплаты совершите перевод по данному номеру:\n"
        f"👉 `{payment_card}` (нажмите, чтобы скопировать)\n"
        f"Сумма к оплате: {total_price} ₽\n\n"
        f"ℹ️ Комментарии указывать не нужно, совершить оплату нужно в течение 30 минут."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Я оплатил", callback_data=f"pay_confirmed_{order_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_order_{order_id}")]
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
    total_price = product['price'] * qty
    user_id = update.effective_user.id
    order_id = await create_order(user_id, product_id, qty, total_price)
    payment_card = await get_payment_details()
    if not payment_card:
        await update.message.reply_text("❌ Реквизиты для оплаты ещё не заданы администратором. Попробуйте позже.")
        return
    text = (
        f"✅ Для оплаты совершите перевод по данному номеру:\n"
        f"👉 `{payment_card}` (нажмите, чтобы скопировать)\n"
        f"Сумма к оплате: {total_price} ₽\n\n"
        f"ℹ️ Комментарии указывать не нужно, совершить оплату нужно в течение 30 минут."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Я оплатил", callback_data=f"pay_confirmed_{order_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_order_{order_id}")]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)
    context.user_data.pop('awaiting_custom_qty', None)

# ================== ОБРАБОТКА ОПЛАТЫ ==================
async def pay_confirmed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден.")
        return
    if order['status'] != 'pending':
        await query.edit_message_text("❌ Этот заказ уже был обработан.")
        return
    user_id = query.from_user.id
    if order['user_id'] != user_id:
        await query.answer("⛔ Это не ваш заказ.", show_alert=True)
        return
    expires = datetime.fromisoformat(order['expires'])
    if datetime.now() > expires:
        await query.edit_message_text("⏰ Время на оплату истекло. Заказ отменён.")
        await update_order(order_id, status='rejected')
        return
    product = await get_product(order['product_id'])
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    buyer_name = await get_user_name(user_id)
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(
                owner_id,
                f"💳 Покупатель @{buyer_name} (ID: {user_id}) сообщает об оплате заказа #{order_id}.\n"
                f"Товар: {product['name']}\n"
                f"Количество: {order['quantity']} шт.\n"
                f"Сумма: {order['total_price']} ₽\n\n"
                f"Подтвердите оплату:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да, оплачено", callback_data=f"confirm_pay_{order_id}")],
                    [InlineKeyboardButton("❌ Нет, не оплачено", callback_data=f"reject_pay_{order_id}")]
                ])
            )
        except:
            pass
    await query.edit_message_text(
        "✅ Ваше уведомление об оплате отправлено администратору. Ожидайте подтверждения.\n"
        "Как только оплата будет подтверждена, вы получите товар."
    )
    await update_order(order_id, status='paid')

async def confirm_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order['status'] not in ('pending', 'paid'):
        await query.edit_message_text("❌ Заказ не найден или уже обработан.")
        return
    await update_order(order_id, status='confirmed')
    user_id = order['user_id']
    product_id = order['product_id']
    product = await get_product(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    buyer_name = await get_user_name(user_id)
    await query.edit_message_text(f"✅ Оплата заказа #{order_id} подтверждена. Товар отправлен покупателю @{buyer_name}.")
    try:
        if product.get('data_from') == 'seller':
            await context.bot.send_message(
                user_id,
                f"✅ Ваша оплата подтверждена! Продавец свяжется с вами для передачи товара."
            )
            seller_id = product['seller_id']
            await context.bot.send_message(
                seller_id,
                f"📦 Покупатель @{buyer_name} оплатил товар «{product['name']}».\n"
                f"Вам необходимо отправить покупателю товар (файл или текст).\n"
                f"Используйте кнопку ниже, чтобы отправить товар покупателю.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Отправить товар покупателю", callback_data=f"deliver_{product_id}_{user_id}")]
                ])
            )
        else:
            await context.bot.send_message(
                user_id,
                f"✅ Ваша оплата подтверждена! Продавец запросит у вас дополнительные данные (если необходимо)."
            )
            seller_id = product['seller_id']
            await context.bot.send_message(
                seller_id,
                f"💡 Покупатель @{buyer_name} оплатил товар «{product['name']}». Используйте команду /request {user_id} для запроса дополнительных данных."
            )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомлений: {e}")

async def reject_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not await is_owner(query.from_user.id):
        await query.answer("⛔ Нет прав.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order['status'] not in ('pending', 'paid'):
        await query.edit_message_text("❌ Заказ не найден или уже обработан.")
        return
    await update_order(order_id, status='rejected')
    user_id = order['user_id']
    await query.edit_message_text(f"❌ Оплата заказа #{order_id} отклонена. Покупатель уведомлён.")
    try:
        await context.bot.send_message(
            user_id,
            f"❌ Ваша оплата заказа #{order_id} отклонена администратором. Пожалуйста, свяжитесь с поддержкой."
        )
    except:
        pass

async def cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    order_id = int(query.data.split("_")[2])
    order = await get_order(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден.")
        return
    if order['user_id'] != query.from_user.id:
        await query.answer("⛔ Это не ваш заказ.", show_alert=True)
        return
    if order['status'] != 'pending':
        await query.edit_message_text("❌ Этот заказ уже не может быть отменён.")
        return
    await update_order(order_id, status='rejected')
    await query.edit_message_text("❌ Заказ отменён.")

# ================== ДОСТАВКА ТОВАРА ==================
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

# ================== ЗАПРОС ДАННЫХ У ПОКУПАТЕЛЯ ==================
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

# ================== ПРОФИЛЬ, О МАГАЗИНЕ, ПОМОЩЬ ==================
async def about_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_about_text()
    await update.message.reply_text(text, parse_mode='Markdown')

async def profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    purchases = int(await redis_client.get(f"stats:user:{user_id}:purchases") or 0)
    seller_status = await is_seller(user_id)
    status = "Продавец" if seller_status else "Покупатель"
    text = (
        f"👤 Ваш профиль\n"
        f"ID: `{user_id}`\n"
        f"Статус: {status}\n"
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

# ================== УНИВЕРСАЛЬНЫЙ РОУТЕР ТЕКСТОВЫХ СООБЩЕНИЙ ==================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    logger.info(f"text_router: user={user_id}, text='{text}', state={context.user_data.get('awaiting_product')}")

    if context.user_data.get('awaiting_section_name'):
        await process_section_name(update, context)
        return

    if context.user_data.get('awaiting_rename_section'):
        await process_rename_section(update, context)
        return

    if context.user_data.get('awaiting_product'):
        await process_product_input(update, context)
        return

    if context.user_data.get('awaiting_edit_value'):
        await process_edit_value(update, context)
        return

    if context.user_data.get('awaiting_custom_qty'):
        await process_custom_qty(update, context)
        return

    if context.user_data.get('delivery'):
        await process_delivery(update, context)
        return

    if context.user_data.get('awaiting_request_text'):
        await process_request_text(update, context)
        return

    await update.message.reply_text("❓ Неизвестная команда. Используйте /start для навигации.")

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РОУТЕРА ==================
async def process_section_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    section_name = update.message.text.strip()
    if not section_name:
        await update.message.reply_text("❌ Название не может быть пустым.")
        return
    await set_seller_section(user_id, section_name)
    await update.message.reply_text(f"✅ Раздел «{section_name}» создан.")
    context.user_data.pop('awaiting_section_name', None)
    await my_products_button(update, context)

async def process_rename_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def process_product_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_seller(user_id):
        await update.message.reply_text("❌ Вы не продавец.")
        return
    state = context.user_data.get('awaiting_product')
    logger.info(f"process_product_input: state={state}, text={update.message.text}")

    if state == 'name':
        context.user_data['product_name'] = update.message.text
        context.user_data['awaiting_product'] = 'price'
        await update.message.reply_text("Введите цену товара (только число):")

    elif state == 'price':
        try:
            price = int(update.message.text)
            context.user_data['product_price'] = price
            context.user_data['awaiting_product'] = 'quantity'
            await update.message.reply_text("Введите количество (например, «1 шт.», «3 шт.», «∞»):")
        except ValueError:
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
        name = context.user_data.get('product_name')
        price = context.user_data.get('product_price')
        quantity = context.user_data.get('product_quantity')
        data_from = context.user_data.get('product_data_from', 'buyer')
        desc = update.message.text
        section = context.user_data.get('product_section')
        if not section:
            await update.message.reply_text("❌ Ошибка: не выбран раздел. Начните заново.")
            context.user_data.clear()
            return
        logger.info(f"Добавляем товар: name={name}, price={price}, section={section}, data_from={data_from}")
        product_id = await add_product(user_id, name, price, desc, section, quantity, data_from)
        await update.message.reply_text(f"✅ Товар «{name}» добавлен в раздел «{section}». ID: {product_id}")
        context.user_data.clear()
        await my_products_button(update, context)

    else:
        await update.message.reply_text("❓ Неизвестная команда. Начните заново через /start.")

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

# ================== ВЕБ-СЕРВЕР ==================
async def health(request):
    return web.Response(text="OK")

# ================== ПОДДЕРЖАНИЕ АКТИВНОСТИ (БЕЗ ВЫВОДА) ==================
async def keep_alive():
    while True:
        await asyncio.sleep(300)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://localhost:{os.environ.get('PORT', 10000)}/health"
                async with session.get(url) as resp:
                    await resp.read()
        except Exception as e:
            print(f"❌ Ошибка в keep_alive: {e}")

# ================== ГЛАВНАЯ ФУНКЦИЯ ==================
async def main():
    global bot_app
    logger.info("Запуск бота...")
    if REDIS_URL:
        await init_redis()
    else:
        logger.warning("REDIS_URL не задан! Данные не будут сохраняться.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app = application

    # Регистрация обработчиков (все, что были выше)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addseller", add_seller_command))
    application.add_handler(CommandHandler("removeseller", remove_seller_command))
    application.add_handler(CommandHandler("setpayment", set_payment_command))
    application.add_handler(CommandHandler("setabout", set_about_command))
    application.add_handler(CommandHandler("request", request_data_command))

    application.add_handler(MessageHandler(filters.Text("📂 Все категории"), catalog_button))
    application.add_handler(MessageHandler(filters.Text("🛒 Наличие товаров"), all_products_button))
    application.add_handler(MessageHandler(filters.Text("ℹ️ О магазине"), about_button))
    application.add_handler(MessageHandler(filters.Text("👤 Профиль"), profile_button))
    application.add_handler(MessageHandler(filters.Text("🆘 Помощь"), help_command))
    application.add_handler(MessageHandler(filters.Text("👑 Управление товарами"), my_products_button))
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
    application.add_handler(CallbackQueryHandler(category_callback, pattern="^category_"))
    application.add_handler(CallbackQueryHandler(product_detail_callback, pattern="^product_"))
    application.add_handler(CallbackQueryHandler(buy_qty_callback, pattern="^buy_qty_"))
    application.add_handler(CallbackQueryHandler(pay_confirmed_callback, pattern="^pay_confirmed_"))
    application.add_handler(CallbackQueryHandler(confirm_pay_callback, pattern="^confirm_pay_"))
    application.add_handler(CallbackQueryHandler(reject_pay_callback, pattern="^reject_pay_"))
    application.add_handler(CallbackQueryHandler(cancel_order_callback, pattern="^cancel_order_"))
    application.add_handler(CallbackQueryHandler(deliver_callback, pattern="^deliver_"))
    application.add_handler(CallbackQueryHandler(back_to_product_callback, pattern="^back_to_product_"))
    application.add_handler(CallbackQueryHandler(back_to_catalog_callback, pattern="^back_to_catalog$"))
    application.add_handler(CallbackQueryHandler(back_to_main_callback, pattern="^back_to_main$"))
    application.add_handler(CallbackQueryHandler(cancel_edit_callback, pattern="^cancel_edit$"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await asyncio.sleep(1)
    await application.start()
    await application.updater.start_polling()
    logger.info("✅ Бот запущен и получает обновления")

    # Веб-сервер
    web_app = web.Application()
    web_app.router.add_get('/health', health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    site = web.TCPSite(runner, host='0.0.0.0', port=port)
    await site.start()
    logger.info(f"✅ Веб-сервер запущен на порту {port}")

    # Небольшая задержка для регистрации порта
    await asyncio.sleep(2)

    # Бесконечное ожидание (надёжный способ)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())