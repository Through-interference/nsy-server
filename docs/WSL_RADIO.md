# Прямой WFB на ноутбуке Linux / WSL2

## Что уже подготовлено на текущем стенде

На ноутбуке установлен usbipd-win, RTL8812AU проброшен в Ubuntu-22.04.
Загружен модуль `88XXau_wfb`, внешний интерфейс — `wlx00c0caba99b1`.
Ядро WSL не заменялось: `6.18.33.2-microsoft-standard-WSL2`.
На Pi внешний интерфейс — `wlan1`, тот же patched-драйвер; управление по
отдельному Ethernet. Не используйте внешний адаптер для обычного Wi-Fi
одновременно с WFB.

Проверить готовность из nsy-server:

```bash
bash start-nsu.sh --mode radio --interface wlx00c0caba99b1 --check
sudo bash start-nsu.sh --mode radio --interface wlx00c0caba99b1
```

В текущем WSL при отсутствии локального sudo-пароля можно из PowerShell:

```powershell
wsl -d Ubuntu-22.04 -u root -- bash /home/arseniy/Skvoz_pomehi/nsy-server/start-nsu.sh --mode radio --interface wlx00c0caba99b1
```

Борт, из ~/bvs-camera на Pi:

```bash
sudo bash start-air.sh --mode radio --interface wlan1
```

Просмотр на ноутбуке — http://127.0.0.1:8091. Управление Pi сохраняется по
Ethernet; hostname может разрешаться в старый Wi-Fi-адрес, поэтому при
необходимости используйте Ethernet-IP. Ключи одной пары лежат в keys/drone1
каждого приложения (drone.key на Pi, gs.key на НСУ).

## После перезапуска Windows / WSL

Сначала проверьте имя и состояние устройства в PowerShell:

```powershell
usbipd list
usbipd attach --wsl --busid 1-2
```

BUSID может измениться. Если устройство ещё Not shared, `usbipd bind --busid
1-2` выполняется один раз из PowerShell администратора. После attach адаптер
временно перестаёт работать как Wi-Fi Windows; основной сетевой интерфейс
ноутбука должен быть другим.

В WSL:

```bash
sudo modprobe 88XXau_wfb rtw_power_mgnt=0 rtw_enusbss=0
sudo iw dev
ip -brief address
```

При первой загрузке драйвера на стенде USB/IP отсоединился. Повторный attach
с уже загруженным драйвером создал интерфейс успешно. Если `iw dev` пуст,
проверьте usbipd list, lsusb и dmesg, затем повторите attach. Не запускайте
второй передатчик/приёмник на занятых портах.

## Воспроизводимая сборка драйвера

Зафиксирован upstream svpcom/rtl8812au commit
`3577999972f350a2213e7509ae1f371b99fc96ab`.

На обычном Debian/Ubuntu сначала установите заголовки работающего ядра,
затем выполните:

```bash
bash scripts/install-radio-driver.sh
```

Скрипт устанавливает модуль, но не выгружает другие драйверы и не меняет сеть.
В WSL стандартных Ubuntu-заголовков недостаточно: нужно подготовленное дерево
Microsoft WSL2-Linux-Kernel **точной версии** с конфигурацией работающего ядра,
Module.symvers, сгенерированными headers и символами cfg80211/USB.

В текущем стенде дерево подготовлено в /tmp/skvoz-wsl-kernel. Оно временное;
для повторной сборки после очистки /tmp его придётся получить заново. Загруженный
и установленный модуль от этого не исчезает. Для подготовленного дерева:

```bash
export KERNEL_SOURCE=/tmp/skvoz-wsl-kernel
export KBUILD_EXTRA_SYMBOLS="$KERNEL_SOURCE/drivers/usb/core/Module.symvers $KERNEL_SOURCE/net/wireless/Module.symvers"
bash scripts/install-radio-driver.sh
```

В этой конфигурации таблицы модулей получались последовательной сборкой
`M=drivers/usb/common`, `M=drivers/usb/core` (с символами common) и
`M=net/wireless`; основной Module.symvers — сборкой ядра. Одного modules_prepare
при CONFIG_MODVERSIONS=y недостаточно. Скрипт намеренно не обновляет и не
перезапускает ядро. После обновления WSL ядро и модуль потребуется сверить снова.

## Завершение и возврат обычного Wi-Fi

Ctrl+C останавливает приложение и возвращает прежний тип интерфейса.
Оно не подключает SSID обратно. Для возврата адаптера Windows:

```powershell
usbipd detach --busid 1-2
```

На Pi внешний адаптер остаётся выделенным под WFB. Возвращать NetworkManager
managed и подключать сохранённый профиль нужно явно, если он снова нужен для IP.

Источники: [WFB Setup HOWTO](https://github.com/svpcom/wfb-ng/wiki/Setup-HOWTO),
[patched-драйвер](https://github.com/svpcom/rtl8812au),
[Microsoft USB в WSL](https://learn.microsoft.com/en-us/windows/wsl/connect-usb).
