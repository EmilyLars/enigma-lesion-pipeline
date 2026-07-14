#!/bin/bash
# =============================================================================
# check_dimensions.sh
# Verify T1 brain images and lesion masks have matching dimensions
#
# Usage:
#   check_dimensions.sh --t1-dir /data/T1s_brain --lesion-dir /data/LesionMasks --output-dir /data
#   check_dimensions.sh --t1-dir ... --lesion-dir ... sub1 sub2 sub3
# =============================================================================

set -uo pipefail

T1_DIR=""
LESION_DIR=""
OUTPUT_DIR="."

# Parse named arguments first, collect positional subjects
SUBJECTS_ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --t1-dir)     T1_DIR="$2"; shift 2 ;;
        --lesion-dir) LESION_DIR="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        -l)
            if [[ -f "$2" ]]; then
                while IFS= read -r sub; do
                    [[ -n "$sub" ]] && SUBJECTS_ARGS+=("$sub")
                done < "$2"
            fi
            shift 2 ;;
        -*)           echo "Unknown option: $1"; exit 1 ;;
        *)            SUBJECTS_ARGS+=("$1"); shift ;;
    esac
done

if [[ -z "$T1_DIR" || -z "$LESION_DIR" ]]; then
    echo "Usage: $0 --t1-dir DIR --lesion-dir DIR [--output-dir DIR] [subjects...]"
    exit 1
fi

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
REPORT="${OUTPUT_DIR}/dimension_check_${TIMESTAMP}.csv"
MISMATCH_LOG="${OUTPUT_DIR}/dimension_mismatches_${TIMESTAMP}.txt"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

total=0; matched=0; mismatched=0; missing=0

get_dims() {
    local file=$1
    if [[ -f "$file" ]]; then
        local d1 d2 d3 p1 p2 p3
        d1=$(fslval "$file" dim1)
        d2=$(fslval "$file" dim2)
        d3=$(fslval "$file" dim3)
        p1=$(fslval "$file" pixdim1)
        p2=$(fslval "$file" pixdim2)
        p3=$(fslval "$file" pixdim3)
        echo "${d1}x${d2}x${d3},${p1}x${p2}x${p3}"
    else
        echo "MISSING,MISSING"
    fi
}

check_subject() {
    local sub=$1
    local t1_file="${T1_DIR}/${sub}_BrainExtractionBrain.nii.gz"
    local lesion_file="${LESION_DIR}/${sub}_Lesion.nii.gz"

    total=$((total + 1))

    local t1_exists="NO" lesion_exists="NO"
    [[ -f "$t1_file" ]] && t1_exists="YES"
    [[ -f "$lesion_file" ]] && lesion_exists="YES"

    if [[ "$t1_exists" == "NO" || "$lesion_exists" == "NO" ]]; then
        missing=$((missing + 1))
        echo -e "${YELLOW}[MISSING]${NC} ${sub}: T1=${t1_exists}, Lesion=${lesion_exists}"
        echo "${sub},MISSING,MISSING,MISSING,MISSING,T1=${t1_exists};Lesion=${lesion_exists}" >> "$REPORT"
        return
    fi

    local t1_info lesion_info t1_dims t1_vox lesion_dims lesion_vox
    t1_info=$(get_dims "$t1_file")
    lesion_info=$(get_dims "$lesion_file")
    t1_dims=$(echo "$t1_info" | cut -d',' -f1)
    t1_vox=$(echo "$t1_info" | cut -d',' -f2)
    lesion_dims=$(echo "$lesion_info" | cut -d',' -f1)
    lesion_vox=$(echo "$lesion_info" | cut -d',' -f2)

    if [[ "$t1_dims" == "$lesion_dims" && "$t1_vox" == "$lesion_vox" ]]; then
        matched=$((matched + 1))
        echo -e "${GREEN}[MATCH]${NC} ${sub}: ${t1_dims} @ ${t1_vox}mm"
        echo "${sub},${t1_dims},${t1_vox},${lesion_dims},${lesion_vox},MATCH" >> "$REPORT"
    else
        mismatched=$((mismatched + 1))
        echo -e "${RED}[MISMATCH]${NC} ${sub}:"
        echo "    T1:     ${t1_dims} @ ${t1_vox}mm"
        echo "    Lesion: ${lesion_dims} @ ${lesion_vox}mm"
        echo "${sub},${t1_dims},${t1_vox},${lesion_dims},${lesion_vox},MISMATCH" >> "$REPORT"

        {
            echo "----------------------------------------"
            echo "Subject: ${sub}"
            echo "T1:     ${t1_file}"
            echo "        Dims: ${t1_dims}, Voxel: ${t1_vox}mm"
            echo "Lesion: ${lesion_file}"
            echo "        Dims: ${lesion_dims}, Voxel: ${lesion_vox}mm"
            echo ""
            echo "# Fix command:"
            echo "antsApplyTransforms -d 3 -i ${lesion_file} -r ${t1_file} -o ${LESION_DIR}/${sub}_Lesion_T1space.nii.gz -n NearestNeighbor"
            echo ""
        } >> "$MISMATCH_LOG"
    fi
}

# Build subject list
subjects=()
if [[ ${#SUBJECTS_ARGS[@]} -gt 0 ]]; then
    subjects=("${SUBJECTS_ARGS[@]}")
else
    for f in "${T1_DIR}"/*_BrainExtractionBrain.nii.gz; do
        [[ -f "$f" ]] || continue
        subjects+=("$(basename "$f" "_BrainExtractionBrain.nii.gz")")
    done
fi

if [[ ${#subjects[@]} -eq 0 ]]; then
    echo "No subjects found."
    exit 1
fi

echo "========================================"
echo "Dimension Check: T1 vs Lesion Masks"
echo "========================================"
echo "T1 Dir:     ${T1_DIR}"
echo "Lesion Dir: ${LESION_DIR}"
echo "Subjects:   ${#subjects[@]}"
echo "========================================"
echo ""

echo "Subject,T1_Dims,T1_VoxelSize,Lesion_Dims,Lesion_VoxelSize,Status" > "$REPORT"
echo "Dimension Mismatches - $(date)" > "$MISMATCH_LOG"

for sub in "${subjects[@]}"; do
    check_subject "$sub"
done

echo ""
echo "========================================"
echo "SUMMARY"
echo "========================================"
echo -e "Total:      ${total}"
echo -e "Matched:    ${GREEN}${matched}${NC}"
echo -e "Mismatched: ${RED}${mismatched}${NC}"
echo -e "Missing:    ${YELLOW}${missing}${NC}"
echo ""
echo "Report: ${REPORT}"
[[ $mismatched -gt 0 ]] && echo -e "${RED}Mismatches: ${MISMATCH_LOG}${NC}"
