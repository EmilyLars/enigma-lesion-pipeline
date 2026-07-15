#!/usr/bin/env python
"""
Label-based registration for images with lesions using ANTsPyNet

This script registers T1 images with lesions to a template using
label-based registration, where the lesion mask is included as an
additional label to help the registration handle lesion regions appropriately.
"""

import argparse
import os
import shutil
import sys

# Early diagnostic: on some HPC systems ants/antspynet imports take
# 30+ seconds. These prints help distinguish "script hung" from "slow imports".
print("Script starting...", flush=True)

import ants  # noqa: E402
print("ants imported", flush=True)
import antspynet  # noqa: E402
print("antspynet imported", flush=True)


def register_with_lesion(t1_path, lesion_path, output_dir, template_path=None, 
                         lesion_weight=1.0, verbose=True):
    """
    Register a T1 image with lesion to a template using label-based registration.
    
    Parameters
    ----------
    t1_path : str
        Path to subject T1 image
    lesion_path : str
        Path to subject lesion mask
    output_dir : str
        Output directory for results
    template_path : str, optional
        Path to template T1. If None, uses ADNI template from ANTsXNet
    lesion_weight : float
        Weight for lesion label in registration (default: 1.0)
    verbose : bool
        Print progress messages
        
    Returns
    -------
    dict with keys:
        'reg': registration output from ANTs
        't1_warped': warped T1 image
        'lesion_warped': warped lesion mask
    """
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Load subject images
    if verbose:
        print(f"Loading subject T1: {t1_path}")
    t1 = ants.image_read(t1_path)
    
    if verbose:
        print(f"Loading lesion mask: {lesion_path}")
    lesion = ants.image_read(lesion_path)
    
    # Ensure lesion mask is binary and in same space
    lesion = ants.threshold_image(lesion, 0.5, 1e9, 1, 0)
    
    # Load or get template
    if template_path:
        if verbose:
            print(f"Loading template: {template_path}")
        t1_template = ants.image_read(template_path)
    else:
        if verbose:
            print("Using ADNI template from ANTsXNet")
        t1_template = ants.image_read(antspynet.get_antsxnet_data('adni'))
    
    # Generate atlas labelings for subject
    if verbose:
        print("Generating DKT labeling for subject...")
    dkt = antspynet.desikan_killiany_tourville_labeling(t1, version=1)
    
    if verbose:
        print("Generating Harvard-Oxford labeling for subject...")
    hoa = antspynet.harvard_oxford_atlas_labeling(t1)['segmentation_image']
    
    # Generate atlas labelings for template
    if verbose:
        print("Generating DKT labeling for template...")
    dkt_template = antspynet.desikan_killiany_tourville_labeling(t1_template, version=1)
    
    if verbose:
        print("Generating Harvard-Oxford labeling for template...")
    hoa_template = antspynet.harvard_oxford_atlas_labeling(t1_template)['segmentation_image']
    
    # Create empty lesion mask for template (no lesions in template)
    lesion_template = ants.image_clone(t1_template) * 0
    
    # Run label-based registration with lesion as additional label
    if verbose:
        print("Running label-based registration...")
        print(f"  Label weights: DKT=2.0, HOA=1.0, Lesion={lesion_weight}")
    
    reg = ants.label_image_registration(
        fixed_label_images=[dkt_template, hoa_template, lesion_template],
        moving_label_images=[dkt, hoa, lesion],
        fixed_intensity_images=t1_template,
        moving_intensity_images=t1,
        initial_transforms='affine',
        type_of_deformable_transform='antsRegistrationSyNQuick[bo]',
        label_image_weighting=[2.0, 1.0, lesion_weight],
        verbose=verbose
    )
    
    # Apply transforms to get warped images
    if verbose:
        print("Applying transforms...")
    
    t1_warped = ants.apply_transforms(
        fixed=t1_template,
        moving=t1,
        transformlist=reg['fwdtransforms']
    )
    
    lesion_warped = ants.apply_transforms(
        fixed=t1_template,
        moving=lesion,
        transformlist=reg['fwdtransforms'],
        interpolator='nearestNeighbor'
    )
    
    # Save outputs
    if verbose:
        print("Saving outputs...")
    
    # Get subject ID from filename
    sub_id = os.path.basename(t1_path).replace('.nii.gz', '').replace('.nii', '')
    
    # Warped images
    t1_warped_path = os.path.join(output_dir, f'{sub_id}_T1_warped.nii.gz')
    ants.image_write(t1_warped, t1_warped_path)
    if verbose:
        print(f"  Warped T1: {t1_warped_path}")
    
    lesion_warped_path = os.path.join(output_dir, f'{sub_id}_Lesion_warped.nii.gz')
    ants.image_write(lesion_warped, lesion_warped_path)
    if verbose:
        print(f"  Warped lesion: {lesion_warped_path}")
    
    # Save transforms
    for i, transform in enumerate(reg['fwdtransforms']):
        if transform.endswith('.mat'):
            dest = os.path.join(output_dir, f'{sub_id}_transform_{i}_affine.mat')
        else:
            dest = os.path.join(output_dir, f'{sub_id}_transform_{i}_warp.nii.gz')
        shutil.copy2(transform, dest)
        if verbose:
            print(f"  Forward transform: {dest}")
    
    for i, transform in enumerate(reg['invtransforms']):
        if transform.endswith('.mat'):
            dest = os.path.join(output_dir, f'{sub_id}_transform_{i}_affine_inv.mat')
        else:
            dest = os.path.join(output_dir, f'{sub_id}_transform_{i}_invwarp.nii.gz')
        if os.path.exists(transform):
            shutil.copy2(transform, dest)
            if verbose:
                print(f"  Inverse transform: {dest}")
    
    # Save segmentations (useful for QC)
    dkt_path = os.path.join(output_dir, f'{sub_id}_DKT.nii.gz')
    ants.image_write(dkt, dkt_path)
    
    hoa_path = os.path.join(output_dir, f'{sub_id}_HOA.nii.gz')
    ants.image_write(hoa, hoa_path)
    
    if verbose:
        print("Done!")
    
    return {
        'reg': reg,
        't1_warped': t1_warped,
        'lesion_warped': lesion_warped
    }


