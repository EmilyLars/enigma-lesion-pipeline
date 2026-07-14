# Release Notes — Brain Extraction Update

This release adds SynthStrip as an alternative brain extraction backend and
introduces a marker-based rerun system for handling per-subject failures.
Driven by beta-tester feedback, primarily from the LLU site, which saw an
81% ANTs failure rate on clinical pediatric data acquired on a Siemens
CIMA.X (XA61) scanner.

## Summary of changes

### New features

- **`--extract-method {ants,synthstrip}`** selects the brain extraction
  backend. ANTs remains the default to preserve existing behavior.
  SynthStrip (Hoopes et al., *NeuroImage* 2022) is age-agnostic and more
  robust to pathology, lesions, and atypical contrasts.

- **`--extract-mask DIR`** lets a site supply pre-prepared brain masks for
  some or all subjects. When a file named `<subject>_BrainMask.nii.gz`
  exists in `DIR`, that mask takes priority for that subject regardless of
  `--extract-method`. Intended as an escape hatch for the small set of
  subjects where both automated methods fail.

- **Method-specific success markers** are now written to
  `<output_dir>/.markers/<subject>_brain_extraction_<method>.done` on
  successful completion. Markers contain provenance: method, age, host,
  timestamp, exit code, and output paths. Failed runs leave a corresponding
  `.failed` breadcrumb for triage.

- **`--rerun-failed`** filters the subjects file to only those without any
  success marker. Combined with `--extract-method`, this is the intended
  workflow for rescuing ANTs failures with SynthStrip:

  ```
  --step extract                                     # try ANTs everywhere
  --step qc-extract                                  # review, delete bad markers
  --step extract --extract-method synthstrip \
                 --rerun-failed                      # rescue the failures
  ```

- **`--list-failed`** prints the subjects that `--rerun-failed` would
  process and exits, without submitting any jobs. Useful for sanity checks
  and for writing rescue counts into site-level processing logs.

### Behavior changes

- The brain extraction worker now writes both `BrainExtractionBrain.nii.gz`
  and `BrainExtractionMask.nii.gz` regardless of the method used. Downstream
  steps are unchanged.

- `--force` now removes existing success markers for the currently-selected
  method before processing, rather than relying on file presence checks.
  This is more robust against partially-corrupted outputs.

- The "skip if output exists" check in the worker has been replaced with
  "skip if marker exists." This eliminates a class of false positives where
  a half-finished output file from a SLURM timeout looked complete enough
  to skip on the next run.

### Container changes

- Container is now a **multi-stage Apptainer build**. Stage 1 pulls
  `freesurfer/synthstrip:1.6` from Docker Hub purely as a source for
  copying the `mri_synthstrip` binary and model weights into the final
  image. The FreeSurfer stage is discarded after copy.

- Requires **Apptainer ≥3.6 or Singularity ≥3.2** to build. Already
  available on USC/LONI and CHPC; no change for runtime users.

- Adds **CPU PyTorch 2.3.1** (`torch==2.3.1` from
  `download.pytorch.org/whl/cpu`) and **surfa** (SynthStrip's I/O library)
  to the Python environment. Approximately +200 MB on the final image. GPU
  builds are possible by swapping the index URL — see comments in
  `enigma-lesion.def`.

- New build hosts vs. the previous ANTs-only build:
  `download.pytorch.org` is the only addition. Docker Hub is already used
  for the base image; the FreeSurfer SynthStrip image lives there too.

### What did *not* change

- ANTs version (2.5.3) and command-line invocation are unchanged.
- Age-binned template selection at cutoff 16 (configurable with
  `--age-cutoff`) is unchanged. Note that SynthStrip is age-agnostic and
  ignores the age column entirely.
- Output file names (`<subject>_BrainExtractionBrain.nii.gz`,
  `<subject>_BrainExtractionMask.nii.gz`) are unchanged. Existing
  downstream steps (`check-dims`, `register`, `qc-register`) require no
  modification.
- Existing runs are forward-compatible: if you re-run a previously
  completed batch, the lack of markers means everything will reprocess.
  To preserve existing outputs without reprocessing, generate retroactive
  markers with the helper snippet at the end of this document.

## Files modified

- `pipeline.sh` — new CLI flags, marker-aware extract step, rerun-failed
  filter.
- `run_brain_extraction_worker.sh` — method dispatch, marker writing,
  user-mask handling.
- `enigma-lesion.def` — multi-stage build pulling SynthStrip from the
  official Docker image.

## Files added

- `BRAIN_EXTRACTION_README.md` — user-facing guide to the new workflow.

## Migration notes for sites already running the pipeline

If you have already completed brain extraction with the previous version
and want the new rerun-failed logic to recognize those completed subjects
without reprocessing them, generate retroactive markers:

```bash
DATA_DIR=/path/to/data
MARKER_DIR="${DATA_DIR}/T1s_brain/.markers"
mkdir -p "$MARKER_DIR"

for f in "${DATA_DIR}/T1s_brain"/*_BrainExtractionBrain.nii.gz; do
    sub=$(basename "$f" "_BrainExtractionBrain.nii.gz")
    cat > "${MARKER_DIR}/${sub}_brain_extraction_ants.done" << EOF
subject=$sub
method=ants
date=$(date -Iseconds)
note=retroactive marker, original run pre-dates marker system
EOF
done
```

Without this step, `--rerun-failed` will treat the entire batch as needing
reprocessing.

## Open items / known limitations

- SynthStrip's `--no-csf` mode (which excludes CSF from the brain
  boundary) is not exposed as a pipeline flag. The default behavior
  (CSF-inclusive) matches what ANTs produces and is what downstream
  registration expects. If a site has a specific reason to need
  CSF-exclusive masks they can run `mri_synthstrip --no-csf` directly and
  feed the result in via `--extract-mask`.

- The audit-trail summary command in the README assumes a single batch
  per `MARKER_DIR`. Sites that batch by session/timepoint and want
  per-batch summaries can pre-filter the glob.

- The pediatric "d-SynthStrip" model (Kelley et al., ISBI 2024) is not yet
  bundled. The general-purpose SynthStrip 1.6 model handles pediatric
  cases well enough for the LLU use case that drove this release, but
  switching to d-SynthStrip would be a one-line change to the container
  build if a future ENIGMA-Pediatric working group decides to standardize
  on it.

## Acknowledgments

Thanks to the LLU team for the initial failure report and to Marsh Königs
(Amsterdam UMC) for the SynthStrip recommendation.

## References

- Hoopes A, Mora JS, Dalca AV, Fischl B, Hoffmann M. SynthStrip:
  Skull-Stripping for Any Brain Image. *NeuroImage* 260, 119474 (2022).
- Tustison NJ, Avants BB, Cook PA, et al. The ANTs cortical thickness
  measurement pipeline. *NeuroImage* 99, 166–179 (2014).
