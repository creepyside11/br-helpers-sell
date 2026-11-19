#!/bin/sh
set -eu
command -v systemctl >/dev/null
test -x /usr/bin/python3
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 10)'
if [ "${1:-}" != "--replace" ] && systemctl is-active --quiet rakcrmp-helper-worker.service; then
    echo 'Worker already installed and running.' >&2
    exit 1
fi
install -d -m 0755 /opt/rakcrmp-helper-worker
install -d -m 0700 /etc/rakcrmp-helper
cp -R helper runtime /opt/rakcrmp-helper-worker/
# The SSH installer runs with umask 077. DynamicUser needs read access
# to the code, including files from a previous install with mode 0600.
find /opt/rakcrmp-helper-worker/helper /opt/rakcrmp-helper-worker/runtime -type d -exec chmod 0755 {} +
find /opt/rakcrmp-helper-worker/helper /opt/rakcrmp-helper-worker/runtime -type f -name '*.py' -exec chmod 0644 {} +
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
systemctl enable rakcrmp-helper-worker.service
systemctl restart rakcrmp-helper-worker.service
systemctl is-active --quiet rakcrmp-helper-worker.service
