#!/system/bin/sh
# OpenSSH auto-start on boot - enhanced for reliability
# Original backed up to service.sh.bak

TRACE_FILE="/data/local/tmp/start_sshd.trace.log"
LOGFILE="/data/local/tmp/var/log/sshd.log"
PARAMETER_FILE="/data/local/tmp/home/.ssh/sshd_parameter"
START_SEMAPHOR="/data/local/tmp/home/start_sshd"

# Always log on boot for debugging
exec 1>${TRACE_FILE} 2>&1
set -x

echo "$(date) - service.sh starting"

# Ensure directories exist
mkdir -p /data/local/tmp/var/log
mkdir -p /data/local/tmp/var/run
mkdir -p /data/local/tmp/var/empty

# Wait for system to be ready
sleep 10

if [ -r ${START_SEMAPHOR} ] ; then
    echo "$(date) - Semaphore found, starting sshd..."

    CUR_SSHD_PARAMETER=""

    if [ -r "${PARAMETER_FILE}" ] ; then
        CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} $( grep -vE "^#|^$" "${PARAMETER_FILE}" | tr "\n" " ")"
    fi

    # Create log file if needed
    touch ${LOGFILE}
    chmod 644 ${LOGFILE}
    CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} -E ${LOGFILE}"

    # Start sshd on port 9022 (config default)
    /system/bin/sshd ${CUR_SSHD_PARAMETER}
    echo "$(date) - sshd port 9022 started, exit code: $?"

    # Start sshd on port 22 (for Tailscale access)
    sleep 2
    /system/bin/sshd -p 22 -f /system/etc/ssh/sshd_config
    echo "$(date) - sshd port 22 started, exit code: $?"

    # Verify both are running
    sleep 2
    if pgrep -f "sshd" > /dev/null 2>&1 ; then
        echo "$(date) - sshd is running"
    else
        echo "$(date) - ERROR: sshd failed to start"
    fi

    # Mount /vendor into Kali chroot (needed for OpenCL if using chroot)
    CHROOT=/data/local/nhsystem/kalifs
    if [ -d "${CHROOT}" ] && [ ! -f "${CHROOT}/vendor/lib64/libOpenCL.so" ]; then
        mkdir -p ${CHROOT}/vendor
        mount --bind /vendor ${CHROOT}/vendor
        echo "$(date) - Mounted /vendor into Kali chroot"
    fi
else
    echo "$(date) - Semaphore not found, not starting sshd"
fi

echo "$(date) - service.sh finished"
