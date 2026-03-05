My personal pixiv novel downloader & manager with webui.  
Base on high version of [pixivpy3](https://github.com/upbit/pixivpy), maybe install manually from git.  
Pixiv account and its refresh token is needed, see [gppt](https://github.com/eggplants/get-pixivpy-token) for more details.

# Usage
prepare:
```bash
python -m venv .venv 
source .venv/bin/activate
pip install -r requirements.txt 
```
frontend:
```bash
cd frontend
npm run build
npm run dev
```
backend:
```bash
python main.py
```