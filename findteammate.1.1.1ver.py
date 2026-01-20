import os
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========== НАСТРОЙКИ БОТА ===========
TOKEN = "8418697488:AAGTLsFfLOke4C5ugq15hwe8HxDGQF__N24"

# ID админов (те, кто могут банить, выдавать тимбалы)
ADMIN_IDS = [1719251644]  # ← Ваш ID и ID других главных админов

# ID верификаторов (те, кто проверяют анкеты)
VERIFIER_IDS = [1719251644]  # ← ID модераторов

# Можно добавить админов, которые будут и там и там
ADMIN_AND_VERIFIER_IDS = [1719251644]  # ← ID тех, кто имеет обе роли
# ======================================

# Настройки тимбалов
TEAMBALLS_PER_MATCH = 5
TEAMBALLS_PER_REFERRAL = 12
REFERRAL_MATCHES_REQUIRED = 2
MATCH_COOLDOWN_HOURS = 1

# Промокоды
PROMO_CODES = {
    "100": 1000,
    "200": 1700,
    "500": 4000,
    "800": 6200,
    "1000+premium": 8500,
    "2250": 18000,
    "5000": 39000
}


class Database:
    def __init__(self):
        self.conn = sqlite3.connect('teammates_bot.db', check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()

        # Пользователи
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                roblox_nickname TEXT,
                photo_id TEXT,
                game_modes TEXT,
                profile_verified INTEGER DEFAULT 0,
                team_balls INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                matches_found INTEGER DEFAULT 0,
                last_match_time TEXT,
                created_at TEXT
            )
        ''')

        # Лайки/дизлайки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                is_like INTEGER,
                message TEXT,
                sent_at TEXT,
                FOREIGN KEY (from_user_id) REFERENCES users (user_id),
                FOREIGN KEY (to_user_id) REFERENCES users (user_id)
            )
        ''')

        # Рефералы
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                completed INTEGER DEFAULT 0,
                created_at TEXT,
                FOREIGN KEY (referrer_id) REFERENCES users (user_id),
                FOREIGN KEY (referred_id) REFERENCES users (user_id)
            )
        ''')

        # Покупки промокодов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                promo_type TEXT,
                team_balls_spent INTEGER,
                purchased_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        # Сообщения в поддержку
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                admin_response TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')

        self.conn.commit()

    def add_user(self, user_id: int, username: str):
        """Добавляет нового пользователя"""
        cursor = self.conn.cursor()
        referral_code = str(uuid.uuid4())[:8]

        cursor.execute('''
            INSERT OR IGNORE INTO users 
            (user_id, username, referral_code, created_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, referral_code, datetime.now().isoformat()))

        self.conn.commit()

    def add_to_verification(self, user_id: int, roblox_nickname: str, photo_id: str, game_modes: str):
        """Добавляет анкету на верификацию"""
        cursor = self.conn.cursor()

        cursor.execute('''
            UPDATE users 
            SET roblox_nickname = ?, photo_id = ?, game_modes = ?, profile_verified = 0
            WHERE user_id = ?
        ''', (roblox_nickname, photo_id, game_modes, user_id))

        self.conn.commit()

    def get_user_profile(self, user_id: int):
        """Получает профиль пользователя"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return cursor.fetchone()

    def approve_profile(self, user_id: int):
        """Одобряет анкету пользователя"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET profile_verified = 1 WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def reject_profile(self, user_id: int):
        """Отклоняет анкету пользователя"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET profile_verified = 2 WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def get_pending_verifications(self):
        """Получает анкеты на проверке"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username, roblox_nickname, photo_id, game_modes 
            FROM users 
            WHERE profile_verified = 0 AND roblox_nickname IS NOT NULL
        ''')
        return cursor.fetchall()

    def find_likes_for_user(self, user_id: int) -> List:
        """Находит пользователей, которые лайкнули текущего пользователя"""
        cursor = self.conn.cursor()

        cursor.execute('''
            SELECT u.* 
            FROM interactions i
            JOIN users u ON i.from_user_id = u.user_id
            WHERE i.to_user_id = ? 
            AND i.is_like = 1
            AND NOT EXISTS (
                SELECT 1 FROM interactions i2 
                WHERE i2.from_user_id = ? 
                AND i2.to_user_id = i.from_user_id
            )
            ORDER BY i.sent_at DESC
            LIMIT 10
        ''', (user_id, user_id))

        return cursor.fetchall()

    def find_random_teammates(self, user_id: int) -> List:
        """Находит случайных тиммейтов (кроме тех, с кем уже было взаимодействие)"""
        cursor = self.conn.cursor()

        cursor.execute('''
            SELECT u.* 
            FROM users u
            WHERE u.user_id != ? 
            AND u.profile_verified = 1
            AND u.is_banned = 0
            AND NOT EXISTS (
                SELECT 1 FROM interactions i 
                WHERE (i.from_user_id = ? AND i.to_user_id = u.user_id)
                OR (i.from_user_id = u.user_id AND i.to_user_id = ? AND i.is_like = 1)
            )
            ORDER BY RANDOM()
            LIMIT 10
        ''', (user_id, user_id, user_id))

        return cursor.fetchall()

    def add_interaction(self, from_user_id: int, to_user_id: int, is_like: bool, message: str = ''):
        """Добавляет взаимодействие между пользователями"""
        cursor = self.conn.cursor()

        cursor.execute('''
            INSERT INTO interactions 
            (from_user_id, to_user_id, is_like, message, sent_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (from_user_id, to_user_id, 1 if is_like else 0, message, datetime.now().isoformat()))

        if is_like:
            self.add_team_balls(from_user_id, TEAMBALLS_PER_MATCH)

            cursor.execute('SELECT last_match_time FROM users WHERE user_id = ?', (from_user_id,))
            last_time_result = cursor.fetchone()

            if last_time_result and last_time_result[0]:
                last_time = datetime.fromisoformat(last_time_result[0])
                if datetime.now() - last_time >= timedelta(hours=MATCH_COOLDOWN_HOURS):
                    cursor.execute('''
                        UPDATE users 
                        SET matches_found = matches_found + 1, last_match_time = ?
                        WHERE user_id = ?
                    ''', (datetime.now().isoformat(), from_user_id))
            else:
                cursor.execute('''
                    UPDATE users 
                    SET matches_found = matches_found + 1, last_match_time = ?
                    WHERE user_id = ?
                ''', (datetime.now().isoformat(), from_user_id))

            self.check_referral_completion(from_user_id)

        self.conn.commit()

    def get_user_interactions(self, user_id: int):
        """Получает взаимодействия пользователя"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT u.*, i.message, i.sent_at
            FROM interactions i
            JOIN users u ON i.from_user_id = u.user_id
            WHERE i.to_user_id = ? AND i.is_like = 1
            ORDER BY i.sent_at DESC
        ''', (user_id,))
        return cursor.fetchall()

    def add_team_balls(self, user_id: int, amount: int):
        """Добавляет тимбалы пользователю"""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET team_balls = team_balls + ? WHERE user_id = ?', (amount, user_id))
        self.conn.commit()

    def check_referral_completion(self, referred_id: int):
        """Проверяет выполнение условий реферальной программы"""
        cursor = self.conn.cursor()

        cursor.execute('SELECT matches_found FROM users WHERE user_id = ?', (referred_id,))
        result = cursor.fetchone()

        if result and result[0] >= REFERRAL_MATCHES_REQUIRED:
            cursor.execute('SELECT referrer_id FROM referrals WHERE referred_id = ? AND completed = 0', (referred_id,))
            referral = cursor.fetchone()

            if referral:
                referrer_id = referral[0]
                self.add_team_balls(referrer_id, TEAMBALLS_PER_REFERRAL)
                cursor.execute('UPDATE referrals SET completed = 1 WHERE referred_id = ?', (referred_id,))
                self.conn.commit()
                return referrer_id

        return None

    def add_referral(self, referrer_id: int, referred_id: int):
        """Добавляет реферала"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO referrals (referrer_id, referred_id, created_at)
            VALUES (?, ?, ?)
        ''', (referrer_id, referred_id, datetime.now().isoformat()))
        self.conn.commit()

    def add_purchase(self, user_id: int, promo_type: str, team_balls_spent: int):
        """Добавляет запись о покупке"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO purchases (user_id, promo_type, team_balls_spent, purchased_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, promo_type, team_balls_spent, datetime.now().isoformat()))
        self.conn.commit()

    def add_support_message(self, user_id: int, message: str):
        """Добавляет сообщение в поддержку"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO support_messages (user_id, message, created_at)
            VALUES (?, ?, ?)
        ''', (user_id, message, datetime.now().isoformat()))
        self.conn.commit()

    def get_user_by_username(self, username: str):
        """Находит пользователя по username"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_all_users(self):
        """Получает всех пользователей"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id, username, roblox_nickname, team_balls, is_banned, profile_verified FROM users')
        return cursor.fetchall()

    def get_top_users_by_teamballs(self, limit: int = 20):
        """Получает топ пользователей по тимбалам"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username, roblox_nickname, team_balls, profile_verified
            FROM users 
            WHERE profile_verified = 1 AND is_banned = 0
            ORDER BY team_balls DESC 
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()


db = Database()

# Состояния пользователей
user_states = {}


# =========== ФУНКЦИИ ДЛЯ ПРОВЕРКИ ПРАВ ===========
def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь админом"""
    return user_id in ADMIN_IDS or user_id in ADMIN_AND_VERIFIER_IDS


