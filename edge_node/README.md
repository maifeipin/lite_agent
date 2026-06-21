# Edge Node Deployment

This directory contains the necessary payload to deploy an Edge Sentinel node.

## Deployment Strategy: Flattening

The contents of this directory are designed to be deployed **flat** on the edge node.
Do **not** copy the `edge_node` directory itself. Instead, copy all files *inside* this directory into the target directory on the edge node (default: `/opt/edge_sentinel/`).

### Steps for a new VPS:
1. `mkdir -p /opt/edge_sentinel/` on the edge node.
2. Copy `edge_sentinel.py`, `edge_crypto.py`, `edge_whitelist.py`, and `whitelist.json` into `/opt/edge_sentinel/`.
3. Create a `.env` file in `/opt/edge_sentinel/` with your keys.
4. Setup a systemd service or cron job to run `python3 /opt/edge_sentinel/edge_sentinel.py`.

*Note: The `__init__.py` file is only used by the central Lite Agent to import these modules as a package. It is ignored by the edge node.*
