import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # ID владельца бота
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

AI_PROMPT_TGM = os.getenv("AI_PROMPT_TGM", "")
AI_PROMPT_PM = os.getenv("AI_PROMPT_PM", "")
AI_PROMPT_GM = os.getenv("AI_PROMPT_GM", "")
AI_PROMPT_IS_RELEVANT_QUESTION = os.getenv("AI_PROMPT_IS_RELEVANT_QUESTION", "")

# Настройки
SILENCE_MINUTES = int(os.getenv("SILENCE_MINUTES", "10")) # На сколько время ставить на паузу бота в минутах

DB_PATH = "chat_history.db"
MODELS_CONFIG_PATH = "models.json"

MAX_MESSAGE_LENGTH = 4096
RATE_LIMIT_SECONDS_PER_USER = 1.0

# Глобальные состояния, которые шарятся между всеми модулями
user_selected_model = {}
last_request_time = {}
GLOBAL_SEMAPHORE = asyncio.Semaphore(1)
user_locks = {}
user_buffers = {} # Переменная для накопления быстрых сообщений
active_tasks = {}
silenced_chats = {}  # здесь храним, до какого времени чат молчит