def is_verifier(user_id: int) -> bool:
    """Проверяет, является ли пользователь верификатором"""
    return user_id in VERIFIER_IDS or user_id in ADMIN_AND_VERIFIER_IDS


def is_admin_or_verifier(user_id: int) -> bool:
    """Проверяет, имеет ли пользователь какие-либо права"""
    return is_admin(user_id) or is_verifier(user_id)


# ================================================

def get_menu_keyboard():
    """Создает клавиатуру с кнопкой 'В меню'"""
    return ReplyKeyboardMarkup([[KeyboardButton("🏠 В меню")]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id

    if context.args:
        referrer_code = context.args[0]
        cursor = db.conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (referrer_code,))
        referrer = cursor.fetchone()

        if referrer and referrer[0] != user_id:
            cursor.execute('SELECT 1 FROM referrals WHERE referrer_id = ? AND referred_id = ?', (referrer[0], user_id))
            if not cursor.fetchone():
                db.add_referral(referrer[0], user_id)
                await update.message.reply_text(
                    "🎉 Вы присоединились по реферальной ссылке! "
                    f"Найдите {REFERRAL_MATCHES_REQUIRED} тиммейтов, чтобы ваш друг получил награду.",
                    reply_markup=get_menu_keyboard()
                )

    db.add_user(user_id, user.username)
    await show_main_menu(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню"""
    user_id = update.effective_user.id

    profile = db.get_user_profile(user_id)
    if profile and profile[8]:  # is_banned
        await update.message.reply_text("❌ Вы забанены!", reply_markup=get_menu_keyboard())
        return

    keyboard = [
        [InlineKeyboardButton("👤 Моя анкета", callback_data="my_profile")],
        [InlineKeyboardButton("🔍 Искать тиммейта", callback_data="find_teammate")],
        [InlineKeyboardButton("🤝 Найденные тиммейты", callback_data="found_teammates")],
        [InlineKeyboardButton("🏪 Магазин", callback_data="shop")],
        [InlineKeyboardButton("🔗 Реф ссылка", callback_data="referral")],
        [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
            "Выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
            "Выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
        await update.message.reply_text(
            "Ты можешь всегда нажать на кнопку '🏠 В меню' чтобы вернуться сюда",
            reply_markup=get_menu_keyboard()
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    logger.info(f"Кнопка нажата: user_id={user_id}, data={data}")

    if data == "my_profile":
        logger.info(f"Показ профиля для {user_id}")
        await show_my_profile(query, context)
    elif data == "find_teammate":
        logger.info(f"Поиск тиммейта для {user_id}")
        await find_teammate(query, context)
    elif data == "found_teammates":
        await show_found_teammates(query, context)
    elif data == "shop":
        await show_shop(query, context)
    elif data == "referral":
        await show_referral_link(query, context)
    elif data == "support":
        await ask_support_message(query, context)
    elif data.startswith("like_"):
        await handle_like(query, context)
    elif data.startswith("dislike_"):
        await handle_dislike(query, context)
    elif data.startswith("buy_"):
        await handle_purchase(query, context)
    elif data.startswith("approve_"):
        await handle_approve_profile(query, context)
    elif data.startswith("reject_"):
        await handle_reject_profile(query, context)
    elif data == "back_to_menu":
        # Показываем главное меню напрямую
        await query.edit_message_text(
            "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
            "Выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Моя анкета", callback_data="my_profile")],
                [InlineKeyboardButton("🔍 Искать тиммейта", callback_data="find_teammate")],
                [InlineKeyboardButton("🤝 Найденные тиммейты", callback_data="found_teammates")],
                [InlineKeyboardButton("🏪 Магазин", callback_data="shop")],
                [InlineKeyboardButton("🔗 Реф ссылка", callback_data="referral")],
                [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
            ])
        )
        await query.message.reply_text(
            "Ты можешь всегда нажать на кнопку '🏠 В меню' чтобы вернуться сюда",
            reply_markup=get_menu_keyboard()
        )
    elif data == "edit_profile":
        await edit_profile(query, context)
    elif data in ["cancel_message", "cancel_support"]:
        await cancel_handler(update, context)
    elif data.startswith("reply_"):
        await handle_admin_reply(query, context)


async def show_my_profile(query, context):
    """Показывает профиль пользователя"""
    user_id = query.from_user.id
    profile = db.get_user_profile(user_id)

    if not profile or not profile[2]:  # Нет roblox_nickname
        # Нет анкеты - создаем новую
        user_states[user_id] = {"state": "waiting_nickname"}
        await query.edit_message_text(
            "📝 <b>Создание анкеты</b>\n\n"
            "Отправьте мне ваш никнейм в Roblox:\n\n"
            "Нажмите '🏠 В меню' для отмены",
            parse_mode=ParseMode.HTML
        )
        return

    # Показываем статистику профиля
    cursor = db.conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM interactions WHERE to_user_id = ? AND is_like = 1', (user_id,))
    likes_count = cursor.fetchone()[0]

    cursor.execute('''
        SELECT u.username, u.team_balls, i.message, i.sent_at
        FROM interactions i
        JOIN users u ON i.from_user_id = u.user_id
        WHERE i.to_user_id = ? AND i.is_like = 1
        ORDER BY i.sent_at DESC LIMIT 10
    ''', (user_id,))

    messages = cursor.fetchall()

    verification_status = ""
    if profile[5] == 0:
        verification_status = "🟡 На проверке"
    elif profile[5] == 1:
        verification_status = "✅ Одобрено"
    elif profile[5] == 2:
        verification_status = "❌ Отклонено"

    text = "<b>👤 Ваша анкета:</b>\n\n"
    text += f"<b>📛 Никнейм:</b> {profile[2]}\n"
    text += f"<b>🎮 Режимы:</b> {profile[4]}\n"
    text += f"<b>📊 Статус:</b> {verification_status}\n"
    text += f"<b>⭐ Лайков:</b> {likes_count}\n"
    text += f"<b>💰 Тимбалов:</b> {profile[6]}\n"
    text += f"<b>🔍 Найдено тиммейтов:</b> {profile[12]}\n"
    text += f"<b>⚠️ Предупреждений:</b> {profile[7]}/3\n\n"

    if messages:
        text += "<b>📬 Последние сообщения:</b>\n"
        for msg in messages:
            username, balls, message, sent_at = msg
            time_str = datetime.fromisoformat(sent_at).strftime("%d.%m %H:%M") if sent_at else ""

            # Экранируем HTML символы
            safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            text += f"├ <b>От:</b> @{username if username else 'нет'}\n"
            text += f"├ <b>Тимбалов:</b> {balls}\n"
            text += f"├ <b>Сообщение:</b> {safe_message[:50]}{'...' if len(message) > 50 else ''}\n"
            text += f"└ <b>Время:</b> {time_str}\n\n"

    keyboard = []
    if profile[5] != 1:
        keyboard.append([InlineKeyboardButton("✏️ Изменить анкету", callback_data="edit_profile")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def edit_profile(query, context):
    """Начинает изменение анкеты"""
    user_id = query.from_user.id
    user_states[user_id] = {"state": "waiting_nickname"}

    await query.edit_message_text(
        "📝 <b>Изменение анкеты</b>\n\n"
        "Отправьте мне ваш новый никнейм в Roblox:\n\n"
        "Нажмите '🏠 В меню' для отмены",
        parse_mode=ParseMode.HTML
    )


async def find_teammate(query, context):
    """Ищет тиммейтов для пользователя"""
    user_id = query.from_user.id
    profile = db.get_user_profile(user_id)

    if not profile or not profile[2]:  # Нет ника в Roblox
        await query.edit_message_text(
            "❌ Сначала создайте анкету!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]])
        )
        return

    if profile[5] != 1:  # Анкета не одобрена
        status_text = {
            0: "⏳ Ваша анкета еще на проверке! Ожидайте верификации.",
            2: "❌ Ваша анкета отклонена. Пожалуйста, создайте новую.",
            None: "❌ Анкета не найдена."
        }
        await query.edit_message_text(
            status_text.get(profile[5], "❌ Произошла ошибка"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]])
        )
        return

    # Сначала проверяем, есть ли пользователи, которые лайкнули текущего пользователя
    liked_users = db.find_likes_for_user(user_id)

    if liked_users:
        # Показываем тех, кто лайкнул пользователя
        context.user_data["current_mode"] = "viewing_likes"
        context.user_data["teammates_list"] = [t[0] for t in liked_users]
        context.user_data["current_teammate_index"] = 0

        teammate = liked_users[0]
        context.user_data["current_teammate"] = teammate[0]

        text = f"<b>👤 Никнейм:</b> {teammate[2]}\n"
        text += f"<b>🎮 Режимы:</b> {teammate[4]}\n\n"
        text += f"<b>💡 Этот пользователь лайкнул вашу анкету!</b>\n"
        text += f"<b>⭐ Найдено тиммейтов:</b> {teammate[12]}\n"

        keyboard_buttons = [
            [
                InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
            ],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
        ]

        try:
            if teammate[3]:
                await query.message.reply_photo(
                    photo=teammate[3],
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                )
                await query.delete_message()
            else:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                )
        except Exception as e:
            logger.error(f"Ошибка при показе тиммейта: {e}")
            await query.edit_message_text(
                text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard_buttons)
            )
    else:
        # Если нет лайков, показываем случайных тиммейтов
        random_teammates = db.find_random_teammates(user_id)

        if not random_teammates:
            await query.edit_message_text(
                "😔 Пока нет подходящих тиммейтов. Попробуйте позже!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]])
            )
            return

        context.user_data["current_mode"] = "viewing_random"
        context.user_data["teammates_list"] = [t[0] for t in random_teammates]
        context.user_data["current_teammate_index"] = 0

        teammate = random_teammates[0]
        context.user_data["current_teammate"] = teammate[0]

        text = f"<b>👤 Никнейм:</b> {teammate[2]}\n"
        text += f"<b>🎮 Режимы:</b> {teammate[4]}\n\n"
        text += f"<b>💡 Найдено тиммейтов:</b> {teammate[12]}\n"

        keyboard = [
            [
                InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
            ],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
        ]

        try:
            if teammate[3]:
                await query.message.reply_photo(
                    photo=teammate[3],
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                await query.delete_message()
            else:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            logger.error(f"Ошибка при показе тиммейта: {e}")
            await query.edit_message_text(
                text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )


async def handle_like(query, context):
    """Обрабатывает лайк"""
    user_id = query.from_user.id
    to_user_id = int(query.data.split("_")[1])

    db.add_interaction(user_id, to_user_id, True)

    # Отправляем уведомление пользователю, которого лайкнули
    try:
        profile = db.get_user_profile(user_id)
        await context.bot.send_message(
            to_user_id,
            f"💖 <b>Вас лайкнули!</b>\n\n"
            f"Пользователь <b>@{profile[1] if profile[1] else 'без username'}</b> оценил вашу анкету!\n"
            f"Теперь вы можете найти его в разделе '🔍 Искать тиммейта'.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")

    await query.answer(f"✅ Лайк отправлен! +{TEAMBALLS_PER_MATCH} тимбалов")

    # Удаляем пользователя из списка
    teammates_list = context.user_data.get("teammates_list", [])
    current_mode = context.user_data.get("current_mode", "viewing_random")

    if teammates_list:
        teammates_list = [tid for tid in teammates_list if tid != to_user_id]
        context.user_data["teammates_list"] = teammates_list

        if teammates_list:
            # Показываем следующего пользователя
            next_teammate_id = teammates_list[0]
            teammate = db.get_user_profile(next_teammate_id)

            if teammate:
                context.user_data["current_teammate"] = teammate[0]
                text = f"<b>👤 Никнейм:</b> {teammate[2]}\n<b>🎮 Режимы:</b> {teammate[4]}\n\n"

                if current_mode == "viewing_likes":
                    text += f"<b>💡 Этот пользователь лайкнул вашу анкету!</b>\n"
                text += f"<b>⭐ Найдено тиммейтов:</b> {teammate[12]}\n"

                keyboard_buttons = [
                    [
                        InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                        InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
                    ],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
                ]

                try:
                    if teammate[3]:
                        await query.message.reply_photo(
                            photo=teammate[3],
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                        )
                        await query.delete_message()
                    else:
                        await query.edit_message_text(
                            text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                        )
                except:
                    await query.edit_message_text(
                        text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                    )
                return

    # Если больше нет пользователей в списке
    if current_mode == "viewing_likes":
        message = "🎉 Вы просмотрели всех, кто вас лайкнул! Теперь будут показаны случайные анкеты."

        # Ищем случайных тиммейтов
        random_teammates = db.find_random_teammates(user_id)

        if random_teammates:
            context.user_data["current_mode"] = "viewing_random"
            context.user_data["teammates_list"] = [t[0] for t in random_teammates]

            teammate = random_teammates[0]
            context.user_data["current_teammate"] = teammate[0]

            text = f"<b>👤 Никнейм:</b> {teammate[2]}\n<b>🎮 Режимы:</b> {teammate[4]}\n\n"
            text += f"<b>💡 Найдено тиммейтов:</b> {teammate[12]}\n"

            keyboard = [
                [
                    InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                    InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
                ],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
            ]

            await query.edit_message_text(
                message,
                parse_mode=ParseMode.HTML
            )

            try:
                if teammate[3]:
                    await query.message.reply_photo(
                        photo=teammate[3],
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await query.message.reply_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            except:
                await query.message.reply_text(
                    text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return
        else:
            message += "\n\n😔 Но пока нет других подходящих тиммейтов."

    # Возвращаемся в главное меню
    await query.edit_message_text(
        "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Моя анкета", callback_data="my_profile")],
            [InlineKeyboardButton("🔍 Искать тиммейта", callback_data="find_teammate")],
            [InlineKeyboardButton("🤝 Найденные тиммейты", callback_data="found_teammates")],
            [InlineKeyboardButton("🏪 Магазин", callback_data="shop")],
            [InlineKeyboardButton("🔗 Реф ссылка", callback_data="referral")],
            [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
        ])
    )
    await query.message.reply_text(
        "Ты можешь всегда нажать на кнопку '🏠 В меню' чтобы вернуться сюда",
        reply_markup=get_menu_keyboard()
    )


async def handle_dislike(query, context):
    """Обрабатывает дизлайк"""
    user_id = query.from_user.id
    to_user_id = int(query.data.split("_")[1])

    db.add_interaction(user_id, to_user_id, False)
    await query.answer("💩 Дизлайк отправлен")

    # Удаляем пользователя из списка
    teammates_list = context.user_data.get("teammates_list", [])
    current_mode = context.user_data.get("current_mode", "viewing_random")

    if teammates_list:
        teammates_list = [tid for tid in teammates_list if tid != to_user_id]
        context.user_data["teammates_list"] = teammates_list

        if teammates_list:
            # Показываем следующего пользователя
            next_teammate_id = teammates_list[0]
            teammate = db.get_user_profile(next_teammate_id)

            if teammate:
                context.user_data["current_teammate"] = teammate[0]
                text = f"<b>👤 Никнейм:</b> {teammate[2]}\n<b>🎮 Режимы:</b> {teammate[4]}\n\n"

                if current_mode == "viewing_likes":
                    text += f"<b>💡 Этот пользователь лайкнул вашу анкету!</b>\n"
                text += f"<b>⭐ Найдено тиммейтов:</b> {teammate[12]}\n"

                keyboard_buttons = [
                    [
                        InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                        InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
                    ],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
                ]

                try:
                    if teammate[3]:
                        await query.message.reply_photo(
                            photo=teammate[3],
                            caption=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                        )
                        await query.delete_message()
                    else:
                        await query.edit_message_text(
                            text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                        )
                except:
                    await query.edit_message_text(
                        text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard_buttons)
                    )
                return

    # Если больше нет пользователей в списке
    if current_mode == "viewing_likes":
        message = "🎉 Вы просмотрели всех, кто вас лайкнул! Теперь будут показаны случайные анкеты."

        # Ищем случайных тиммейтов
        random_teammates = db.find_random_teammates(user_id)

        if random_teammates:
            context.user_data["current_mode"] = "viewing_random"
            context.user_data["teammates_list"] = [t[0] for t in random_teammates]

            teammate = random_teammates[0]
            context.user_data["current_teammate"] = teammate[0]

            text = f"<b>👤 Никнейм:</b> {teammate[2]}\n<b>🎮 Режимы:</b> {teammate[4]}\n\n"
            text += f"<b>💡 Найдено тиммейтов:</b> {teammate[12]}\n"

            keyboard = [
                [
                    InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{teammate[0]}"),
                    InlineKeyboardButton("💩 Дизлайк", callback_data=f"dislike_{teammate[0]}")
                ],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
            ]

            await query.edit_message_text(
                message,
                parse_mode=ParseMode.HTML
            )

            try:
                if teammate[3]:
                    await query.message.reply_photo(
                        photo=teammate[3],
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await query.message.reply_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            except:
                await query.message.reply_text(
                    text + "\n<b>🖼 Фото:</b> (не удалось загрузить)\n",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return
        else:
            message += "\n\n😔 Но пока нет других подходящих тиммейтов."

    # Возвращаемся в главное меню
    await query.edit_message_text(
        "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Моя анкета", callback_data="my_profile")],
            [InlineKeyboardButton("🔍 Искать тиммейта", callback_data="find_teammate")],
            [InlineKeyboardButton("🤝 Найденные тиммейты", callback_data="found_teammates")],
            [InlineKeyboardButton("🏪 Магазин", callback_data="shop")],
            [InlineKeyboardButton("🔗 Реф ссылка", callback_data="referral")],
            [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
        ])
    )
    await query.message.reply_text(
        "Ты можешь всегда нажать на кнопку '🏠 В меню' чтобы вернуться сюда",
        reply_markup=get_menu_keyboard()
    )


async def show_found_teammates(query, context):
    """Показывает найденных тиммейтов (историю лайков)"""
    user_id = query.from_user.id
    interactions = db.get_user_interactions(user_id)

    if not interactions:
        await query.edit_message_text(
            "😔 Пока вас никто не лайкнул",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]])
        )
        return

    text = "<b>🤝 Пользователи, которые вас лайкнули:</b>\n\n"

    for i, interaction in enumerate(interactions[:10], 1):
        teammate_id = interaction[0]
        username = interaction[1]
        roblox_nick = interaction[2]
        game_modes = interaction[4]
        message = interaction[13]

        # Экранируем HTML символы
        safe_username = username.replace("&", "&amp;").replace("<", "&lt;").replace(">",
                                                                                    "&gt;") if username else "нет_username"
        safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if message else ""

        text += f"<b>{i}. @{safe_username}</b>\n"
        text += f"   <b>📛 Ник в Roblox:</b> {roblox_nick}\n"
        text += f"   <b>🎮 Режимы:</b> {game_modes}\n"
        if message:
            text += f"   <b>💌 Сообщение:</b> {safe_message[:50]}{'...' if len(message) > 50 else ''}\n"
        text += f"   <b>💬</b> <a href='tg://user?id={teammate_id}'>Написать в Telegram</a>\n\n"

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]])
    )


async def show_shop(query, context):
    """Показывает магазин"""
    user_id = query.from_user.id
    profile = db.get_user_profile(user_id)
    team_balls = profile[6] if profile else 0

    text = f"<b>🏪 Магазин</b>\n\n<b>💰 Ваши тимбаллы:</b> {team_balls}\n\nВыберите промокод:\n\n"

    keyboard = []
    for promo, price in PROMO_CODES.items():
        keyboard.append([InlineKeyboardButton(f"{promo} робуксов - {price} тимбалов", callback_data=f"buy_{promo}")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_purchase(query, context):
    """Обрабатывает покупку промокода"""
    user_id = query.from_user.id
    promo_type = query.data.split("_")[1]
    price = PROMO_CODES[promo_type]

    profile = db.get_user_profile(user_id)
    if not profile:
        await query.answer("❌ Ошибка: профиль не найден")
        return

    if profile[6] < price:
        await query.answer(f"❌ Недостаточно тимбалов! Нужно: {price}")
        return

    db.add_team_balls(user_id, -price)
    db.add_purchase(user_id, promo_type, price)

    await query.answer(f"✅ Покупка успешна! Промокод на {promo_type} робуксов приобретен.")

    for admin_id in ADMIN_IDS + ADMIN_AND_VERIFIER_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"<b>🛒 Новая покупка!</b>\n\n"
                f"<b>Пользователь:</b> @{profile[1] if profile[1] else 'нет'}\n"
                f"<b>ID:</b> {user_id}\n"
                f"<b>Промокод:</b> {promo_type} робуксов\n"
                f"<b>Стоимость:</b> {price} тимбалов\n"
                f"<a href='tg://user?id={user_id}'>Ссылка</a>",
                parse_mode=ParseMode.HTML
            )
        except:
            pass

    await show_shop(query, context)


async def show_referral_link(query, context):
    """Показывает реферальную ссылку"""
    user_id = query.from_user.id
    profile = db.get_user_profile(user_id)

    if not profile:
        await query.answer("❌ Ошибка: профиль не найден")
        return

    referral_code = profile[9]
    bot_username = context.bot.username
    referral_link = f"https://t.me/{bot_username}?start={referral_code}"

    cursor = db.conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND completed = 1', (user_id,))
    completed_refs = cursor.fetchone()[0]

    text = f"<b>🔗 Ваша реферальная ссылка:</b>\n\n<code>{referral_link}</code>\n\n"
    text += f"<b>📊 Статистика:</b>\n• Приглашено друзей: {completed_refs}\n"
    text += f"• Заработано тимбалов: {completed_refs * TEAMBALLS_PER_REFERRAL}\n\n"
    text += f"💡 За каждого друга, который перейдет по ссылке и найдет {REFERRAL_MATCHES_REQUIRED} тиммейтов, вы получите {TEAMBALLS_PER_REFERRAL} тимбалов!"

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]])
    )


async def ask_support_message(query, context):
    """Запрашивает сообщение для поддержки"""
    user_id = query.from_user.id
    user_states[user_id] = {"state": "waiting_support"}

    await query.edit_message_text(
        "📞 Напишите ваше сообщение в поддержку (макс. 500 символов):\n\n"
        "Нажмите '🏠 В меню' для отмены",
        parse_mode=ParseMode.HTML
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    message_text = update.message.text

    # Обработка кнопки "В меню"
    if message_text == "🏠 В меню":
        if user_id in user_states:
            del user_states[user_id]
        if "message_for_user" in context.user_data:
            del context.user_data["message_for_user"]
        if "replying_to" in context.user_data:
            del context.user_data["replying_to"]
        await show_main_menu(update, context)
        return

    if user_id in user_states:
        state_data = user_states[user_id]

        if state_data["state"] == "waiting_nickname":
            context.user_data["roblox_nickname"] = message_text
            user_states[user_id] = {"state": "waiting_photo"}
            await update.message.reply_text(
                "📸 Теперь отправьте фото вашего скина в Roblox:\n\n"
                "Нажмите '🏠 В меню' для отмены",
                reply_markup=get_menu_keyboard()
            )

        elif state_data["state"] == "waiting_game_modes":
            context.user_data["game_modes"] = message_text

            db.add_to_verification(
                user_id,
                context.user_data["roblox_nickname"],
                context.user_data.get("photo_id", ""),
                message_text
            )

            profile = db.get_user_profile(user_id)
            for verifier_id in VERIFIER_IDS + ADMIN_AND_VERIFIER_IDS:
                try:
                    text = f"<b>📝 Новая анкета на проверку!</b>\n\n"
                    text += f"<b>Пользователь:</b> @{update.effective_user.username or 'нет'}\n"
                    text += f"<b>ID:</b> {user_id}\n"
                    text += f"<b>Ник в Roblox:</b> {context.user_data['roblox_nickname']}\n"
                    text += f"<b>Режимы:</b> {message_text}"

                    keyboard = [
                        [
                            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{user_id}"),
                            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}")
                        ]
                    ]

                    await context.bot.send_message(
                        verifier_id,
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )

                    if "photo_id" in context.user_data:
                        await context.bot.send_photo(
                            verifier_id,
                            photo=context.user_data["photo_id"],
                            caption="Фото скина"
                        )
                except Exception as e:
                    logger.error(f"Ошибка отправки верификатору {verifier_id}: {e}")

            # Очищаем временные данные
            if user_id in user_states:
                del user_states[user_id]
            if "roblox_nickname" in context.user_data:
                del context.user_data["roblox_nickname"]
            if "photo_id" in context.user_data:
                del context.user_data["photo_id"]
            if "game_modes" in context.user_data:
                del context.user_data["game_modes"]

            await update.message.reply_text(
                "✅ Анкета отправлена на модерацию! Ожидайте проверки.",
                reply_markup=get_menu_keyboard()
            )
            # Показываем главное меню
            await show_main_menu(update, context)

        elif state_data["state"] == "waiting_support":
            if len(message_text) <= 500:
                db.add_support_message(user_id, message_text)

                for admin_id in ADMIN_IDS + ADMIN_AND_VERIFIER_IDS:
                    try:
                        # Экранируем HTML символы
                        safe_message = message_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                        await context.bot.send_message(
                            admin_id,
                            f"<b>📩 Новое сообщение в поддержку!</b>\n\n"
                            f"<b>От:</b> @{update.effective_user.username or 'нет'}\n"
                            f"<b>ID:</b> {user_id}\n\n"
                            f"<b>Сообщение:</b> {safe_message}",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("💌 Ответить", callback_data=f"reply_{user_id}")]
                            ])
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки админу {admin_id}: {e}")

                await update.message.reply_text(
                    "✅ Ваше сообщение отправлено в поддержку!",
                    reply_markup=get_menu_keyboard()
                )
                if user_id in user_states:
                    del user_states[user_id]
                # Показываем главное меню
                await show_main_menu(update, context)
            else:
                await update.message.reply_text(
                    "❌ Сообщение слишком длинное! Макс. 500 символов.\n"
                    "Попробуйте снова:",
                    reply_markup=get_menu_keyboard()
                )

    # Если это ответ админа на поддержку
    elif is_admin_or_verifier(user_id) and "replying_to" in context.user_data:
        target_id = context.user_data["replying_to"]

        try:
            await context.bot.send_message(
                target_id,
                f"<b>📨 Ответ от поддержки:</b>\n\n{message_text}",
                parse_mode=ParseMode.HTML
            )
            await update.message.reply_text(
                f"✅ Ответ отправлен пользователю {target_id}",
                reply_markup=get_menu_keyboard()
            )
            del context.user_data["replying_to"]
        except Exception as e:
            await update.message.reply_text(
                f"❌ Не удалось отправить сообщение: {e}",
                reply_markup=get_menu_keyboard()
            )

    # Если сообщение не обработано, показываем меню
    else:
        await show_main_menu(update, context)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фото"""
    user_id = update.effective_user.id

    if user_id in user_states and user_states[user_id]["state"] == "waiting_photo":
        # Получаем file_id фото
        photo_file_id = update.message.photo[-1].file_id
        context.user_data["photo_id"] = photo_file_id  # Сохраняем file_id

        user_states[user_id] = {"state": "waiting_game_modes"}

        await update.message.reply_text(
            "🎮 Теперь введите игровые режимы, в которые вы играете (через запятую):\n"
            "Пример: BedWars, Murder Mystery 2, Tower of Hell\n\n"
            "Нажмите '🏠 В меню' для отмены",
            reply_markup=get_menu_keyboard()
        )


# =========== КОМАНДЫ ДЛЯ АДМИНОВ ===========
async def admin_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /give для выдачи тимбалов"""
    if not is_admin(update.effective_user.id):
        return

    try:
        amount = int(context.args[0])

        if len(context.args) > 1:
            target_arg = context.args[1]
            if target_arg.startswith('@'):
                username = target_arg[1:]
                target_id = db.get_user_by_username(username)
                if not target_id:
                    await update.message.reply_text("❌ Пользователь не найден")
                    return
            else:
                target_id = int(target_arg)
        elif update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        else:
            await update.message.reply_text("❌ Укажите пользователя")
            return

        db.add_team_balls(target_id, amount)
        await update.message.reply_text(f"✅ Пользователю {target_id} выдано {amount} тимбалов")

        try:
            await context.bot.send_message(target_id, f"🎉 Администратор выдал вам {amount} тимбалов!")
        except:
            pass

    except:
        await update.message.reply_text("❌ Использование: /give <количество> [@username или id]")


async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /ban для бана пользователя"""
    if not is_admin(update.effective_user.id):
        return

    try:
        if len(context.args) > 0:
            target_arg = context.args[0]
            if target_arg.startswith('@'):
                username = target_arg[1:]
                target_id = db.get_user_by_username(username)
                if not target_id:
                    await update.message.reply_text("❌ Пользователь не найден")
                    return
            else:
                target_id = int(target_arg)
        elif update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        else:
            await update.message.reply_text("❌ Укажите пользователя")
            return

        cursor = db.conn.cursor()
        cursor.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (target_id,))
        db.conn.commit()

        await update.message.reply_text(f"✅ Пользователь {target_id} забанен")

    except:
        await update.message.reply_text("❌ Использование: /ban [@username или id]")


async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /unban для разбана пользователя"""
    if not is_admin(update.effective_user.id):
        return

    try:
        if len(context.args) > 0:
            target_arg = context.args[0]
            if target_arg.startswith('@'):
                username = target_arg[1:]
                target_id = db.get_user_by_username(username)
                if not target_id:
                    await update.message.reply_text("❌ Пользователь не найден")
                    return
            else:
                target_id = int(target_arg)
        elif update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        else:
            await update.message.reply_text("❌ Укажите пользователя")
            return

        cursor = db.conn.cursor()
        cursor.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (target_id,))
        db.conn.commit()

        await update.message.reply_text(f"✅ Пользователь {target_id} разбанен")

    except:
        await update.message.reply_text("❌ Использование: /unban [@username или id]")


async def admin_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /warn для выдачи предупреждения"""
    if not is_admin(update.effective_user.id):
        return

    target_id = None

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_arg = context.args[0]
        if target_arg.startswith('@'):
            username = target_arg[1:]
            target_id = db.get_user_by_username(username)
        else:
            try:
                target_id = int(target_arg)
            except:
                pass

    if target_id:
        cursor = db.conn.cursor()
        cursor.execute('SELECT warnings FROM users WHERE user_id = ?', (target_id,))
        result = cursor.fetchone()

        if result:
            warnings = result[0] + 1
            cursor.execute('UPDATE users SET warnings = ? WHERE user_id = ?', (warnings, target_id))

            if warnings >= 3:
                cursor.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (target_id,))
                await update.message.reply_text(
                    f"⚠️ Пользователь {target_id} получил предупреждение ({warnings}/3). Достигнут лимит - забанен!")

                try:
                    await context.bot.send_message(target_id,
                                                   f"❌ Вы получили {warnings}/3 предупреждений и были забанены!")
                except:
                    pass
            else:
                await update.message.reply_text(f"⚠️ Пользователь {target_id} получил предупреждение ({warnings}/3)")

                try:
                    await context.bot.send_message(target_id, f"⚠️ Вы получили предупреждение ({warnings}/3)")
                except:
                    pass

            db.conn.commit()
            return

    await update.message.reply_text("❌ Ответьте на сообщение пользователя или используйте: /warn [@username или id]")


