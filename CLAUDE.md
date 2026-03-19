# Cyberclaw - Kali NetHunter AI Toolkit

## Device Info
- Phone: OnePlus 8 (kebab), Snapdragon 865, Adreno 650 GPU
- Kernel: 4.19.157-perf+ (Nameless AOSP, Android 12)
- RAM: 12GB (shared between CPU and GPU)
- GPU Driver: Qualcomm v819.2, Compiler E031.50.02.00 (via Magisk module)
- Chroot: Kali Linux at /data/local/nhsystem/kalifs
- Termux: installed (used for OpenCL GPU builds)

## SSH Access
```bash
ssh -p 9022 shell@192.168.1.53
# Enter Kali chroot:
/data/data/com.offsec.nhterm/files/usr/bin_aarch64/kali
```

## GPU Acceleration - OpenCL (RECOMMENDED)

### How it works
- llama.cpp built in Termux with OpenCL, linked against vendor /vendor/lib64/libOpenCL.so
- Adreno 650 detected as "QUALCOMM Adreno(TM) 650 (OpenCL 2.0)"
- Driver: v819.2, Compiler E031.50.02.00 (installed via Magisk module)
- Required patches applied to llama.cpp for CL2.0 device on CL3.0 platform

### Running models on GPU
```bash
# From Android root shell (not Kali chroot):
TERMUX_HOME=/data/data/com.termux/files/home
KERNELS=$TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels
CLI=$TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli

su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $KERNELS; \
  $CLI -m $TERMUX_HOME/models/MODEL.gguf \
  -ngl 99 -c 512 -n 100 -no-cnv -p 'Your prompt'"
```

### Patches applied to llama.cpp (latest HEAD)
1. `if (0)` the CL3.0 `CL_DEVICE_OPENCL_C_ALL_VERSIONS` path (device is CL2.0)
2. Add `-Dcl_khr_subgroups` to kernel compile_opts
3. Hardcode `sgs = 128` (replace `clGetKernelSubGroupInfo` CL2.1 call)
4. Non-embedded kernels (`-DGGML_OPENCL_EMBED_KERNELS=OFF`)
5. Run from kernel source dir for `.cl` file loading

### First run: ~3 min for OpenCL kernel JIT compilation

## Performance Benchmarks

### With v819.2 driver (E031.50) + Adreno-optimized kernels
| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| Qwen3.5-0.8B | Q8_0 | **OpenCL GPU (Adreno)** | **30.5 t/s** | **6.3 t/s** |
| Qwen3.5-0.8B | Q4_0 | OpenCL GPU (Adreno) | 25.7 t/s | 4.5 t/s |
| Qwen3.5-0.8B | Q8_0 | CPU (llama.cpp) | 21.9 t/s | 3.9 t/s |
| Qwen3.5-2B | Q8_0 | **OpenCL GPU (Adreno)** | **23.3 t/s** | **4.8 t/s** |
| Qwen3.5-2B | Q4_0 | OpenCL GPU (Adreno) | 19.6 t/s | 3.4 t/s |
| Qwen3.5-2B | Q4_K_M | OpenCL GPU (Adreno) | 3.9 t/s | 1.9 t/s |
| Qwen3.5-2B | Q8_0 | CPU (ollama) | 18.4 t/s | 3.3 t/s |
| Qwen3.5-4B | Q4_0 | **OpenCL GPU (Adreno)** | **10.1 t/s** | **2.0 t/s** |

### Quantization guide for this device
- **Q8_0 is fastest on GPU** when the model fits in memory (~8GB available)
- **0.8B**: Use Q8_0 (6.3 t/s) - 775MB, fits easily
- **2B**: Use Q8_0 (4.8 t/s) - 1.9GB, fits fine
- **4B: Use Q4_0 (2.0 t/s) - 2.5GB. Q8_0 (4.2GB) fails: exceeds 1GB per-allocation limit
- **Never use Q4_K_M on GPU** - mixed quant falls back to slow generic kernels
- Q4_0 is only needed when Q8_0 won't fit in memory

### With old driver (E031.37) - Before update
| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| Qwen3.5-0.8B | Q8_0 | **OpenCL GPU (Adreno)** | **30.5 t/s** | **6.3 t/s** |
| Qwen3.5-2B | Q4_K_M | **OpenCL GPU (Adreno)** | **3.9 t/s** | **1.9 t/s** |
| Qwen3.5-2B | Q4_0 | **OpenCL GPU (Adreno)** | **19.6 t/s** | **3.4 t/s** |

### Key insights
- GPU gen speed crushes CPU for 0.8B (6.3 vs 3.9 t/s = 62% faster)
- GPU prompt eval now matches CPU (30.5 vs 21.9 t/s) (GPU overhead for small batches)
- Driver update improved 2B GPU performance by 9x (0.2 -> 1.8 t/s)
- For 2B with Q4_0, GPU matches CPU (3.4 vs 3.3 t/s)! Use Q4_0 not Q4_K_M on GPU

## Models
Location: /root/cyberclaw/models/ (Kali) and Termux: ~/models/

| Model | File | Size | Best Backend |
|-------|------|------|-------------|
| Qwen3.5-0.8B Q4_0 | Qwen3.5-0.8B-Q4_0.gguf | 484MB | GPU (Q4_0 has SOA bug) |
| Qwen3.5-0.8B Q8_0 | Qwen3.5-0.8B-Q8_0.gguf | 775MB | **GPU (4.6 t/s)** |
| Qwen3.5-2B Q4_K_M | Qwen3.5-2B-Q4_K_M.gguf | 1.2GB | CPU via ollama (3.3 t/s) |
| Qwen3.5-2B Q4_0 | Qwen3.5-2B-Q4_0.gguf | 1.2GB | **GPU (3.4 t/s)** |
| Qwen3.5-4B Q4_0 | Qwen3.5-4B-Q4_0.gguf | 2.5GB | **GPU (2.0 t/s)** |

## Magisk Modules
- openssh: v9.9p2 - persistent SSH on port 9022
- adreno-650_819v2: Updated GPU driver blobs (XDA: A65X-819v2.zip)
- nethunter: Kali NetHunter v1.4.0

## Memory Management
- Always kill models before loading new ones
- Before large models: `am kill-all && echo 3 > /proc/sys/vm/drop_caches`
- 4B+ models will crash the phone (OOM)
- Monitor: `cat /proc/meminfo | grep MemAvail`

## Known Issues
- Q4_0 on GPU: SOA buffer alignment crash (null extra0_q4_0->q)
- Flash attention: crashes Vulkan Turnip driver
- Vulkan llama.cpp: DeviceLostError on inference
- OpenCL embedded kernels: 30+ min JIT compile time
- First OpenCL run with non-embedded: ~3 min JIT
