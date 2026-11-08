# nsy-server — приём видео на НСУ

Наземная часть проекта «Сквозь помехи». Принимает WFB/RTP, декодирует H.264 и
MJPEG, показывает видео в браузере и возвращает метрики бортовому контроллеру.
Камера и бортовая адаптация находятся в [bvs-camera](https://github.com/Through-interference/bvs-camera).
Исходники Raspberry Pi для запуска этого проекта не нужны.

## Установка на ноутбуке

Требуется Linux с Python 3.10+. В Windows используйте WSL2; для прямого радио
потребуются проброс USB-адаптера в WSL и совместимый Linux-драйвер.

```bash
git clone https://github.com/Through-interference/nsy-server.git
cd nsy-server
bash scripts/install-nsu.sh
bash start-nsu.sh --demo --seconds 30
```

Откройте http://127.0.0.1:8091. Demo передаёт синтетическое видео локально и
позволяет проверить декодер без Raspberry Pi. Это не тест радиоканала.
Скрипты работают из checkout, установка Python-пакета через pip необязательна.

## Приём от Raspberry Pi

Один раз получите **соответствующий** `gs.key` из пары, созданной на доверенной
машине скриптом bvs-camera/scripts/generate-link-keys.sh:

```bash
mkdir -p keys/drone1
chmod 700 keys keys/drone1
# Перенесите gs.key в keys/drone1/gs.key безопасным способом.
chmod 600 keys/drone1/gs.key
```

Не создавайте независимые пары на каждом конце. Ключи исключены из Git.

### Через обычную IP-сеть

На Pi из bvs-camera:

```bash
bash start-air.sh --mode ip --bind PI_IP --peer NSU_IP
```

На ноутбуке замените адреса реальными IPv4:

```bash
bash start-nsu.sh --mode ip --bind NSU_IP --peer PI_IP --check
bash start-nsu.sh --mode ip --bind NSU_IP --peer PI_IP
```

IP-режим переносит шифрованные WFB/FEC-пакеты по UDP 17030. RSSI/SNR здесь
не измеряются, физическим MCS управляет ОС/точка доступа. Это не monitor/injection.
НСУ инициирует сессию первой; адрес peer на Pi должен соответствовать видимому
адресу ноутбука, в том числе при NAT/WSL.

### Прямой WFB через внешний адаптер

Нужен выделенный Linux-интерфейс с monitor/injection и WFB-драйвером.
Подготовка RTL8812AU описана в [bvs-camera/docs/RADIO.md](https://github.com/Through-interference/bvs-camera/blob/main/docs/RADIO.md).
Интерфейс не должен использоваться для IP-сети или единственного SSH-доступа.

```bash
bash start-nsu.sh --mode radio --interface wlan1 --check
sudo bash start-nsu.sh --mode radio --interface wlan1
```

Частота, полоса, link_id и ключи должны совпадать с бортом.
`config/station.json` содержит три примера независимых каналов FDMA; каждому
одновременно принимаемому каналу требуется отдельный адаптер. Укажите реальные
имена интерфейсов вместо REPLACE_GS_RADIO_*. Для одного борта достаточно
`--drone drone1 --interface wlan1`. Мультидрон в эфире пока не подтверждён тестом.

### Только RTP

```bash
bash start-nsu.sh --mode udp --bind 0.0.0.0 --video-port 5600
```

Принимает H.264 PT96 или JPEG PT26 напрямую, без WFB/FEC и обратного радио.
Используйте только в доверенной сети: входящий RTP здесь не аутентифицирован.
HTTP-интерфейс во всех режимах доступен только на localhost.

## Управление и диагностика

- Просмотр: http://127.0.0.1:8091
- Статистика: http://127.0.0.1:8091/status
- Последний JPEG: http://127.0.0.1:8091/frame.jpg (503, если кадра ещё нет)
- Логи: results/nsu/; Ctrl+C останавливает дочерние процессы.
- `--check` проверяет зависимости/порты; `--plan` показывает команды без запуска.
- `--seconds 60` ограничивает длительность теста.

```bash
bash scripts/test.sh
python3 tests/test_nsu_integration.py
```

Второй тест требует установленного GStreamer и проверяет реальные локальные
потоки H.264/MJPEG, состояние ожидания и освобождение портов. Радиодальность,
задержка <50 мс и работа под помехами этим тестом не проверяются.

## Структура

```text
src/nsy_server/  приём, декодер, viewer, статистика и обратная связь
config/         частоты, интерфейсы и порты каналов
scripts/        установка и проверки
tests/          модульные и интеграционные проверки
docs/           контракт с бортовой частью
```

[Протокол двух приложений](docs/PROTOCOL.md).

[Запуск прямого WFB на текущем ноутбуке и подготовка WSL](docs/WSL_RADIO.md).

[Результаты проверки прямого радиоканала и ограничения](docs/RADIO_TEST.md).
