from dotenv import load_dotenv
import os

load_dotenv()

API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
OPENAI_API = os.getenv('OPENAI_API_KEY')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
WEBAPP_URL = os.getenv('WEBAPP_URL')
SECRETS_ENCRYPTION_KEY = os.getenv("SECRETS_ENCRYPTION_KEY")