def main():
    parser = argparse.ArgumentParser(
        description='Register T1 with lesion to template using label-based registration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with ADNI template
  python register_with_lesion.py sub001_T1.nii.gz sub001_Lesion.nii.gz output/
  
  # With custom template
  python register_with_lesion.py sub001_T1.nii.gz sub001_Lesion.nii.gz output/ -t MNI152.nii.gz
  
  # With higher lesion weight
  python register_with_lesion.py sub001_T1.nii.gz sub001_Lesion.nii.gz output/ --lesion-weight 2.0
        """
    )
    parser.add_argument('t1', help='Subject T1 image')
    parser.add_argument('lesion', help='Subject lesion mask')
    parser.add_argument('output_dir', help='Output directory')
    parser.add_argument('--template', '-t', help='Template T1 (default: ADNI from ANTsXNet)')
    parser.add_argument('--lesion-weight', '-w', type=float, default=1.0,
                        help='Weight for lesion label (default: 1.0)')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress progress messages')
    
    args = parser.parse_args()
    
    # Check input files exist
    if not os.path.exists(args.t1):
        print(f"ERROR: T1 file not found: {args.t1}", file=sys.stderr)
        sys.exit(1)
    
    if not os.path.exists(args.lesion):
        print(f"ERROR: Lesion file not found: {args.lesion}", file=sys.stderr)
        sys.exit(1)
    
    if args.template and not os.path.exists(args.template):
        print(f"ERROR: Template file not found: {args.template}", file=sys.stderr)
        sys.exit(1)
    
    register_with_lesion(
        t1_path=args.t1,
        lesion_path=args.lesion,
        output_dir=args.output_dir,
        template_path=args.template,
        lesion_weight=args.lesion_weight,
        verbose=not args.quiet
    )


if __name__ == '__main__':
    main()
