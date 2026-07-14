#!/usr/bin/env python3
"""
precache_antsxnet.py — download ANTsXNet templates and model weights into a
known on-disk location during the container build.

Why this exists
---------------
ANTsPyNet's helpers (template fetchers, brain extraction models, segmentation
models, etc.) lazily download their assets at first use, from Figshare and
AWS S3. That works fine for a developer with internet, but it fails on:
  - air-gapped HPC nodes
  - restricted VMs with no outbound traffic
  - networks where Figshare or AWS happen to be transiently unreachable

We solve this by pre-downloading everything we use at container BUILD time and
setting ANTSXNET_CACHE_DIRECTORY at RUNTIME so antspynet reads from the local
copy instead of re-downloading.

What gets cached
----------------
This script is intentionally conservative: it only fetches assets that the
ENIGMA lesion pipeline actually uses. If you add a new antspynet helper to
the pipeline, add it here so it's also pre-cached.

Run from .def
-------------
Invoked from `enigma-lesion.def` during the build stage, after antspynet is
installed. The destination is /opt/antsxnet-cache, and the .def sets
  ENV ANTSXNET_CACHE_DIRECTORY=/opt/antsxnet-cache
so runtime use of antspynet finds the cached assets without modification.
"""

import os
import sys
import traceback

# =============================================================================
# Set the cache directory BEFORE importing antspynet, so antspynet picks it up
# =============================================================================

# ANTsPyNet defers to Keras's file-download machinery, which reads KERAS_HOME
# (falling back to ~/.keras). Setting ANTSXNET_CACHE_DIRECTORY alone is not
# enough — antspynet in the current release stores assets under
# ${KERAS_HOME:-~/.keras}/ANTsXNet/. So we set both, pointing them at a
# location outside the user's home directory. Apptainer bind-mounts the
# host's home over the container's home at runtime, which would otherwise
# hide anything cached under /root/.keras.
CACHE_DIR = os.environ.get("ANTSXNET_CACHE_DIRECTORY", "/opt/antsxnet-cache")
os.environ["ANTSXNET_CACHE_DIRECTORY"] = CACHE_DIR
os.environ["KERAS_HOME"] = CACHE_DIR
os.makedirs(CACHE_DIR, exist_ok=True)

print("=" * 60)
print("ANTsXNet asset pre-cache")
print("=" * 60)
print("Cache directory: %s" % CACHE_DIR)
print("KERAS_HOME:      %s" % os.environ["KERAS_HOME"])
print()

# =============================================================================
# Import antspynet (which will use the cache directory above)
# =============================================================================

try:
    import antspynet
except ImportError as e:
    print("ERROR: antspynet not importable: %s" % e)
    print("Make sure antspynet is installed BEFORE running this script.")
    sys.exit(1)

print("antspynet version: %s" % getattr(antspynet, "__version__", "unknown"))
print()


# =============================================================================
# Helpers
# =============================================================================

def try_call(label, fn, *args, **kwargs):
    """Run a fetch helper and log success / failure, but don't abort the build
    on a single failed asset — we want partial caching to still ship.
    """
    print("[fetch] %s ..." % label, flush=True)
    try:
        result = fn(*args, **kwargs)
        print("        OK")
        return result
    except Exception as e:
        print("        FAILED: %s" % e)
        traceback.print_exc(limit=2)
        return None


# =============================================================================
# 1. Templates used by registration helpers
# =============================================================================

# These are the registration target candidates that any antspynet helper might
# request internally. Pre-caching them avoids runtime Figshare hits.
# Note: 'mni' is not a valid antspynet asset name (removed after the first
# build revealed the ValueError). ADNI is used as the default registration
# target for the label-based registration path.
TEMPLATES = [
    "adni",      # ADNI T1 template
    "kirby",     # Kirby aging template (used by some skullstrip helpers)
    "biobank",   # UK Biobank (only used if we ever switch base templates)
]

print("--- Templates ---")
for name in TEMPLATES:
    try_call("get_antsxnet_data(%r)" % name,
             antspynet.get_antsxnet_data, name)
print()


# =============================================================================
# 2. Brain segmentation / labelling model weights
# =============================================================================

# The pipeline's lesion-masked registration uses DKT-style cortical/subcortical
# labels for the label-based registration variant. Pre-cache both the DKT and
# Harvard-Oxford weights since either can be invoked depending on user choice.
MODEL_WEIGHTS = [
    "dktInner",                  # DKT cortical parcellation (inner net)
    "dktOuter",                  # DKT cortical parcellation (outer net)
    "dktOuterWithSpatialPriors", # DKT outer with priors variant
    # Note: 'harvardOxfordAtlasSubcorticalAndCerebellum' was tried but is
    # not a valid antspynet asset name in this version. DKT weights above
    # cover the label-based registration path.
]

print("--- Model weights ---")
for name in MODEL_WEIGHTS:
    try_call("get_pretrained_network(%r)" % name,
             antspynet.get_pretrained_network, name)
print()


# =============================================================================
# 3. Brain extraction networks
# =============================================================================

# antspynet brain_extraction() has multiple modalities; pre-cache the
# ones the pipeline might invoke. We only use these if the user picks
# antspynet-based extraction; ANTs-template-based extraction doesn't need
# them, and SynthStrip is independent. Pre-caching them is cheap insurance.
BRAIN_EXTRACTION_NETWORKS = [
    "brainExtractionT1",      # generic T1 brain extraction
    "brainExtractionT1v1",    # newer variant
]

print("--- Brain extraction networks ---")
for name in BRAIN_EXTRACTION_NETWORKS:
    try_call("get_pretrained_network(%r)" % name,
             antspynet.get_pretrained_network, name)
print()


# =============================================================================
# Summary
# =============================================================================

print("=" * 60)
print("Pre-cache complete")
print("=" * 60)

# Walk the cache and report total size + file count
total_bytes = 0
total_files = 0
for root, dirs, files in os.walk(CACHE_DIR):
    for f in files:
        try:
            total_bytes += os.path.getsize(os.path.join(root, f))
            total_files += 1
        except OSError:
            pass

print("Cached files: %d" % total_files)
print("Total size:   %.1f MB" % (total_bytes / 1e6))
print()
print("At runtime, set ANTSXNET_CACHE_DIRECTORY=%s" % CACHE_DIR)
print("(this is done automatically in the container's ENV block)")
