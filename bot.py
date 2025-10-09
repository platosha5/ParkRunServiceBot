import os
import time
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from datetime import datetime, timedelta
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получение токена из переменных окружения Railway
BOT_TOKEN = os.environ.get('BOT_TOKEN')

def get_db_connection():
    """Установка соединения с PostgreSQL"""
    try:
        # Для Railway
        if 'DATABASE_URL' in os.environ:
            conn = psycopg2.connect(
                os.environ['DATABASE_URL'],
                cursor_factory=RealDictCursor
            )
        else:
            # Для локальной разработки
            conn = psycopg2.connect(
                host=os.environ.get('DB_HOST', 'localhost'),
                port=os.environ.get('DB_PORT', '5432'),
                database=os.environ.get('DB_NAME', 'parkrunning'),
                user=os.environ.get('DB_USER', 'postgres'),
                password=os.environ.get('DB_PASSWORD', ''),
                cursor_factory=RealDictCursor
            )
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")
        raise

def get_next_saturday():
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    return (today + timedelta(days=days_until_saturday)).strftime("%d.%m.%Y")

next_saturday = get_next_saturday()

def get_or_create_user(telegram_user):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT user_id, first_name, full_name, qr_code FROM users WHERE user_id = %s', 
            (telegram_user.id,)
        )
        user = cursor.fetchone()
        
        if user is None:
            cursor.execute('''
                INSERT INTO users (user_id, first_name, last_name, full_name, telegram_name)
                VALUES (%s, %s, %s, %s, %s)
            ''', (
                telegram_user.id,
                telegram_user.first_name or '',
                telegram_user.last_name or '',
                telegram_user.full_name or '',
                getattr(telegram_user, 'name', '') or '',
            ))
            conn.commit()
            
            cursor.execute(
                'SELECT user_id, first_name, full_name, qr_code FROM users WHERE user_id = %s', 
                (telegram_user.id,)
            )
            user = cursor.fetchone()
    
    return user

def get_or_create_event(location_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT event_id FROM events WHERE location_id = %s AND event_date = %s', 
            (location_id, next_saturday)
        )
        event = cursor.fetchone()
        
        if event is None:
            cursor.execute('''
                INSERT INTO events (location_id, event_date)
                VALUES (%s, %s)
            ''', (
                location_id,
                next_saturday
            ))
            conn.commit()
            
            cursor.execute(
                'SELECT event_id FROM events WHERE location_id = %s AND event_date = %s', 
                (location_id, next_saturday)
            )
            event = cursor.fetchone()
    
    return event

