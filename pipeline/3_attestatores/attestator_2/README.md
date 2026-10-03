# Attestator 2

This chair holds DAI's record reader (`Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`), adapter `dai.v1`. The pinned revision is declared in
`config/models-real.toml`, so repinning that vendor's model is a configuration
change. Moving the vendor to another chair is a code change: its answer bound,
decoding rule and request shape are keyed by this chair or its adapter
(`pipeline/3_attestatores/live_witness.py`, `common/request_capacity.py`,
`common/decoding.py`). It reads the crops its own project's record detector (the `secondary_proposer` chair) cut from the page.
