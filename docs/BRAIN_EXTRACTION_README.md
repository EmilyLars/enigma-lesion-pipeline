# Brain Extraction in the ENIGMA Lesion Registration Pipeline

This document describes how to choose a brain extraction method, what to do
when extractions fail at QC, and how to reprocess only the subjects that need
it. It is intended for site analysts running the pipeline; if you are looking
for implementation details, see the `CHANGELOG.md` and the inline comments in
`pipeline.sh` and `run_brain_extraction_worker.sh`.

## Background

Brain extraction (skull stripping) is the first major processing step in the
pipeline. Its output — a T1 with non-brain tissue zeroed out and a binary
brain mask — feeds every downstream step, so failures here propagate.

The pipeline now supports three sources of brain masks:

1. **ANTs `antsBrainExtraction.sh`** with age-appropriate NKI templates
   (pediatric vs. adult, switched at age 16 by default). This has been the
   default since the original release.
2. **SynthStrip 1.6** (Hoopes et al., *NeuroImage* 2022), a deep-learning
   skull stripper that is robust to pathology including focal lesions,
   atrophy, and unusual contrasts. Age-agnostic — one model for all ages.
3. **User-supplied masks**, in cases where neither automatic method succeeds
   and the site has manually edited a mask in ITK-SNAP or similar.

You select the method per run with `--extract-method ants` (default) or
`--extract-method synthstrip`. User-supplied masks are activated by pointing
`--extract-mask DIR` at a directory of pre-prepared masks.

## When to use which method

The recommended default is ANTs. It produces tight, anatomically motivated
brain boundaries and uses age-appropriate priors, which matters for
pediatric cohorts where adult priors would under-mask the cerebellum and
inferior temporal regions.

Switch to SynthStrip when:

- A site reports a high ANTs failure rate (more than roughly 5–10% of
  subjects flagged at QC).
- Scans come from a scanner or sequence ANTs has trouble with, such as
  certain Siemens CIMA.X/XA61 acquisitions where intensity distributions
  differ enough from the NKI template space to throw off the affine init.
- Subjects have large, signal-distorting lesions (encephalomalacia, chronic
  hemorrhage, large infarcts) that pull the ANTs template-driven mask off
  the brain edge.
- Pediatric subjects have unusual head sizes or shapes that the age-binned
  template (10-and-under vs. adult NKI) doesn't represent well.

Use user-supplied masks for the small residual set of subjects where both
methods fail. This is meant to be an escape hatch for 1–5 subjects per site,
not a workflow for whole batches.

## ANTs template selection: pediatric vs. adult

When using `--extract-method ants`, the pipeline automatically picks
between two age-binned templates based on each subject's age in
`demographics.csv`:

- **Adult template** (NKI): used for subjects with age > `--age-cutoff`
  (default 16).
- **Pediatric template** (NKI10AndUnder): used for subjects with age ≤
  `--age-cutoff`.

The split exists because adult brain priors under-mask pediatric brains,
particularly the cerebellum and inferior temporal regions, while pediatric
priors over-mask adult brains and pick up too much dura.

In most cases the default `--age-cutoff 16` is correct and you should not
change it. Two situations call for adjustment:

- **Adolescent cohorts with reported under-masking on adult-template
  subjects.** Try lowering the cutoff (e.g. `--age-cutoff 14`) so more
  subjects route through the pediatric template. Review QC carefully —
  this can over-correct.
- **Pediatric cohorts with reported over-masking on pediatric-template
  subjects.** Try raising the cutoff (e.g. `--age-cutoff 18`) so more
  subjects route through the adult template, or skip the ANTs template
  dance entirely and switch the whole cohort to SynthStrip, which is
  age-agnostic.

**Do not manipulate the age column in `demographics.csv` to force a
template change.** This was a known workaround in earlier versions but it
corrupts the audit trail and any downstream analyses that depend on age.
If you need a different template selection, use `--age-cutoff` or
`--extract-method synthstrip`.

