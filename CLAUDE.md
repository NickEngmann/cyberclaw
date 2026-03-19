# Cyberclaw - Kali NetHunter AI Toolkit

## Device Info
- Phone: OnePlus 8 (kebab), Snapdragon 865, Adreno 650 GPU
- Kernel: 4.19.157-perf+ (Nameless AOSP, Android 12)
- RAM: 12GB (shared between CPU and GPU)
- GPU Driver: Qualcomm v819.2, Compiler E031.50.02.00 (Magisk module)
- Chroot: Kali Linux at /data/local/nhsystem/kalifs
- Termux: installed (used for OpenCL GPU builds)

## SSH Access
```bash
# Local network
ssh -p 9022 shell@192.168.1.53
# Via Tailscale
ssh -p 22 shell@<tailscale-ip>
# Enter Kali chroot
/data/data/com.offsec.nhterm/files/usr/bin_aarch64/kali
```

Both ports auto-start on boot via Magisk openssh module.

## GPU Inference (OpenCL) - RECOMMENDED

### Quick run (2B Q8_0 - best balance of quality and speed)
```bash
# From Android root shell (NOT Kali chroot):
TERMUX_HOME=/data/data/com.termux/files/home
su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels; \
  $TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli \
  -m $TERMUX_HOME/models/Qwen3.5-2B-Q8_0.gguf \
  -ngl 99 -c 512 -n 200 -no-cnv -p 'Your prompt'"
```

See `docs/COMMANDS.md` for all model commands.

### How it works
- llama.cpp compiled in Termux (bionic) with Adreno-optimized OpenCL kernels
- Links against vendor `/vendor/lib64/libOpenCL.so` (Qualcomm proprietary)
- Must run from kernel source dir (`cd ggml/src/ggml-opencl/kernels/`)
- First run after reboot: ~3 min kernel JIT (cached for subsequent runs)

### Patches applied to llama.cpp
Source: `patches/llama-cpp-opencl-adreno650.patch`
1. Skip CL3.0 `CL_DEVICE_OPENCL_C_ALL_VERSIONS` path (device is CL2.0 on CL3.0 platform)
2. Add `-Dcl_khr_subgroups` to kernel compile options
3. Hardcode subgroup size to 128 (replace `clGetKernelSubGroupInfo` CL2.1 call)

### Build: `build-fast` (Adreno kernels ON, non-embedded)
```bash
cmake -S . -B build-fast -G Ninja \
  -DGGML_OPENCL=ON \
  -DGGML_OPENCL_USE_ADRENO_KERNELS=ON \
  -DGGML_OPENCL_EMBED_KERNELS=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DOpenCL_LIBRARY=/vendor/lib64/libOpenCL.so \
  -DOpenCL_INCLUDE_DIR=$PREFIX/include
```

## Performance Benchmarks

### With v819.2 driver (E031.50) + Adreno-optimized kernels
| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| Qwen3.5-0.8B | Q8_0 | **OpenCL GPU** | **30.5 t/s** | **6.3 t/s** |
| Qwen3.5-0.8B | Q4_0 | OpenCL GPU | 25.7 t/s | 4.5 t/s |
| Qwen3.5-0.8B | Q8_0 | CPU | 21.9 t/s | 3.9 t/s |
| Qwen3.5-2B | Q8_0 | **OpenCL GPU** | **23.3 t/s** | **4.8 t/s** |
| Qwen3.5-2B | Q4_0 | OpenCL GPU | 19.6 t/s | 3.4 t/s |
| Qwen3.5-2B | Q4_K_M | OpenCL GPU | 3.9 t/s | 1.9 t/s |
| Qwen3.5-2B | Q8_0 | CPU (ollama) | 18.4 t/s | 3.3 t/s |
| Qwen3.5-4B | Q4_0 | **OpenCL GPU** | **10.1 t/s** | **2.0 t/s** |

### Quantization guide
- **Q8_0 is fastest on GPU** when model fits in memory
- **0.8B**: Use Q8_0 (6.3 t/s) - 775MB
- **2B**: Use Q8_0 (4.8 t/s) - 1.9GB
- **4B**: Use Q4_0 (2.0 t/s) - 2.5GB. Q8_0 (4.2GB) fails: exceeds 1GB alloc limit
- **Never use Q4_K_M on GPU** - mixed quant = slow generic kernels

### With old driver (E031.37) - Before update
| Model | Backend | Prompt | Generation |
|-------|---------|--------|------------|
| 0.8B Q8_0 | OpenCL GPU | 2.5-4.7 t/s | 3.6 t/s |
| 2B Q4_K_M | OpenCL GPU | 0.5 t/s | 0.2 t/s |

## CPU Inference (ollama)
```bash
# Inside Kali chroot:
export TMPDIR=/tmp OLLAMA_VULKAN=0 OLLAMA_KEEP_ALIVE=1m
ollama serve &
ollama run qwen3.5:2b "Your prompt"
ollama stop qwen3.5:2b   # ALWAYS stop when done
```

## Models
Stored in: Kali `/root/cyberclaw/models/` and Termux `~/models/`

| Model | File | Size | Best Backend |
|-------|------|------|-------------|
| Qwen3.5-0.8B | Q8_0 | 775MB | GPU (6.3 t/s) |
| Qwen3.5-0.8B | Q4_0 | 484MB | GPU (4.5 t/s) |
| Qwen3.5-2B | Q8_0 | 1.9GB | **GPU (4.8 t/s)** |
| Qwen3.5-2B | Q4_0 | 1.2GB | GPU (3.4 t/s) |
| Qwen3.5-2B | Q4_K_M | 1.2GB | CPU only (1.9 t/s GPU) |
| Qwen3.5-4B | Q4_0 | 2.5GB | GPU (2.0 t/s) |

## After Reboot
Everything persists across reboots. No recompilation needed.
1. SSH auto-starts on ports 9022 and 22
2. `/vendor` auto-mounted in Kali chroot
3. All builds in Termux are ready to use
4. First GPU inference after reboot: ~3 min kernel JIT (then cached)

## Memory Management
```bash
# Free RAM before large models (from Android shell):
am kill-all && echo 3 > /proc/sys/vm/drop_caches
# Check: cat /proc/meminfo | grep MemAvail
# Kill llama: ps -ef | grep llama | grep -v grep | awk '{print $2}' | xargs kill -9
```

## Magisk Modules
| Module | Purpose |
|--------|---------|
| openssh (v9.9p2) | Persistent SSH on ports 9022 + 22 |
| adreno-650_819v2 | GPU driver v819.2 (E031.50) from XDA |
| nethunter (v1.4.0) | Kali chroot + tools |
| tailscaled | Tailscale VPN |

## Known Issues
- Q4_K_M on GPU: extremely slow (falls back to generic kernels)
- Q4_0 on latest llama.cpp HEAD: SOA buffer alignment crash (works on build-fast)
- 4B Q8_0: fails to load (exceeds 1GB per-allocation limit)
- Vulkan: dead end (vendor=1.1, Mesa Turnip=DeviceLostError)
- OpenCL embedded kernels: 60+ min JIT (use non-embedded instead)

## Repository
- GitHub: github.com/NickEngmann/cyberclaw
- Local backup: C:\Users\cyeng\cyberclaw\
- Phone: /root/cyberclaw/ (Kali chroot)
