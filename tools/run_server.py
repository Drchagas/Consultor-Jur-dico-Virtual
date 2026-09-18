from __future__ import annotations
import os, sys
from pathlib import Path
ROOT=Path(os.environ.get('JARBAS_ROOT',Path(__file__).resolve().parent.parent)).resolve()
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env.local',override=False)
import uvicorn
port=int(os.environ.get('JARBAS_PORT','8765'))
uvicorn.run('app.main:app',host='127.0.0.1',port=port,log_level='info',access_log=True)
