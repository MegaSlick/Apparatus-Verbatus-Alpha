# data

Reserved for future, verified movement of runs and exports between machines. Nothing is
implemented in this directory; the movements that exist live with the surfaces that own
them, and are named here so nobody looks for a fourth.

**Up, to the volume.** `verbatus upload --network-volume DATACENTER:VOLUME_ID` sends only
the files a sealed submission record names, SHA-256-checked before and after
(`operations/pod/transfer.py`, `operations/operator/volume_s3.py::S3VolumeTarget`).

**Back, from the volume.** `verbatus fetch-run --run-id <id> --into <local root>
--network-volume DATACENTER:VOLUME_ID` lists every object under `runs/<id>/` on the
volume and fetches each into `<local root>/<id>/`, then checks it the way the run tree
checks itself: a blob must hash to its own name, a receipt to its own name, an artifact to
the digest its stage manifest records, `run.json` to its own self-hash, and every stage
manifest must equal the one the fetched artifacts rebuild. An object no stage accounts
for is a refusal by name; a publication temporary is skipped and its name recorded; a
local file that already exists is compared, never replaced. Zero GPU-hours; the listing
and `GetObject` path has never run against a real endpoint
(`operations/operator/volume_s3.py::S3VolumeObjectReader`, `surface.fetch_run`).

**Sideways, on this machine.** `verbatus backup` copies a local run tree into a
content-addressed synced directory (`operations/operator/backup.py`). It does not read a
volume.

What does not exist: any movement of a run tree from a pod to anywhere but its own
attached volume, and any export transfer beyond the base Armarium bundle `verbatus
export` writes locally.
