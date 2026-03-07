# Go2 Python — Unitree Go2 Robot Controller

Python-приложение для управления роботом Unitree Go2 через WebRTC. Порт проекта [Go2-Swift](../Go2-Swift).

## Установка

```bash
cd go2-python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

### Подключение

Робот и компьютер должны быть в одной сети:
- **AP режим** — подключитесь к Wi-Fi робота (`GoxxxxxxWiFi5G`), IP: `192.168.12.1`
- **STA режим** — робот и компьютер подключены к одному роутеру, укажите IP робота

### CLI — субкоманды

CLI построен на субкомандах. Все поддерживают `--json` для машинно-читаемого вывода (для нейросети).

```bash
# Интерактивный режим (по умолчанию)
python main.py --ip 192.168.1.66
python main.py --ip 192.168.1.66 interactive --telemetry

# Выполнить команды
python main.py --ip 192.168.1.66 exec stand_up
python main.py --ip 192.168.1.66 exec stand_up hello dance1 --delay 2
python main.py --ip 192.168.1.66 --json exec stand_up   # JSON-вывод

# Движение
python main.py --ip 192.168.1.66 move -x 0.3 -y 0 --yaw 0 -d 3    # вперёд 3 сек
python main.py --ip 192.168.1.66 move --forward 0.5 -d 2            # shortcut вперёд
python main.py --ip 192.168.1.66 move --turn-left 0.8 -d 1          # поворот влево 1 сек
python main.py --ip 192.168.1.66 --json move -x 0.3 -d 2            # JSON + состояние
python main.py --ip 192.168.1.66 --json --snap /tmp/go2.jpg move -x 0.5 -d 2  # + снимок после

# Установить параметр
python main.py --ip 192.168.1.66 set body_height 0.2
python main.py --ip 192.168.1.66 set speed_level 2
python main.py --ip 192.168.1.66 set gait 1          # trot
python main.py --ip 192.168.1.66 set euler 0.1,0,0   # наклон корпуса

# Телеметрия
python main.py --ip 192.168.1.66 telemetry                          # однократно
python main.py --ip 192.168.1.66 --json telemetry                   # JSON-телеметрия
python main.py --ip 192.168.1.66 --json telemetry -s -i 0.2         # потоковая, 5 Гц
python main.py --ip 192.168.1.66 --json telemetry -s -n 10 -i 0.1   # 10 замеров

# Камера / изображение
python main.py --ip 192.168.1.66 image -o snapshot.jpg               # один кадр
python main.py --ip 192.168.1.66 --json image -o -                   # base64 JSON на stdout
python main.py --ip 192.168.1.66 image -o frames.jpg -s -n 5 -i 0.5 # 5 кадров
python main.py --ip 192.168.1.66 --json image -o - -s -i 1           # потоковые кадры base64

# Сырая команда по API ID
python main.py --ip 192.168.1.66 raw 1008 -p '{"x":0.3,"y":0,"z":0}'

# Список всех команд, параметров, API ID
python main.py list
python main.py --json list
```

### Веб-интерфейс

```bash
python web.py
```

После запуска откройте [http://127.0.0.1:8080](http://127.0.0.1:8080), введите IP робота и нажмите `Connect`.

Для веб-интерфейса и LLM можно задать переменные окружения вручную или положить их в `.env` рядом с `web.py`.

```bash
export GO2_IP=192.168.1.90
export OPENAI_API_KEY=...
export GO2_LLM_MODEL=gpt-4.1-mini   # optional
export OPENAI_BASE_URL=https://api.openai.com/v1   # optional
python web.py
```

Или через `.env`:

```bash
GO2_IP=192.168.1.90
OPENAI_API_KEY=...
GO2_LLM_MODEL=gpt-4.1-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

В веб-интерфейсе можно ввести цель, например `подойти к двери и остановиться перед ней`, после чего кнопка `Analyze Current Frame` отправляет в LLM:
- текущий JPEG-кадр
- упрощенную телеметрию
- текст цели

LLM возвращает только рекомендацию следующего действия на один безопасный шаг вперед:
- `move` с малыми скоростями и короткой длительностью
- `stand_up`
- `stand_down`
- `none`, если безопасного действия нет

