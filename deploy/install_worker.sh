#!/bin/sh
set -eu
command -v systemctl >/dev/null
test -x /usr/bin/python3
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 10)'
if systemctl is-active --quiet rakcrmp-helper-worker.service; then
    echo 'Worker already installed and running.' >&2
    exit 1
fi
install -d -m 0755 /opt/rakcrmp-helper-worker
install -d -m 0700 /etc/rakcrmp-helper
cp -R helper runtime /opt/rakcrmp-helper-worker/
install -m 0600 worker.env /etc/rakcrmp-helper/worker.env
cat >/etc/systemd/system/rakcrmp-helper-worker.service <<'UNIT'
[Unit]
Description=RakCRMP Helper marketplace worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=yes
EnvironmentFile=/etc/rakcrmp-helper/worker.env
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory=/opt/rakcrmp-helper-worker
ExecStart=/usr/bin/python3 -m helper.worker
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now rakcrmp-helper-worker.service
systemctl is-active --quiet rakcrmp-helper-worker.service
