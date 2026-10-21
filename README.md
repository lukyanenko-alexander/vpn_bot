# VPN Telegram Bot

Telegram-бот для управления VPN-подписками через панель [Remnawave](https://remnawave.io).  
Выдаёт, показывает и продлевает подписки, хранит данные в PostgreSQL, фоновые задачи — в ARQ (Redis).

## Стек

| Слой | Технология |
|---|---|
| Бот | Python 3.12, aiogram 3 |
| БД | PostgreSQL 16 + SQLAlchemy 2 async + Alembic |
| Очередь | ARQ 0.26 (asyncio + Redis) |
| FSM / throttling | Redis 7 |
| HTTP-клиент | httpx async |
| Конфиг | pydantic-settings |

## Быстрый старт

```bash
cp .env.example .env   # заполнить BOT_TOKEN, REMNAWAVE_URL, REMNAWAVE_TOKEN
docker compose up -d
```

Миграции применяются автоматически контейнером `migrate` при старте.

## Архитектура

```
Telegram → Bot (aiogram)
               ├── DatabaseMiddleware   # AsyncSession per request
               ├── UserMiddleware       # drop unknown users (no /start)
               └── ThrottlingMiddleware # Redis rate-limit 1 req/s per user

                       ↓ SubscriptionService
                       ├── PostgreSQL (subscriptions, users)
                       └── Remnawave API (httpx)

ARQ Worker (отдельный процесс)
  ├── create_subscription_in_panel   # recovery: pending creates
  ├── extend_subscription_in_panel   # recovery: pending extends
  ├── send_notification              # Telegram-уведомления
  ├── scan_and_notify        [cron]  # каждую минуту: expiring / expired
  ├── recover_pending        [cron]  # каждую минуту: зависшие создания
  └── recover_pending_extends[cron]  # каждую минуту: зависшие продления
```

## Решённые проблемы

### 1. Создание ключа — согласованность панели и БД

Проблема: бот может упасть после записи в БД, но до вызова панели.

Решение — двухфазный подход:
1. `INSERT INTO subscriptions (status='pending')` — коммит транзакции
2. Вызов Remnawave `POST /api/users`
3. Успех → `UPDATE status='active'`
4. Ошибка → `enqueue_job("create_subscription_in_panel")` для ARQ-recovery

Крон `recover_pending` каждую минуту находит подписки со `status='pending'` старше 30 секунд и переставляет задачу в очередь. Задача идемпотентна: сначала делает `GET by_username`, создаёт только если пользователя нет.

### 2. Продление ключа — согласованность + 90-дневный лимит

Проблема: два параллельных клика «Продлить» могут оба пройти проверку лимита и оба продлить.

Решение:
1. `SELECT FOR UPDATE` — берём эксклюзивную блокировку на строку подписки
2. Проверка лимита 90 дней
3. `UPDATE expires_at` + `pending_panel_expires_at = new_expires` — внутри той же транзакции под блокировкой
4. Коммит — блокировка снята, второй конкурентный запрос видит уже обновлённую дату
5. Вызов панели вне транзакции
6. Успех → `clear pending_panel_expires_at`
7. Ошибка → ARQ-recovery через поле `pending_panel_expires_at`

Крон `recover_pending_extends` каждую минуту находит подписки с ненулевым `pending_panel_expires_at` и переставляет задачу в очередь.

### 3. Защита от двойного создания (конкурентные запросы)

Частичный уникальный индекс в БД:
```sql
CREATE UNIQUE INDEX idx_subscriptions_one_active
    ON subscriptions(user_id) WHERE status IN ('pending', 'active');
```

`SELECT FOR UPDATE` + этот индекс гарантируют: из N параллельных кликов только один INSERT проходит, остальные находят существующую запись.

### 4. Уведомления — дедупликация

Таблица `notification_events` с `UNIQUE(subscription_id, event_type)`.  
`INSERT ON CONFLICT DO NOTHING` — отправка гарантированно происходит не более одного раза, даже при повторном запуске задачи после краша.

### 5. Circuit Breaker — Redis, работает в нескольких процессах

In-memory circuit breaker не подходит: бот и воркер — разные процессы.

Реализация через Redis:
- `INCR circuit:remnawave:failures` + `EXPIRE` только при первом инкременте (скользящее окно без сдвига)
- При достижении порога 5 ошибок за 300 секунд: `SET circuit:remnawave:open NX EX 60`
- `NX` — только первый процесс открывает, остальные не сбрасывают TTL
- Через 60 секунд ключ истекает → полуоткрытое состояние → один probe-запрос

### 6. Ретраи и ошибки панели

| Ситуация | Поведение |
|---|---|
| 5xx, ConnectError, ReadTimeout | Retry с backoff 1→2→4 сек, считается в circuit breaker |
| 429 Too Many Requests | Retry после `Retry-After` из заголовка, не считается в CB |
| 401 / 403 | ERROR-лог «check REMNAWAVE_TOKEN», не retry, не CB |
| 4xx (400, 422, ...) | Немедленное исключение, не retry, не CB |
| 404 | Не retry, не CB (панель жива, ресурс не найден) |
| POST (create_user) | Без retry — не идемпотентен, recovery через ARQ |

### 7. Graceful shutdown

Бот дожидается завершения текущих обработчиков, затем закрывает:
- SQLAlchemy connection pool (`engine.dispose()`)
- httpx-клиент Remnawave (`aclose()`)
- ARQ Redis pool, FSM Redis соединение

### 8. Безопасность пользователей без /start

`UserMiddleware` дропает любые апдейты от пользователей, не прошедших `/start`.  
На callback отвечает «Нажмите /start». Предотвращает FK-ошибки при создании подписки.

## Нагрузочные характеристики

| Параметр | Значение |
|---|---|
| PostgreSQL pool | 10 соединений + 20 overflow |
| ARQ max_jobs | 100 параллельных задач |
| Job timeout | 60 секунд |
| Keep result TTL | 60 секунд |
| Throttling | 1 запрос/сек на пользователя |
| Circuit breaker threshold | 5 ошибок за 300 сек |
| Cron granularity | 1 раз в минуту |

При одном экземпляре бота и одном воркере комфортно держит **~1 000–3 000 активных пользователей** с пиками до 200–300 одновременных кликов (ограничение — Telegram Bot API rate limits, не БД).

## Масштабирование

**Горизонтальное масштабирование бота:**  
Запустить несколько реплик `bot` — всё состояние в PostgreSQL + Redis, гонок нет благодаря `SELECT FOR UPDATE` и уникальным индексам.

**Горизонтальное масштабирование воркера:**  
ARQ сам распределяет задачи между несколькими воркерами через Redis. Cron-задачи выполняются на каждом воркере — идемпотентность через `_job_id` дедупликацию в ARQ.

**БД:**  
При росте нагрузки добавить pgBouncer (transaction pooling) перед PostgreSQL — уменьшит число реальных соединений при большом числе реплик.

**Remnawave:**  
Circuit breaker разделён через Redis — все процессы видят единое состояние. При недоступности панели бот продолжает работать: новые ключи ставятся в очередь recovery.

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота |
| `REMNAWAVE_URL` | Базовый URL панели |
| `REMNAWAVE_TOKEN` | Bearer-токен для API панели |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host/db` |
| `REDIS_URL` | `redis://host:6379/0` |
| `ADMIN_IDS` | JSON-массив Telegram ID администраторов, напр. `[123456789]` |
| `LOG_LEVEL` | `INFO` / `DEBUG` / `WARNING` |

## Команды администратора

| Команда | Описание |
|---|---|
| `/stats` | Число пользователей и активных подписок |
| `/user <tg_id>` | Детали пользователя и его подписки |
