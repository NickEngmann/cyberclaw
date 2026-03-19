#!/system/bin/sh
# Nightcrawler boot services
# - Port 9022: Android SSH (Magisk openssh)
# - Port 22: Kali SSH (openssh-server in chroot)
# - /vendor mount in Kali chroot
# - llama-server on port 8080 (delayed 20 min, watchdog with 20 min cooldown)

TRACE_FILE="/data/local/tmp/start_sshd.trace.log"
LOGFILE="/data/local/tmp/var/log/sshd.log"
PARAMETER_FILE="/data/local/tmp/home/.ssh/sshd_parameter"
START_SEMAPHOR="/data/local/tmp/home/start_sshd"
LLAMA_LOG="/data/local/tmp/var/log/llama-server.log"
WATCHDOG_LOG="/data/local/tmp/var/log/llama-watchdog.log"

exec 1>${TRACE_FILE} 2>&1
set -x

echo "$(date) - service.sh starting"

mkdir -p /data/local/tmp/var/log
mkdir -p /data/local/tmp/var/run
mkdir -p /data/local/tmp/var/empty

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
    echo "$(date) - Android sshd port 9022: $?"

    sleep 2

    # 2. Mount /vendor in Kali chroot
    CHROOT=/data/local/nhsystem/kalifs
    if [ -d "${CHROOT}" ] && [ ! -f "${CHROOT}/vendor/lib64/libOpenCL.so" ]; then
        mkdir -p ${CHROOT}/vendor
        mount --bind /vendor ${CHROOT}/vendor
        echo "$(date) - Mounted /vendor into chroot"
    fi

    # 3. Kali SSH (port 22)
    if [ -d "${CHROOT}" ]; then
        mkdir -p ${CHROOT}/run/sshd; chmod 755 ${CHROOT}/run/sshd
        /system/bin/chroot ${CHROOT} /usr/sbin/sshd
        echo "$(date) - Kali sshd port 22: $?"
    fi

    # 4. llama-server watchdog (20 min delay, runs as root)
    (
        TERMUX_HOME=/data/data/com.termux/files/home
        KERNEL_DIR=${TERMUX_HOME}/llama.cpp/ggml/src/ggml-opencl/kernels
        MODEL=${TERMUX_HOME}/models/Qwen3.5-2B-Q8_0.gguf
        PORT=8080
        INITIAL_DELAY=1200
        CRASH_COOLDOWN=1200

        exec 1>${WATCHDOG_LOG} 2>&1
        echo "$(date) - Watchdog: waiting ${INITIAL_DELAY}s before first start..."
        sleep ${INITIAL_DELAY}

        while true; do
            # Already healthy?
            if wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok"; then
                sleep 60
                continue
            fi

            # Clean up
            pkill -f "llama-server.*${PORT}" 2>/dev/null; sleep 2
            am kill-all 2>/dev/null
            echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; sleep 2

            echo "$(date) - Watchdog: starting llama-server..."

            export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib:/vendor/lib64
            export GGML_OPENCL_PLATFORM=0
            export GGML_OPENCL_DEVICE=0
            cd ${KERNEL_DIR}

            nohup ${TERMUX_HOME}/llama.cpp/build-fast/bin/llama-server \
                -m ${MODEL} -ngl 99 -c 4096 -t 4 \
                --port ${PORT} --host 0.0.0.0 \
                --jinja --reasoning off --log-disable \
                > ${LLAMA_LOG} 2>&1 &
            LLAMA_PID=$!
            echo "$(date) - Watchdog: PID ${LLAMA_PID}"

            # Wait for healthy (up to 5 min)
            HEALTHY=false
            for i in $(seq 1 60); do
                sleep 5
                if wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok"; then
                    HEALTHY=true
                    echo "$(date) - Watchdog: healthy after $((i*5))s"
                    break
                fi
                if ! kill -0 ${LLAMA_PID} 2>/dev/null; then
                    echo "$(date) - Watchdog: died during startup"
                    break
                fi
            done

            if [ "${HEALTHY}" = "true" ]; then
                # Monitor health every 30s
                while true; do
                    sleep 30
                    if ! wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok"; then
                        sleep 5
                        if ! wget -qO- http://127.0.0.1:${PORT}/health 2>/dev/null | grep -q "ok"; then
                            echo "$(date) - Watchdog: CRASHED. Cooldown ${CRASH_COOLDOWN}s"
                            pkill -f "llama-server.*${PORT}" 2>/dev/null
                            sleep ${CRASH_COOLDOWN}
                            break
                        fi
                    fi
                done
            else
                echo "$(date) - Watchdog: failed to start. Cooldown ${CRASH_COOLDOWN}s"
                pkill -f "llama-server.*${PORT}" 2>/dev/null
                sleep ${CRASH_COOLDOWN}
            fi
        done
    ) &
    echo "$(date) - Watchdog launched"
else
    echo "$(date) - Semaphore not found, skipping"
fi

echo "$(date) - service.sh finished"
