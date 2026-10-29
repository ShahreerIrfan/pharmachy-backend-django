"""
ASGI config for Bangladesh Multi-Tenant Pharmacy ERP & POS Cloud API.
"""

import os
import sys
from pathlib import Path

base_dir = Path(__file__).resolve().parent.parent
root_dir = base_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(base_dir) not in sys.path:
    sys.path.insert(0, str(base_dir))

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
