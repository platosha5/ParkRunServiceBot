import os
import time
from telegram import Update, ReplyKeyboardMarkup, User
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

# Определяем среду выполнения
IS_RAILWAY = os.environ.get('RAILWAY_ENVIRONMENT') is not None

if IS_RAILWAY:
    # На Railway используем переменные окружения
    BOT_TOKEN = os.environ.get('BOT_TOKEN')
    DATABASE_URL = os.environ.get('DATABASE_URL')
    logger.info("🚀 Режим: Railway (PostgreSQL)")
else:
    # Локально используем .env файл
    try:
        from dotenv import load_dotenv
        load_dotenv()
        BOT_TOKEN = os.environ.get('BOT_TOKEN')
        
        # Локальные настройки PostgreSQL
        DB_HOST = os.environ.get('DB_HOST', 'localhost')
        DB_PORT = os.environ.get('DB_PORT', '5432')
        DB_NAME = os.environ.get('DB_NAME', 'park_running')
        DB_USER = os.environ.get('DB_USER', 'park_user')
        DB_PASSWORD = os.environ.get('DB_PASSWORD', 'KX-p9CXS')
        
        DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        logger.info("💻 Режим: Локальная разработка (PostgreSQL)")
        
    except ImportError:
        logger.error("❌ Установите python-dotenv: pip install python-dotenv")
        exit(1)
        
def get_db_connection():
    try:
        if IS_RAILWAY:
            # Для Railway используем предоставленный URL
            if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
                database_url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
            else:
                database_url = DATABASE_URL
            conn = psycopg2.connect(database_url, sslmode='require')
        else:
            # Локально подключаемся напрямую с правильной кодировкой
            conn = psycopg2.connect(
                host=os.environ.get('DB_HOST', 'localhost'),
                port=os.environ.get('DB_PORT', '5432'),
                database=os.environ.get('DB_NAME', 'park_running'),
                user=os.environ.get('DB_USER', 'postgres'),  # Используем postgres по умолчанию
                password=os.environ.get('DB_PASSWORD', 'password')
            )
        
        logger.info("✅ Успешное подключение к PostgreSQL")
        return conn
        
    except Exception as e:
        # Декодируем ошибку правильно для Windows
        try:
            error_msg = str(e).encode('latin1').decode('cp1251')
        except:
            error_msg = str(e)
        logger.error(f"❌ Ошибка подключения к PostgreSQL: {error_msg}")
        return None

def get_next_saturday():
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    return (today + timedelta(days=days_until_saturday)).strftime("%Y-%m-%d")

next_saturday = get_next_saturday()

def get_or_create_user(telegram_user):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        telegram_id = telegram_user.id if telegram_user.id else None
        first_name = telegram_user.first_name if telegram_user.first_name else None
        last_name = telegram_user.last_name if telegram_user.last_name else None
        full_name = telegram_user.full_name if telegram_user.full_name else None
        telegram_name = getattr(telegram_user, 'name', '') or None
        user = None

        if telegram_id:
            cursor.execute(
                'SELECT user_id, telegram_id, first_name, last_name, full_name, telegram_name, qr_code FROM users WHERE telegram_id = %s', 
                (telegram_id,)
            )
            user = cursor.fetchone()
        
        if user is None and telegram_name:
            cursor.execute(
                'SELECT user_id, telegram_id, first_name, last_name, full_name, telegram_name, qr_code FROM users WHERE telegram_name = %s', 
                (telegram_name,)
            )
            user = cursor.fetchone()

        if user:
            db_user_id, db_telegram_id, db_first_name, db_last_name, db_full_name, db_telegram_name, qr_code = user
            
            if (db_telegram_id != telegram_id or db_first_name != first_name or 
                db_last_name != last_name or db_full_name != full_name):
                
                cursor.execute('''
                    UPDATE users 
                    SET telegram_id = %s, first_name = %s, last_name = %s, full_name = %s, telegram_name = %s
                    WHERE user_id = %s
                ''', (telegram_id, first_name, last_name, full_name, telegram_name, db_user_id))
                conn.commit()
        
        else:
            cursor.execute('''
                INSERT INTO users (telegram_id, first_name, last_name, full_name, telegram_name)
                VALUES (%s, %s, %s, %s, %s)
            ''', (
                telegram_id,
                first_name,
                last_name,
                full_name,
                telegram_name,
            ))
            conn.commit()
    
        cursor.execute(
            'SELECT user_id, telegram_id, first_name, full_name, telegram_name, qr_code FROM users WHERE telegram_name = %s', 
            (telegram_name,)
        )
        user = cursor.fetchone()
    except Exception as e:
        if conn:
            conn.rollback()
        error_text = f"{e}"    
        logger.error(error_text)
        return ""
    
    return user

