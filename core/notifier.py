import requests
from core.config import config
from core.logger import logger

class TelegramNotifier:
    def __init__(self):
        self.token = config.get('telegram', {}).get('token')
        self.chat_id = config.get('telegram', {}).get('chat_id')
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send_message(self, text: str):
        if not self.token or not self.chat_id:
            logger.warning("Telegram configuration missing, skipping notification.")
            return

        try:
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }
            response = requests.post(self.api_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram notification sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    def send_task_result(self, task_name: str, status: str, duration: float = None, error: str = None, new_novels_count: int = None):
        if status == 'success':
            icon = "✅"
            text = f"{icon} *Task Completed*\n*Task:* `{task_name}`\n*Status:* {status}\n*Duration:* `{duration:.2f}s`"
            if new_novels_count is not None:
                text += f"\n*New Novels:* `{new_novels_count}`"
        else:
            icon = "❌"
            text = f"{icon} *Task Failed*\n*Task:* `{task_name}`\n*Status:* {status}\n*Error:* `{error}`"
        
        self.send_message(text)

notifier = TelegramNotifier()
