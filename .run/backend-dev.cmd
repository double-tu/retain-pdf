@echo off
cd /d "E:\Workspace\git\retain-pdf\backend\rust_api"
set "RUST_API_BIND_HOST=127.0.0.1"
set "RUST_API_PORT=41000"
set "RUST_API_SIMPLE_PORT=42000"
set "TYPST_BIN=E:\Workspace\git\retain-pdf\typst\bin\typst.exe"
set "RUST_API_UPLOAD_MAX_BYTES=524288000"
set "RUST_API_UPLOAD_MAX_PAGES=500"
cargo run > "E:\Workspace\git\retain-pdf\.run\backend.log" 2>&1
