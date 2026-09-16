# 🚀 РУКОВОДСТВО ПО ЗАПУСКУ НА ХОСТИНГЕ

**INDIA SUBBOT** — Telegram-бот + Mini App магазин (v26)
Последняя проверка сборки: регрессия 26/26 PASS.

---

## Содержимое релиза

```
bot_fixed/
├── main.py              # точка входа: бот + HTTP-сервер (health, Mini App, API)
├── config.py            # чтение настроек из .env
├── webapp_api.py        # API Mini App (12 эндпоинтов, HMAC-авторизация)
├── webapp/              # фронтенд Mini App (v26: паритет + анимации)
│   ├── index.html
│   ├── style.css
│   └── app.js
├── handlers/ services/ models/ keyboards/ utils/   # логика бота
├── catalog.json         # каталог товаров (редактируй под свои цены)
├── deploy/
│   ├── indiasubbot.service   # systemd-юнит (автозапуск на VPS)
│   └── nginx.conf.example    # конфиг nginx (HTTPS + прокси)
├── .env.example         # шаблон настроек — скопировать в .env
├── requirements.txt     # зависимости Python
└── upload_custom_emoji.py  # (опция) загрузка кастомных эмодзи-пака
```

Один процесс обслуживает и бота, и магазин: Mini App доступен на
`https://твой-домен/app/`, API — на `/api/*`. Отдельный хостинг для
сайта НЕ нужен.

---

## ШАГ 0. Что нужно перед стартом

| Что | Где взять |
|---|---|
| Токен бота | @BotFather → /mybots → твой бот → API Token |
| Telegram ID админов | @userinfobot (через запятую, если несколько) |
| VPS или аккаунт Render | см. Вариант A (VPS) или Вариант B (Render) |
| Домен (только для VPS) | любой регистратор; A-запись на IP сервера |
| Кошелёк TON/USDT | адрес своего кошелька для приёма оплат |
| Ключи Digiseller / Tribute | только если принимаешь банковские карты |
| ENCRYPTION_KEY | сгенерируй командой ниже — обязателен в проде |

Сгенерировать ключ шифрования (хранит логины/пароли аккаунтов в БД
в зашифрованном виде):

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## ШАГ 1. Заполни .env

```bash
cp .env.example .env
nano .env   # или mcedit/vim
```

Минимально обязательное для запуска магазина:

```ini
BOT_TOKEN=1234567890:AA...ваш_токен
ADMIN_IDS=123456789

# --- Хостинг/магазин ---
PORT=8000                        # на Render НЕ ставь (платформа задаёт сама)
WEBAPP_URL=https://твой-домен.ру # публичный URL, БЕЗ /app/ на конце
STORE_NAME=SUBSTORE              # название в шапке магазина

# --- Оплаты (включи нужные) ---
TON_WALLET_ADDRESS=UQ...         # Gram/TON
TONCENTER_API_KEY=               # toncenter.com — быстрее без лимитов
TONAPI_KEY=                      # tonapi.io — надёжная проверка USDT

DIGISELLER_SELLER_ID=            # карты через Digiseller (или Tribute ниже)
DIGISELLER_API_KEY=
DIGISELLER_PRODUCT_ID=

TRIBUTE_API_KEY=                 # карты через Tribute (вместо Digiseller)

# --- Безопасность ---
ENCRYPTION_KEY=...               # сгенерированный ключ из ШАГА 0
```

`WEBAPP_URL` — важнейшая переменная: после старта бот сам поставит
кнопку «🛍 Магазин» в меню чата, а deep links (`startapp=svc_...`)
начнут открывать нужные экраны.

---

## ВАРИАНТ A. VPS (Ubuntu 22.04/24.04) — рекомендуется

Подходит для Digiseller/Tribute и стабильной работы 24/7.
Хватит самого дешёвого тарифа: 1 CPU / 1 GB RAM.

### 1. Подготовка сервера

```bash
ssh root@IP_СЕРВЕРА
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip nginx git
```

### 2. Загрузка проекта

Залей папку `bot_fixed` на сервер (любым способом):

```bash
# со своего компьютера (scp):
scp -r bot_fixed.zip root@IP_СЕРВЕРА:/opt/
# затем на сервере:
cd /opt && unzip bot_fixed.zip && mv bot_fixed indiasubbot
```

### 3. Зависимости и .env

