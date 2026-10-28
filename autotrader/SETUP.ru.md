# Установка исполнителя на VPS и Telegram-бот, по шагам

Цель: процесс `autotrader/autotrader.py daemon` живёт на сервере в США,
торгует на бумажном счёте Alpaca и шлёт вам сообщения в Telegram.
Время на всё: около часа. Проверено на Ubuntu 24.04.

## 1. Сервер

Любой VPS с 1 vCPU, 1 ГБ RAM, Ubuntu 24.04, в регионе на востоке США
(ближе к Нью-Йорку, хотя для 5-минутных баров задержка не критична).
Два понятных варианта на октябрь 2026, цены сверьте на сайте провайдера:

- DigitalOcean, Droplet Basic 1 vCPU / 1 GB, регион NYC, около $6 в месяц.
  Самая простая панель.
- Vultr, Regular 1 vCPU / 1 GB, регион New Jersey, около $5–6 в месяц
  (план за $2.50 только IPv6, не подходит).

При создании выберите Ubuntu 24.04 и вход по SSH-ключу, не по паролю.
Ключ, если его нет, делается на вашем компьютере:

```bash
ssh-keygen -t ed25519 -C "day-trader"
```

Публичный ключ `~/.ssh/id_ed25519.pub` вставьте в форму провайдера.
После создания сервера запишите его IP и зайдите:

```bash
ssh root@IP_СЕРВЕРА
```

## 2. Базовая защита сервера (от root)

```bash
apt update && apt upgrade -y
apt install -y git python3-venv python3-pip ufw unattended-upgrades
ufw allow OpenSSH && ufw --force enable
timedatectl set-timezone America/New_York
dpkg-reconfigure -f noninteractive unattended-upgrades
```

Отдельный пользователь для торговли, без пароля, вход только через root:

```bash
adduser --disabled-password --gecos "" trader
```

## 3. Доступ к приватному репозиторию (deploy key)

Репозиторий приватный, поэтому серверу нужен свой ключ только на чтение.

```bash
sudo -iu trader
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "day-trader-vps"
cat ~/.ssh/id_ed25519.pub
```

Скопируйте строку и добавьте её на GitHub: репозиторий day-trader →
Settings → Deploy keys → Add deploy key, галочку «Allow write access»
не ставить. Затем:

```bash
git clone git@github.com:SirGio6262/day-trader.git
cd day-trader
```

На вопрос про отпечаток github.com ответьте yes.

## 4. Python-окружение и конфиг (от trader)

```bash
python3 -m venv venv && venv/bin/pip install -r autotrader/requirements.txt
mkdir -p trader && cp .claude/skills/trader-budget/config.example.json trader/config.json
nano trader/config.json
```

В конфиге поменяйте: `budget` (бюджет для расчёта размеров; на бумаге
логично поставить ту сумму, которой вы реально рискнёте потом),
`max_loss_per_trade_usd`, `max_daily_loss_usd`, `max_trades_per_day`,
`sec_user_agent` (имя и почта). Оставьте `"paper": true`. Добавьте два
блока: `journal` из `.claude/skills/trader-journal/SKILL.md` и, при
желании, `autotrader` из `autotrader/README.md` (без него работают
значения по умолчанию: 20 бумаг, 15-минутный диапазон, цель 2R).

Проверьте, что JSON валиден:

```bash
python3 -m json.tool trader/config.json > /dev/null && echo ok
```

## 5. Ключи Alpaca

В личном кабинете Alpaca переключитесь на Paper Trading и создайте API
key и secret (у бумажного счёта они свои). На сервере, от trader:

```bash
cat > /home/trader/day-trader.env <<'EOF'
ALPACA_API_KEY=ВАШ_PAPER_KEY
ALPACA_SECRET_KEY=ВАШ_PAPER_SECRET
ALPACA_PAPER_TRADE=true
EOF
chmod 600 /home/trader/day-trader.env
```

Ключи живут только в этом файле. Никогда в чат, никогда в репозиторий.

## 6. Telegram-бот (пять минут)

1. В Telegram откройте `@BotFather`, отправьте `/newbot`, задайте имя и
   username (должен кончаться на `bot`). BotFather пришлёт токен вида
   `123456789:AAH...`. Это секрет: кто знает токен, управляет ботом.
2. Найдите своего бота по username и отправьте ему любое сообщение,
   например «hi». Без этого бот не может вам писать.
3. На сервере (или в браузере) получите chat id:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getUpdates"
```

   В ответе найдите `"chat":{"id":123456789,...}`. Это ваш chat id
   (у личного чата положительное число).
4. Проверьте отправку:

```bash
curl -s -X POST "https://api.telegram.org/bot<ТОКЕН>/sendMessage" -d chat_id=<CHAT_ID> -d text="day-trader test"
```

   Сообщение должно прийти на телефон.
5. Допишите в env-файл две строки:

```bash
cat >> /home/trader/day-trader.env <<'EOF'
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН
TELEGRAM_CHAT_ID=ВАШ_CHAT_ID
EOF
```

## 7. Ручная проверка перед запуском сервиса

```bash
cd /home/trader/day-trader
set -a; . /home/trader/day-trader.env; set +a
venv/bin/python autotrader/autotrader.py check
```

Ожидаемый вывод: строка `config` с вашими лимитами и словом PAPER,
строка `account` со статусом ACTIVE и бумажным кэшем (по умолчанию
$100 000), строка `clock` с нью-йоркским временем и следующим открытием,
строка `data` с числом баров больше нуля, строка `telegram: set`.
Если где-то ошибка, пришлите вывод целиком.

Затем тесты:

```bash
venv/bin/python -m unittest discover -s tests
```

## 8. Сервис systemd (от root)

```bash
cp /home/trader/day-trader/autotrader/day-trader.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now day-trader
systemctl status day-trader
```

Статус должен быть `active (running)`. Демон сам спит до 9:45 ближайшего
торгового дня по часам Alpaca, отрабатывает сессию, спит дальше.

Логи:

```bash
tail -f /home/trader/day-trader/trader/state/autotrader.log
journalctl -u day-trader -n 50
```

## 9. Первая неделя

- В Telegram должны приходить: старт дня в 9:45, каждый ордер, каждое
  исполнение, закрытие в 15:55, вечерний отчёт журнала.
- Если день прошёл без единого сообщения, сервер или ключи не работают:
  `systemctl status day-trader` и `journalctl -u day-trader -n 100`.
- Экстренная остановка с телефона через SSH-клиент (Termius и подобные):

```bash
touch /home/trader/day-trader/trader/STOP
```

  Входы прекращаются, открытое закрывается. Удалите файл, чтобы
  возобновить.

## 10. Обновление кода

```bash
sudo -iu trader
cd day-trader && git pull
exit
systemctl restart day-trader
```

Обновляйте только после 16:05 по Нью-Йорку, не в сессию.

## 11. Переход на живой счёт

Только после вердикта GO от `/trader-journal report` на 60 закрытых
бумажных сделках этого же процесса. Тогда: живые ключи в env-файл,
`ALPACA_PAPER_TRADE=false`, `"paper": false` в конфиге, тот же
`max_loss_per_trade_usd`, `systemctl restart day-trader`. Процесс
откажется стартовать, если конфиг и env-файл не согласны.
