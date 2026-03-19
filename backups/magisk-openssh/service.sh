#!/system/bin/sh
# OpenSSH auto-start on boot
# - Port 9022: Android shell (Magisk openssh)
# - Port 22: Kali chroot shell (Kali openssh-server)

TRACE_FILE="/data/local/tmp/start_sshd.trace.log"
LOGFILE="/data/local/tmp/var/log/sshd.log"
PARAMETER_FILE="/data/local/tmp/home/.ssh/sshd_parameter"
START_SEMAPHOR="/data/local/tmp/home/start_sshd"

exec 1>${TRACE_FILE} 2>&1
set -x

echo "$(date) - service.sh starting"

mkdir -p /data/local/tmp/var/log
mkdir -p /data/local/tmp/var/run
mkdir -p /data/local/tmp/var/empty

# Wait for system to be ready
sleep 10

if [ -r ${START_SEMAPHOR} ] ; then
    echo "$(date) - Starting Android sshd on port 9022..."
    CUR_SSHD_PARAMETER=""
    if [ -r "${PARAMETER_FILE}" ] ; then
        CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} $( grep -vE "^#|^$" "${PARAMETER_FILE}" | tr "\n" " ")"
    fi
    touch ${LOGFILE}
    chmod 644 ${LOGFILE}
    CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} -E ${LOGFILE}"
    /system/bin/sshd ${CUR_SSHD_PARAMETER}
    echo "$(date) - Android sshd port 9022: exit code $?"

    sleep 2

    # Mount /vendor into Kali chroot
    CHROOT=/data/local/nhsystem/kalifs
    if [ -d "${CHROOT}" ] && [ ! -f "${CHROOT}/vendor/lib64/libOpenCL.so" ]; then
        mkdir -p ${CHROOT}/vendor
        mount --bind /vendor ${CHROOT}/vendor
        echo "$(date) - Mounted /vendor into Kali chroot"
    fi

    # Start Kali chroot sshd on port 22
    if [ -d "${CHROOT}" ]; then
        # Ensure privilege separation dir exists
        mkdir -p ${CHROOT}/run/sshd
        chmod 755 ${CHROOT}/run/sshd
        # Start sshd inside chroot
        /system/bin/chroot ${CHROOT} /usr/sbin/sshd
        echo "$(date) - Kali sshd port 22: exit code $?"
    fi

    sleep 2
    echo "$(date) - All services started"
else
    echo "$(date) - Semaphore not found, not starting sshd"
fi

echo "$(date) - service.sh finished"
