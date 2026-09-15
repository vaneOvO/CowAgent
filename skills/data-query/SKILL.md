---
name: data-query
description: 查询各类数据信息，包括汇率、农历、历史事件、百科、油价、金价和化学元素。Use when users need exchange rates, lunar calendar, historical events, encyclopedia, commodity prices, or chemical element information.
license: MIT
metadata:
  author: vikiboss
  version: "1.0"
  api_base: https://60s.vaneus.ccwu.cc/v2
  tags:
    - data
    - information
    - query
    - reference
---

# Data Query Skill

Query various data and information including exchange rates, calendar, history, encyclopedia, and prices.

## Available Queries

1. **Exchange Rates** - Currency conversion rates
2. **Lunar Calendar** - Chinese lunar calendar conversion
3. **Today in History** - Historical events
4. **Encyclopedia (Baike)** - Search Chinese encyclopedia
5. **Fuel Prices** - Gasoline/diesel prices in China
6. **Gold Prices** - Current gold prices
7. **Chemical Elements** - Element information

## API Endpoints

| Query Type | Endpoint | Method |
|------------|----------|--------|
| Exchange Rate | `/v2/exchange-rate` | GET |
| Lunar Calendar | `/v2/lunar` | GET |
| History | `/v2/today-in-history` | GET |
| Encyclopedia | `/v2/baike` | GET |
| Fuel Price | `/v2/fuel-price` | GET |
| Gold Price | `/v2/gold-price` | GET |
| Chemical | `/v2/chemical` | GET |

## Quick Examples

### Exchange Rates

```python
import requests

# Get exchange rate
params = {'from': 'USD', 'to': 'CNY'}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/exchange-rate', params=params)
rate = response.json()

print(f"💱 1 {rate['from']} = {rate['rate']} {rate['to']}")
print(f"更新时间：{rate['update_time']}")
```

### Lunar Calendar

```python
# Get today's lunar date
response = requests.get('https://60s.vaneus.ccwu.cc/v2/lunar')
lunar = response.json()

print(f"📅 公历：{lunar['solar_date']}")
print(f"🏮 农历：{lunar['lunar_date']}")
print(f"🐲 生肖：{lunar['zodiac']}")
print(f"🌾 节气：{lunar['solar_term']}")

# Specific date
params = {'date': '2024-01-15'}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/lunar', params=params)
```

### Today in History

```python
# Get today's historical events
response = requests.get('https://60s.vaneus.ccwu.cc/v2/today-in-history')
history = response.json()

print(f"📜 历史上的今天 ({history['date']})")
for event in history['events'][:5]:
    print(f"{event['year']}年：{event['title']}")

# Specific date
params = {'month': 1, 'day': 15}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/today-in-history', params=params)
```

### Encyclopedia Search

```python
# Search encyclopedia
params = {'keyword': 'Python编程'}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/baike', params=params)
result = response.json()

print(f"📖 {result['title']}")
print(f"📝 {result['summary']}")
print(f"🔗 {result['url']}")
```

### Fuel Prices

```python
# Get fuel prices
params = {'province': '北京'}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/fuel-price', params=params)
prices = response.json()

print(f"⛽ {prices['province']} 油价")
print(f"92号汽油：{prices['92号汽油']} 元/升")
print(f"95号汽油：{prices['95号汽油']} 元/升")
print(f"98号汽油：{prices['98号汽油']} 元/升")
print(f"0号柴油：{prices['0号柴油']} 元/升")
```

### Gold Prices

```python
# Get current gold prices
response = requests.get('https://60s.vaneus.ccwu.cc/v2/gold-price')
gold = response.json()

print(f"💰 黄金价格")
print(f"国际金价：{gold['国际金价']} 美元/盎司")
print(f"国内金价：{gold['国内金价']} 元/克")
print(f"更新时间：{gold['update_time']}")
```

### Chemical Elements

```python
# Search chemical element
params = {'query': 'H'}  # Can be symbol, name, or atomic number
response = requests.get('https://60s.vaneus.ccwu.cc/v2/chemical', params=params)
element = response.json()

print(f"⚛️ {element['name']} ({element['symbol']})")
print(f"原子序数：{element['atomic_number']}")
print(f"原子量：{element['atomic_mass']}")
print(f"元素类别：{element['category']}")
```

## Use Cases

### Currency Converter Bot

```python
def convert_currency(amount, from_currency, to_currency):
    params = {'from': from_currency, 'to': to_currency}
    response = requests.get('https://60s.vaneus.ccwu.cc/v2/exchange-rate', params=params)
    rate = response.json()
    
    converted = amount * float(rate['rate'])
    return f"{amount} {from_currency} = {converted:.2f} {to_currency}"

# Usage
print(convert_currency(100, 'USD', 'CNY'))  # 100 USD = 725.50 CNY
```

### Historical Event Reminder

```python
def get_today_history():
    response = requests.get('https://60s.vaneus.ccwu.cc/v2/today-in-history')
    history = response.json()
    
    message = f"📜 历史上的今天 ({history['date']})\n\n"
    for event in history['events'][:3]:
        message += f"· {event['year']}年：{event['title']}\n"
    
    return message
```

### Lunar Calendar Widget

```python
def get_lunar_info():
    response = requests.get('https://60s.vaneus.ccwu.cc/v2/lunar')
    lunar = response.json()
    
    return f"""
📅 今日日历

公历：{lunar['solar_date']} {lunar['weekday']}
农历：{lunar['lunar_date']}
生肖：{lunar['zodiac']}
节气：{lunar['solar_term'] or '无'}
    """
```

## Example Interactions

### User: "美元兑人民币汇率是多少？"

```python
params = {'from': 'USD', 'to': 'CNY'}
response = requests.get('https://60s.vaneus.ccwu.cc/v2/exchange-rate', params=params)
rate = response.json()
print(f"💱 1 美元 = {rate['rate']} 人民币")
```

### User: "今天农历是几月几号？"

```python
lunar = requests.get('https://60s.vaneus.ccwu.cc/v2/lunar').json()
print(f"🏮 今天是农历 {lunar['lunar_date']}")
print(f"🐲 生肖：{lunar['zodiac']}")
```

### User: "查询一下氢元素的信息"

```python
params = {'query': '氢'}
element = requests.get('https://60s.vaneus.ccwu.cc/v2/chemical', params=params).json()
print(f"⚛️ {element['name']} (H)")
print(f"原子序数：{element['atomic_number']}")
print(f"元素类别：{element['category']}")
```

## Related Resources

- [60s API Documentation](https://docs.60s-api.viki.moe)
- [GitHub Repository](https://github.com/vikiboss/60s)