```bash
cd /opt/indiasubbot
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env        # заполни по ШАГУ 1
chmod 600 .env   # права: только владелец
```

### 4. Пробный запуск (проверка, что всё живо)

```bash
venv/bin/python main.py
```

В логе должно появиться:

```
Database initialized
Catalog loaded: 5 services
Health server listening on 0.0.0.0:8000
Mini App mounted: /app/ + /api/...
Run polling for bot @...
```

Останови Ctrl+C. Если есть ошибки — смотри раздел
«Проблемы и решения» ниже.

### 5. Автозапуск через systemd

```bash
cp deploy/indiasubbot.service /etc/systemd/system/
# если делал venv — поправь ExecStart в юните:
sed -i 's|/usr/bin/python3|/opt/indiasubbot/venv/bin/python|' \
  /etc/systemd/system/indiasubbot.service
systemctl daemon-reload
systemctl enable --now indiasubbot
systemctl status indiasubbot     # должно быть active (running)
journalctl -u indiasubbot -f     # живые логи (выход Ctrl+C)
```

Теперь бот перезапускается сам после падений и ребутов сервера.

### 6. Домен + HTTPS (nginx + certbot)

```bash
nano /etc/nginx/sites-available/indiasubbot
# вставь содержимое deploy/nginx.conf.example,
# замени «твой-домен.ру» на свой домен
ln -s /etc/nginx/sites-available/indiasubbot /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

apt install -y certbot python3-certbot-nginx
certbot --nginx -d твой-домен.ру
```

### 7. Финальный штрих — перезапуск бота

```bash
systemctl restart indiasubbot
```

При старте с заполненным `WEBAPP_URL` бот сам поставит кнопку
«🛍 Магазин» в меню чата (лог: `Mini App menu button set → ...`).

---

## ВАРИАНТ B. Render.com — без домена и настройки

Самый быстрый способ поднять магазин, если не хочешь возиться с VPS.

> **Про бесплатный тариф (v27.5).** На Free сервис «засыпает» без входящего
> HTTP-трафика ~15 минут — вместе с процессом умирает поллинг, и бот
> перестаёт отвечать до следующего запроса. В v27.5 встроен анти-сон:
> бот сам пингует свой `WEBAPP_URL/health` раз в 10 минут (первые ~30 с
> после старта) — сервис живёт круглосуточно в пределах бесплатных
> 750 ч/мес (один всегда-живой сервис ≈ 730 ч). Для этого держи
> РОВНО ОДИН сервис на аккаунте: два — и часы кончатся к середине
> месяца. Если хочешь подстраховаться — добавь внешний пингер
> (см. «Render Free: чтобы бот не засыпал» ниже).

1. **Залей код на GitHub** (private-репозиторий, `.env` НЕ загружай!).

2. **Render → New → Web Service** → подключи репозиторий:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** Free — теперь допустимо: встроенный keep-alive
     (v27.5) не даёт сервису заснуть. Хочешь гарантию без пингов —
     Starter ($7/мес).

3. **Environment → Add** все переменные из ШАГА 1, КРОМЕ `PORT` —
   Render задаёт его сам. `WEBAPP_URL` впиши ПОСЛЕ первого деплоя,
   когда узнаешь выданный URL вида `https://имя.onrender.com`:
   - сначала оставь `WEBAPP_URL` пустым и задеплой;
   - затем добавь `WEBAPP_URL=https://имя.onrender.com` — Render
     перезапустит сервис автоматически.

4. **Проверка:** открой `https://имя.onrender.com/health` — должно
   ответить `ok v27.5 bot=@ТвойБот (id 123…)`, а
   `https://имя.onrender.com/app/` — показать магазин.

### Render Free: чтобы бот не засыпал (v27.5)

**Встроенный анти-сон (включён по умолчанию).** Работает сам, если:
- `WEBAPP_URL` = URL твоего сервиса (обязательно!);
- переменная `KEEPALIVE` не выключена (по умолчанию `on`).

Как проверить, что он жив: в логах Render должны появляться строки
`Keep-alive: включён — пинг …/health каждые 10 мин` и далее
`Keep-alive: … отвечает — сервис не уснёт`. Отключить: `KEEPALIVE=off`.
Интервал: `KEEPALIVE_INTERVAL_SEC` (сек, минимум 300, по умолчанию 600).