For mixed pediatric/adult cohorts where neither ANTs template fits well
across the whole range, SynthStrip is usually the right call because it
uses one model for all ages and avoids the discontinuity at the cutoff.

## Recommended site workflow

The intended pattern is "try ANTs everywhere, then rescue the failures with
SynthStrip, then hand-fix anything that's still broken." Here is what that
looks like end-to-end.

### 1. Run brain extraction with the default method

```bash
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates \
    enigma-lesion.sif \
    --step extract --scheduler slurm \
    --demographics /data/demographics.csv \
    /data /templates
```

This runs ANTs on every subject. As each subject completes, a marker file is
written to `/data/T1s_brain/.markers/<subject>_brain_extraction_ants.done`
recording the method, age, host, timestamp, and exit code. Subjects that
fail leave a `.failed` breadcrumb in the same directory.

### 2. Generate QC images and review

```bash
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates \
    enigma-lesion.sif \
    --step qc-extract --port 8890 \
    /data /templates
```

For any subject whose mask is unacceptable, delete the corresponding success
marker:

```bash
rm /data/T1s_brain/.markers/<subject>_brain_extraction_ants.done
```

This is the trigger for the rerun-failed logic. A subject without any
`.done` marker for any method will be picked up next time you run
`--step extract --rerun-failed`.

You can also delete the mask and brain NIfTI files if you want a clean
output directory, but it is not strictly required — the new run will
overwrite them.

### 3. Rerun failed subjects with SynthStrip

```bash
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates \
    enigma-lesion.sif \
    --step extract --extract-method synthstrip --rerun-failed \
    --scheduler slurm \
    /data /templates
```

The `--rerun-failed` flag scans the markers directory and builds a filtered
subject list containing only subjects without a success marker for any
method. SynthStrip then processes those subjects, writing
`<subject>_brain_extraction_synthstrip.done` markers for the ones it
rescues.

To preview what would be reprocessed without actually submitting jobs:

```bash
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates \
    enigma-lesion.sif \
    --step extract --extract-method synthstrip --list-failed \
    /data /templates
```

### 4. QC the rescued subjects and, if needed, supply manual masks

Re-run the QC step. For any subject still failing after both methods, edit
a mask by hand (ITK-SNAP, FSLeyes, or whatever your site uses), save it as
`<subject>_BrainMask.nii.gz` in some directory, then:

```bash
apptainer run --bind /path/to/data:/data,/path/to/templates:/templates,/path/to/manual_masks:/manual \
    enigma-lesion.sif \
    --step extract --extract-mask /manual --rerun-failed \
    --scheduler slurm \
    /data /templates
```

When `--extract-mask DIR` is set and a subject has a matching file in that
directory, the manual mask takes priority and is applied directly to the
T1. A `usermask` marker is written so the audit trail records that a
manual mask was used.

### 5. Continue with the rest of the pipeline

Once every subject has *some* success marker, proceed to the rest of the
pipeline as usual:

```bash
apptainer run ... --step check-dims  /data /templates
apptainer run ... --step register    --scheduler slurm /data /templates
apptainer run ... --step qc-register --port 8890 /data /templates
```

Downstream steps look only for `<subject>_BrainExtractionBrain.nii.gz` and
`<subject>_BrainExtractionMask.nii.gz` files, which have the same naming
regardless of which method produced them. Nothing else in the pipeline
needs to know about the method choice.

## Rerunning individual failed subjects

Brain extraction uses the marker system described above (delete a marker,
rerun with `--rerun-failed`). Other pipeline steps don't yet have marker
files, but you can still rerun a single subject or a small subset by
preparing a custom subjects list and using `--subjects`.

### Rerunning specific subjects at any step

Create a small text file containing only the subjects you want to
reprocess (one subject ID per line, plus age if the step needs it):

```bash
# Example: only rerun sub-003 and sub-017
cat > /data/just_two.txt << 'EOF'
sub-003	14
sub-017	19
EOF
```

