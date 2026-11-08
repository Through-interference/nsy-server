"""Stable source-tree paths and Python subprocess entrypoints."""
import os
from pathlib import Path
import sys
import site

PROJECT_ROOT=Path(__file__).resolve().parents[2]
SOURCE_ROOT=PROJECT_ROOT/'src'
def resolve_config(project_root=PROJECT_ROOT,prefixes=None):
    """Find source, venv/distro-prefix, or user-installed configuration."""
    if prefixes is None:
        user=Path(site.getuserbase())
        prefixes=[Path(sys.prefix),Path(sys.prefix)/'local']
        if Path(__file__).resolve().is_relative_to(user):prefixes.insert(0,user)
        else:prefixes.append(user)
    candidates=[Path(project_root)/'config/station.json']
    candidates.extend(Path(prefix)/'share/nsy-server/config/station.json' for prefix in prefixes)
    return next((path for path in candidates if path.is_file()),candidates[0])

DEFAULT_CONFIG=resolve_config()

def module_command(name,*args):
    return [sys.executable,'-m','nsy_server.'+name,*map(str,args)]

def child_env():
    env=os.environ.copy()
    existing=env.get('PYTHONPATH')
    env['PYTHONPATH']=str(SOURCE_ROOT)+(os.pathsep+existing if existing else '')
    return env
