# Attestator 1

This chair holds Chandra (`datalab-to/chandra-ocr-2`), adapter `chandra.v1`. The pinned revision is declared in
`config/models-real.toml`, so repinning that vendor's model is a configuration
change. Moving the vendor to another chair is a code change: its answer bound,
decoding rule and request shape are keyed by this chair or its adapter
(`pipeline/3_attestatores/live_witness.py`, `common/request_capacity.py`,
`common/decoding.py`). Its native retry loop (`pipeline/3_attestatores/chandra_native.py`) and its wire flags (`common/chair_wire.py`) run only in this chair.
