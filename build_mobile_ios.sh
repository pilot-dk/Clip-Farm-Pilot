#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
exec "$PROJECT_DIR/build_mobile_local_ios.sh"
