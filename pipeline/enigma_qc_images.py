#!/usr/bin/env python3
"""
enigma_qc_images.py — generate QC PNG mosaics for the ENIGMA lesion pipeline.

Replaces the previous shell-based enigma_qc_images.sh that shelled out to FSL
(fslreorient2std, overlay, slicer, pngappend, fslswapdim, imcp, fslstats,
fslmaths). That version was fragile because it depended on the host system's
FSL install being ABI-compatible with the container's Ubuntu 22.04 environment,
and on FSL utility scripts finding their bundled Python interpreter through
various host-specific paths. Beta tester failures traced almost entirely to
this dependency chain.

The Python version needs only nibabel, numpy, and matplotlib. All three are
either already in the container or trivially added. No host FSL is required
at any point in the QC step.

Output layout matches the previous shell version exactly, so existing QC
directories and the QC web server are backward compatible:

    <qc-dir>/brain_extraction/<subject>_brain_extract.png
    <qc-dir>/T1_brain/<subject>_t1_brain.png
    <qc-dir>/T1_lesion_overlay/<subject>_lesion_overlay.png
    <qc-dir>/T1_warped/<subject>_t1_warped.png
    <qc-dir>/logjacobian/<subject>_logjacobian.png

Each PNG is a 3×3 mosaic: three sagittal slices on the top row, three coronal
on the middle, three axial on the bottom. Slice positions default to the
mask/lesion centroid (or the brain centroid for images without a mask) with
offsets, which better guarantees the lesion actually appears somewhere in the
mosaic than the fixed 35/50/65 percentages used by the previous version.

Modes
-----
The script can be invoked in either of two orientations:

    --mode qc-extract      Brain extraction QC (Step 3). Generates
                           brain_extraction/ PNGs (T1 with brain mask overlay).

    --mode qc-register     Registration QC (Step 6). Generates four sets of
                           PNGs from the registration output directory.

Usage
-----
    # Step 3: brain extraction QC
    enigma_qc_images.py --mode qc-extract \\
        --t1-dir /data/T1s \\
        --t1-brain-dir /data/T1s_brain \\
        --qc-dir /data/QC

    # Step 6: registration QC
    enigma_qc_images.py --mode qc-register \\
        --t1-brain-dir /data/T1s_brain \\
        --lesion-dir /data/LesionMasks \\
        --reg-dir /data/registration_output \\
        --qc-dir /data/QC

Add --force to overwrite existing PNGs. Add --subject SUB to process just
one subject (useful for spot-checking).
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence, Tuple

import numpy as np

if TYPE_CHECKING:
    import nibabel

# nibabel and matplotlib are imported lazily inside the entry function so that
# `--help` still works even if one of them is unavailable, which makes
# diagnostic output easier for beta testers.


# =============================================================================
# Image loading and orientation
# =============================================================================

def load_reoriented(path: Path) -> Tuple[np.ndarray, "nibabel.Nifti1Image"]:
    """Load a NIfTI and reorient it to canonical RAS+ axes.

    Canonical RAS means:
      axis 0 -> Right (increasing index goes right)
      axis 1 -> Anterior (increasing index goes anterior)
      axis 2 -> Superior (increasing index goes up)

    This replaces the previous shell version's `fslreorient2std` call. Doing
    the reorientation via nibabel keeps everything header-aware and avoids
    the host-FSL dependency that was breaking Step 6 for beta testers.
    """
    import nibabel as nib
    img = nib.load(str(path))
    canonical = nib.as_closest_canonical(img)
    # nib.as_closest_canonical returns a proxy that lazily reorients — call
    # get_fdata once and pin the array so downstream slicing is fast.
    data = np.asarray(canonical.get_fdata(), dtype=np.float32)
    # Squeeze trailing singleton dims (some scanners write 4D T1s with one
    # volume). If the file is truly 4D, take the first volume for QC.
    while data.ndim > 3 and data.shape[-1] == 1:
        data = data[..., 0]
    if data.ndim == 4:
        data = data[..., 0]
    return data, canonical


# =============================================================================
# Intensity windowing
# =============================================================================

def robust_window(data: np.ndarray, mask: Optional[np.ndarray] = None,
                  low: float = 2.0, high: float = 98.0
                  ) -> Tuple[float, float]:
    """Return (vmin, vmax) for display, using robust percentiles.

    If a mask is supplied, percentiles are computed inside the mask so
    background zeros don't drag the range. Falls back to full-volume
    percentiles if the mask is empty.
    """
    if mask is not None and mask.sum() > 0:
        vals = data[mask > 0]
    else:
        vals = data[data > 0] if (data > 0).any() else data
    if vals.size == 0:
        return 0.0, 1.0
    vmin, vmax = np.percentile(vals, [low, high])
    if vmax <= vmin:
        vmax = vmin + 1e-6
    return float(vmin), float(vmax)


# =============================================================================
# Slice selection
# =============================================================================

def centroid_or_center(mask: Optional[np.ndarray], shape: Sequence[int]
                       ) -> Tuple[int, int, int]:
    """Pick a reference voxel: mask centroid if a mask is provided and non-empty,
    otherwise the image center.

    The previous shell version used fixed 35/50/65 percentage cuts, which
    missed lesions in the top or bottom of the brain entirely. Anchoring on
    the mask centroid guarantees the lesion is visible in at least the middle
    of the three slices per plane.
    """
    if mask is not None and mask.sum() > 0:
        indices = np.argwhere(mask > 0)
        cx, cy, cz = indices.mean(axis=0)
        return int(round(cx)), int(round(cy)), int(round(cz))
    return shape[0] // 2, shape[1] // 2, shape[2] // 2


def slice_positions(reference_idx: int, dim_size: int,
                    offsets: Sequence[float] = (-0.15, 0.0, 0.15)
                    ) -> Sequence[int]:
    """Return three slice indices along one axis: at the reference and offsets
    of ±15% of the axis length. Clamped to keep slices inside the volume.
    """
    positions = []
    for off in offsets:
        idx = int(round(reference_idx + off * dim_size))
        idx = max(1, min(dim_size - 2, idx))
        positions.append(idx)
    # Deduplicate but preserve order — small volumes can produce duplicates
    # after clamping, and the user shouldn't see the same slice three times.
    seen = set()
    unique = []
    for p in positions:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    # If deduplication reduced the count, pad by nearest indices to keep three
    while len(unique) < 3:
        candidates = [p for p in range(1, dim_size - 1) if p not in seen]
        if not candidates:
            break
        pick = min(candidates, key=lambda p: min(abs(p - u) for u in unique))
        unique.append(pick)
        seen.add(pick)
    return sorted(unique[:3])


# =============================================================================
# Rendering primitives
# =============================================================================

def _prep_slice(sl: np.ndarray) -> np.ndarray:
    """Rotate a slice so it displays with the conventional radiological
    orientation when passed to imshow(origin='lower'). Nibabel's canonical
    RAS gives us axes (R, A, S), so:
      sagittal (fix x, look along R): (A, S)  -> rotate 90° CCW
      coronal  (fix y, look along A): (R, S)  -> rotate 90° CCW
      axial    (fix z, look along S): (R, A)  -> rotate 90° CCW
    All three planes need the same rotation to display with superior up
    and right on the viewer's right side.
    """
    return np.rot90(sl)


def render_mosaic(background: np.ndarray,
                  overlay: Optional[np.ndarray],
                  out_path: Path,
                  overlay_alpha: float = 0.5,
                  overlay_cmap: str = "autumn",
                  bg_range: Optional[Tuple[float, float]] = None,
                  ) -> None:
    """Render a 3-row × 3-column mosaic PNG.

    Rows are sagittal (X), coronal (Y), axial (Z). Columns are three slice
    positions per plane, chosen around the overlay centroid (or image center
    if no overlay).

    Parameters
    ----------
    background : 3D array
        Structural image to display grayscale.
    overlay : 3D array or None
        Optional mask/lesion/Jacobian to overlay. Values > 0 are shown.
    out_path : Path
        Where to write the PNG.
    overlay_alpha : float
        Overlay transparency. 0.5 matches the previous FSL `overlay` call.
    overlay_cmap : str
        Matplotlib colormap for the overlay. "autumn" gives yellow-to-red
        on a dark background, which matches the FSL default.
    bg_range : (float, float) or None
        Background display range. If None, computed as 2nd-98th percentile.
    """
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend, no display server needed
    import matplotlib.pyplot as plt

    if bg_range is None:
        bg_range = robust_window(background)
    vmin, vmax = bg_range

    cx, cy, cz = centroid_or_center(overlay, background.shape)

    x_positions = slice_positions(cx, background.shape[0])
    y_positions = slice_positions(cy, background.shape[1])
    z_positions = slice_positions(cz, background.shape[2])

    fig, axes = plt.subplots(3, 3, figsize=(9, 9), facecolor="black")
    # Tight layout: no whitespace between panels
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0, wspace=0.02, hspace=0.02)

    def draw(ax, bg_slice, ov_slice=None):
        ax.imshow(_prep_slice(bg_slice), cmap="gray",
                  vmin=vmin, vmax=vmax, origin="lower", interpolation="nearest")
        if ov_slice is not None and ov_slice.any():
            ov = _prep_slice(ov_slice.astype(np.float32))
            # Mask zeros so they don't get colored
            ov_masked = np.ma.masked_where(ov <= 0, ov)
            ax.imshow(ov_masked, cmap=overlay_cmap, alpha=overlay_alpha,
                      origin="lower", interpolation="nearest",
                      vmin=ov_masked.min() if ov_masked.count() else 0,
                      vmax=ov_masked.max() if ov_masked.count() else 1)
        ax.set_axis_off()
        ax.set_facecolor("black")

    # Row 0: sagittal (fix x)
    for col, x in enumerate(x_positions):
        bg_slice = background[x, :, :]
        ov_slice = overlay[x, :, :] if overlay is not None else None
        draw(axes[0, col], bg_slice, ov_slice)

    # Row 1: coronal (fix y)
    for col, y in enumerate(y_positions):
        bg_slice = background[:, y, :]
        ov_slice = overlay[:, y, :] if overlay is not None else None
        draw(axes[1, col], bg_slice, ov_slice)

    # Row 2: axial (fix z)
    for col, z in enumerate(z_positions):
        bg_slice = background[:, :, z]
        ov_slice = overlay[:, :, z] if overlay is not None else None
        draw(axes[2, col], bg_slice, ov_slice)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), facecolor="black", dpi=90,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)


# =============================================================================
# Per-subject QC generation
# =============================================================================

def qc_brain_extraction(sub: str, t1_dir: Path, t1_brain_dir: Path,
                        qc_dir: Path, force: bool = False) -> bool:
    """Generate the brain-extraction QC PNG for one subject.

    Overlays the ANTs/SynthStrip brain mask on the raw T1 to show what
    was included or missed.
    """
    out = qc_dir / "brain_extraction" / f"{sub}_brain_extract.png"
    if out.exists() and not force:
        return True

    t1_raw = t1_dir / f"{sub}_T1.nii.gz"
    mask = t1_brain_dir / f"{sub}_BrainExtractionMask.nii.gz"
    if not (t1_raw.exists() and mask.exists()):
        return False

    try:
        t1, _ = load_reoriented(t1_raw)
        m, _ = load_reoriented(mask)
        # Match shapes (nearest-neighbour interpolation not needed here; both
        # come from the same acquisition and canonical reorient should already
        # align them). Guard against subtle shape mismatch anyway.
        if t1.shape != m.shape:
            print(f"    WARN: {sub}: T1 shape {t1.shape} != mask shape {m.shape}, skipping")
            return False
        render_mosaic(t1, m, out, overlay_alpha=0.4, overlay_cmap="autumn")
        return True
    except Exception as e:
        print(f"    WARN: {sub}: brain-extraction QC failed: {e}")
        return False


def qc_t1_brain(sub: str, t1_brain_dir: Path, qc_dir: Path,
                force: bool = False) -> bool:
    """Generate the brain-only T1 QC PNG (grayscale, no overlay)."""
    out = qc_dir / "T1_brain" / f"{sub}_t1_brain.png"
    if out.exists() and not force:
        return True

    brain = t1_brain_dir / f"{sub}_BrainExtractionBrain.nii.gz"
    if not brain.exists():
        return False
    try:
        data, _ = load_reoriented(brain)
        render_mosaic(data, None, out)
        return True
    except Exception as e:
        print(f"    WARN: {sub}: T1_brain QC failed: {e}")
        return False


def qc_t1_lesion_overlay(sub: str, t1_brain_dir: Path, lesion_dir: Path,
                         qc_dir: Path, force: bool = False) -> bool:
    """Generate the native-space T1 + lesion overlay QC PNG."""
    out = qc_dir / "T1_lesion_overlay" / f"{sub}_lesion_overlay.png"
    if out.exists() and not force:
        return True

    brain = t1_brain_dir / f"{sub}_BrainExtractionBrain.nii.gz"
    lesion = lesion_dir / f"{sub}_Lesion.nii.gz"
    if not (brain.exists() and lesion.exists()):
        return False
    try:
        t1, _ = load_reoriented(brain)
        les, _ = load_reoriented(lesion)
        if t1.shape != les.shape:
            print(f"    WARN: {sub}: T1 shape {t1.shape} != lesion shape {les.shape}, skipping")
            return False
        render_mosaic(t1, les, out, overlay_alpha=0.55, overlay_cmap="autumn")
        return True
    except Exception as e:
        print(f"    WARN: {sub}: T1_lesion_overlay QC failed: {e}")
        return False


def qc_t1_warped(sub: str, reg_dir: Path, qc_dir: Path,
                 force: bool = False) -> bool:
    """Generate the warped T1 (template space) with warped lesion overlay."""
    out = qc_dir / "T1_warped" / f"{sub}_t1_warped.png"
    if out.exists() and not force:
        return True

    warped_t1 = reg_dir / sub / f"{sub}_T1_warped.nii.gz"
    warped_lesion = reg_dir / sub / f"{sub}_Lesion_warped.nii.gz"
    if not warped_t1.exists():
        return False
    try:
        t1, _ = load_reoriented(warped_t1)
        les = None
        if warped_lesion.exists():
            les, _ = load_reoriented(warped_lesion)
            if les.shape != t1.shape:
                print(f"    WARN: {sub}: warped shapes disagree, showing T1 only")
                les = None
        render_mosaic(t1, les, out, overlay_alpha=0.55, overlay_cmap="autumn")
        return True
    except Exception as e:
        print(f"    WARN: {sub}: T1_warped QC failed: {e}")
        return False


def qc_logjacobian(sub: str, reg_dir: Path, qc_dir: Path,
                   force: bool = False) -> bool:
    """Generate the log-Jacobian QC PNG.

    log|J| shows expansion (positive) and compression (negative) fields
    from the registration. A useful QC feature is visible tissue coverage —
    holes in the log-J image usually indicate FOV problems in the input.
    """
    out = qc_dir / "logjacobian" / f"{sub}_logjacobian.png"
    if out.exists() and not force:
        return True

    logjac = reg_dir / sub / f"{sub}_logjacobian.nii.gz"
    if not logjac.exists():
        return False
    try:
        data, _ = load_reoriented(logjac)
        # log|J| is signed and centered near 0. Use a symmetric range around 0.
        limit = np.percentile(np.abs(data[np.isfinite(data)]), 95) if np.isfinite(data).any() else 1.0
        limit = max(limit, 0.1)
        render_mosaic(data, None, out, bg_range=(-limit, limit))
        return True
    except Exception as e:
        print(f"    WARN: {sub}: logjacobian QC failed: {e}")
        return False


# =============================================================================
# Subject enumeration
# =============================================================================

def enumerate_subjects_from_dir(dir_path: Path, suffix: str) -> Sequence[str]:
    """Find all subjects with files matching *_<suffix>.nii.gz in dir_path."""
    if not dir_path.exists():
        return []
    subs = []
    for f in sorted(dir_path.glob(f"*_{suffix}.nii.gz")):
        name = f.name
        sub = name[: -len(f"_{suffix}.nii.gz")]
        subs.append(sub)
    return subs


def enumerate_subjects_from_reg(reg_dir: Path) -> Sequence[str]:
    """Registration output uses per-subject subdirectories; list those."""
    if not reg_dir.exists():
        return []
    return sorted([p.name for p in reg_dir.iterdir() if p.is_dir()])


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ENIGMA Lesion Pipeline QC image generator (Python; no host FSL required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", required=True,
                        choices=["qc-extract", "qc-register"],
                        help="qc-extract for Step 3, qc-register for Step 6")
    parser.add_argument("--t1-dir", type=Path, default=None,
                        help="Raw T1 directory (for qc-extract mode)")
    parser.add_argument("--t1-brain-dir", type=Path, default=None,
                        help="Brain-extracted T1 directory")
    parser.add_argument("--lesion-dir", type=Path, default=None,
                        help="Native-space lesion mask directory (for qc-register mode)")
    parser.add_argument("--reg-dir", type=Path, default=None,
                        help="Registration output directory (for qc-register mode)")
    parser.add_argument("--qc-dir", type=Path, required=True,
                        help="Where to write QC PNGs")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Overwrite existing PNGs")
    parser.add_argument("--subject", default=None,
                        help="Process only this subject (default: all found)")
    args = parser.parse_args()

    # Import dependencies now (so --help still works if any is missing)
    try:
        import nibabel  # noqa: F401
    except ImportError:
        print("ERROR: nibabel is required. Install with: pip install nibabel", file=sys.stderr)
        return 1
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("ERROR: matplotlib is required. Install with: pip install matplotlib", file=sys.stderr)
        return 1

    args.qc_dir.mkdir(parents=True, exist_ok=True)

    total_ok = 0
    total_seen = 0

    if args.mode == "qc-extract":
        if not args.t1_brain_dir:
            print("ERROR: --t1-brain-dir is required for qc-extract mode", file=sys.stderr)
            return 1
        if not args.t1_dir:
            print("ERROR: --t1-dir is required for qc-extract mode", file=sys.stderr)
            return 1

        subs = ([args.subject] if args.subject
                else enumerate_subjects_from_dir(args.t1_brain_dir,
                                                 "BrainExtractionBrain"))
        print(f"Brain-extraction QC: {len(subs)} subjects")
        for sub in subs:
            total_seen += 1
            ok = qc_brain_extraction(sub, args.t1_dir, args.t1_brain_dir,
                                     args.qc_dir, args.force)
            if ok:
                total_ok += 1
                print(f"  OK: {sub}")
            else:
                print(f"  MISS: {sub}")

    else:  # qc-register
        if not args.t1_brain_dir:
            print("ERROR: --t1-brain-dir is required for qc-register mode", file=sys.stderr)
            return 1
        if not args.reg_dir:
            print("ERROR: --reg-dir is required for qc-register mode", file=sys.stderr)
            return 1
        if not args.lesion_dir:
            print("ERROR: --lesion-dir is required for qc-register mode", file=sys.stderr)
            return 1

        subs = ([args.subject] if args.subject
                else enumerate_subjects_from_reg(args.reg_dir))
        print(f"Registration QC: {len(subs)} subjects, 4 image types each")
        for sub in subs:
            total_seen += 1
            per_sub_ok = 0
            per_sub_total = 4
            if qc_t1_brain(sub, args.t1_brain_dir, args.qc_dir, args.force):
                per_sub_ok += 1
            if qc_t1_lesion_overlay(sub, args.t1_brain_dir, args.lesion_dir,
                                    args.qc_dir, args.force):
                per_sub_ok += 1
            if qc_t1_warped(sub, args.reg_dir, args.qc_dir, args.force):
                per_sub_ok += 1
            if qc_logjacobian(sub, args.reg_dir, args.qc_dir, args.force):
                per_sub_ok += 1
            print(f"  {sub}: {per_sub_ok}/{per_sub_total}")
            if per_sub_ok == per_sub_total:
                total_ok += 1

    print()
    print(f"Complete: {total_ok}/{total_seen} subjects")
    return 0


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        sys.exit(main())