def get_event_data(location_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                R.role_id,
                R.role_full_name, 
                COALESCE(U.full_name, '') as volunteer_name,
                U.telegram_name
            FROM roles AS R
            LEFT JOIN volunteers AS V ON V.role_id = R.role_id 
                AND V.event_id IN (
                    SELECT event_id FROM events 
                    WHERE event_date = %s AND location_id = %s
                )
            LEFT JOIN users AS U ON U.user_id = V.user_id
            ORDER BY R.sort_id
        ''', (next_saturday, location_id))
        
        positions = cursor.fetchall()
        
        if not positions:
            return None
    
    return positions

def add_volunteer_to_event(role_text, user_id, event_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем role_id по названию роли
            cursor.execute(
                'SELECT role_id FROM roles WHERE role_full_name = %s', 
                (role_text,)
            )
            role_result = cursor.fetchone()
            
            if not role_result:
                logger.error(f"Роль '{role_text}' не найдена")
                return False
                
            role_id = role_result['role_id']
            
            # Проверяем, не записан ли уже пользователь на эту роль
            cursor.execute(
                'SELECT volunteer_id FROM volunteers WHERE event_id = %s AND user_id = %s', 
                (event_id, user_id)
            )
            existing_volunteer = cursor.fetchone()
            
            if existing_volunteer:
                # Обновляем существующую запись
                cursor.execute(
                    'UPDATE volunteers SET role_id = %s WHERE volunteer_id = %s',
                    (role_id, existing_volunteer['volunteer_id'])
                )
            else:
                # Создаем новую запись
                cursor.execute('''
                    INSERT INTO volunteers (user_id, role_id, event_id)
                    VALUES (%s, %s, %s)
                ''', (user_id, role_id, event_id))
            
            conn.commit()
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при добавлении волонтера: {e}")
        return False

def remove_volunteer_from_event(user_id, event_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM volunteers WHERE event_id = %s AND user_id = %s', 
                (event_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0
            
    except Exception as e:
        logger.error(f"Ошибка при отмене записи: {e}")
        return False

def check_parameters(user, location_id):
    if not user:
        return "⚠️ Сначала выполни команду /start"
        
    if not location_id:
        check_text = (
        "⚠️ Пожалуйста, выбери локацию, например: /location Ангарка\n"
        "Чтобы посмотреть список всех локаций набери /locationlist"
        )
        return check_text
    
    return None

def get_position_text(location_name, positions):
    position_lines = []
    for pos in positions:
        line = f"• {pos['role_full_name']}"
        if pos['volunteer_name']:
            line += f" - {pos['volunteer_name']}"
        if pos['telegram_name']:
            line += f" @{pos['telegram_name']}"
        position_lines.append(line)

    event_text = (
        f"Дата: {next_saturday}\n"
        f"Локация: {location_name}\n\n" 
        "📋 Список позиций\n\n" + "\n".join(position_lines) + "\n\n"
    )

    return event_text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(update.effective_user)
    context.user_data['current_user'] = user
    
    user_name = update.effective_user.first_name
    if not user_name:
        user_name = "друг"

    welcome_text = (
        f"Привет, {user_name}! 👋\n\n"
        "Я чат-бот Координатор волонтеров.\n"
        "Помогаю собрать команду на ближайший забег.\n\n"
    )
    await update.message.reply_text(welcome_text)

    check_text = check_parameters(user, None)

    if check_text:
        await update.message.reply_text(check_text)

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command_text = update.message.text
    user = context.user_data.get('current_user')
    location = context.user_data.get('current_location')
    event = context.user_data.get('current_event')

    if not user or not location:
        await update.message.reply_text("⚠️ Сначала выполни /start и выбери локацию через /location")
        return

    check_text = check_parameters(user, location['location_id'])

    if check_text:
        await update.message.reply_text(check_text)
        return

    if 'записаться' in command_text.lower():
        keyboard = [
            ["👨‍💼 Руководитель забега", "Координатор волонтеров", "💻 Обработка результатов"],
            ["🏃‍♂ Подготовка трассы", "🤸‍♂ Разминка", "🏃‍♂ Замыкающий"],
            ["⏱️ Секундомер", "🎫 Раздача карточек позиций", "📱 Сканер штрих-кодов"],
            ["📸 Фотограф", "☕ Буфет", "❓ Другое"],
        ]
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Выбери позицию ниже:", reply_markup=reply_markup)
        return
    
    if 'отменить' in command_text.lower():
        success = remove_volunteer_from_event(user['user_id'], event['event_id'])
        if success:
            positions = get_event_data(location['location_id'])
            if positions:
                await update.message.reply_text(get_position_text(location['location_name'], positions))
        else:
            await update.message.reply_text("❌ Не удалось отменить запись")
        return
       
    success = add_volunteer_to_event(command_text, user['user_id'], event['event_id'])
    if success:
        positions = get_event_data(location['location_id'])
        if positions:
            event_text = get_position_text(location['location_name'], positions)

        keyboard = [
            ["✍️ Записаться волонтером", "❌ Отменить запись"],
        ]

        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(event_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ Не удалось записаться на выбранную позицию")

async def location_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text.strip()
    
    if message_text.startswith('/location'):
        location_name = message_text[len('/location'):].strip()
    else:
        location_name = message_text

    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT location_id, location_name FROM locations WHERE location_name = %s AND statecode = 0 LIMIT 1", 
            (location_name,)
        )
        location = cursor.fetchone()
        
        if location:
            context.user_data['current_location'] = location

            event = get_or_create_event(location['location_id'])
            context.user_data['current_event'] = event

            positions = get_event_data(location['location_id'])

            if positions:
                event_text = get_position_text(location['location_name'], positions)

            keyboard = [
                ["✍️ Записаться волонтером", "❌ Отменить запись"],
            ]

            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text(event_text, reply_markup=reply_markup)

        else:
            event_text = "❌ Нет такой локации."  
            await update.message.reply_text(event_text)

        user = context.user_data.get('current_user')
        check_text = check_parameters(user, location['location_id'] if location else None)
        if check_text:
            await update.message.reply_text(check_text)   
            return

    except Exception as e:
        await update.message.reply_text(f"⚠️ Произошла ошибка при выборе локации: {str(e)}")
        logger.error(f"Ошибка в location_command: {e}")
        
    finally:
        cursor.close()
        conn.close()

async def location_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT location_name FROM locations WHERE statecode = 0 ORDER BY location_name")
        locations = cursor.fetchall()
        
        if locations:
            location_text = "📋 Список доступных локаций:\n\n" + "\n".join([f"• {loc['location_name']}" for loc in locations])
        else:
            location_text = "❌ Нет доступных локаций"
            
        await update.message.reply_text(location_text)
        
    except Exception as e:
        await update.message.reply_text(f"⚠️ Произошла ошибка при получении списка локаций: {str(e)}")
        logger.error(f"Ошибка в location_list: {e}")
        
    finally:
        cursor.close()
        conn.close()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **Команды бота:**\n"
        "/start - начать работу с ботом\n"
        "/locationlist - список всех локаций\n"
        "/help - получить справку\n\n"
        "📋 Что я умею:\n"
        "• Помогаю с записью в волонтеры\n"
        "• Показываю свободные позиции\n"
        "• Даю краткую сводку о выбранной позиции\n"
        "• Уведомляю руководителя забега о набранной команде\n"
    )
    await update.message.reply_text(help_text)

def init_database():
    """Инициализация таблиц в PostgreSQL"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Создаем таблицы, если они не существуют
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    statecode	INTEGER DEFAULT 0,
                    parkrun_id	INTEGER,
                    5verst_id	INTEGER,
                    runpark_id	INTEGER,
                    s95_id	INTEGER,
                    first_name TEXT,
                    last_name TEXT,
                    full_name TEXT,
                    telegram_name TEXT,
                    qr_code TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    modified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS locations (
                    location_id SERIAL PRIMARY KEY,
                    location_name TEXT UNIQUE NOT NULL,
                    statecode INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    modified_at	TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tme_chat TEXT NULL,
                    latitude REAL,
                    longitude REAL,
                    is_s95 INTEGER NOT NULL DEFAULT 0,
                    is_5verst INTEGER NOT NULL DEFAULT 0,
                    is_runpark INTEGER NOT NULL DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS roles (
                    role_id SERIAL PRIMARY KEY,
                    role_name TEXT UNIQUE NOT NULL,
                    role_full_name TEXT UNIQUE NOT NULL,
                    code TEXT NULL,
                    sort_id INTEGER DEFAULT 0,
                    is_uniq INTEGER DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS events (
                    event_id SERIAL PRIMARY KEY,
                    event_number INTEGER NULL,
                    location_id INTEGER REFERENCES locations(location_id),
                    event_date TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(location_id, event_date)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS volunteers (
                    user_id BIGINT REFERENCES users(user_id),
                    role_id INTEGER REFERENCES roles(role_id),
                    event_id INTEGER REFERENCES events(event_id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, role_id, event_id)
                )
            ''')
            
            # Добавляем базовые роли, если их нет
            base_roles = [
                ["Руководитель забега", "👨‍💼 Руководитель забега", "DIR", 1, 1],
                ["Координатор волонтеров", "Координатор волонтеров", "COORD", 2, 1], 
                ["Подготовка трассы", "🏃‍♂ Подготовка трассы", "PREPARE", 3, 0],
                ["Разминка", "🤸‍♂ Разминка", "WARMUP", 4, 1],
                ["Замыкающий", "🏃‍♂ Замыкающий", "LAST", 5, 0],
                ["Секундомер", "⏱️ Секундомер", "SEC", 6, 0],
                ["Раздача карточек позиций", "🎫 Раздача карточек позиций", "CARDS", 7, 0],
                ["Сканер штрих-кодов", "📱 Сканер штрих-кодов", "SCANNER", 8, 0],
                ["Фотограф", "📸 Фотограф", "PHOTO", 9, 0],
                ["Обработка результатов", "💻 Обработка результатов", "POST", 10, 1],
                ["Буфет", "☕ Буфет", "LUNCH", 11, 0],
                ["Другое", "❓ Другое", "ANOTHER", 12, 0]
            ]
            
            for role in base_roles:
                cursor.execute(
                    'INSERT INTO roles (role_name, role_full_name, code, sort_id, is_uniq) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (role_full_name) DO NOTHING',
                    role  # Передаем кортеж напрямую
                )
            
            conn.commit()
            logger.info("Таблицы базы данных инициализированы")
            
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен!")
        return
    
    # Инициализация базы данных
    init_database()
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("location", location_command))
    application.add_handler(CommandHandler("locationlist", location_list))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    
    logger.info("🚀 Бот запускается...")
    while True:
        try:
            application.run_polling()
        except Exception as e:
            logger.error(f"Ошибка: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)

if __name__ == "__main__":
    main()