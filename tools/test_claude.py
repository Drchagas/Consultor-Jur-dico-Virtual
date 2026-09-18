#!/usr/bin/env python3
"""Testa a chave da Anthropic. Sai com 0 se funcionar."""
import os, sys
from pathlib import Path
ROOT = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(ROOT))
try:
    from app.ai_gateway import test_connection, friendly_error, legal_model_name
    r = test_connection()
    print(f"CLAUDE_OK modelo={r.model} tokens={r.input_tokens}+{r.output_tokens}")
    sys.exit(0)
except Exception as exc:
    try:
        from app.ai_gateway import friendly_error
        print("CLAUDE_FALHOU:", friendly_error(exc))
    except Exception:
        print("CLAUDE_FALHOU:", exc)
    sys.exit(1)
