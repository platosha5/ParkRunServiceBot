import os
import time
import sqlite3
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from datetime import datetime, timedelta
import logging

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получение токена из переменных окружения
BOT_TOKEN = os.environ.get('BOT_TOKEN')

sqlConnectionName = 'DbParkRunning.db'

def get_next_saturday():
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    return (today + timedelta(days=days_until_saturday)).strftime("%d.%m.%Y")

next_saturday = get_next_saturday()

def get_db_connection():
    return sqlite3.connect(sqlConnectionName, timeout=10.0)

def get_or_create_user(telegram_user):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT user_id, first_name, full_name, qr_code FROM user WHERE user_id = ?', 
            (telegram_user.id,)
        )
        user = cursor.fetchone()
        
        if user is None:
            cursor.execute('''
                INSERT INTO user (user_id, first_name, last_name, full_name, telegram_name)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                telegram_user.id,
                telegram_user.first_name or '',
                telegram_user.last_name or '',
                telegram_user.full_name or '',
                getattr(telegram_user, 'name', '') or '',
            ))
            conn.commit()
            
            cursor.execute(
                'SELECT user_id, first_name, full_name, qr_code FROM user WHERE user_id = ?', 
                (telegram_user.id,)
            )
            user = cursor.fetchone()
    
    return user

def get_or_create_event(location_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
                'SELECT event_id FROM event WHERE location_id = ? AND event_date = ?', 
                (location_id, next_saturday)
            )
        event = cursor.fetchone()
        
        if event is None:
            cursor.execute('''
                INSERT INTO event (location_id, event_date)
                VALUES (?, ?)
            ''', (
                location_id,
                next_saturday
            ))
            conn.commit()
            
            cursor.execute(
                'SELECT event_id FROM event WHERE location_id = ? AND event_date = ?', 
                (location_id, next_saturday)
            )
            event = cursor.fetchone()
    
    return event

def get_event_data(location_id):
    with get_db_connection()  as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                R.role_id,
                R.role_full_name, 
                COALESCE(U.full_name, '') as volunteer_name
            FROM role AS R
            LEFT JOIN volunteer AS V ON V.role_id = R.role_id 
                AND V.event_id IN (
                    SELECT event_id FROM event 
                    WHERE event_date = ? AND location_id = ?
                )
            LEFT JOIN user AS U ON U.user_id = V.user_id
            ORDER BY R.sort_id
        ''', (next_saturday, location_id))
        
        positions = cursor.fetchall()
        
        if not positions:
            return None
    
    return positions

def add_volunteer_to_event(role_text, user_id, event_id):
    try:
        # Сначала нужно получить role_id из базы данных
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем role_id по названию роли
            cursor.execute(
                'SELECT role_id FROM role WHERE role_full_name = ?', 
                (role_text,)
            )
            role_result = cursor.fetchone()
            
            if not role_result:
                logger.error(f"Роль '{role_text}' не найдена")
                return False
                
            role_id = role_result[0]
            
            cursor.execute('''
                INSERT INTO volunteer (user_id, role_id, event_id)
                VALUES (?, ?, ?)
            ''', (user_id, role_id, event_id))
            conn.commit()
                   
            return cursor.rowcount > 0
            
    except Exception as e:
        logger.error(f"Ошибка при добавлении волонтера: {e}")
        return False
    
def remove_volunteer_from_event(user_id, event_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM volunteer WHERE event_id = ? AND user_id = ?', 
                (event_id, user_id)
            )
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
    event_text = (
        f"Дата: {next_saturday}\n"
        f"Локация: {location_name}\n\n" 
        "📋 Список позиций\n\n" + "\n".join([f"• {pos[1]}" + (f" - {pos[2]}" if pos[2] else "") for pos in positions]) + "\n\n"
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

    check_text = check_parameters(user, location[1])

    if check_text:
        await update.message.reply_text(check_text)

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
        remove_volunteer_from_event(user[0], event[0])
        positions = get_event_data(location[0])

        if positions:
            await update.message.reply_text(get_position_text(location[1], positions))

        return
       
    add_volunteer_to_event(command_text, user[0], event[0])
    positions = get_event_data(location[0])

    if positions:
        event_text = get_position_text(location[1], positions)

    keyboard = [
    ["✍️ Записаться волонтером", "❌ Отменить запись"],
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(event_text, reply_markup=reply_markup)

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
            "SELECT location_id, location_name FROM location WHERE location_name = ? AND statecode = 0 LIMIT 1", 
            (location_name,)
        )
        location = cursor.fetchone()
        
        if location:
            context.user_data['current_location'] = location

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
        check_text = check_parameters(user, location[0])
        if check_text:
            await update.message.reply_text(check_text)   
            return

    except Exception as e:
        await update.message.reply_text(f"⚠️ Произошла ошибка при выборе локации: {str(e)}")
        
    finally:
        cursor.close()
        conn.close()

async def location_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection() 
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT location_name FROM location WHERE statecode = 0 ORDER BY location_name")
        locations = cursor.fetchall()
        
        if locations:
            location_text = "📋 Список доступных локаций:\n\n" + "\n".join([f"• {loc[0]}" for loc in locations])
        else:
            location_text = "❌ Нет доступных локаций"
            
        await update.message.reply_text(location_text)
        
    except Exception as e:
        await update.message.reply_text(f"⚠️ Произошла ошибка при получении списка локаций: {str(e)}")
        
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
    
    while True:
        try:
            logger.info("Запуск бота...")
            application.run_polling()
        except Exception as e:
            logger.error(f"Ошибка: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)

if __name__ == "__main__":
    main()