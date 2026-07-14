#!/bin/bash
# =============================================================================
# ENIGMA Lesion Registration Pipeline
#
# Main entrypoint — dispatches to individual steps with scheduler support.
#
# Usage:
#   pipeline.sh [OPTIONS] DATA_DIR TEMPLATE_DIR
#
# Examples:
#   pipeline.sh --step all --scheduler local /data /templates
#   pipeline.sh --step extract --scheduler slurm /data /templates
#   pipeline.sh --step qc-extract --port 8890 /data /templates
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# =============================================================================
# Defaults
# =============================================================================

STEP="all"
SCHEDULER="local"
DEMOGRAPHICS=""
ID_COL=""
AGE_COL=""
AGE_CUTOFF=16
REG_TEMPLATE=""
REG_METHOD="simple"          # simple | label-based
QC_PORT=8890
SUBJECT_LIST=""
NSUBJECTS=""
FORCE=false
SLURM_PARTITION=""
SLURM_ACCOUNT=""
JOB_MEM="16G"
JOB_TIME="8:00:00"
JOB_CPUS=2

# Brain-extraction method options
EXTRACT_METHOD="ants"        # ants | synthstrip
EXTRACT_MASK_DIR=""          # directory of user-supplied <sub>_BrainMask.nii.gz
RERUN_FAILED=false           # process only subjects without a success marker
LIST_FAILED=false            # print rerun list and exit, don't submit

# =============================================================================
# Usage
# =============================================================================

usage() {
    cat << 'EOF'
ENIGMA Lesion Registration Pipeline

Usage: pipeline.sh [OPTIONS] DATA_DIR TEMPLATE_DIR

DATA_DIR       Base directory containing T1s/, LesionMasks/, demographics
TEMPLATE_DIR   Directory containing NKI/, NKI10AndUnder/ brain priors

Steps (--step):
  all            Run full pipeline (stops at QC checkpoints)
  prep           Prepare subject list from demographics file
  extract        Brain extraction (ANTs)
  qc-extract     QC brain extraction (web server)
  check-dims     Verify T1/lesion dimension match
  register       Lesion-masked registration to template
  qc-register    QC registration results (web server)

Scheduler (--scheduler):
  local          Run serially on current machine (default)
  slurm          Submit as SLURM array job
  sge            Submit as SGE array job
  pbs            Submit as PBS/Torque array job

Options:
  --demographics FILE   CSV/Excel with subject IDs and ages
  --id-col NAME         Column name for subject ID (auto-detected)
  --age-col NAME        Column name for age (auto-detected)
  --age-cutoff N        Template age cutoff (default: 16)
  --extract-method M    Brain extraction: ants (default) or synthstrip
  --extract-mask DIR    Directory of pre-supplied brain masks
                        (files named <subject>_BrainMask.nii.gz)
  --rerun-failed        Only process subjects without a success marker
  --list-failed         Print subjects that --rerun-failed would process, then exit
  --template FILE       Registration target template
  --reg-method METHOD   Registration method: simple (default) or label-based
  --port N              QC server port (default: 8890)
  --subjects FILE       Pre-built subject list (skip prep step)
  --nsubjects N         Process only first N subjects
  --force               Overwrite existing outputs
  --partition NAME      SLURM partition
  --account NAME        SLURM account
  --mem SIZE            Job memory (default: 16G)
  --time TIME           Job walltime (default: 8:00:00)
  --cpus N              CPUs per task (default: 2)
  -h, --help            Show this help
EOF
    exit 0
}

# =============================================================================
# Logging
# =============================================================================

log_info()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] INFO:  $1"; }
log_success() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] OK:    $1"; }
log_warn()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN:  $1"; }
log_error()   { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2; }
log_step()    { echo ""; echo "==== $1 ===="; echo ""; }

# =============================================================================
# Parse arguments
# =============================================================================

POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --step)             STEP="$2"; shift 2 ;;
        --scheduler)        SCHEDULER="$2"; shift 2 ;;
        --demographics)     DEMOGRAPHICS="$2"; shift 2 ;;
        --id-col)           ID_COL="$2"; shift 2 ;;
        --age-col)          AGE_COL="$2"; shift 2 ;;
        --age-cutoff)       AGE_CUTOFF="$2"; shift 2 ;;
        --extract-method)   EXTRACT_METHOD="$2"; shift 2 ;;
        --extract-mask)     EXTRACT_MASK_DIR="$2"; shift 2 ;;
        --rerun-failed)     RERUN_FAILED=true; shift ;;
        --list-failed)      LIST_FAILED=true; RERUN_FAILED=true; shift ;;
        --template)         REG_TEMPLATE="$2"; shift 2 ;;
        --reg-method)       REG_METHOD="$2"; shift 2 ;;
        --port)             QC_PORT="$2"; shift 2 ;;
        --subjects)         SUBJECT_LIST="$2"; shift 2 ;;
        --nsubjects)        NSUBJECTS="$2"; shift 2 ;;
        --force)            FORCE=true; shift ;;
        --partition)        SLURM_PARTITION="$2"; shift 2 ;;
        --account)          SLURM_ACCOUNT="$2"; shift 2 ;;
        --mem)              JOB_MEM="$2"; shift 2 ;;
        --time)             JOB_TIME="$2"; shift 2 ;;
        --cpus)             JOB_CPUS="$2"; shift 2 ;;
        -h|--help)          usage ;;
        -*)                 log_error "Unknown option: $1"; usage ;;
        *)                  POSITIONAL+=("$1"); shift ;;
    esac
done

# Validate --extract-method
case "$EXTRACT_METHOD" in
    ants|synthstrip) ;;
    *) log_error "Invalid --extract-method: $EXTRACT_METHOD (must be 'ants' or 'synthstrip')"; exit 1 ;;
esac

if [[ ${#POSITIONAL[@]} -lt 2 && "$STEP" != "help" ]]; then
    log_error "DATA_DIR and TEMPLATE_DIR are required"
    usage
fi

DATA_DIR="${POSITIONAL[0]}"
TEMPLATE_DIR="${POSITIONAL[1]}"

# =============================================================================
# Validate environment
# =============================================================================

validate_dirs() {
    if [[ ! -d "$DATA_DIR" ]]; then
        log_error "DATA_DIR not found: $DATA_DIR"
        exit 1
    fi

    if [[ ! -d "$TEMPLATE_DIR" ]]; then
        log_error "TEMPLATE_DIR not found: $TEMPLATE_DIR"
        exit 1
    fi
}

# Directory layout
T1_DIR="${DATA_DIR}/T1s"
LESION_DIR="${DATA_DIR}/LesionMasks"
T1_BRAIN_DIR="${DATA_DIR}/T1s_brain"
REG_DIR="${DATA_DIR}/registration_output"
QC_DIR="${DATA_DIR}/QC"
LOG_DIR="${DATA_DIR}/logs"
SUBJECT_FILE="${DATA_DIR}/subjects_with_ages.txt"

# Template paths
TEMPLATE_YOUNG_DIR="${TEMPLATE_DIR}/NKI10AndUnder"
TEMPLATE_YOUNG="${TEMPLATE_YOUNG_DIR}/T_template0.nii.gz"
PROB_MASK_YOUNG="${TEMPLATE_YOUNG_DIR}/T_template0_BrainCerebellumProbabilityMask.nii.gz"

TEMPLATE_OLDER_DIR="${TEMPLATE_DIR}/NKI"
TEMPLATE_OLDER="${TEMPLATE_OLDER_DIR}/T_template.nii.gz"
PROB_MASK_OLDER="${TEMPLATE_OLDER_DIR}/T_template_BrainCerebellumProbabilityMask.nii.gz"

# Use provided subject list if given
[[ -n "$SUBJECT_LIST" ]] && SUBJECT_FILE="$SUBJECT_LIST"

# =============================================================================
# Scheduler abstraction
# =============================================================================

# Submit an array job. Arguments:
#   $1 = job name
#   $2 = number of tasks
#   $3 = script to run
#   $4... = extra args passed to script
submit_array_job() {
    local job_name="$1"
    local ntasks="$2"
    local script="$3"
    shift 3
    local extra_args="$*"

    local task_range="1-${ntasks}"
    [[ -n "$NSUBJECTS" && "$NSUBJECTS" -lt "$ntasks" ]] && task_range="1-${NSUBJECTS}"

    mkdir -p "$LOG_DIR"

    case "$SCHEDULER" in

        local)
            log_info "Running ${ntasks} tasks locally (serial)..."
            local end=${ntasks}
            [[ -n "$NSUBJECTS" && "$NSUBJECTS" -lt "$ntasks" ]] && end=$NSUBJECTS
            for (( i=1; i<=end; i++ )); do
                log_info "Task $i / $end"
                TASK_ID=$i bash "$script" $extra_args 2>&1 | tee "${LOG_DIR}/${job_name}_${i}.log"
            done
            ;;

        slurm)
            local sbatch_args=(
                --job-name="$job_name"
                --array="$task_range"
                --output="${LOG_DIR}/${job_name}_%A_%a.out"
                --error="${LOG_DIR}/${job_name}_%A_%a.err"
                --mem="$JOB_MEM"
                --time="$JOB_TIME"
                --cpus-per-task="$JOB_CPUS"
            )
            [[ -n "$SLURM_PARTITION" ]] && sbatch_args+=(--partition="$SLURM_PARTITION")
            [[ -n "$SLURM_ACCOUNT" ]] && sbatch_args+=(--account="$SLURM_ACCOUNT")

            log_info "Submitting SLURM array job: ${task_range}"
            sbatch "${sbatch_args[@]}" "$script" $extra_args
            log_info "Monitor with: squeue -u \$USER"
            ;;

        sge)
            log_info "Submitting SGE array job: ${task_range}"
            qsub -N "$job_name" \
                 -t "$task_range" \
                 -o "${LOG_DIR}" \
                 -j y \
                 -l h_vmem="$JOB_MEM" \
                 "$script" $extra_args
            log_info "Monitor with: qstat"
            ;;

        pbs)
            log_info "Submitting PBS/Torque array job: ${task_range}"
            qsub -N "$job_name" \
                 -J "$task_range" \
                 -o "${LOG_DIR}" \
                 -e "${LOG_DIR}" \
                 -l "mem=${JOB_MEM},walltime=${JOB_TIME},ncpus=${JOB_CPUS}" \
                 -v "EXTRA_ARGS=${extra_args}" \
                 "$script"
            log_info "Monitor with: qstat"
            ;;

        *)
            log_error "Unknown scheduler: $SCHEDULER"
            exit 1
            ;;
    esac
}