async def admin_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clear для очистки анкеты"""
    if not is_admin(update.effective_user.id):
        return

    target_id = None

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_arg = context.args[0]
        if target_arg.startswith('@'):
            username = target_arg[1:]
            target_id = db.get_user_by_username(username)
        else:
            try:
                target_id = int(target_arg)
            except:
                pass

    if target_id:
        cursor = db.conn.cursor()
        cursor.execute(
            'UPDATE users SET roblox_nickname = NULL, photo_id = NULL, game_modes = NULL, profile_verified = 0 WHERE user_id = ?',
            (target_id,))
        db.conn.commit()

        await update.message.reply_text(f"✅ Анкета пользователя {target_id} очищена")

        try:
            await context.bot.send_message(target_id,
                                           "⚠️ Ваша анкета была очищена администратором. Пожалуйста, создайте новую.")
        except:
            pass

        return

    await update.message.reply_text("❌ Ответьте на сообщение пользователя или используйте: /clear [@username или id]")


async def admin_clearpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clearpoint для очистки тимбалов"""
    if not is_admin(update.effective_user.id):
        return

    try:
        if len(context.args) > 0:
            target_arg = context.args[0]
            if target_arg.startswith('@'):
                username = target_arg[1:]
                target_id = db.get_user_by_username(username)
                if not target_id:
                    await update.message.reply_text("❌ Пользователь не найден")
                    return
            else:
                target_id = int(target_arg)
        elif update.message.reply_to_message:
            target_id = update.message.reply_to_message.from_user.id
        else:
            await update.message.reply_text("❌ Укажите пользователя")
            return

        cursor = db.conn.cursor()
        cursor.execute('UPDATE users SET team_balls = 0 WHERE user_id = ?', (target_id,))
        db.conn.commit()

        await update.message.reply_text(f"✅ Тимбалы пользователя {target_id} очищены")

    except:
        await update.message.reply_text("❌ Использование: /clearpoint [@username или id]")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats для статистики бота"""
    if not is_admin_or_verifier(update.effective_user.id):
        return

    cursor = db.conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM users WHERE profile_verified = 1')
    verified_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM users WHERE profile_verified = 0 AND roblox_nickname IS NOT NULL')
    pending_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1')
    banned_users = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM interactions WHERE is_like = 1')
    total_likes = cursor.fetchone()[0]

    cursor.execute('SELECT SUM(team_balls) FROM users')
    total_teamballs = cursor.fetchone()[0] or 0

    text = "<b>📊 Статистика бота:</b>\n\n"
    text += f"👥 Всего пользователей: {total_users}\n"
    text += f"✅ Верифицировано: {verified_users}\n"
    text += f"⏳ На проверке: {pending_users}\n"
    text += f"❌ Забанено: {banned_users}\n"
    text += f"👍 Всего лайков: {total_likes}\n"
    text += f"💰 Всего тимбалов в системе: {total_teamballs}\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users для просмотра пользователей"""
    if not is_admin_or_verifier(update.effective_user.id):
        return

    users = db.get_all_users()

    if not users:
        await update.message.reply_text("📭 Пользователей нет")
        return

    text = "<b>👥 Список пользователей:</b>\n\n"

    for user in users[:30]:
        user_id, username, roblox_nick, team_balls, is_banned, verified = user
        status = "❌" if is_banned else ("✅" if verified == 1 else ("⏳" if verified == 0 else "🚫"))
        text += f"{status} ID: {user_id} | @{username or 'нет'}\n"
        text += f"   Ник: {roblox_nick or 'нет'} | Тимбалы: {team_balls}\n\n"

    if len(users) > 30:
        text += f"\n... и еще {len(users) - 30} пользователей"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_verifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /verifications для просмотра анкет на проверке"""
    if not is_verifier(update.effective_user.id):
        return

    verifications = db.get_pending_verifications()

    if not verifications:
        await update.message.reply_text("📭 Нет анкет на проверке")
        return

    text = f"<b>📋 Анкеты на проверке:</b> {len(verifications)}\n\n"

    for i, (user_id, username, roblox_nick, photo_id, game_modes) in enumerate(verifications[:5], 1):
        text += f"<b>{i}. @{username or 'нет'}</b>\n"
        text += f"   ID: {user_id}\n"
        text += f"   Ник: {roblox_nick}\n"
        text += f"   Режимы: {game_modes}\n"

        keyboard = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}")
            ]
        ]

        try:
            if photo_id:
                await update.message.reply_photo(
                    photo=photo_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except:
            await update.message.reply_text(
                text + "\n🖼 Фото: (не удалось загрузить)",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        text = ""

    if len(verifications) > 5:
        await update.message.reply_text(f"📋 ... и еще {len(verifications) - 5} анкет на проверке")


async def admin_leaders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /leaders для показа топ-20 игроков по тимбалам"""
    if not is_admin(update.effective_user.id):
        return

    top_users = db.get_top_users_by_teamballs(20)

    if not top_users:
        await update.message.reply_text("📭 Нет пользователей в рейтинге")
        return

    text = "<b>🏆 Топ-20 игроков по тимбалам:</b>\n\n"

    for i, (user_id, username, roblox_nick, team_balls, verified) in enumerate(top_users, 1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
        text += f"{medal} <b>@{username or 'нет_username'}</b>\n"
        text += f"   <b>Ник в Roblox:</b> {roblox_nick or 'нет'}\n"
        text += f"   <b>Тимбалов:</b> {team_balls}\n"
        text += f"   <b>ID:</b> {user_id}\n\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =========== КОМАНДЫ ДЛЯ ВЕРИФИКАТОРОВ ===========
async def handle_approve_profile(query, context):
    """Одобряет анкету"""
    if not is_verifier(query.from_user.id):
        await query.answer("❌ У вас нет прав для этого")
        return

    user_id = int(query.data.split("_")[1])
    db.approve_profile(user_id)

    await query.edit_message_text(f"✅ Анкета пользователя {user_id} одобрена")

    try:
        await context.bot.send_message(
            user_id,
            "🎉 Ваша анкета одобрена! Теперь вы можете искать тиммейтов."
        )
    except:
        pass


async def handle_reject_profile(query, context):
    """Отклоняет анкету"""
    if not is_verifier(query.from_user.id):
        await query.answer("❌ У вас нет прав для этого")
        return

    user_id = int(query.data.split("_")[1])
    db.reject_profile(user_id)

    await query.edit_message_text(f"❌ Анкета пользователя {user_id} отклонена")

    try:
        await context.bot.send_message(
            user_id,
            "❌ Ваша анкета отклонена. Пожалуйста, создайте новую анкету."
        )
    except:
        pass


async def handle_admin_reply(query, context):
    """Обработчик кнопки ответа на поддержку"""
    if not is_admin_or_verifier(query.from_user.id):
        await query.answer("❌ У вас нет прав для этого")
        return

    user_id = int(query.data.split("_")[1])
    context.user_data["replying_to"] = user_id

    await query.message.reply_text(f"💌 Введите ответ для пользователя {user_id}:")


# =========== ОБЩИЕ ФУНКЦИИ ===========
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик отмены"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if query.data in ["cancel_message", "cancel_support"]:
        if user_id in user_states:
            del user_states[user_id]
        if "message_for_user" in context.user_data:
            del context.user_data["message_for_user"]

        # Показываем главное меню напрямую
        await query.edit_message_text(
            "🎮 <b>Бот для поиска тиммейтов в Roblox</b>\n\n"
            "Выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Моя анкета", callback_data="my_profile")],
                [InlineKeyboardButton("🔍 Искать тиммейта", callback_data="find_teammate")],
                [InlineKeyboardButton("🤝 Найденные тиммейты", callback_data="found_teammates")],
                [InlineKeyboardButton("🏪 Магазин", callback_data="shop")],
                [InlineKeyboardButton("🔗 Реф ссылка", callback_data="referral")],
                [InlineKeyboardButton("📞 Поддержка", callback_data="support")]
            ])
        )
        await query.message.reply_text(
            "Ты можешь всегда нажать на кнопку '🏠 В меню' чтобы вернуться сюда",
            reply_markup=get_menu_keyboard()
        )


