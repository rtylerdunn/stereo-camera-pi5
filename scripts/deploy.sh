#!/bin/bash
# Deploy or update the stereo-camera project on the Raspberry Pi.
# Run from the project root on any machine with SSH access to pi5.
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-pi5}"
REMOTE_PATH="/home/tyler/stereo-camera"

echo ">>> Syncing project files to ${REMOTE_HOST}:${REMOTE_PATH}"
rsync -av --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  backend/ config/ frontend/ services/ scripts/ \
  "${REMOTE_HOST}:${REMOTE_PATH}/"

echo ">>> Installing systemd service"
ssh "${REMOTE_HOST}" "sudo cp ${REMOTE_PATH}/services/stereocam.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && \
  sudo systemctl enable stereocam && \
  sudo systemctl restart stereocam && \
  sudo systemctl status stereocam --no-pager"

echo ">>> Done. Service running at http://pi5.fritz.box:8080"
