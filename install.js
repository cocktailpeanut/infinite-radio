// Infinite Radio installer
// 1. Downloads the audio.cpp prebuilt release that matches this machine
// 2. Installs the Python web app dependencies into a venv
// 3. Extracts the audio.cpp binaries (cross-platform, via Python)
// 4. Downloads the YuE2 GGUF model package from Hugging Face
const AUDIOCPP_VERSION = "v0.9.4"
const BASE = `https://github.com/0xShug0/audio.cpp/releases/download/${AUDIOCPP_VERSION}`
const asset = (name) => `${BASE}/audio-${AUDIOCPP_VERSION}-${name}`
// llama.cpp powers the built-in lyric writer (a small local LLM). Vulkan builds cover any
// Windows/Linux GPU without a 400+ MB CUDA runtime; CPU builds are used when no GPU is found.
const LLAMA_BUILD = "b12211"
const LLAMA_BASE = `https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_BUILD}`
const llama = (name) => `${LLAMA_BASE}/llama-${LLAMA_BUILD}-bin-${name}`

module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [
    // ---- audio.cpp binaries (one of these runs, depending on platform / GPU) ----
    {
      when: "{{platform === 'darwin' && arch === 'arm64'}}",
      method: "fs.download",
      params: {
        url: asset("bin-macos-arm64-metal.tar.gz"),
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'darwin' && arch !== 'arm64'}}",
      method: "fs.download",
      params: {
        url: asset("bin-macos-x64-metal.tar.gz"),
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'win32' && gpu === 'nvidia' && Number.parseFloat(gpu_driver || '0') >= 580}}",
      method: "fs.download",
      params: {
        url: [
          asset("bin-windows-x64-cuda13.3.zip"),
          asset("cudart-windows-x64-cuda13.3.zip")
        ],
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'win32' && gpu === 'nvidia' && Number.parseFloat(gpu_driver || '0') < 580}}",
      method: "fs.download",
      params: {
        url: [
          asset("bin-windows-x64-cuda12.4.zip"),
          asset("cudart-windows-x64-cuda12.4.zip")
        ],
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'win32' && gpu !== 'nvidia' && gpu !== 'none'}}",
      method: "fs.download",
      params: {
        url: asset("bin-windows-x64-vulkan.zip"),
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'win32' && gpu === 'none'}}",
      method: "fs.download",
      params: {
        url: asset("bin-windows-x64-cpu.zip"),
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'linux' && gpu === 'nvidia'}}",
      method: "fs.download",
      params: {
        url: [
          asset("bin-ubuntu-x64-cuda12.8-colab.tar.gz"),
          asset("cudart-ubuntu-x64-cuda12.8.tar.gz")
        ],
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'linux' && gpu !== 'nvidia' && gpu !== 'none'}}",
      method: "fs.download",
      params: {
        url: asset("bin-ubuntu-x64-vulkan.tar.gz"),
        dir: "app/bin"
      }
    },
    {
      when: "{{platform === 'linux' && gpu === 'none'}}",
      method: "fs.download",
      params: {
        url: asset("bin-ubuntu-x64-cpu.tar.gz"),
        dir: "app/bin"
      }
    },
    // ---- llama.cpp binaries for the lyric writer (one of these runs) ----
    {
      when: "{{platform === 'darwin' && arch === 'arm64'}}",
      method: "fs.download",
      params: { url: llama("macos-arm64.tar.gz"), dir: "app/bin/llama" }
    },
    {
      when: "{{platform === 'darwin' && arch !== 'arm64'}}",
      method: "fs.download",
      params: { url: llama("macos-x64.tar.gz"), dir: "app/bin/llama" }
    },
    {
      when: "{{platform === 'win32' && gpu !== 'none'}}",
      method: "fs.download",
      params: { url: llama("win-vulkan-x64.zip"), dir: "app/bin/llama" }
    },
    {
      when: "{{platform === 'win32' && gpu === 'none'}}",
      method: "fs.download",
      params: { url: llama("win-cpu-x64.zip"), dir: "app/bin/llama" }
    },
    {
      when: "{{platform === 'linux' && gpu !== 'none'}}",
      method: "fs.download",
      params: { url: llama("ubuntu-vulkan-x64.tar.gz"), dir: "app/bin/llama" }
    },
    {
      when: "{{platform === 'linux' && gpu === 'none'}}",
      method: "fs.download",
      params: { url: llama("ubuntu-x64.tar.gz"), dir: "app/bin/llama" }
    },
    // ---- web app dependencies ----
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install -r requirements.txt"
        ]
      }
    },
    // ---- unpack audio.cpp (writes app/runtime.json with the detected backend) ----
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          `python tools/extract_bin.py --version ${AUDIOCPP_VERSION}`
        ]
      }
    },
    // ---- lyric writer model: Qwen3 1.7B instruct, Q4_K_M (~1.1 GB) ----
    {
      method: "hf.download",
      params: {
        path: "app",
        _: [
          "unsloth/Qwen3-1.7B-GGUF",
          "Qwen3-1.7B-Q4_K_M.gguf"
        ],
        "local-dir": "models/lyrics"
      }
    },
    // ---- YuE2 model package (main GGUF chosen by the install menu, default Q8_0) ----
    {
      method: "hf.download",
      params: {
        path: "app",
        _: [
          "audio-cpp/Yue2-3B-GGUF",
          "yue2-3b-{{args.quant || 'q8_0'}}.gguf",
          "yue2-vae-f16.gguf",
          "sidecars/yue2-model-config.json",
          "sidecars/yue2-generation-config.json",
          "sidecars/yue2-qwen.tiktoken",
          "sidecars/yue2-vae-config.json"
        ],
        "local-dir": "models/Yue2-3B-GGUF"
      }
    }
  ]
}
