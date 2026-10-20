#!/usr/bin/env bash

set -euo pipefail

uvicorn app.main:app --reload