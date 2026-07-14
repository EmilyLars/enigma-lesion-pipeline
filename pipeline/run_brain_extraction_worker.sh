#!/bin/bash
# =============================================================================
# Brain extraction worker — called by pipeline.sh via scheduler
#
# Arguments (positional, set by pipeline.sh):
#   $1  SUBJECT_FILE        Tab-separated: subject_id  age
#   $2  T1_DIR              Input T1 directory
#   $3  OUTPUT_DIR          Output directory for brain-extracted images
#   $4  TEMPLATE_YOUNG      Pediatric template path     (ANTs only)
#   $5  PROB_MASK_YOUNG     Pediatric probability mask  (ANTs only)
#   $6  TEMPLATE_OLDER      Adult template path         (ANTs only)
#   $7  PROB_MASK_OLDER     Adult probability mask      (ANTs only)
#   $8  AGE_CUTOFF          Age threshold (default: 16)
#   $9  EXTRACT_METHOD      ants | synthstrip            (default: ants)
#   $10 EXTRACT_MASK_DIR    Directory of pre-supplied masks, or "" if none
#
# Task ID comes from the scheduler environment:
#   TASK_ID (local), SLURM_ARRAY_TASK_ID, SGE_TASK_ID, or PBS_ARRAY_INDEX
#
# Outputs (regardless of method, for downstream compatibility):
#   ${OUTPUT_PREFIX}BrainExtractionBrain.nii.gz
#   ${OUTPUT_PREFIX}BrainExtractionMask.nii.gz
#
# Success marker (for rerun-failed):
#   ${OUTPUT_DIR}/.markers/${SUBJECT}_brain_extraction_${METHOD}.done
# =============================================================================

set -uo pipefail

SUBJECT_FILE="$1"
T1_DIR="$2"
OUTPUT_DIR="$3"
TEMPLATE_YOUNG="$4"
PROB_MASK_YOUNG="$5"
TEMPLATE_OLDER="$6"
PROB_MASK_OLDER="$7"
AGE_CUTOFF="${8:-16}"
EXTRACT_METHOD="${9:-ants}"
EXTRACT_MASK_DIR="${10:-}"

# Resolve task ID from whichever scheduler is active
TASK=${TASK_ID:-${SLURM_ARRAY_TASK_ID:-${SGE_TASK_ID:-${PBS_ARRAY_INDEX:-}}}}

if [[ -z "$TASK" ]]; then
    echo "ERROR: No task ID found. Run via pipeline.sh or a scheduler."
    exit 1
fi

# Get subject and age for this task
LINE=$(sed -n "${TASK}p" "$SUBJECT_FILE")
if [[ -z "$LINE" ]]; then
    echo "ERROR: No subject at line $TASK in $SUBJECT_FILE"
    exit 1
fi

SUBJECT=$(echo "$LINE" | awk '{print $1}')
AGE=$(echo "$LINE" | awk '{print $2}')

# Validate method early
case "$EXTRACT_METHOD" in
    ants|synthstrip|usermask) ;;
    *)
        echo "ERROR: Unknown extraction method: $EXTRACT_METHOD"
        echo "       Valid options: ants, synthstrip, usermask"
        exit 1
        ;;
esac

# If a user-supplied mask directory is set, prefer that for this subject
# (allows mixing: most subjects via ANTs, problem cases via supplied mask)
USER_MASK=""
if [[ -n "$EXTRACT_MASK_DIR" && -f "${EXTRACT_MASK_DIR}/${SUBJECT}_BrainMask.nii.gz" ]]; then
    USER_MASK="${EXTRACT_MASK_DIR}/${SUBJECT}_BrainMask.nii.gz"
    EXTRACT_METHOD="usermask"
fi

echo "=============================================="
echo "Brain Extraction"
echo "Subject:  $SUBJECT"
echo "Age:      $AGE"
echo "Method:   $EXTRACT_METHOD"
echo "Task ID:  $TASK"
echo "Host:     $(hostname)"
echo "Date:     $(date)"
echo "=============================================="

# Build file paths
INPUT_FILE="${T1_DIR}/${SUBJECT}_T1.nii.gz"
OUTPUT_PREFIX="${OUTPUT_DIR}/${SUBJECT}_"
MARKER_DIR="${OUTPUT_DIR}/.markers"
MARKER_FILE="${MARKER_DIR}/${SUBJECT}_brain_extraction_${EXTRACT_METHOD}.done"

echo "Input:    $INPUT_FILE"
echo "Output:   ${OUTPUT_PREFIX}BrainExtractionBrain.nii.gz"
echo "Marker:   $MARKER_FILE"
echo ""

# Validate inputs
if [[ ! -f "$INPUT_FILE" ]]; then
    echo "ERROR: T1 not found: $INPUT_FILE"
    exit 1
