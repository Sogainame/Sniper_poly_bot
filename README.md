# Sniper Poly Bot

Бот для 5-минутных BTC Up/Down рынков на Polymarket.

## Как работает

1. Каждые 5 минут Polymarket открывает рынок: "BTC вырастет или упадёт?"
2. Бот ждёт до последней минуты (T-60 → T-20 секунд до конца)
3. Анализирует направление BTC по delta + momentum + tick consistency
4. Покупает UP или DOWN токен по $0.50-0.82
5. Если токен вырос на +$0.10 до закрытия — продаёт досрочно (фиксирует прибыль)
6. Если выиграл и не продал — Polymarket автоматически возвращает $1.00 за токен
7. Если проиграл — потерял стоимость токена

## Быстрый старт

```bash
# 1. Установка
git clone https://github.com/Sogainame/Sniper_poly_bot.git
cd Sniper_poly_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Настройка
cp .env.example .env
nano .env

# 3. Разрешения (один раз, нужен POL на кошельке для газа)
python set_allowances.py

# 4. Проверить состояние
python status.py

# 5. Запуск
python bot.py --asset btc --live --mode safe
```

## Скрипты

| Файл | Что делает | Когда запускать |
|------|-----------|-----------------|
| `bot.py` | Основной бот — торгует | Постоянно |
| `status.py` | Баланс, позиции, история сделок | Когда хочешь посмотреть состояние |
| `set_allowances.py` | Разрешения на продажу токенов | Один раз после депозита POL |
| `check_balance.py` | Проверка баланса USDC | Для отладки |

## Режимы

```bash
python bot.py --asset btc --live --mode safe        # Осторожный (25% Kelly)
python bot.py --asset btc --live --mode aggressive   # Агрессивный (50% Kelly)
python bot.py --asset btc --live --mode degen         # На всё (100% Kelly)
python bot.py --asset btc                             # Тест без денег
```

## Настройки BTC

| Параметр | Значение | Зачем |
|----------|----------|-------|
| Eval window | T-60 → T-20 | Вход в последнюю минуту |
| Min delta | 0.02% | Минимальное движение BTC |
| Max token | $0.82 | Не покупать дороже |
| Early exit | +$0.10 | Продать при росте токена на 10 центов |

## Файлы

```
bot.py              — Точка входа, CLI
sniper.py           — Ядро: цикл → сигнал → покупка → отслеживание → результат
signal_engine.py    — Анализ: delta + momentum + acceleration + consistency
assets.py           — Настройки по активам
market.py           — Polymarket API: цены, ордера, ордербук
notifier.py         — Telegram уведомления
config.py           — Загрузка .env
status.py           — Панель состояния
set_allowances.py   — Одноразовые разрешения
check_balance.py    — Отладка баланса
data/sniper/        — CSV логи сделок
```

## .env

```
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## VPS

Лучший вариант: Дублин, Ирландия (0.83ms до Polymarket).
Серверы Polymarket в Лондоне, Дублин — ближайшая незаблокированная страна.
