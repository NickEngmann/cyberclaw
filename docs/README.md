# Cyberclaw - Local LLM on Kali NetHunter (Adreno 650 GPU)

## Quick Start - GPU Inference (OpenCL)
```bash
# From Android root shell (ssh -p 9022 shell@192.168.1.53):
TERMUX_HOME=/data/data/com.termux/files/home
KERNELS=$TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels
CLI=$TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli

su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $KERNELS; \
  $CLI -m $TERMUX_HOME/models/Qwen3.5-0.8B-Q8_0.gguf \
  -ngl 99 -c 512 -n 100 -no-cnv \
  -p 'Your prompt here'"
```
First run takes ~3 min for kernel JIT. Subsequent runs are fast.

## Quick Start - CPU Inference (ollama)
```bash
# Enter Kali chroot first:
/data/data/com.offsec.nhterm/files/usr/bin_aarch64/kali
# Then:
export TMPDIR=/tmp OLLAMA_VULKAN=0 OLLAMA_KEEP_ALIVE=1m
ollama serve &
ollama run qwen3.5:2b "Your prompt"
ollama stop qwen3.5:2b  # ALWAYS stop when done
```

## After Reboot Checklist
1. SSH should auto-start (Magisk module)
2. Re-mount /vendor in Kali chroot if needed:
   ```bash
   # From Android root shell:
   mount --bind /vendor /data/local/nhsystem/kalifs/vendor
   ```
3. GPU inference (Termux OpenCL) works immediately - no remount needed

## Performance Benchmarks (v819.2 driver, E031.50)

| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| **Qwen3.5-0.8B** | Q8_0 | **OpenCL GPU** | **30.5 t/s** | **6.3 t/s** |
| Qwen3.5-0.8B | Q8_0 | CPU (llama.cpp) | 21.9 t/s | 3.9 t/s |
| Qwen3.5-2B | Q4_K_M | OpenCL GPU | 3.9 t/s | 1.8 t/s |
| Qwen3.5-2B | Q8_0 | CPU (ollama) | 18.4 t/s | 3.3 t/s |

**Best for 0.8B**: OpenCL GPU (6.3 t/s gen, 62% faster than CPU)
**Best for 2B**: CPU via ollama (3.3 t/s gen, 74% faster than GPU)

## Architecture

### GPU Path (OpenCL via Termux)
- llama.cpp compiled in Termux (bionic) against vendor `/vendor/lib64/libOpenCL.so`
- Adreno 650 driver v819.2 (E031.50) installed via Magisk module
- Patched for CL2.0 device on CL3.0 platform
- Must run from kernel source dir for `.cl` file loading

### CPU Path (Kali chroot)
- ollama v0.18.1 for easy model management
- llama.cpp Vulkan build (CPU-only, ngl=0) for raw performance

### Why not Vulkan?
- Vendor Adreno Vulkan = v1.1 only (llama.cpp needs 1.2)
- Mesa Turnip Vulkan = crashes with DeviceLostError during inference
- OpenCL is the only working GPU path on this device

## Models
| Model | File | Size | Location |
|-------|------|------|----------|
| Qwen3.5-0.8B Q4_0 | Qwen3.5-0.8B-Q4_0.gguf | 484MB | Kali + Termux |
| Qwen3.5-0.8B Q8_0 | Qwen3.5-0.8B-Q8_0.gguf | 775MB | Kali + Termux |
| Qwen3.5-2B Q4_K_M | Qwen3.5-2B-Q4_K_M.gguf | 1.2GB | Kali + Termux |

## Memory Safety
- **Always** `ollama stop <model>` or kill llama-cli when done
- Before large models: `am kill-all && echo 3 > /proc/sys/vm/drop_caches`
- **4B+ models WILL crash the phone** (OOM on shared memory)
- Check available: `cat /proc/meminfo | grep MemAvail` (need >4GB)

## Magisk Modules
| Module | Purpose |
|--------|---------|
| openssh (v9.9p2) | Persistent SSH on port 9022 |
| adreno-650_819v2 | Updated GPU driver (E031.50) |
| nethunter (v1.4.0) | Kali chroot + tools |

## Known Issues & Future Work
- Q4_0 on GPU crashes (SOA buffer alignment bug in llama.cpp)
- OpenCL Adreno-optimized kernels available but need 30+ min JIT
- Embedded kernel build takes 60+ min JIT (use non-embedded instead)
- Could try MLC LLM for potentially better Adreno OpenCL optimization
