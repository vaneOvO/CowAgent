---
name: daily-news-60s
description: 获取每天60秒读懂世界的每日新闻，包含15条精选国内外新闻和每日微语。Use when users need daily news summaries, current events, or want to stay informed about world news in Chinese.
license: MIT
metadata:
  author: vikiboss
  version: "1.0"
  api_base: https://60s.vaneus.ccwu.cc/v2
  tags:
    - news
    - daily
    - china
    - information
---

# 每天60秒读懂世界 - Daily News Skill

This skill helps AI agents fetch and present daily curated news from the 60s API, which provides 15 selected news items plus a daily quote, updated every 30 minutes.

## When to Use This Skill

Use this skill when users:
- Ask for today's news or current events
- Want a quick daily briefing
- Request news summaries in Chinese
- Need historical news from a specific date
- Want news in different formats (text, markdown, image)

## API Endpoint

**Base URL:** `https://60s.vaneus.ccwu.cc/v2/60s`

**Method:** GET

## Parameters

- `date` (optional): Date in YYYY-MM-DD format (e.g., "2024-01-15")
  - If not provided, returns the latest available news
  
- `encoding` (optional): Output format
  - `json` (default): Structured JSON data
  - `text`: Plain text format
  - `markdown`: Formatted markdown
  - `image`: Redirect to image URL
  - `image-proxy`: Returns image binary data

## How to Use

### Basic Usage - Get Latest News

```bash
curl "https://60s.vaneus.ccwu.cc/v2/60s"
```

```python
import requests

response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s')
news = response.json()

print(f"📰 {news['date']} 新闻简报")
print(f"农历：{news['lunar_date']} {news['day_of_week']}\n")

for i, item in enumerate(news['news'], 1):
    print(f"{i}. {item['title']}")
    
print(f"\n💭 微语：{news['tip']}")
```

### Get News for Specific Date

```python
response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s', params={'date': '2024-01-15'})
```

### Get News as Markdown

```python
response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s', params={'encoding': 'markdown'})
markdown_content = response.text
```

### Get News as Plain Text

```python
response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s', params={'encoding': 'text'})
text_content = response.text
```

## Response Format (JSON)

```json
{
  "date": "2024-01-15",
  "day_of_week": "星期一",
  "lunar_date": "腊月初五",
  "news": [
    {
      "title": "新闻标题1",
      "link": "https://example.com/news1"
    },
    ...
  ],
  "tip": "每日微语内容",
  "image": "https://..../image.png",
  "updated": "2024-01-15 09:00:00",
  "updated_at": 1705280400000,
  "api_updated": "2024-01-15 09:00:00",
  "api_updated_at": 1705280400000
}
```

## Example Interactions

### User Request: "今天有什么新闻？"

**Agent Response:**
```
📰 2024年1月15日 星期一 农历腊月初五

【今日要闻】
1. 新闻标题1
2. 新闻标题2
3. 新闻标题3
...

💭 微语：[每日微语内容]
```

### User Request: "Get yesterday's news"

```python
from datetime import datetime, timedelta

yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s', params={'date': yesterday})
```

## Best Practices

1. **Caching**: The API has built-in caching, responses are very fast
2. **Update Frequency**: News updates every 30 minutes, typically by 10 AM
3. **Error Handling**: Always check response status and handle errors gracefully
4. **Format Selection**: Use JSON for structured data, markdown for formatted output, text for simple presentation
5. **Date Validation**: When requesting specific dates, ensure the date format is YYYY-MM-DD

## Common Use Cases

### 1. Daily News Bot
```python
def send_morning_news():
    news = requests.get('https://60s.vaneus.ccwu.cc/v2/60s').json()
    message = f"早安！今天是 {news['date']} {news['day_of_week']}\n\n"
    message += "\n".join([f"{i}. {item['title']}" for i, item in enumerate(news['news'][:5], 1)])
    message += f"\n\n{news['tip']}"
    return message
```

### 2. News Summary for Chatbots
```python
def get_news_summary(count=5):
    news = requests.get('https://60s.vaneus.ccwu.cc/v2/60s').json()
    return {
        'date': news['date'],
        'headlines': [item['title'] for item in news['news'][:count]],
        'quote': news['tip']
    }
```

### 3. Historical News Lookup
```python
def get_historical_news(date_str):
    response = requests.get('https://60s.vaneus.ccwu.cc/v2/60s', params={'date': date_str})
    if response.ok:
        return response.json()
    return None
```

## Troubleshooting

### Issue: No data returned
- **Solution**: Try requesting previous dates (yesterday or the day before)
- The service tries latest 3 days automatically

### Issue: Image not loading
- **Solution**: Use `encoding=image-proxy` instead of `encoding=image`
- The proxy endpoint directly returns image binary data

### Issue: Old date requested
- **Solution**: Data is only available for recent dates
- Check the response status code

## API Characteristics

- ✅ **Free**: No authentication required
- ✅ **Fast**: Millisecond-level cached responses
- ✅ **Reliable**: Global CDN acceleration
- ✅ **Updated**: Every 30 minutes
- ✅ **Quality**: 15 curated news items from authoritative sources

## Related Resources

- [60s API Documentation](https://docs.60s-api.viki.moe)
- [GitHub Repository](https://github.com/vikiboss/60s)
- [Data Source Repository](https://github.com/vikiboss/60s-static-host)
