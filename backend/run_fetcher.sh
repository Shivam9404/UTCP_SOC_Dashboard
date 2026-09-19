#!/bin/bash

docker run --rm \
  --env-file /mssp_dashoard/UTCP_SOC_Dashboard/backend/.env \
  -e OUTPUT_DIR=/app/data \
  -v /mssp_dashoard/UTCP_SOC_Dashboard/frontend/public/data:/app/data \
  utcp-fetcher
