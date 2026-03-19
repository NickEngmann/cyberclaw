# Cyberclaw Quick Reference Commands

## SSH Access
```bash
# Local network
ssh -p 9022 shell@192.168.1.53

# Via Tailscale (port 22)
ssh shell@<tailscale-ip>

# Enter Kali chroot
/data/data/com.offsec.nhterm/files/usr/bin_aarch64/kali
```

## GPU Inference (OpenCL - Recommended)

### Run 2B Q8_0 on GPU (best quality + speed balance)
```bash
TERMUX_HOME=/data/data/com.termux/files/home
su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels; \
  $TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli \
  -m $TERMUX_HOME/models/Qwen3.5-2B-Q8_0.gguf \
  -ngl 99 -c 512 -n 200 -no-cnv \
  -p 'Your prompt here'"
```

### Run 0.8B Q8_0 on GPU (fastest)
```bash
TERMUX_HOME=/data/data/com.termux/files/home
su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels; \
  $TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli \
  -m $TERMUX_HOME/models/Qwen3.5-0.8B-Q8_0.gguf \
  -ngl 99 -c 512 -n 200 -no-cnv \
  -p 'Your prompt here'"
```

### Run 4B Q4_0 on GPU (best quality, slower)
```bash
TERMUX_HOME=/data/data/com.termux/files/home
su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels; \
  $TERMUX_HOME/llama.cpp/build-fast/bin/llama-cli \
  -m $TERMUX_HOME/models/Qwen3.5-4B-Q4_0.gguf \
  -ngl 99 -c 512 -n 200 -no-cnv \
  -p 'Your prompt here'"
```

## CPU Inference (ollama)
```bash
# Enter Kali chroot first, then:
export TMPDIR=/tmp OLLAMA_VULKAN=0 OLLAMA_KEEP_ALIVE=1m
ollama serve &
ollama run qwen3.5:2b "Your prompt"
ollama stop qwen3.5:2b   # ALWAYS stop when done
```

## Memory Management
```bash
# Free RAM (run from Android root shell, not chroot)
am kill-all
echo 3 > /proc/sys/vm/drop_caches

# Check available memory
cat /proc/meminfo | grep MemAvail

# Kill any running llama processes
ps -ef | grep llama-cli | grep -v grep | awk '{print $2}' | xargs kill -9
```

## Performance Summary
| Model | Quant | GPU Gen | GPU Prompt | Use Case |
|-------|-------|---------|------------|----------|
| 0.8B | Q8_0 | 6.3 t/s | 30.5 t/s | Quick tasks, fastest response |
| 2B | Q8_0 | 4.8 t/s | 23.3 t/s | Good quality + speed balance |
| 4B | Q4_0 | 2.0 t/s | 10.1 t/s | Best quality, ~2 words/sec |

## Useful Flags
```
-ngl 99          # Offload all layers to GPU
-c 512           # Context size (keep small for memory)
-n 200           # Max tokens to generate
-no-cnv          # Non-conversation mode (single prompt, exit after)
--no-display-prompt  # Don't echo the prompt back
-e               # Enable escape sequences in prompt
-p "..."         # The prompt
```
