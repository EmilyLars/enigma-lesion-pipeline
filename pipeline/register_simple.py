#!/usr/bin/env python
"""
Simple lesion-masked registration using ANTsPy (no deep learning/ANTsPyNet)
Uses lesion mask to exclude lesion region from cost function during registration.
"""

import os
import sys
import argparse
import shutil
import ants


def register_with_lesion_mask(t1_path, lesion_path, output_dir, template_path=None, verbose=True):
    """
    Register a T1 image with lesion to a template.
    The lesion mask is used to exclude the lesion from the registration cost function.
    """
    
    os.makedirs(output_dir, exist_ok=True)
    
    if verbose:
        print(f"Loading subject T1: {t1_path}", flush=True)
    t1 = ants.image_read(t1_path)
    
    if verbose:
        print(f"Loading lesion mask: {lesion_path}", flush=True)
    lesion = ants.image_read(lesion_path)
    
    # Binarize lesion mask
    lesion = ants.threshold_image(lesion, 0.5, 1e9, 1, 0)
    
    # Create brain mask excluding lesion (for cost function)
    # Invert lesion: 1 where no lesion, 0 where lesion
    mask = ants.threshold_image(lesion, 0, 0.5, 1, 0)
    
    # Load template
    if template_path:
        if verbose:
            print(f"Loading template: {template_path}", flush=True)
        template = ants.image_read(template_path)
    else:
        if verbose:
            print("Using MNI template", flush=True)
        template = ants.get_ants_data('mni')
        template = ants.image_read(template)

    # Pad subject images to match template FOV if needed
    t1_shape = t1.numpy().shape
    template_shape = template.numpy().shape

    if t1_shape != template_shape:
        if verbose:
            print(f"Padding subject to template FOV: {t1_shape} -> {template_shape}", flush=True)
        t1 = ants.resample_image_to_target(t1, template, interp_type='linear')
        lesion = ants.resample_image_to_target(lesion, template, interp_type='nearestNeighbor')
        lesion = ants.threshold_image(lesion, 0.5, 1e9, 1, 0)
        mask = ants.threshold_image(lesion, 0, 0.5, 1, 0)

    # Run registration with lesion mask
    if verbose:
        print("Running SyN registration (lesion masked)...", flush=True)
    
    reg = ants.registration(
        fixed=template,
        moving=t1,
        type_of_transform='SyN',
        mask=mask,  # Exclude lesion from cost function
        verbose=verbose
    )
    
    # Apply transforms
    if verbose:
        print("Applying transforms...", flush=True)
    
    t1_warped = reg['warpedmovout']
    
    lesion_warped = ants.apply_transforms(
        fixed=template,
        moving=lesion,
        transformlist=reg['fwdtransforms'],
        interpolator='nearestNeighbor'
    )
    
    # Save outputs
    if verbose:
        print("Saving outputs...", flush=True)
    
    sub_id = os.path.basename(t1_path).replace('_T1.nii.gz', '').replace('.nii.gz', '')
    
    t1_warped_path = os.path.join(output_dir, f'{sub_id}_T1_warped.nii.gz')
    ants.image_write(t1_warped, t1_warped_path)
    if verbose:
        print(f"  Warped T1: {t1_warped_path}", flush=True)
    
    lesion_warped_path = os.path.join(output_dir, f'{sub_id}_Lesion_warped.nii.gz')
    ants.image_write(lesion_warped, lesion_warped_path)
    if verbose:
        print(f"  Warped lesion: {lesion_warped_path}", flush=True)
    
    # Save transforms
    for i, tx in enumerate(reg['fwdtransforms']):
        ext = '.mat' if tx.endswith('.mat') else '_warp.nii.gz'
        dest = os.path.join(output_dir, f'{sub_id}_fwd_{i}{ext}')
        shutil.copy2(tx, dest)
        if verbose:
            print(f"  Forward transform: {dest}", flush=True)
    
    for i, tx in enumerate(reg['invtransforms']):
        ext = '.mat' if tx.endswith('.mat') else '_invwarp.nii.gz'
        dest = os.path.join(output_dir, f'{sub_id}_inv_{i}{ext}')
        shutil.copy2(tx, dest)
        if verbose:
            print(f"  Inverse transform: {dest}", flush=True)
    
    # Compute Jacobian determinant from warp field
    if verbose:
        print("Computing Jacobian determinant...", flush=True)
    
    # Find the warp field (non-linear transform)
    warp_file = None
    for tx in reg['fwdtransforms']:
        if not tx.endswith('.mat'):
            warp_file = tx
            break
    
    if warp_file:
        # Create Jacobian determinant image
        jacobian = ants.create_jacobian_determinant_image(
            template,
            warp_file,
            do_log=False  # Set True for log-Jacobian
        )
        
        jacobian_path = os.path.join(output_dir, f'{sub_id}_jacobian.nii.gz')
        ants.image_write(jacobian, jacobian_path)
        if verbose:
            print(f"  Jacobian: {jacobian_path}", flush=True)
        
        # Also save log-Jacobian (often preferred for statistical analysis)
        log_jacobian = ants.create_jacobian_determinant_image(
            template,
            warp_file,
            do_log=True
        )
        
        log_jacobian_path = os.path.join(output_dir, f'{sub_id}_logjacobian.nii.gz')
        ants.image_write(log_jacobian, log_jacobian_path)
        if verbose:
            print(f"  Log-Jacobian: {log_jacobian_path}", flush=True)
    else:
        if verbose:
            print("  Warning: No warp field found, skipping Jacobian", flush=True)
    
    if verbose:
        print("Done!", flush=True)
    
    return reg


def main():
    parser = argparse.ArgumentParser(description='Register T1 with lesion mask to template')
    parser.add_argument('t1', help='Subject T1 image')
    parser.add_argument('lesion', help='Subject lesion mask')
    parser.add_argument('output_dir', help='Output directory')
    parser.add_argument('--template', '-t', help='Template (default: MNI)')
    parser.add_argument('--quiet', '-q', action='store_true')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.t1):
        print(f"ERROR: T1 not found: {args.t1}", file=sys.stderr)
        sys.exit(1)
    
    if not os.path.exists(args.lesion):
        print(f"ERROR: Lesion not found: {args.lesion}", file=sys.stderr)
        sys.exit(1)
    
    register_with_lesion_mask(
        t1_path=args.t1,
        lesion_path=args.lesion,
        output_dir=args.output_dir,
        template_path=args.template,
        verbose=not args.quiet
    )


if __name__ == '__main__':
    main()
