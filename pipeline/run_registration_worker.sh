#!/bin/bash
# =============================================================================
# Registration worker — called by pipeline.sh via scheduler
#
# Arguments (positional, set by pipeline.sh):
#   $1  SUBJECT_FILE     One subject ID per line
#   $2  T1_BRAIN_DIR     Brain-extracted T1 directory
#   $3  LESION_DIR       Lesion mask directory
#   $4  REG_DIR          Output registration directory
#   $5  REG_SCRIPT       Python registration script path
#   $6  TEMPLATE         Registration target template (optional, can be "")
# =============================================================================

set -uo pipefail

SUBJECT_FILE="$1"
T1_BRAIN_DIR="$2"
LESION_DIR="$3"
REG_DIR="$4"
REG_SCRIPT="$5"
TEMPLATE="${6:-}"

# Resolve task ID
TASK=${TASK_ID:-${SLURM_ARRAY_TASK_ID:-${SGE_TASK_ID:-${PBS_ARRAY_INDEX:-}}}}

if [[ -z "$TASK" ]]; then
    echo "ERROR: No task ID found. Run via pipeline.sh or a scheduler."
    exit 1
fi

SUBJECT=$(sed -n "${TASK}p" "$SUBJECT_FILE")
if [[ -z "$SUBJECT" ]]; then
    echo "ERROR: No subject at line $TASK in $SUBJECT_FILE"
    exit 1
fi

echo "=============================================="
echo "Lesion-Masked Registration"
echo "Subject:  $SUBJECT"
echo "Task ID:  $TASK"
echo "Script:   $(basename "$REG_SCRIPT")"
echo "Host:     $(hostname)"
echo "Date:     $(date)"
echo "=============================================="

T1_FILE="${T1_BRAIN_DIR}/${SUBJECT}_BrainExtractionBrain.nii.gz"
LESION_FILE="${LESION_DIR}/${SUBJECT}_Lesion.nii.gz"
SUB_OUTPUT="${REG_DIR}/${SUBJECT}"

echo "T1:       $T1_FILE"
echo "Lesion:   $LESION_FILE"
echo "Output:   $SUB_OUTPUT"
echo ""

# Validate
if [[ ! -f "$T1_FILE" ]]; then
    echo "ERROR: T1 not found: $T1_FILE"
    exit 1
fi

if [[ ! -f "$LESION_FILE" ]]; then
    echo "ERROR: Lesion not found: $LESION_FILE"
    exit 1
fi

# Skip if warped T1 already exists
if [[ -f "${SUB_OUTPUT}/${SUBJECT}_BrainExtractionBrain_T1_warped.nii.gz" || \
      -f "${SUB_OUTPUT}/${SUBJECT}_T1_warped.nii.gz" ]]; then
    echo "Output already exists, skipping."
    exit 0
fi

mkdir -p "$SUB_OUTPUT"

# Build command
CMD="python3 $REG_SCRIPT $T1_FILE $LESION_FILE $SUB_OUTPUT"
[[ -n "$TEMPLATE" ]] && CMD="$CMD --template $TEMPLATE"

echo "Running: $CMD"
echo ""

$CMD
EXIT_CODE=$?

echo ""
echo "Finished: $(date)"
echo "Exit code: $EXIT_CODE"
exit $EXIT_CODE