def get_or_create_event(location_id):
    conn = get_db_connection()
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
    conn = get_db_connection()
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

def get_role_by_code(role_code):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT role_full_name FROM roles WHERE lower(role_code) = lower(%s)', 
            (role_code,)
        )
        role_result = cursor.fetchone()
        
        if not role_result:
            logger.error(f"Роль c кодом '{role_code}' не найдена")
            return ""

        return role_result[0] if role_result else ""
        
    except Exception as e:
        if conn:
            conn.rollback()
        error_text = f"Ошибка при получении названия роли по коду '{role_code}': {e}"    
        logger.error(error_text)
        return ""
    finally:
        if cursor:  
            cursor.close()
        if conn:
            conn.close()

def add_volunteer_to_event(role_text, user_id, event_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Получаем данные о роли и проверяем существование
        cursor.execute(
            'SELECT role_id, is_uniq FROM roles WHERE role_full_name = %s', 
            (role_text,)
        )
        role_result = cursor.fetchone()
        
        if not role_result:
            logger.error(f"Роль '{role_text}' не найдена")
            return False, f"Роль '{role_text}' не найдена"
            
        role_id, is_uniq = role_result

        # Проверяем, не записан ли уже пользователь
        cursor.execute(
            'SELECT 1 FROM volunteers WHERE user_id = %s AND role_id = %s AND event_id = %s', 
            (user_id, role_id, event_id)
        )
        if cursor.fetchone():
            logger.error(f"Ты уже записан на позицию '{role_text}'")
            return False, f"Ты уже записан на позицию '{role_text}'. Пожалуйста, выбери другую"

        # Проверяем уникальность роли
        if is_uniq:
            cursor.execute(
                'SELECT 1 FROM volunteers WHERE role_id = %s AND event_id = %s', 
                (role_id, event_id)
            )
            if cursor.fetchone():
                logger.error(f"Позиция '{role_text}' уже занята")
                return False, f"Позиция '{role_text}' уже занята. Пожалуйста, выбери другую"

        # Проверяем исключения
        exclusion1 = ["⏱️ Секундомер", "📱 Сканер штрих-кодов"]
        exclusion2 = ["⏱️ Секундомер", "🎫 Раздача карточек позиций"]
        
        cursor.execute(
            '''SELECT R.role_full_name 
               FROM volunteers V 
               JOIN roles R ON R.role_id = V.role_id 
               WHERE V.user_id = %s AND V.event_id = %s AND R.role_full_name IN %s''',
            (user_id, event_id, tuple(exclusion1 + exclusion2))
        )
        conflicting_roles = [row[0] for row in cursor.fetchall()]
        
        for exclusion_list in [exclusion1, exclusion2]:
            if role_text in exclusion_list:
                for conflicting_role in conflicting_roles:
                    if conflicting_role in exclusion_list:
                        result_test = f"Ты уже записан на позицию '{conflicting_role}', нельзя также записаться на '{role_text}'"
                        logger.error(result_test)
                        return False, result_test

        # Создаем новую запись
        cursor.execute(
            'INSERT INTO volunteers (user_id, role_id, event_id) VALUES (%s, %s, %s)',
            (user_id, role_id, event_id)
        )
        
        conn.commit()
        logger.info(f"Пользователь {user_id} успешно записан на роль '{role_text}' в мероприятии {event_id}")
        return True, "Ok"
            
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Ошибка при добавлении волонтера: {e}")
        return False, f"Ошибка при добавлении волонтера: {e}"
    finally:
        if conn:
            cursor.close()
            conn.close()

def remove_volunteer_from_event(user_id, event_id):
    try:
        conn = get_db_connection()
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
        line = f"• {pos[1]}"
        if pos[2]:
            line += f" - {pos[2]}"
        if pos[3]:
            line += f" {pos[3]}"
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
    command_list = update.message.text.strip().split()
    location_id = None

    user_name = update.effective_user.first_name
    if not user_name:
        user_name = "друг"

    welcome_text = (
        f"Привет, {user_name}! 👋\n\n"
        "Я чат-бот Координатор волонтеров.\n"
        "Помогаю собрать команду на ближайший забег.\n\n"
    )
    await update.message.reply_text(welcome_text)

    if len(command_list) == 2:  
        tg_group = command_list[1]
        conn = get_db_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT location_id, location_name FROM locations WHERE tg_group = %s AND statecode = 0 LIMIT 1", 
                (tg_group,)
            )
            location = cursor.fetchone()
            
            if location:
                location_id = location[0]
                context.user_data['current_location'] = {
                    'location_id': location[0],
                    'location_name': location[1]
                }

        except Exception as e:
            await update.message.reply_text(f"⚠️ Произошла ошибка при определении локации через {tg_group}: {str(e)}")
            logger.error(f"Ошибка при определении локации через {tg_group}: {e}")
            
        finally:
            cursor.close()
            conn.close()

    check_text = check_parameters(user, location_id)

    if check_text:
        await update.message.reply_text(check_text)
        return ""
    
    event = get_or_create_event(location_id) 
    context.user_data['current_event'] = event

    positions = get_event_data(location_id) 

    if positions:
        event_text = get_position_text(location[1], positions)  

    keyboard = [
        ["✍️ Записаться волонтером", "❌ Отменить запись"],
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(event_text, reply_markup=reply_markup)

async def handle_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        command_list = update.message.text.strip().split()

        current_user = update.effective_user
        current_telegram_name = getattr(current_user, 'name', '') or None

        location = context.user_data.get('current_location')
        location_id = location.get('location_id') if isinstance(location, dict) else location[0] if location and len(location) > 0 else None
        location_name = location.get('location_name') if isinstance(location, dict) else location[1] if location and len(location) > 1 else None
        
        event = get_or_create_event(location_id) 
        context.user_data['current_event'] = event
        event_id = event.get('event_id') if isinstance(event, dict) else event[0] if event and len(event) > 0 else None

        if not location or not event:
            await update.message.reply_text("⚠️ Сначала выполни /start и выбери локацию через /location")
            return

        positions = get_event_data(location_id)
        coord_positions = [row for row in positions if row[1] == 'Координатор волонтеров' and row[3] == current_telegram_name]

        if not coord_positions:
            await update.message.reply_text("⚠️ Для записи других сначала запишись на позицию 'Координатов волонтеров'")
            return

        if len(command_list) == 2:  
            command = command_list[0]  # команда (/dir, /scan, /cards)
            tg_name = command_list[1]
            if tg_name.startswith('@'):
                tg_name = tg_name[1:]
            
            command_text = get_role_by_code(command.lstrip('/'))  
            
            try:
                user = User(
                    id=0,
                    first_name=None,
                    is_bot = False,
                    username = tg_name,
                )

                tg_user = get_or_create_user(user)
                user_id = tg_user[0]
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось найти пользователя {tg_name}")
                logger.error(f"Ошибка получения пользователя: {e}")
                return

        else:
            await update.message.reply_text("❌ Неверный формат команды. Используйте: /команда @username")
            return

        result = add_volunteer_to_event(command_text, user_id, event_id)
        
        if result and result[0]:  
            positions = get_event_data(location_id)
            event_text = ""
            
            if positions:
                event_text = get_position_text(location_name, positions)
            else:
                event_text = f"📍 {location_name}\n\nНет доступных позиций"

            keyboard = [
                ["✍️ Записаться волонтером", "❌ Отменить запись"],
            ]

            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text(event_text, reply_markup=reply_markup)
        else:
            error_message = result[1] if result and len(result) > 1 else "Неизвестная ошибка"
            await update.message.reply_text(f"❌ {error_message}")
            
    except Exception as e:
        logger.error(f"Ошибка в handle_commands: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке команды")

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    command_text = update.message.text
    user = context.user_data.get('current_user')
    location = context.user_data.get('current_location')
    event = context.user_data.get('current_event')

    if not user or not location:
        await update.message.reply_text("⚠️ Сначала выполни /start и выбери локацию через /location")
        return

    user_id = user.get('user_id') if isinstance(user, dict) else user[0] if user else None
    location_id = location.get('location_id') if isinstance(location, dict) else location[0] if location else None
    location_name = location.get('location_name') if isinstance(location, dict) else location[1] if location else None
    event_id = event.get('event_id') if isinstance(event, dict) else event[0] if event else None

    check_text = check_parameters(user, location_id)

    if check_text:
        await update.message.reply_text(check_text)
        return

    if 'записаться' in command_text.lower():
        keyboard = [ 
            ["Координатор волонтеров", "💻 Обработка результатов", "Маршал"],
            ["🏃‍♂ Подготовка трассы", "🤸‍♂ Разминка", "🏃‍♂ Замыкающий"],
            ["⏱️ Секундомер", "🎫 Раздача карточек позиций", "📱 Сканер штрих-кодов"],
            ["📸 Фотограф", "☕ Буфет", "❓ Другое"],
        ]
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Выбери позицию ниже:", reply_markup=reply_markup)
        return
    
    if 'отменить' in command_text.lower():
        success = remove_volunteer_from_event(user_id, event_id)
        if success:
            positions = get_event_data(location_id)
            if positions:
                await update.message.reply_text(get_position_text(location_name, positions))
        else:
            await update.message.reply_text("❌ Не удалось отменить запись")
        return 

    result = add_volunteer_to_event(command_text, user_id, event_id)
    if result[0]:
        positions = get_event_data(location_id)
        if positions:
            event_text = get_position_text(location_name, positions)

        keyboard = [
            ["✍️ Записаться волонтером", "❌ Отменить запись"],
        ]

        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(event_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(result[1])

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
            context.user_data['current_location'] = {
                'location_id': location[0],
                'location_name': location[1]
            }

            event = get_or_create_event(location[0]) 
            context.user_data['current_event'] = event

            positions = get_event_data(location[0]) 

            if positions:
                event_text = get_position_text(location[1], positions)  

            keyboard = [
                ["✍️ Записаться волонтером", "❌ Отменить запись"],
            ]

            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text(event_text, reply_markup=reply_markup)

        else:
            event_text = "❌ Нет такой локации."  
            await update.message.reply_text(event_text)

        user = context.user_data.get('current_user')
        check_text = check_parameters(user, location[0] if location else None)  
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
            location_text = "📋 Список доступных локаций:\n\n" + "\n".join([f"• {loc[0]}" for loc in locations])
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

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("location", location_command))
    application.add_handler(CommandHandler("locationlist", location_list))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    
    command_filter = filters.Regex(r'^(/dir|/scanner|/cards|/sec|/another|/photo|/coord|/prepare)')
    application.add_handler(MessageHandler(command_filter, handle_commands))

    logger.info("🚀 Бот запускается...")

    if IS_RAILWAY:
        logger.info("📍 Режим: Railway")
    else:
        logger.info("📍 Режим: Локальная разработка (PostgreSQL)")

    while True:
        try:
            application.run_polling()
        except Exception as e:
            logger.error(f"Ошибка: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)

if __name__ == "__main__":
    main()