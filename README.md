# Sniper Poly Bot

Бот для 5-минутных BTC Up/Down рынков на Polymarket.

## Как работает

1. Каждые 5 минут Polymarket открывает рынок: "BTC вырастет или упадёт?"
2. Бот ждёт до последней минуты (T-60 → T-20 секунд до конца)
3. Анализирует направление BTC по delta + momentum + tick consistency
4. Покупает UP или DOWN токен по $0.50-0.86
5. Если токен вырос на +$0.06 до закрытия — продаёт досрочно
6. Если выиграл и не продал — Polymarket возвращает $1.00 за токен
7. Если проиграл — потерял стоимость токена

## Быстрый старт

```bash
git clone https://github.com/Sogainame/Sniper_poly_bot.git
cd Sniper_poly_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python3 bot.py --asset btc
```

## Режимы

```bash
python3 bot.py --asset btc --live --mode safe        # risk_scale=0.25
python3 bot.py --asset btc --live --mode aggressive   # risk_scale=0.50
python3 bot.py --asset btc --live --mode degen         # risk_scale=1.00
python3 bot.py --asset btc                             # dry-run
```

## Настройки BTC

| Параметр | Значение |
|----------|----------|
| Eval window | T-60 → T-20 |
| Min delta | 0.015% |
| Min confidence | 0.30 |
| Max token | $0.86 |
| Min token | $0.50 |
| Early exit | +$0.06 |
| Max spread | $0.06 |
| Confirm ticks | 2 |

## VPS

Stockholm (SE) или Dublin (IE). Polymarket серверы в eu-west-2 (London).
