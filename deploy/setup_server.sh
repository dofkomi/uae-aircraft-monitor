#!/usr/bin/env bash
# تثبيت البوت كخدمة دائمة على Ubuntu (Oracle Cloud Always Free).
# يُنفَّذ على الخادم بعد رفع ملفات المشروع إلى /opt/uae-aircraft-monitor
set -euo pipefail

APP_DIR=/opt/uae-aircraft-monitor
SERVICE=uae-aircraft-monitor

echo "===[1/6] تحديث النظام وتثبيت المتطلبات الأساسية]==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl

echo "===[2/6] إنشاء المستخدم/المجلدات]==="
sudo mkdir -p "$APP_DIR/data"
sudo chown -R "$USER:$USER" "$APP_DIR"
sudo touch /var/log/${SERVICE}.log
sudo chown "$USER:$USER" /var/log/${SERVICE}.log

echo "===[3/6] البيئة الافتراضية وتثبيت حزم Python]==="
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "===[4/6] تنزيل بيانات المطارات إن لم تكن موجودة]==="
if [ ! -f data/airports.csv ]; then
  echo "جارٍ تنزيل OurAirports (حوالي 12MB) ..."
  curl -fL --retry 3 -o data/airports_raw.csv \
    https://davidmegginson.github.io/ourairports-data/airports.csv
  .venv/bin/python - <<'PY'
from airports import AirportDB
from pathlib import Path
import shutil
# يقرأ الملف الخام ويكتب النسخة المصفّأة
from airports import OURAIRPORTS_URL, KEEP_COLUMNS, WANTED_TYPES
import csv, io
raw = Path('data/airports_raw.csv').read_text(encoding='utf-8')
out = Path('data/airports.csv')
reader = csv.DictReader(io.StringIO(raw))
with out.open('w', encoding='utf-8', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(KEEP_COLUMNS))
    w.writeheader()
    for row in reader:
        if row.get('type') in WANTED_TYPES:
            w.writerow({k: row.get(k, '') for k in KEEP_COLUMNS})
print('تم تجهيز', out)
PY
  rm -f data/airports_raw.csv
fi

echo "===[5/6] تثبيت وحدة systemd]==="
sudo cp "$APP_DIR/deploy/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

echo "===[6/6] الحالة]==="
sleep 3
sudo systemctl status "$SERVICE" --no-pager
echo ""
echo "السجلات: sudo journalctl -u $SERVICE -f"
echo "أو: tail -f /var/log/${SERVICE}.log"