**Подстраховка — внешний бесплатный пингер (по желанию).** Если встроенному
не доверяешь (или WEBAPP_URL пуст):
1. Зарегистрируйся на **cron-job.org** или **UptimeRobot** (бесплатно).
2. Добавь монитор: HTTP GET `https://имя.onrender.com/health`, интервал
   5–10 минут.
3. Всё — платформа считает сервис активным и не глушит его.

**Важно про лимит часов.** Free-аккаунт Render получает 750 часов работы
сервисов в месяц. Один всегда-живой бот ≈ 730–744 ч — влезает. Второй
сервис (или забытый тестовый) съедит лимит — и в конце месяца уснут ОБА.
Лишние сервисы — Delete.

**И про базу.** На Free нет постоянного диска: при редких пересозданиях
контейнера SQLite (`subscriptions.db`) сбрасывается на состояние деплоя.
Если заметил пропажу заказов после Manual Deploy — это оно; для истории
платежей периодически скачивай базу или переезжай на VPS.

> Render не даёт persistent-диск на бесплатном плане: SQLite-база
> может сбрасываться при пересоздании контейнера. Для надёжности
> периодически бэкапь `subscriptions.db` или используй VPS.

### Render: не открывается магазин — что проверить (по порядку)

1. **Правильный URL.** Магазин живёт на `/app/`, а НЕ на корне:
   `https://имя.onrender.com/app/` (со слэшем). Корень `/` теперь сам
   перебрасывает в магазин (страница «Открываем магазин…»).
   Быстрые пробы в браузере:
   - `https://имя.onrender.com/health` → `ok v27.3 bot=@ТвойБот (id 123…)`
   - `https://имя.onrender.com/api/catalog` → JSON со списком сервисов
   - `https://имя.onrender.com/app/` → витрина
2. **Личность бота (v27.3) — проверь ПЕРВЫМ ДЕЛОМ.** В `/health` теперь
   показано, бот какого токена живёт на этом сервисе: `bot=@Имя (id 123…)`.
   Сравни id с ПЕРВОЙ ЦИФРОЙ токена в Environment (число до двоеточия).
   - id НЕ совпал → на этом сервисе чужой BOT_TOKEN (частая ловушка:
     сервисов на Render два, а WEBAPP_URL смотрит не на тот, или токен
     от тестового бота). Исправь BOT_TOKEN или перенаправь WEBAPP_URL
     рабочего бота на ЭТОТ сервис.
   - `bot=? (getMe failed)` → токен вообще недействителен (удалён
     в BotFather / опечатка).
   Пока бот на сервисе «не тот», все заказы из магазина падают с
   «bad signature» в логах — подпись initData делает ДРУГОЙ бот.
3. **Сервис спит (Free-план).** Первый запрос может открываться
   30–60 секунд («waking up»). Если ответ приходит после паузы —
   это сон бесплатного тарифа. С v27.5 бот пингует сам себя и спать
   не должен: если всё же «waking up» — проверь, что `WEBAPP_URL`
   заполнен и в логах есть строки `Keep-alive` (см. выше).
4. **Тип сервиса.** Нужен **Web Service** (Python), а не Static Site:
   у статики нет бэкенда — `/api/*` и бот там не работают.
5. **Логи (вкладка Logs) — там ответ почти всегда. Типовые строки:**
   - `BOT_TOKEN is not set!` → добавь переменную `BOT_TOKEN` в
     Environment → Save → произойдёт редеплой.
   - `can't open file '/opt/render/project/src/main.py'` → в корне
     репозитория нет main.py: содержимое `bot_fixed/` должно лежать
     в корне репо (либо поменяй команды: `pip install -r bot_fixed/requirements.txt`
     и `python bot_fixed/main.py` — проект везде использует пути от
     своего файла, так что и вложенный вариант работает).
   - `ModuleNotFoundError: No module named 'aiogram'` → Build Command
     пустой; впиши `pip install -r requirements.txt`.
   - `address already in use` → ты задал `PORT` вручную. Убери его:
     Render передаёт свой `PORT` сам, дубликат ломает бинд.
   - `Mini App mount failed` → в репо нет папки `webapp/` — залей её.
   - `initData: bad signature … проверьте BOT_TOKEN` → магазин открыт
     из бота A, а сервис работает с токеном бота B. Сверь id в `/health`
     с токеном в Environment (п. 2 выше).
   - `getMe failed — BOT_TOKEN недействителен` → токен на сервисе не
     подходит даже для API: исправь BOT_TOKEN в Environment → Save.