# Get the task ID from whatever scheduler is running
get_task_id() {
    # Local mode or explicit TASK_ID
    if [[ -n "${TASK_ID:-}" ]]; then
        echo "$TASK_ID"
    # SLURM
    elif [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
        echo "$SLURM_ARRAY_TASK_ID"
    # SGE
    elif [[ -n "${SGE_TASK_ID:-}" ]]; then
        echo "$SGE_TASK_ID"
    # PBS/Torque
    elif [[ -n "${PBS_ARRAY_INDEX:-}" ]]; then
        echo "$PBS_ARRAY_INDEX"
    else
        echo ""
    fi
}

# =============================================================================
# Step 1: Prep subjects
# =============================================================================

step_prep() {
    log_step "Step 1: Prepare subjects"

    if [[ -n "$SUBJECT_LIST" && -f "$SUBJECT_LIST" ]]; then
        log_info "Using provided subject list: $SUBJECT_LIST"
        local count
        count=$(wc -l < "$SUBJECT_LIST")
        log_info "Found $count subjects"
        return 0
    fi

    if [[ -z "$DEMOGRAPHICS" ]]; then
        # No demographics file — build list from T1 directory with no ages
        log_warn "No demographics file provided. Building subject list without ages."
        log_warn "All subjects will use the adult (NKI) template for brain extraction."

        local count=0
        > "$SUBJECT_FILE"
        for f in "${T1_DIR}"/*_T1.nii.gz; do
            [[ -f "$f" ]] || continue
            local sub
            sub=$(basename "$f" "_T1.nii.gz")
            echo -e "${sub}\t99" >> "$SUBJECT_FILE"    # default age=99 → adult template
            count=$((count + 1))
        done
        log_info "Found $count T1 files, subject list: $SUBJECT_FILE"
        return 0
    fi

    # Build subject list from demographics
    local prep_args=("$DEMOGRAPHICS" --t1-dir "$T1_DIR" --output "$SUBJECT_FILE")
    [[ -n "$ID_COL" ]] && prep_args+=(--id-col "$ID_COL")
    [[ -n "$AGE_COL" ]] && prep_args+=(--age-col "$AGE_COL")

    python3 "${SCRIPT_DIR}/prep_subjects.py" "${prep_args[@]}"
}

# =============================================================================
# Step 2: Brain extraction
# =============================================================================

step_extract() {
    log_step "Step 2: Brain extraction"

    if [[ ! -f "$SUBJECT_FILE" ]]; then
        log_error "Subject list not found: $SUBJECT_FILE"
        log_error "Run --step prep first"
        exit 1
    fi

    mkdir -p "$T1_BRAIN_DIR"
    mkdir -p "${T1_BRAIN_DIR}/.markers"

    # If --rerun-failed (or --list-failed), filter to subjects without a
    # success marker in .markers/. The marker naming convention is set by
    # run_brain_extraction_worker.sh:
    #     <T1_BRAIN_DIR>/.markers/<subject>_brain_extraction_<method>.done
    # A subject with any method's .done file is treated as complete.
    local effective_subject_file="$SUBJECT_FILE"

    if [[ "$RERUN_FAILED" == "true" ]]; then
        local filtered_list
        filtered_list="${DATA_DIR}/.subjects_to_rerun.txt"
        > "$filtered_list"
        local total=0
        local remaining=0
        while IFS=$'\t' read -r sub age; do
            [[ -z "$sub" ]] && continue
            total=$((total + 1))
            # If any *.done marker exists for this subject, skip
            if compgen -G "${T1_BRAIN_DIR}/.markers/${sub}_brain_extraction_*.done" > /dev/null; then
                continue
            fi
            echo -e "${sub}\t${age}" >> "$filtered_list"
            remaining=$((remaining + 1))
        done < "$SUBJECT_FILE"

        log_info "Rerun filter: ${remaining}/${total} subjects have no success marker"

        if [[ "$LIST_FAILED" == "true" ]]; then
            echo ""
            echo "Subjects that would be reprocessed:"
            cat "$filtered_list"
            echo ""
            log_info "Exiting (--list-failed). Remove marker(s) under ${T1_BRAIN_DIR}/.markers/"
            log_info "to force reprocessing of specific subjects."
            exit 0
        fi

        if [[ $remaining -eq 0 ]]; then
            log_info "Nothing to do — all subjects have success markers."
            return 0
        fi

        effective_subject_file="$filtered_list"
    fi

    local ntasks
    ntasks=$(wc -l < "$effective_subject_file")

    # Export extraction-method options to the worker via env vars so the
    # positional argument list stays backward-compatible with older workers.
    # The May 20+ worker reads these; older workers safely ignore them.
    export EXTRACT_METHOD
    export EXTRACT_MASK_DIR
    export FORCE

    log_info "Brain extraction method: $EXTRACT_METHOD"
    [[ -n "$EXTRACT_MASK_DIR" ]] && log_info "User mask directory: $EXTRACT_MASK_DIR"

    submit_array_job "brain_extract" "$ntasks" \
        "${SCRIPT_DIR}/run_brain_extraction_worker.sh" \
        "$effective_subject_file" "$T1_DIR" "$T1_BRAIN_DIR" \
        "$TEMPLATE_YOUNG" "$PROB_MASK_YOUNG" \
        "$TEMPLATE_OLDER" "$PROB_MASK_OLDER" \
        "$AGE_CUTOFF"
}

# =============================================================================
# Step 3: QC brain extraction
# =============================================================================

step_qc_extract() {
    log_step "Step 3: QC — Brain extraction"

    mkdir -p "${QC_DIR}/brain_extraction"

    # Generate QC images (Python; no host FSL required).
    #
    # Historical note: this step previously used FSL utilities (overlay,
    # slicer, pngappend) invoked through the host system's FSL install.
    # That was fragile because FSL 6.0.7+ ships its own bundled Python
    # interpreter, and various FSL command-line scripts (imcp, immv,
    # remove_ext) can't find that interpreter inside the container. The
    # Python replacement removes the host-FSL dependency entirely.
    log_info "Generating brain extraction QC images..."

    FORCE_FLAG=""
    [[ "$FORCE" == "true" ]] && FORCE_FLAG="--force"

    python3 "${SCRIPT_DIR}/enigma_qc_images.py" \
        --mode qc-extract \
        --t1-dir "$T1_DIR" \
        --t1-brain-dir "$T1_BRAIN_DIR" \
        --qc-dir "$QC_DIR" \
        $FORCE_FLAG

    # Launch QC server
    log_info "Launching QC server on port $QC_PORT..."
    log_info "SSH tunnel: ssh -L ${QC_PORT}:localhost:${QC_PORT} user@this-server"
    log_info "Then open: http://localhost:${QC_PORT}"
    echo ""

    python3 "${SCRIPT_DIR}/enigma_qc_server.py" \
        --port "$QC_PORT" \
        --qc-dir "$QC_DIR" \
        --mode brain_extraction
}

# =============================================================================
# Step 4: Check dimensions
# =============================================================================

step_check_dims() {
    log_step "Step 4: Dimension check (T1 brain vs lesion mask)"

    bash "${SCRIPT_DIR}/check_dimensions.sh" \
        --t1-dir "$T1_BRAIN_DIR" \
        --lesion-dir "$LESION_DIR" \
        --output-dir "$DATA_DIR"
}

# =============================================================================
# Step 5: Registration
# =============================================================================

step_register() {
    log_step "Step 5: Lesion-masked registration"

    # Build subject list from QC-passed brain extractions
    local reg_subjects="${DATA_DIR}/subjects_to_register.txt"
    local count=0

    > "$reg_subjects"
    for f in "${T1_BRAIN_DIR}"/*_BrainExtractionBrain.nii.gz; do
        [[ -f "$f" ]] || continue
        local sub
        sub=$(basename "$f" "_BrainExtractionBrain.nii.gz")

        # Check lesion mask exists
        if [[ -f "${LESION_DIR}/${sub}_Lesion.nii.gz" ]]; then
            echo "$sub" >> "$reg_subjects"
            count=$((count + 1))
        else
            log_warn "No lesion mask for $sub, skipping"
        fi
    done

    log_info "Found $count subjects for registration"
    mkdir -p "$REG_DIR"

    # Select registration script
    local reg_script="register_simple.py"
    [[ "$REG_METHOD" == "label-based" ]] && reg_script="register_with_lesion.py"

    submit_array_job "lesion_reg" "$count" \
        "${SCRIPT_DIR}/run_registration_worker.sh" \
        "$reg_subjects" "$T1_BRAIN_DIR" "$LESION_DIR" "$REG_DIR" \
        "${SCRIPT_DIR}/${reg_script}" "${REG_TEMPLATE}"
}

# =============================================================================
# Step 6: QC registration
# =============================================================================

step_qc_register() {
    log_step "Step 6: QC — Registration"

    # Generate QC images (Python; no host FSL required).
    #
    # See step_qc_extract for the history of why this step no longer uses
    # host FSL. Same Python script, different --mode.
    log_info "Generating registration QC images..."

    FORCE_FLAG=""
    [[ "$FORCE" == "true" ]] && FORCE_FLAG="--force"

    python3 "${SCRIPT_DIR}/enigma_qc_images.py" \
        --mode qc-register \
        --t1-brain-dir "$T1_BRAIN_DIR" \
        --lesion-dir "$LESION_DIR" \
        --reg-dir "$REG_DIR" \
        --qc-dir "$QC_DIR" \
        $FORCE_FLAG

    # Launch QC server
    log_info "Launching QC server on port $QC_PORT..."
    log_info "SSH tunnel: ssh -L ${QC_PORT}:localhost:${QC_PORT} user@this-server"
    log_info "Then open: http://localhost:${QC_PORT}"
    echo ""

    python3 "${SCRIPT_DIR}/enigma_qc_server.py" \
        --port "$QC_PORT" \
        --qc-dir "$QC_DIR" \
        --mode registration
}

# =============================================================================
# Main dispatch
# =============================================================================

validate_dirs

log_info "ENIGMA Lesion Registration Pipeline"
log_info "Data directory:     $DATA_DIR"
log_info "Template directory: $TEMPLATE_DIR"
log_info "Step:               $STEP"
log_info "Scheduler:          $SCHEDULER"
echo ""

case "$STEP" in

    all)
        step_prep
        step_extract

        echo ""
        echo "============================================================"
        echo "  CHECKPOINT: Brain extraction complete."
        echo ""
        echo "  You MUST review brain extractions before continuing."
        echo "  Run:"
        echo "    pipeline.sh --step qc-extract --port $QC_PORT $DATA_DIR $TEMPLATE_DIR"
        echo ""
        echo "  After QC, continue with:"
        echo "    pipeline.sh --step check-dims $DATA_DIR $TEMPLATE_DIR"
        echo "    pipeline.sh --step register --scheduler $SCHEDULER $DATA_DIR $TEMPLATE_DIR"
        echo "    pipeline.sh --step qc-register --port $QC_PORT $DATA_DIR $TEMPLATE_DIR"
        echo "============================================================"
        ;;

    prep)        step_prep ;;
    extract)     step_extract ;;
    qc-extract)  step_qc_extract ;;
    check-dims)  step_check_dims ;;
    register)    step_register ;;
    qc-register) step_qc_register ;;

    *)
        log_error "Unknown step: $STEP"
        log_error "Valid steps: all, prep, extract, qc-extract, check-dims, register, qc-register"
        exit 1
        ;;
esac
