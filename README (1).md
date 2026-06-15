# DEAD MONEY — Live Trading Bot

Стратегия v4 (pyramid + trailing stop + time-based exit) на Binance USDT-M Futures.

## ⚠️ Перед запуском

1. Убедись что на Futures-кошельке есть $20 USDT
2. API ключ: Enable Futures = ON, Enable Withdrawals = OFF
3. Telegram бот создан, токен и chat_id получены

## Деплой на Railway

### 1. Залей этот код в приватный GitHub репозиторий

```bash
git init
git add .
git commit -m "Dead Money live bot"
git branch -M main
git remote add origin https://github.com/ТВОЙ_АККАУНТ/deadmoney-bot.git
git push -u origin main
```

### 2. В Railway

1. New Project → Deploy from GitHub repo → выбери этот репозиторий
2. Railway сам определит Python проект через requirements.txt
3. В разделе **Variables** добавь:

| Переменная | Значение |
|---|---|
| `BINANCE_API_KEY` | твой API key |
| `BINANCE_API_SECRET` | твой API secret |
| `TELEGRAM_BOT_TOKEN` | токен от BotFather |
| `TELEGRAM_CHAT_ID` | твой chat id |
| `LEVERAGE` | 20 |
| `RISK_PCT` | 2 |
| `MAX_POSITIONS` | 3 |
| `MIN_SCORE` | 70 |
| `MIN_WITNESSES` | 2 |
| `SCAN_INTERVAL_MIN` | 30 |
| `MAX_DAILY_LOSS_PCT` | 3 |
| `DIRECTION` | both |

4. Deploy. В Telegram придёт сообщение "DEAD MONEY bot started"

### 3. Volume для сохранения состояния (опционально, но рекомендуется)

Railway → Settings → Volumes → Add Volume → mount path `/data`
Это сохранит позиции/историю при перезапуске бота.

## Команды в Telegram

- `/status` — баланс и открытые позиции
- `/stats` — статистика по сделкам
- `/pause` — остановить открытие новых позиций (старые продолжат мониториться)
- `/resume` — снова включить
- `/closeall` — закрыть все позиции по рынку немедленно
- `/help` — список команд

## Логика стратегии (как в paper-trading-v4.html)

- Сканирует все USDT-M перпетуалы каждые 30 мин
- Multi-timeframe анализ: 1D + 4H + 1H + 15m
- Вход требует Score ≥ 70 и ≥2 из 3 "witnesses"
- SL = 1.5×ATR, TP1 = 2.5×ATR (закрытие 100% на TP1)
- Trailing stop активируется на 30% прогресса к TP1 (трейл 1.0×ATR)
- Pyramiding (+50% размера) на 50% прогресса к TP1
- Time exit: закрытие если позиция открыта >8ч и прогресс <30%
- Session filter: не торгует 20:00-02:00 UTC
- Correlation filter: максимум 2 позиции на сектор
- Commission filter: пропускает если комиссии съедают >50% прибыли TP1
- Daily loss limit: 3% от equity — бот останавливается на день

## ⚠️ Риски

- Плечо 20x = ликвидация при движении ~5% против позиции
- SL ставится сразу при входе, но в волатильные моменты возможен slippage
- Бот размещает РЕАЛЬНЫЕ ордера за РЕАЛЬНЫЕ деньги
- Начни с минимального капитала, мониторь первые дни через /status
