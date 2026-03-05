import requests
from io import BytesIO
from datetime import datetime
from core.config import config
from core.logger import logger

class TelegramNotifier:
    def __init__(self):
        self.token = config.get('telegram', {}).get('token')
        self.chat_id = config.get('telegram', {}).get('chat_id')
        self.message_api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.document_api_url = f"https://api.telegram.org/bot{self.token}/sendDocument"

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
            response = requests.post(self.message_api_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram notification sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    def send_document(self, caption: str, file_content: str, file_name: str):
        if not self.token or not self.chat_id:
            logger.warning("Telegram configuration missing, skipping notification.")
            return
        
        try:
            file_bytes = BytesIO(file_content.encode('utf-8'))
            files = {'document': (file_name, file_bytes, 'text/plain')}
            payload = {
                'chat_id': self.chat_id,
                'caption': caption,
                'parse_mode': 'Markdown'
            }
            response = requests.post(self.document_api_url, data=payload, files=files, timeout=20)
            response.raise_for_status()
            logger.info("Telegram document sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send Telegram document: {e}")

    def send_task_result(self, task_name: str, status: str, duration: float = None, error: str = None, new_novels_count: int = None, new_novel_titles: list[str] | None = None):
        if status == 'success':
            icon = "✅"
            text = f"{icon} *Task Completed*\n*Task:* `{task_name}`\n*Status:* {status}\n*Duration:* `{duration:.2f}s`"
            if new_novels_count is not None:
                text += f"\n*New Novels:* `{new_novels_count}`"
            
            if new_novel_titles:
                if len(new_novel_titles) > 10:
                    file_content = "\n".join(new_novel_titles)
                    file_name = f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    self.send_document(caption=text, file_content=file_content, file_name=file_name)
                else:
                    text += "\n*Novels:*\n"
                    for title in new_novel_titles:
                        text += f"- `{title}`\n"
                    self.send_message(text)
            else:
                self.send_message(text)
        else:
            icon = "❌"
            text = f"{icon} *Task Failed*\n*Task:* `{task_name}`\n*Status:* {status}\n*Error:* `{error}`"
            self.send_message(text)

notifier = TelegramNotifier()