def main():
    """Запуск бота"""
    application = Application.builder().token(TOKEN).build()

    # Команды пользователей
    application.add_handler(CommandHandler("start", start))

    # Команды админов
    application.add_handler(CommandHandler("give", admin_give))
    application.add_handler(CommandHandler("ban", admin_ban))
    application.add_handler(CommandHandler("unban", admin_unban))
    application.add_handler(CommandHandler("warn", admin_warn))
    application.add_handler(CommandHandler("clear", admin_clear))
    application.add_handler(CommandHandler("clearpoint", admin_clearpoint))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler("users", admin_users))
    application.add_handler(CommandHandler("leaders", admin_leaders))

    # Команды верификаторов
    application.add_handler(CommandHandler("verifications", admin_verifications))

    # Обработчики кнопок
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CallbackQueryHandler(handle_like, pattern="^like_"))
    application.add_handler(CallbackQueryHandler(handle_dislike, pattern="^dislike_"))
    application.add_handler(CallbackQueryHandler(handle_purchase, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(handle_approve_profile, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(handle_reject_profile, pattern="^reject_"))
    application.add_handler(CallbackQueryHandler(handle_admin_reply, pattern="^reply_"))

    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Запуск
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    print("Бот запускается...")
    print(f"Админы: {ADMIN_IDS}")
    print(f"Верификаторы: {VERIFIER_IDS}")
    print(f"Админы+Верификаторы: {ADMIN_AND_VERIFIER_IDS}")
    main()