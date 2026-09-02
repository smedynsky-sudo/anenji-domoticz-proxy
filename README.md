# ANENJI Domoticz Proxy

Локальный Python-proxy для получения телеметрии инвертора ANENJI через Wi-Fi-модуль SmartESS/Eybond и передачи показаний в Domoticz.

Проект проверен с моделью **ANJ-4000W-24V-WIFI**. Веб-панель не требуется: сервис занимается только обменом с инвертором, декодированием Modbus-регистров и обновлением виртуальных датчиков Domoticz.

## Возможности

- локальный приём соединения Wi-Fi-модуля SmartESS/Eybond;
- периодический опрос Modbus-регистров инвертора;
- солнечная, сетевая, выходная и аккумуляторная телеметрия;
- напряжение, ток, частота, температура, заряд и режим работы;
- отдельные Alert-датчики наличия внешней сети и доступности инвертора;
- отправка состояний в Domoticz только при их изменении;
- автоматический повторный запрос callback у Wi-Fi-модуля;
- DNS-переопределение `dtu_ess.eybond.com` для полностью локальной работы.

## Требования

- Linux с Python 3.10+;
- Domoticz с доступным JSON API;
- сервер, инвертор и Domoticz в одной локальной сети;
- права на UDP-порт 53, если используется встроенный DNS-перехват.

## Настройка Domoticz

Скрипт создаёт оборудование Dummy и необходимые виртуальные датчики:

```bash
python3 setup_domoticz.py http://DOMOTICZ_IP:8080 > domoticz.json
```

Проверьте созданный `domoticz.json`. Этот файл зависит от вашей установки и исключён из Git.

## Установка

```bash
sudo useradd --system --home /opt/smartess-proxy --shell /usr/sbin/nologin smartess
sudo mkdir -p /opt/smartess-proxy
sudo cp smartess_proxy.py setup_domoticz.py domoticz.json /opt/smartess-proxy/
sudo chown -R smartess:smartess /opt/smartess-proxy
```

Скопируйте `smartess-proxy.example.service` в `/etc/systemd/system/smartess-proxy.service` и замените:

- `SERVER_IP` — адрес Linux-сервера;
- `INVERTER_IP` — адрес Wi-Fi-модуля инвертора;
- путь или URL Domoticz в `domoticz.json`.

Затем запустите сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now smartess-proxy
sudo systemctl status smartess-proxy
```

## Проверка

```bash
sudo journalctl -u smartess-proxy -f
```

В журнале должны появиться сообщения о callback и подключении DTU. Числовые датчики Domoticz обновляются при поступлении новых данных, а `Режим` и `Состояние` — только при смене состояния.

## Переменные окружения

| Переменная | Назначение |
| --- | --- |
| `PROXY_PORT` | TCP-порт локального proxy |
| `DTU_IP` | IP Wi-Fi-модуля инвертора |
| `ADVERTISE_IP` | адрес proxy, сообщаемый Wi-Fi-модулю |
| `DNS_HOST` | локальный адрес встроенного DNS |
| `DNS_OVERRIDE_IP` | ответ для `dtu_ess.eybond.com` |
| `DOMOTICZ_CONFIG` | путь к `domoticz.json` |
| `LOCAL_ONLY` | `1` для работы без пересылки пакетов в облако |

## Безопасность

Не публикуйте `domoticz.json`, токены уведомлений, пароли и внешние адреса доступа. Сервис предназначен для доверенной локальной сети.

## Лицензия

MIT


