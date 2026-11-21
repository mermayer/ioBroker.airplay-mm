"""Temporary storage for isolated tests."""
import os
from pathlib import Path
import tempfile
TEST_TMP=Path(os.environ.get('AIRPLAY_TEST_TMP',tempfile.gettempdir()))
TEST_TMP.mkdir(parents=True,exist_ok=True)
