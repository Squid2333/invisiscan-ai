# RGBDSort runtime component

This directory contains the code reached by the original `depth.sh` flow:

- `rtsp_server.py` publishes the camera stream through MediaMTX.
- `RGBsort.py` performs RGB detection, depth estimation, and tracking.
- `track_comsumer.py` consumes Redis tracking events and runs LSTM/face/segmentation post-processing.

The source copy is self-contained and resolves model paths relative to this
directory.  Large model files are present locally under `weights/` but are
ignored by Git.  Before running from a fresh clone, restore the required files
from the local model archive (see the filenames already present in `weights/`)
and restore the three MTCNN files under
`face_alignment/mtcnn_pytorch/src/weights/`.

Install the pinned Python dependencies with `requirements.txt` or
`environment.yml`.  Start each process from this directory (or use the
repository-level launcher) so the local modules are on `PYTHONPATH`:

```bash
python rtsp_server.py
python RGBsort.py
python track_comsumer.py
```

Copy `.env.example` to `.env` and export the values appropriate for the
deployment.  The scripts read environment variables directly; this project
does not load `.env` automatically, so use `set -a; source .env; set +a` or a
process manager's environment configuration.  MySQL and MediaMTX are external
services and are not included here.
