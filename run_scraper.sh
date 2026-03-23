#!/bin/bash
set -e
cd "$(dirname "$0")"
TOKEN=$(grep GITHUB_TOKEN .env | cut -d= -f2 | tr -d '\r')
echo "Token prefix: ${TOKEN:0:20}..."
venv/bin/python3 scraper.py --github-token "$TOKEN" --concurrency 8
