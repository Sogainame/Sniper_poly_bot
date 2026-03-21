# Polymarket Auto-Redeem

Автоматически забирает выигрышные позиции Polymarket через Builder Relayer (gasless Safe/Proxy flow).

## Важно

- Используется **proxy wallet** (не EOA) — это адрес который виден в интерфейсе Polymarket
- Нужны **Builder API credentials** — получить на https://polymarket.com/settings?tab=builder
- Работает только для **обычных non-negative-risk** рынков (5-min BTC и т.д.)
- Auto-redeem — **отдельный процесс**, не встроен в торговый цикл бота
- Redeem идёт через `@polymarket/builder-relayer-client`, не через py-clob-client и не через прямой EOA вызов

## Установка

```bash
cd tools/auto-redeem
cp .env.example .env
# Заполнить .env реальными значениями
npm install
```

## Заполнение .env

```
PRIVATE_KEY=0x_приватный_ключ_EOA
PROXY_WALLET=0x_адрес_proxy_кошелька_Polymarket
BUILDER_API_KEY=из_Builder_панели
BUILDER_SECRET=из_Builder_панели
BUILDER_PASSPHRASE=из_Builder_панели
WALLET_TYPE=PROXY   # PROXY (signature_type=1) или SAFE (signature_type=2)
DRY_RUN=true        # true = только логи, false = реальный redeem
```

## Использование

### Один проход (тест)

```bash
# Сначала в DRY_RUN=true — проверить что видит redeemable позиции
node auto-redeem.mjs --once

# Потом переключить DRY_RUN=false и запустить реально
node auto-redeem.mjs --once
```

### Непрерывный режим

```bash
node auto-redeem.mjs
```

### Фоновый режим (nohup)

```bash
nohup node auto-redeem.mjs > redeem.log 2>&1 &
```

### Systemd

```bash
sudo cp redeem.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable redeem
sudo systemctl start redeem
sudo journalctl -u redeem -f
```

## Как работает

1. Каждые 60 секунд запрашивает redeemable позиции через Data API
2. Для каждой позиции где `redeemable=true` и `size>0`:
   - Пропускает `negativeRisk=true`
   - Пропускает уже обработанные (`done`/`pending` в state)
   - Формирует calldata `redeemPositions(USDC.e, 0x00..00, conditionId, [1,2])`
   - Отправляет через `client.execute([tx], "redeem positions")`
   - Записывает результат в `redeem-state.json`

3. Параметры redeem (из Polymarket docs):
   - `collateralToken` = `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (USDC.e)
   - `parentCollectionId` = `0x00...00`
   - `indexSets` = `[1, 2]` — обе стороны, выплата только по победившей

## Файлы

| Файл | Назначение |
|------|-----------|
| `auto-redeem.mjs` | Основной сервис |
| `.env.example` | Шаблон переменных окружения |
| `redeem-state.json` | Дедупликация |
| `redeem.service` | Systemd unit |
| `package.json` | Node.js зависимости |
