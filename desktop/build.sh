#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p ../exe
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-H windowsgui -s -w" -o ../exe/LinguaFlow.exe .
echo "built ../exe/LinguaFlow.exe"