fi

mkdir -p "$OUTPUT_DIR" "$MARKER_DIR"

# Skip if this method already succeeded
if [[ -f "$MARKER_FILE" ]]; then
    echo "Marker already exists, skipping: $MARKER_FILE"
    exit 0
fi

# =============================================================================
# Method dispatch
# =============================================================================

run_ants_extraction() {
    if [[ ! -f "$TEMPLATE_YOUNG" || ! -f "$PROB_MASK_YOUNG" ||
          ! -f "$TEMPLATE_OLDER" || ! -f "$PROB_MASK_OLDER" ]]; then
        echo "ERROR: ANTs templates/masks missing — required for --extract-method ants"
        return 1
    fi

    local template prob_mask
    if (( $(echo "$AGE <= $AGE_CUTOFF" | bc -l) )); then
        template="$TEMPLATE_YOUNG"
        prob_mask="$PROB_MASK_YOUNG"
        echo "Template: NKI10AndUnder (age <= $AGE_CUTOFF)"
    else
        template="$TEMPLATE_OLDER"
        prob_mask="$PROB_MASK_OLDER"
        echo "Template: NKI (age > $AGE_CUTOFF)"
    fi

    antsBrainExtraction.sh \
        -d 3 \
        -a "$INPUT_FILE" \
        -e "$template" \
        -m "$prob_mask" \
        -o "$OUTPUT_PREFIX"
}

run_synthstrip_extraction() {
    # SynthStrip is age-agnostic — same model for ped and adult
    echo "SynthStrip: lesion-tolerant deep-learning skull strip (age-agnostic)"

    local mask_out="${OUTPUT_PREFIX}BrainExtractionMask.nii.gz"
    local brain_out="${OUTPUT_PREFIX}BrainExtractionBrain.nii.gz"

    # Use the official mri_synthstrip binary extracted from
    # freesurfer/synthstrip:1.6 during container build. Identical to the
    # FreeSurfer-bundled command — see Hoopes et al. NeuroImage 2022.
    # If CUDA is available the binary picks it up automatically; otherwise CPU.
    local synthstrip_args=(
        -i "$INPUT_FILE"
        -o "$brain_out"
        -m "$mask_out"
    )
    if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]] && command -v nvidia-smi &>/dev/null; then
        synthstrip_args+=(-g)
        echo "SynthStrip: CUDA detected, using GPU"
    fi

    /opt/synthstrip/mri_synthstrip "${synthstrip_args[@]}"
}

run_usermask_extraction() {
    echo "User-supplied mask: $USER_MASK"

    local mask_out="${OUTPUT_PREFIX}BrainExtractionMask.nii.gz"
    local brain_out="${OUTPUT_PREFIX}BrainExtractionBrain.nii.gz"

    # Copy the mask into the standard location and apply it
    cp "$USER_MASK" "$mask_out"
    ImageMath 3 "$brain_out" m "$INPUT_FILE" "$mask_out"
}

# Run the selected method
case "$EXTRACT_METHOD" in
    ants)        run_ants_extraction ;;
    synthstrip)  run_synthstrip_extraction ;;
    usermask)    run_usermask_extraction ;;
esac
EXIT_CODE=$?

# =============================================================================
# Verify output and write marker
# =============================================================================

BRAIN_OUT="${OUTPUT_PREFIX}BrainExtractionBrain.nii.gz"
MASK_OUT="${OUTPUT_PREFIX}BrainExtractionMask.nii.gz"

echo ""
if [[ $EXIT_CODE -eq 0 && -f "$BRAIN_OUT" && -f "$MASK_OUT" ]]; then
    # Write success marker with provenance
    cat > "$MARKER_FILE" << EOF
subject=$SUBJECT
method=$EXTRACT_METHOD
age=$AGE
host=$(hostname)
date=$(date -Iseconds)
exit_code=$EXIT_CODE
brain=$BRAIN_OUT
mask=$MASK_OUT
EOF
    echo "SUCCESS: $BRAIN_OUT"
    echo "Wrote marker: $MARKER_FILE"
else
    echo "FAILED: Brain extraction did not produce expected outputs"
    echo "  Brain output present: $([[ -f $BRAIN_OUT ]] && echo yes || echo no)"
    echo "  Mask output present:  $([[ -f $MASK_OUT ]] && echo yes || echo no)"
    # Leave a .failed breadcrumb (not skipped on next run, but useful for triage)
    echo "subject=$SUBJECT method=$EXTRACT_METHOD exit=$EXIT_CODE date=$(date -Iseconds)" \
        > "${MARKER_DIR}/${SUBJECT}_brain_extraction_${EXTRACT_METHOD}.failed"
fi

echo "Finished: $(date)"
echo "Exit code: $EXIT_CODE"
exit $EXIT_CODE
