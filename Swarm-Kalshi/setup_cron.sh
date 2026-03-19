#!/bin/bash
# =============================================================================
# setup_cron.sh
# =============================================================================
# Installs a daily 3 AM cron job that runs the Kalshi Swarm health check.
#
# Usage (on your VPS):
#   chmod +x setup_cron.sh
#   ./setup_cron.sh
#
# To verify installation:
#   crontab -l
#
# To remove the cron job:
#   crontab -l | grep -v health_check | crontab -
# =============================================================================

set -euo pipefail

SWARM_DIR="${SWARM_DIR:-/root/Swarm/Swarm-Kalshi}"
VENV_PYTHON="${SWARM_DIR}/.venv/bin/python"

# Verify the project directory exists
if [ ! -d "$SWARM_DIR" ]; then
    echo "ERROR: Swarm directory not found: $SWARM_DIR"
    echo "Set the SWARM_DIR environment variable to the correct path, e.g.:"
    echo "  SWARM_DIR=/home/user/Swarm-Kalshi ./setup_cron.sh"
    exit 1
fi

# Verify the venv Python exists (fall back to system python3)
if [ ! -f "$VENV_PYTHON" ]; then
    echo "WARNING: venv Python not found at $VENV_PYTHON"
    VENV_PYTHON="$(which python3 2>/dev/null || which python 2>/dev/null)"
    if [ -z "$VENV_PYTHON" ]; then
        echo "ERROR: No Python interpreter found. Install Python 3 or create the venv first."
        exit 1
    fi
    echo "Falling back to system Python: $VENV_PYTHON"
fi

CRON_JOB="0 3 * * * cd ${SWARM_DIR} && ${VENV_PYTHON} health_check.py >> ${SWARM_DIR}/logs/health_check.log 2>&1"

echo "Installing cron job:"
echo "  $CRON_JOB"
echo ""

# Remove any existing health_check entry, then add the new one
(crontab -l 2>/dev/null | grep -v "health_check"; echo "$CRON_JOB") | crontab -

echo "Cron job installed successfully."
echo ""
echo "Verify with:  crontab -l"
echo "Test now  :   cd ${SWARM_DIR} && ${VENV_PYTHON} health_check.py"
echo "View logs :   tail -f ${SWARM_DIR}/logs/health_check.log"
echo ""
echo "The health check will run daily at 03:00 (server local time)."
echo "Reports are saved to:"
echo "  ${SWARM_DIR}/data/health_report_latest.json"
echo "  ${SWARM_DIR}/data/health_reports/health_report_YYYY-MM-DD.json"
