#!/bin/bash
# Setup Ollama and pull the appropriate model for your hardware.
#
# Model selection:
#   NVIDIA GPU (CUDA) or Apple Silicon  →  qwen3.5:9b  (~5 GB VRAM/RAM)
#   CPU only                            →  qwen2.5:3b  (~2 GB RAM, 8 GB total recommended)
#
# You can override the model: ./setup_ollama.sh qwen3.5:9b

set -e

echo "=== AI Apply: Ollama Setup ==="

# --- Detect hardware ---
HARDWARE="cpu"
if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    HARDWARE="cuda"
elif [[ "$(uname -m)" == "arm64" && "$(uname -s)" == "Darwin" ]]; then
    HARDWARE="mps"
fi

echo "Hardware detected: $HARDWARE"

# --- Choose model ---
if [[ -n "$1" ]]; then
    MODEL="$1"
    echo "Model override: $MODEL"
elif [[ "$HARDWARE" == "cuda" || "$HARDWARE" == "mps" ]]; then
    MODEL="qwen3.5:9b"
    echo "GPU/Apple Silicon detected → using $MODEL"
else
    MODEL="qwen2.5:3b"
    echo "CPU-only detected → using lighter model $MODEL"
    echo "(Tip: generation will be slower on CPU. Consider a machine with a GPU for best results.)"
fi

# --- Install Ollama if not present ---
if ! command -v ollama &> /dev/null; then
    echo ""
    echo "Installing Ollama..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install ollama
    else
        curl -fsSL https://ollama.com/install.sh | sh
    fi
else
    echo "Ollama already installed: $(which ollama)"
fi

# --- Start Ollama server if not running ---
if ! curl -s http://localhost:11434/api/tags &> /dev/null; then
    echo "Starting Ollama server..."
    ollama serve &
    sleep 3
fi

# --- Pull model ---
echo ""
echo "Pulling model: $MODEL (this may take a few minutes on first run)"
ollama pull "$MODEL"

# --- Write model to profile if it exists ---
if [[ -f "profile.yaml" ]]; then
    if grep -q "ollama_model:" profile.yaml; then
        # Update existing entry
        sed -i.bak "s|ollama_model:.*|ollama_model: \"$MODEL\"|" profile.yaml && rm -f profile.yaml.bak
        echo "Updated profile.yaml → ollama_model: $MODEL"
    fi
fi

# --- Verify ---
echo ""
echo "=== Verification ==="
echo "Available models:"
ollama list
echo ""
echo "Testing generation..."
curl -s http://localhost:11434/api/generate \
    -d "{\"model\": \"$MODEL\", \"prompt\": \"Say hello in one sentence.\", \"stream\": false}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('response','ERROR'))"
echo ""
echo "=== Setup complete! Model: $MODEL ==="
