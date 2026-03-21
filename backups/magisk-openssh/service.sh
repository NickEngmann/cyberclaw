#!/system/bin/sh
# Nightcrawler boot services
TRACE_FILE="/data/local/tmp/start_sshd.trace.log"
LOGFILE="/data/local/tmp/var/log/sshd.log"
PARAMETER_FILE="/data/local/tmp/home/.ssh/sshd_parameter"
START_SEMAPHOR="/data/local/tmp/home/start_sshd"

exec 1>${TRACE_FILE} 2>&1
set -x
echo "$(date) - service.sh starting"
mkdir -p /data/local/tmp/var/log /data/local/tmp/var/run /data/local/tmp/var/empty
sleep 10

if [ -r ${START_SEMAPHOR} ] ; then
    # 1. Android SSH (port 9022)
    CUR_SSHD_PARAMETER=""
    if [ -r "${PARAMETER_FILE}" ] ; then
        CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} $( grep -vE "^#|^$" "${PARAMETER_FILE}" | tr "\n" " ")"
    fi
    touch ${LOGFILE}; chmod 644 ${LOGFILE}
    CUR_SSHD_PARAMETER="${CUR_SSHD_PARAMETER} -E ${LOGFILE}"
    /system/bin/sshd ${CUR_SSHD_PARAMETER}
    echo "$(date) - Android sshd 9022: $?"
    sleep 2

    # 2. Mount /vendor in Kali chroot
    CHROOT=/data/local/nhsystem/kalifs
    if [ -d "${CHROOT}" ] && [ ! -f "${CHROOT}/vendor/lib64/libOpenCL.so" ]; then
        mkdir -p ${CHROOT}/vendor
        mount --bind /vendor ${CHROOT}/vendor
        echo "$(date) - Mounted /vendor"
    fi

    # 3. Kali SSH (port 22) with retry
    if [ -d "${CHROOT}" ]; then
        mkdir -p ${CHROOT}/run/sshd; chmod 755 ${CHROOT}/run/sshd
        /system/bin/chroot ${CHROOT} /usr/sbin/sshd
        echo "$(date) - Kali sshd 22: $?"
        (
            for _i in 1 2 3 4 5 6 7 8 9 10; do
                sleep 30
                if ! netstat -tlnp 2>/dev/null | grep -q ":22 "; then
                    mkdir -p ${CHROOT}/run/sshd; chmod 755 ${CHROOT}/run/sshd
                    /system/bin/chroot ${CHROOT} /usr/sbin/sshd 2>/dev/null
                    echo "$(date) - Kali sshd restarted (attempt $_i)"
                fi
            done
        ) &
    fi

    # 4. llama-server watchdog (20 min delay, 7h refresh, 20 min crash cooldown)
    (
        TERMUX_HOME=/data/data/com.termux/files/home
        KERNEL_DIR=${TERMUX_HOME}/llama.cpp/ggml/src/ggml-opencl/kernels
        MODEL=${TERMUX_HOME}/models/Qwen3.5-2B-Unredacted-MAX.Q8_0.gguf
        PORT=8080
        INITIAL_DELAY=1200
        CRASH_COOLDOWN=1200
        REFRESH_INTERVAL=18000

        exec 1>/data/local/tmp/var/log/llama-watchdog.log 2>&1
        echo "$(date) - Watchdog: waiting ${INITIAL_DELAY}s..."
        sleep ${INITIAL_DELAY}

        LAST_START=0

        while true; do
            NOW=$(date +%s)
            UPTIME=$(( NOW - LAST_START ))

            HEALTHY=false
            if wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok"; then
                HEALTHY=true
            else
                sleep 5
                wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok" && HEALTHY=true
            fi

            if [ "${HEALTHY}" = "true" ] && [ ${LAST_START} -gt 0 ] && [ ${UPTIME} -lt ${REFRESH_INTERVAL} ]; then
                sleep 30
                continue
            fi

            if [ ${LAST_START} -gt 0 ] && [ ${UPTIME} -ge ${REFRESH_INTERVAL} ]; then
                echo "$(date) - Refresh ($((UPTIME/3600))h uptime)"
            elif [ ${LAST_START} -gt 0 ]; then
                echo "$(date) - CRASHED"
            else
                echo "$(date) - Initial start"
            fi

            # SIGKILL — graceful SIGTERM doesn't work when llama-server
            # is doing GPU/OpenCL work, causing dual-process OOM
            pkill -9 -f "llama-server.*${PORT}" 2>/dev/null
            sleep 2
            # Verify kill — retry if still alive
            if pgrep -f "llama-server.*${PORT}" >/dev/null 2>&1; then
                echo "$(date) - WARN: still alive after SIGKILL, retrying"
                pkill -9 -f "llama-server" 2>/dev/null
                sleep 3
            fi
            am kill-all 2>/dev/null; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; sleep 2

            export LD_LIBRARY_PATH=${TERMUX_HOME}/../usr/lib:/vendor/lib64
            export GGML_OPENCL_PLATFORM=0 GGML_OPENCL_DEVICE=0
            cd ${KERNEL_DIR}
            nohup ${TERMUX_HOME}/llama.cpp/build-fast/bin/llama-server \
                -m ${MODEL} -ngl 99 -c 8192 -t 4 -np 1 \
                --port ${PORT} --host 0.0.0.0 \
                --jinja --reasoning off --log-disable \
                > /data/local/tmp/var/log/llama-server.log 2>&1 &
            echo "$(date) - PID $!"

            for i in $(seq 1 60); do
                sleep 5
                wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok" && break
            done

            LAST_START=$(date +%s)

            if [ "${HEALTHY}" != "true" ] && [ ${UPTIME} -lt ${REFRESH_INTERVAL} ]; then
                echo "$(date) - Crash cooldown ${CRASH_COOLDOWN}s"
                sleep ${CRASH_COOLDOWN}
            fi
        done
    ) &
    echo "$(date) - Watchdog launched (5h refresh)"
else
    echo "$(date) - Semaphore not found"
fi
echo "$(date) - service.sh finished"
