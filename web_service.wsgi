import os
import sys

# Make web_service.py importable no matter which directory Apache starts in.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# mod_wsgi looks for a callable named "application".
from web_service import app as application