Then point any step at that file with `--subjects`:

```bash
# Brain extraction for just those two
apptainer run ... --step extract --subjects /data/just_two.txt \
    --scheduler local /data /templates

# Registration for just those two (age column is ignored at this step)
apptainer run ... --step register --subjects /data/just_two.txt \
    --scheduler local /data /templates
```

`--scheduler local` is recommended for small reruns since the SLURM array
overhead is wasted on 1–2 subjects.

### Forcing a full reprocess of a single subject

For brain extraction, the cleanest reset is to delete that subject's
marker and any output files, then `--rerun-failed`:

```bash
sub=sub-003
rm -f /data/T1s_brain/.markers/${sub}_brain_extraction_*.done
rm -f /data/T1s_brain/.markers/${sub}_brain_extraction_*.failed
rm -f /data/T1s_brain/${sub}_BrainExtraction*.nii.gz

apptainer run ... --step extract --extract-method synthstrip \
    --rerun-failed --scheduler local /data /templates
```

For registration, the worker's "skip if warped T1 already exists" check
gates reprocessing. Delete the subject's registration output directory
then re-run with `--subjects`:

```bash
sub=sub-003
rm -rf /data/registration_output/${sub}
apptainer run ... --step register --subjects /data/just_one.txt \
    --scheduler local /data /templates
```

### Forcing reprocess of the whole batch

`--force` clears existing markers (for `extract`) or QC PNG files (for
`qc-extract`, `qc-register`) before running. Use with care — there is no
confirmation prompt and prior QC ratings stay in the QC database
regardless.

```bash
apptainer run ... --step extract --force --scheduler slurm /data /templates
```

## Reading the audit trail

After a batch finishes, you can summarize what happened with a one-liner:

```bash
grep "^method=" /data/T1s_brain/.markers/*.done | \
    sed 's/.*method=//' | sort | uniq -c
```

Example output for a site that needed the fallback:

```
     94 ants
     12 synthstrip
      2 usermask
```

This is the kind of number worth recording in a site processing log and
including in the methods section of any publication that uses the dataset.

## Resource usage

Both methods are designed to run inside the pipeline's default SLURM
allocation. Approximate per-subject figures for a typical 1mm T1:

| Method     | Wall time         | Peak RAM  | GPU benefit         |
|------------|-------------------|-----------|---------------------|
| ANTs       | 10–30 min         | 6–10 GB   | none                |
| SynthStrip | 1–2 min CPU       | 4–6 GB    | ~10 s/subject       |
| usermask   | a few seconds     | negligible | n/a                |

The default `--mem 16G` is plenty for either method. Sites running large
SynthStrip-only batches can safely drop to `--mem 8G`. SynthStrip will
automatically pick up a GPU if one is allocated to the job (via
`--gres=gpu:1` on the SLURM side), but CPU performance is acceptable for
production use.

## Troubleshooting

**The container won't build, error mentioning multi-stage:** the build
needs Apptainer ≥3.6 or Singularity ≥3.2. Check with `apptainer --version`.

**Apptainer can't pull `freesurfer/synthstrip:1.6`:** your build host may
not have outbound Docker Hub access. Pull the image on a workstation and
copy it over, or have your sysadmin configure a Docker Hub mirror.

**SynthStrip output looks like the entire head, not just the brain:** the
binary may have written outputs in the wrong order. Confirm by opening the
mask file directly — it should be binary 0/1, not the original intensities.
Open an issue with a screenshot.

**ANTs fails with "Inputs do not occupy the same physical space":** this
is usually a header mismatch between the T1 and the NKI template after
DICOM conversion. Re-export the T1 with `dcm2niix -b y` and try again.

**Manual mask is not picked up:** the file must be named exactly
`<subject>_BrainMask.nii.gz`, where `<subject>` matches the subject ID
used in the subjects file. Capitalization matters.