Рекомендация не выполняется автоматически.

При обычном запуске `web.py` шумные access-логи `aiohttp` и повторяющиеся сообщения про видео отключены. Для подробных логов можно запустить `python web.py --debug`.

### Использование нейросетью

Все команды с `--json` возвращают структурированный JSON, включающий состояние робота после команды.
Это позволяет нейросети в цикле: получить изображение + телеметрию -> принять решение -> послать команду.

```bash
# Цикл управления: получить кадр с телеметрией
python main.py --ip 192.168.1.66 --json image -o -
# -> {"ts": ..., "image_base64": "...", "state": {"battery_soc": 85, "position": [...], ...}}

# Послать команду, получить новое состояние
python main.py --ip 192.168.1.66 --json move -x 0.3 -d 1
# -> {"status": "ok", "command": "move", "state": {...}}
```

## Команды (exec)

| Команда | Действие |
|---------|----------|
| `stand_up` | Встать |
| `stand_down` | Лечь |
| `sit` | Сесть |
| `balance` | Балансировать стоя |
| `recovery` | Восстановить стойку |
| `stop` | Остановить движение |
| `damp` | Выключить моторы |
| `hello` | Помахать |
| `stretch` | Потянуться |
| `content` | Радостный жест |
| `wallow` | Валяться |
| `dance1` / `dance2` | Танцы |
| `front_flip` | Переднее сальто |
| `front_jump` | Прыжок вперёд |
| `front_pounce` | Бросок вперёд |
| `wiggle_hips` | Вилять бёдрами |
| `finger_heart` | Сердце пальцами |
| `handstand` | Стойка на руках |
| `cross_step` | Перекрёстный шаг |
| `bound` | Прыжки |
| `moon_walk` | Лунная походка |
| `economic_gait` | Экономичная походка |
| `lead_follow` | Следование за лидером |

## Параметры (set)

| Параметр | Значение |
|----------|----------|
| `body_height` | float — высота корпуса (0.1-0.32) |
| `foot_raise_height` | float — высота подъёма ноги |
| `speed_level` | int — уровень скорости 1-3 |
| `gait` | int — тип походки (0=idle, 1=trot, 2=run, 3=stairs) |
| `euler` | r,p,y — наклон корпуса (через запятую) |
| `video` | on/off — видеопоток |
| `audio` | on/off — аудиопоток |

## Программное использование

```python
import asyncio
from go2 import Go2Connection, SportCommand

async def main():
    conn = Go2Connection(robot_ip="192.168.1.66")
    await conn.connect()

    conn.stand_up()
    await asyncio.sleep(2)
    conn.move(x=0.3, y=0, yaw=0)  # вперёд
    await asyncio.sleep(3)
    conn.stop()

    print(f"Battery: {conn.state.battery.soc}%")
    print(f"Position: {conn.state.position}")

    await conn.disconnect()

asyncio.run(main())
```

## Структура

```
go2-python/
├── go2/
│   ├── __init__.py         # Экспорт Go2Connection, SportCommand
│   ├── constants.py        # IP, порты, топики
│   ├── crypto.py           # AES-256-ECB, AES-128-GCM, RSA, MD5 validation
│   ├── signaling.py        # HTTP signaling (8081 / 9991, шифрование)
│   ├── data_channel.py     # Маршрутизация сообщений, validation, heartbeat
│   ├── commands.py         # 47 команд SportCommand
│   ├── telemetry.py        # RobotState (IMU, батарея, моторы, позиция)
│   └── connection.py       # Go2Connection — WebRTC + управление
├── main.py                 # CLI
├── requirements.txt
└── README.md
```

## Протокол

Подключение через WebRTC с data channel. Signaling по HTTP:
- **Порт 9991** — новый протокол (RSA + AES-256-ECB шифрование SDP)
- **Порт 8081** — старый или новый протокол (зависит от прошивки)

Автоматически пробуются все варианты по очереди.

## Зависимости

- `aiortc` — WebRTC для Python
- `aiohttp` — HTTP клиент
- `pycryptodome` — AES, RSA криптография
