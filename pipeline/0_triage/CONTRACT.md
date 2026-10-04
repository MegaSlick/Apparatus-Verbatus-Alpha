# Triage — contract

Triage is optional work done before the Door: deciding how each photographed frame is
split into pages, cropped, deskewed and converted, and which frames are captures of
the same leaf. It is not a pipeline stage and nothing here runs in a run. The decisions
reach the Door as JSON documents whose schema is `common/contracts/triage.py`; the
tools that produce them are in `operations/triage/` (see its README).

## The decision manifest

`triage-decision-manifest-v1` is `{schema, corpus_id, records}`: one non-blank corpus
id and at most 1,000 rows, at most one per submitted frame. Each row is closed:

```text
corpus_id               must equal the manifest's
source_frame_sha256     digest of the submitted master's bytes
frame                   {width, height} of the master's stored raster
split                   {operation_order, parts: [...]}; the order is the split's version
re_shoot_cluster_id     null, or the cluster this frame is a capture in
confidence              integer 0-4
mode                    manual | semi | auto  (common.contracts.stages.TRIAGE_MODES)
actor                   {kind, identity, revision}: who proposed the geometry
human_override          whether a person then changed it
manifest_row_sha256     digest of every other field
```

There is no winner or canonical field: triage never chooses among captures.

`actor.kind` is `human`, `model`, `scantailor` or `producer`; a human's `revision` is
null, every other actor names its resolved revision. `actor` and `human_override` are
independent: a person correcting a tool's crop is a tool actor with `human_override`
true.

## Geometry is per split part

Each part is `{region, crop_box, rotation, colour_mode}`, because one frame's pages can
need different crops, angles and colour treatment (a document taped over the page at
its own angle, a colour insert beside a bitonal page). A frame that is not split
declares one full-frame part.

- `region` (`space: "frame"`) is a half-open integer rectangle in the master's stored
  raster, before any EXIF orientation. The parts' regions are pairwise disjoint and
  cover the frame exactly, so no pixel is dropped or counted twice.
- `crop_box` (`space: "part"`) is a half-open rectangle in the part's own coordinates.
- `rotation` is `{rotation_millidegrees in [-180000, 180000], direction: "clockwise",
  origin: "crop-centre", canvas: "expand"}`.
- `colour_mode` is `keep`, `grayscale`, `rgb` or `bitonal`. `keep` admits only modes
  the deterministic encoder stores losslessly; anything else needs an explicit
  conversion.

A consumer applies a part in the order `operation_order` names. There are two, and a
row of either keeps its meaning:

- `region-crop-rotate`: cut the region from the untouched master, crop, rotate
  clockwise about the crop's centre onto an expanded canvas, convert. A part is exactly
  the four fields above, and the canvas beyond the scan is black.
- `region-crop-rotate-crop`: the same, then cut `post_crop_box` from the rotated canvas,
  with every pixel beyond the rotated scan set to `fill`. A part adds two fields:
  `post_crop_box` (`space: "rotated"`, a half-open integer rectangle in the rotated
  canvas's coordinates, which may reach past the canvas) and `fill`
  (`{levels: [...]}`, one level in [0, 255] per band of the master's own mode; for a
  palette master, the palette index). A deskewed page is then cropped tight, and its
  margin is whatever the row records, such as the paper's level.

Sampling, fill rules and encoding belong to the order's apply recipe,
`common.imaging.triage_apply_recipe(operation_order)` (`triage-raster-apply-v1` or
`-v2`), not to this record.

Every operation is affine, so a point on a sealed page maps back to the master frame
from the part alone: `common.imaging.triage_point_to_frame(part, point)`.

## Re-shoot clusters

`triage-re-shoot-cluster-v1` is `{schema, corpus_id, cluster_id, member_frame_sha256,
split_count}`, supplied as a mapping keyed by `cluster_id`. A cluster has two to 4,096
distinct member digests, all with the same split count. Membership holds both ways: a
row may name a cluster only if it is a member, a frame belongs to at most one cluster,
and a member whose row is in the manifest must name that cluster. A manifest that names
a cluster is refused without the cluster records.

The Door refuses any submission that contains a cluster (see
`pipeline/1_exemplar/CONTRACT.md`), because no stage links captures of one leaf.

## Modes and refusals

The modes are named once, as `common.contracts.stages.TRIAGE_MODES`. The sealed
`config/triage_modes.toml` must name exactly those modes, which
`common.stage.load_triage_modes` checks when it reads the file.

Every refusal is a `SchemaRefusal`. Work is bounded before it can amplify: 1,000 rows
and 1,000 cluster records per manifest, 4,096 members per cluster, 64 parts per frame
(the pairwise overlap check is quadratic in parts).

## What the Door checks

Before rendering any part the Door refuses a row whose `source_frame_sha256` is not the
submitted bytes' digest, or whose `frame` is not exactly the decoded master's size (a
row bound to a cropped derivative, or a stale row, would otherwise shift or drop
pixels). Each derivative page carries `derivative_page_backlink(row, part_index)`:
corpus, source digest, row digest and part index. The Exemplar boundary re-validates
the embedded row with the same `validate_row` and re-derives the page from its master.
