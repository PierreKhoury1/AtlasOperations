# Sample camera footage

Committed so fresh clones (CI, cloud sessions) have camera sources without downloading.
`atlas.designer.sample_dir()` uses `ATLAS_SAMPLE_VIDEOS`, then `~/AtlasDemo/videos`, then this folder.

| Clips | Source | Licence |
|---|---|---|
| corner-store_ezymart, retail-store, liquor-store-delivery | Wikimedia Commons | Public domain |
| restaurant-sushi-counter | Wikimedia Commons, "2026-0120 Hiden sushi making Seijun Okano" | CC BY 3.0 (credit the uploader) |
| hotel-lobby_* | EC Funded CAVIAR project / IST 2001 37540 (INRIA) | Free for research use, cite CAVIAR |
| campus-* | MEVA dataset (Kitware) | CC BY 4.0 |
| kitchen-* | EPFL Smart Kitchen, Zenodo record 15535461 | Check the record before commercial use |
| data/theatre_clips VIRAT_* | VIRAT Video Dataset | Research use |

`campus-*` were re-encoded to about 2.3 Mbps so each stays under GitHub's 100 MB file limit.
Full-quality originals and raw sources over 95 MB are not committed. Regenerate them with
`scripts/get_demo_videos.py` and `raw/kitchen/pull.py`.
