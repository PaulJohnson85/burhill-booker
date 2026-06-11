#!/bin/bash
set -e
echo "Installing dependencies …"
pip install -r requirements.txt
playwright install chromium
echo "Done. Edit config.py then run: python book.py --dry-run"
