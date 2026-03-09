import requests
import json

# 测试后端 API
base_url = 'http://localhost:9000'

# 这是一个示例，展示如何使用从浏览器开发者工具中获取的 URL
# 1. 在浏览器中打开前端页面
# 2. 打开开发者工具 (通常是 F12)，切换到 "Console" 或 "控制台" 标签页
# 3. 在前端页面进行搜索或翻页等操作
# 4. 你会在控制台看到 "Request URL: /api/novels/?..." 这样的输出
# 5. 复制完整的 URL (从 /api 开始) 并将其粘贴到下面的 url_from_browser 变量中

url_from_browser = "/api/novels/?queries=%7B%22%E7%99%BE%E5%90%88%E7%A0%B4%E5%9D%8F%22:%22keyword%22%7D&order_by=like&order_direction=DESC&min_like=500&min_text=3000&per_page=15"

if not url_from_browser:
    print("请将从浏览器开发者工具中复制的 URL 粘贴到 url_from_browser 变量中")
else:
    full_url = f"{base_url}{url_from_browser}"
    
    try:
        print(f"Testing URL: {full_url}")
        response = requests.get(full_url, timeout=5)
        print(f'状态码: {response.status_code}')
        if response.status_code == 200:
            data = response.json()
            print(f'成功! 返回 {len(data.get("novels", []))} 本小说')
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f'错误: {response.text}')
    except requests.exceptions.ConnectionError:
        print('后端服务未运行，请先启动后端')
    except Exception as e:
        print(f'测试失败: {type(e).__name__}: {e}')