6. **После успеха:** добавь `WEBAPP_URL=https://имя.onrender.com`
   (Environment) — после автопередеплоя в чате появится кнопка
   «🛍 Магазин», а deep links станут открывать нужные экраны.
7. Если `/health` отвечает `ok`, а `/app/` отдаёт 404 — папки `webapp/`
   нет в репозитории (проверь вкладку Commit/Files) или ты выложил
   только часть файлов из архива.

---

## ШАГ 2. Проверка после деплоя (чек-лист)

| № | Проверка | Ожидание |
|---|---|---|
| 1 | `https://домен/health` | `OK` |
| 2 | `https://домен/app/` в обычном браузере | витрина с 5 сервисами |
| 3 | /start боту в Telegram | приветствие + скидка 10% |
| 4 | Кнопка «🛍 Магазин» в меню чата | открывает Mini App |
| 5 | Заказ в Mini App (Stars — самый простой тест) | экран оплаты со счётчиком |
| 6 | Оплата/отмена, история, профиль | работают без ошибок |
| 7 | Заказ из чата появляется в «Заказах» Mini App | единая база |

Тестовые платежи удобно гонять на Telegram Stars (XTR) — там нет
внешних реквизитов.

---

## ШАГ 3. Кастомные эмодзи (опция)

Премиум-пользователям вместо SVG-иконок показываются фирменные
кастомные эмодзи. Чтобы включить:

```bash
# один раз: загрузить пак (нужен OWNER_USER_ID в .env — твой Telegram ID)
venv/bin/python upload_custom_emoji.py
```

Скрипт напечатает карту ID — вставь их в `webapp/app.js`
(константа `CUSTOM_EMOJI_IDS`) и поставь `USE_CUSTOM_EMOJI=true`.
Без этого шага всё работает на SVG — это нормальный режим.

---

## Обновление версии на сервере

```bash
# VPS:
systemctl stop indiasubbot
# залить новые файлы поверх /opt/indiasubbot (кроме .env и *.db!)
systemctl start indiasubbot

# Render: push в GitHub → деплой запустится сам
```

**Никогда не перезаписывай на сервере:** `.env` (там секреты) и
`subscriptions.db` (там заказы и пользователи).

---

## Проблемы и решения

| Симптом | Причина / решение |
|---|---|
| В логе `PORT not set — health server skipped` | не заполнен `PORT` в `.env` (или юнит без `Environment=PORT=8000`). Mini App недоступен. |
| `Health server ... address already in use` | порт занят другим процессом: `ss -tlnp \| grep 8000`, смени порт или останови процесс. |
| Магазин открывается, но всё «Без сети» | `WEBAPP_URL` не совпадает с реальным доменом, либо HTTPS-сертификат не выпущен. Mini App требует валидный HTTPS. |
| `InitData validation failed` / 401 в /api | открыл магазин вне Telegram, либо сильно расходятся часы на сервере: `apt install systemd-timesyncd && timedatectl set-ntp true`. |
| Кнопка «Магазин» не появилась в меню | пустой `WEBAPP_URL` при старте. Заполни и `systemctl restart indiasubbot`. |
| Оплата TON/USDT не подтверждается | проверь `TON_WALLET_ADDRESS` и ключи Toncenter/TonAPI; посмотри лог `journalctl -u indiasubbot -f` в момент оплаты. |
| «Банковская карта» пишет про shop not found | магазин в @tribute не создан, либо `TRIBUTE_API_KEY` неверный. |
| Digiseller не находит платёж | проверь `DIGISELLER_API_KEY` (право «Статистика продаж») и `DIGISELLER_PRODUCT_ID` (товар с произвольной ценой). |
| На Render сервис проснулся, заказы «повисли» | это Free-тариф; апгрейд до Starter либо VPS. |

---

## Безопасность (памятка)

- `.env` — права `600`, никогда в git/архивы для публичной выкладки.
- `ENCRYPTION_KEY` в проде обязателен: без него данные аккаунтов
  хранятся открытым текстом (бот предупредит в логе).
- Обновляй сервер: `apt update && apt upgrade` раз в неделю.
- Бэкап: раз в день копируй `subscriptions.db` (боту достаточно
  остановки на 2 секунды) — например, в cron с выкладкой в облако.
