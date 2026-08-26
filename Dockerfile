# eltex-cutter — заливка своей прошивки в устройства Eltex «по воздуху».
#
# Управление Wi-Fi (подключение к точке устройства через nmcli) требует, как и в
# tuya-cloudcutter, Linux-хоста с реальным Wi-Fi-адаптером, запуска с
# --network host и --cap-add NET_ADMIN (см. docker-compose.yml).
#
# На Docker Desktop (Windows/macOS) контейнер Wi-Fi НЕ видит — там подключайтесь к
# точке устройства сами, а контейнер используйте для брокера+HTTP+OTA
# (--skip-provision или без --ap).
FROM python:3.12-slim

# nmcli — управление Wi-Fi; libzbar/opencv — чтение QR-кода наклейки.
RUN apt-get update && apt-get install -y --no-install-recommends \
        network-manager \
        libzbar0 \
        iputils-ping \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# QR-декодеры (не обязательны для ядра, но нужны для авто-чтения пароля точки).
# Ядро инструмента внешних зависимостей не имеет.
RUN pip install --no-cache-dir pyzbar pillow || true

WORKDIR /app
COPY *.py /app/
COPY profiles/ /app/profiles/

# Каталоги для образов и профилей монтируются снаружи (см. compose).
VOLUME ["/firmware", "/app/profiles"]

ENTRYPOINT ["python3", "eltex_cutter.py"]
CMD ["--help"]
